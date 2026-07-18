"""Receipt-collector runner — main flow per kwartaal-batch.

Stappen:
1. Parse Excel → list[Transaction]
2. Derive date-window (oudste txn - 30d, jongste txn + 30d)
3. Init `receipt_runs` rij + per-transactie `receipt_run_items`
4. Per transactie:
   a. Look-up vendor_strategy (canonical of via aliases)
   b. Als strategy='portal' → status=needs_portal, sla zoeken over
   c. Anders: zoek in Gmail + alle ingeschakelde IMAP-accounts
   d. Score elke kandidaat, kies beste boven threshold
   e. Download attachment → output_dir/<vendor>-<date>-<amount>.pdf
   f. Update item-status
5. Update run-counts + emit summary dict
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.receipt_collector.matcher import (
    MatchCandidate,
    _clean_vendor_for_search,
    score_candidate,
    search_gmail_by_amount,
    search_gmail_for_transaction,
    search_imap_by_amount,
    search_imap_for_transaction,
)
from extensions.receipt_collector.parser import (
    Transaction,
    derive_date_window,
    extract_vendor_candidates,
    parse_excel,
)
from extensions.receipt_collector.schema import (
    find_vendor_strategy,
    insert_run,
    insert_run_item,
    mark_vendor_used,
    update_run_counts,
    update_run_item,
)
from integrations.imap import ImapAccount

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

# Confidence-drempel waaronder we niet automatisch een match accepteren
MATCH_THRESHOLD = 0.55
# Strengere drempel voor reverse-match (vendor-naam-loze amount-match) —
# false-positive-risk is hoger dus we eisen sterke datum + amount-hit.
REVERSE_MATCH_THRESHOLD = 0.65


def run_receipt_collection(
    *,
    excel_path: Path,
    db_path: Path,
    output_root: Path,
    gmail: Any | None = None,
    imap_accounts: list[tuple[ImapAccount, str]] | None = None,
    margin_days: int = 30,
    ollama: Any | None = None,
) -> dict[str, Any]:
    """Returns summary dict met run_id, counts en per-status listing."""
    transactions = parse_excel(excel_path)
    if not transactions:
        return {"error": "no transactions parsed from excel",
                "excel_path": str(excel_path)}

    window_start, window_end = derive_date_window(
        transactions, margin_days=margin_days,
    )
    period_label = _derive_period_label(excel_path, transactions)
    output_dir = output_root / f"run-{period_label}-{int(_time.time())}"
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        run_id = insert_run(
            conn,
            excel_path=str(excel_path),
            output_dir=str(output_dir),
            period_label=period_label,
            date_window_start=int(datetime.combine(window_start, datetime.min.time(), tzinfo=TZ).timestamp()),
            date_window_end=int(datetime.combine(window_end, datetime.min.time(), tzinfo=TZ).timestamp()),
            transaction_count=len(transactions),
        )
        item_ids: list[tuple[int, Transaction]] = []
        for txn in transactions:
            item_id = insert_run_item(
                conn, run_id=run_id, row_idx=txn.row_idx,
                transaction_date=int(datetime.combine(txn.transaction_date, datetime.min.time(), tzinfo=TZ).timestamp()),
                vendor_raw=txn.vendor,
                amount_cents=txn.amount_cents,
                currency=txn.currency,
                description=txn.description,
            )
            item_ids.append((item_id, txn))

    matched = 0
    needs_portal = 0
    unknown = 0
    physical_only = 0
    ignored = 0
    # Eén factuur-mail mag binnen een run maar aan één transactie gekoppeld
    # worden. De reverse-match (vendor-loze amount-search) kan anders
    # dezelfde mail aan meerdere txns met identiek bedrag toewijzen
    # (Zoomash 2× zelfde mail in Run-7).
    claimed_keys: set[tuple[str, str]] = set()

    for item_id, txn in item_ids:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            strategy = find_vendor_strategy(conn, vendor_text=txn.vendor)

        # ignore = expliciet uitgesloten (test-subscriptions, interne
        # verrekeningen). Geen mail-search, geen reminder, eindstatus 'ignored'.
        if strategy is not None and strategy["source_kind"] == "ignore":
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                update_run_item(
                    conn, item_id, status="ignored",
                    vendor_canonical=strategy["name"],
                    notes=strategy.get("portal_notes") or "ignored per strategy",
                )
                mark_vendor_used(conn, strategy["id"])
            ignored += 1
            continue

        # physical = vendor levert alleen fysieke bonnetjes (POS, parking).
        # Geen mail-search, eindstatus 'physical_only' — the user weet
        # "scan zelf".
        if strategy is not None and strategy["source_kind"] == "physical":
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                update_run_item(
                    conn, item_id, status="physical_only",
                    vendor_canonical=strategy["name"],
                    notes=strategy.get("portal_notes") or "physical receipt only",
                )
                mark_vendor_used(conn, strategy["id"])
            physical_only += 1
            continue

        # source_kind=portal + email_query_hint = "probeer mail eerst,
        # fallback portal". Sommige vendors (Clicks via Stripe-receipts)
        # hebben WEL mail beschikbaar maar zijn historisch via portal
        # opgeslagen — geen reden de hint te negeren.
        portal_only = (strategy is not None
                        and strategy["source_kind"] == "portal"
                        and not strategy.get("email_query_hint"))
        if portal_only:
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                update_run_item(
                    conn, item_id, status="needs_portal",
                    vendor_canonical=strategy["name"],
                    notes=strategy.get("portal_notes") or "",
                )
                mark_vendor_used(conn, strategy["id"])
            needs_portal += 1
            continue

        # Bouw vendor-zoek-kandidaten: Crediteur + alle uit description.
        # Ollama-fallback alleen als regex 0 useful candidates oplevert.
        search_vendors = _build_search_vendors(
            txn, ollama=ollama,
        )
        candidates = _search_all_sources_multi(
            txn, search_vendors=search_vendors,
            window_start=window_start, window_end=window_end,
            gmail=gmail, imap_accounts=imap_accounts or [],
            vendor_strategy=strategy,
        )

        scored: list[tuple[float, list[str], MatchCandidate]] = []
        for cand in candidates:
            score, reasons = score_candidate(cand, txn)
            scored.append((score, reasons, cand))
        scored.sort(key=lambda s: s[0], reverse=True)

        # Skip reeds-geclaimde primary candidates (zelfde mail kan niet
        # 2× aan verschillende txns toegewezen worden).
        scored = [s for s in scored
                   if (s[2].source, s[2].message_id) not in claimed_keys]

        if scored and scored[0][0] >= MATCH_THRESHOLD:
            best_score, reasons, best = scored[0]
            attachment_filename = _save_attachment(
                best, txn, output_dir,
            )
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                update_run_item(
                    conn, item_id, status="matched",
                    matched_via=best.source,
                    match_score=best_score,
                    attachment_path=attachment_filename,
                    source_message_id=best.message_id,
                    vendor_canonical=(strategy["name"] if strategy else None),
                    notes="; ".join(reasons),
                )
                if strategy:
                    mark_vendor_used(conn, strategy["id"])
            claimed_keys.add((best.source, best.message_id))
            matched += 1
            continue

        # Reverse-pass: vendor-naam-loze amount-search in date-window.
        # Voor "voorgeschoten Kaartje2Go"-achtige cases waar de echte
        # vendor niet in vendor_raw zit. Strenger threshold + review-flag.
        rev_best = _try_reverse_match(
            txn, candidates, window_start=window_start,
            window_end=window_end, gmail=gmail,
            imap_accounts=imap_accounts or [],
            claimed_keys=claimed_keys,
        )
        if rev_best is not None:
            best_score, reasons, best = rev_best
            attachment_filename = _save_attachment(
                best, txn, output_dir,
            )
            review_note = "amount-only match (review): " + "; ".join(reasons)
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                update_run_item(
                    conn, item_id, status="matched",
                    matched_via=best.source,
                    match_score=best_score,
                    attachment_path=attachment_filename,
                    source_message_id=best.message_id,
                    vendor_canonical=(strategy["name"] if strategy else None),
                    notes=review_note,
                )
                if strategy:
                    mark_vendor_used(conn, strategy["id"])
            claimed_keys.add((best.source, best.message_id))
            matched += 1
            continue

        new_status = ("unknown_vendor" if strategy is None
                       else "needs_portal")
        if new_status == "needs_portal":
            needs_portal += 1
        else:
            unknown += 1
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            update_run_item(
                conn, item_id, status=new_status,
                notes=("no match above threshold; tried "
                       f"{len(candidates)} candidates"),
                vendor_canonical=(strategy["name"] if strategy else None),
            )

    final_status = ("needs_input" if (needs_portal + unknown) > 0
                     else "completed")
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        update_run_counts(
            conn, run_id,
            matched=matched, needs_portal=needs_portal, unknown=unknown,
            status=final_status, completed=(final_status == "completed"),
        )

    return {
        "run_id": run_id,
        "period_label": period_label,
        "output_dir": str(output_dir),
        "transaction_count": len(transactions),
        "matched": matched,
        "needs_portal": needs_portal,
        "unknown_vendor": unknown,
        "physical_only": physical_only,
        "ignored": ignored,
        "status": final_status,
        "date_window": [window_start.isoformat(), window_end.isoformat()],
    }


def _search_all_sources_multi(
    txn: Transaction, *, search_vendors: list[str],
    window_start: date, window_end: date,
    gmail: Any | None,
    imap_accounts: list[tuple[ImapAccount, str]],
    vendor_strategy: dict[str, Any] | None,
) -> list[MatchCandidate]:
    """Loop over alle vendor-kandidaten + dedup hits per (source, message_id)."""
    seen: set[tuple[str, str]] = set()
    out: list[MatchCandidate] = []

    # Als er een vendor_strategy met email-hint is, gebruik die alleen
    # (geen multi-search nodig — strategy is autoritatief).
    if vendor_strategy and vendor_strategy.get("email_query_hint"):
        return _search_one_vendor(
            txn, search_vendor=None, window_start=window_start,
            window_end=window_end, gmail=gmail, imap_accounts=imap_accounts,
            vendor_strategy=vendor_strategy,
        )

    for vendor in (search_vendors or [None]):
        results = _search_one_vendor(
            txn, search_vendor=vendor, window_start=window_start,
            window_end=window_end, gmail=gmail, imap_accounts=imap_accounts,
            vendor_strategy=None,
        )
        for cand in results:
            key = (cand.source, cand.message_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
    return out


def _search_one_vendor(
    txn: Transaction, *, search_vendor: str | None,
    window_start: date, window_end: date,
    gmail: Any | None,
    imap_accounts: list[tuple[ImapAccount, str]],
    vendor_strategy: dict[str, Any] | None,
) -> list[MatchCandidate]:
    out: list[MatchCandidate] = []
    if gmail is not None:
        out.extend(search_gmail_for_transaction(
            gmail, txn,
            window_start=window_start, window_end=window_end,
            vendor_strategy=vendor_strategy,
            search_vendor=search_vendor,
        ))
    for account, password in imap_accounts:
        if not account.enabled:
            continue
        out.extend(search_imap_for_transaction(
            account, password, txn,
            window_start=window_start, window_end=window_end,
            vendor_strategy=vendor_strategy,
            search_vendor=search_vendor,
        ))
    return out


def _is_reverse_match_eligible(txn: Transaction) -> bool:
    """Skip kleine en ronde bedragen om false-positives te beperken.
    €15,00 of €2,50 zijn zo gangbaar dat een body-search op het bedrag
    teveel ruis oplevert. Eis een uniek-genoeg bedrag (cents != 00) en
    minimaal €5."""
    abs_cents = abs(txn.amount_cents)
    if abs_cents < 500:
        return False
    if abs_cents % 100 == 0:
        return False
    return True


def _try_reverse_match(
    txn: Transaction,
    primary_candidates: list[MatchCandidate],
    *,
    window_start: date, window_end: date,
    gmail: Any | None,
    imap_accounts: list[tuple[ImapAccount, str]],
    claimed_keys: set[tuple[str, str]] | None = None,
) -> tuple[float, list[str], MatchCandidate] | None:
    """Doe een vendor-loze amount-search en pak best scorende boven
    REVERSE_MATCH_THRESHOLD. Returns None als niet eligible of geen hit.
    Skipt kandidaten die al in primary_candidates zaten of al door een
    eerdere transactie binnen deze run geclaimed zijn (`claimed_keys`)."""
    if not _is_reverse_match_eligible(txn):
        return None
    if gmail is None and not imap_accounts:
        return None

    existing_keys = {(c.source, c.message_id) for c in primary_candidates}
    if claimed_keys:
        existing_keys |= claimed_keys
    rev: list[MatchCandidate] = []
    if gmail is not None:
        try:
            rev.extend(search_gmail_by_amount(
                gmail,
                amount_cents=txn.amount_cents,
                window_start=window_start, window_end=window_end,
            ))
        except Exception:
            log.exception("gmail reverse-search failed for txn %d", txn.row_idx)
    for account, password in imap_accounts:
        if not account.enabled:
            continue
        try:
            rev.extend(search_imap_by_amount(
                account, password,
                amount_cents=txn.amount_cents,
                window_start=window_start, window_end=window_end,
            ))
        except Exception:
            log.exception("imap reverse-search failed for txn %d (%s)",
                            txn.row_idx, account.name)

    rev_unique = [c for c in rev
                   if (c.source, c.message_id) not in existing_keys]
    if not rev_unique:
        return None

    scored: list[tuple[float, list[str], MatchCandidate]] = []
    for cand in rev_unique:
        score, reasons = score_candidate(cand, txn)
        scored.append((score, reasons, cand))
    scored.sort(key=lambda s: s[0], reverse=True)
    if scored and scored[0][0] >= REVERSE_MATCH_THRESHOLD:
        return scored[0]
    return None


def _build_search_vendors(
    txn: Transaction, *, ollama: Any | None = None,
) -> list[str]:
    """Bouw de lijst vendor-namen waarop we per transactie willen zoeken.
    1. txn.vendor (uit Crediteur of regex-extractie)
    2. + alle extra kandidaten uit description (regex)
    3. Als beide niets opleveren → Ollama-fallback (lokaal, geen externe call)
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(v: str | None) -> None:
        if not v:
            return
        v = v.strip()
        if not v or v == "(unknown)":
            return
        key = v.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(v)

    # Clean txn.vendor (boekhouders-prefix strip, payment-rail noise) ZOWEL
    # de raw als de cleaned versie toevoegen — zodat zowel '50101 - Coolblue
    # B.V.' als 'Coolblue' meegaan in de search-loop.
    cleaned = _clean_vendor_for_search(txn.vendor)
    _add(cleaned)
    _add(txn.vendor)
    for c in extract_vendor_candidates(txn.description or ""):
        _add(c)

    # Always-on Ollama-pass over description: regex-extractor mist subtiele
    # cases ("voorgeschoten Kaartje2Go", "PAYPAL *DOCUSIGNINC"). Dedup via
    # `seen` zodat overlap met regex-output geen duplicates oplevert.
    if ollama is not None and txn.description:
        try:
            extra = _ollama_extract_vendors(ollama, txn.description)
            for v in extra:
                _add(v)
        except Exception:
            log.exception("ollama vendor-extract failed for txn %d", txn.row_idx)

    return candidates


_OLLAMA_EXTRACT_PROMPT = """Je krijgt een banktransactie-omschrijving. Extract alle mogelijke vendor/leverancier namen waar de gebruiker een factuur van zou kunnen hebben, in volgorde van waarschijnlijkheid.

Negeer:
- IBANs, transactie-IDs, payment-rails (Tikkie, Mollie, PayPal Europe als intermediair)
- Persoonsnamen die voorgeschoten hebben (de échte vendor staat verderop)
- Generieke woorden (BV, NV, LLC, Inc, via, voor, etc.)

Output STRIKT als JSON-array van strings, max 5. Geen prose. Voorbeeld:
["Kaartje2go", "bol.com"]"""


def _ollama_extract_vendors(ollama: Any, description: str) -> list[str]:
    """Vraag lokaal Llama om vendor-kandidaten. Returns lege lijst bij fout."""
    import json as _json
    response = ollama.chat(
        system=_OLLAMA_EXTRACT_PROMPT,
        messages=[{"role": "user", "content": description[:1000]}],
        max_tokens=200,
    )
    text = ""
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            text += block.text
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        arr = _json.loads(text[start:end + 1])
        if isinstance(arr, list):
            return [str(v).strip() for v in arr if str(v).strip()][:5]
    except _json.JSONDecodeError:
        pass
    return []


def _save_attachment(
    cand: MatchCandidate, txn: Transaction, output_dir: Path,
) -> str:
    """Schrijf eerste PDF/image naar output_dir met sprekende naam.
    Returns filename relatief tov output_dir."""
    if not cand.attachments:
        return ""
    att = cand.attachments[0]
    ext = Path(att.filename).suffix.lower() or ".pdf"
    vendor_slug = re.sub(r"[^a-z0-9]+", "-",
                          txn.vendor.lower()).strip("-")[:30]
    amount_str = f"{abs(txn.amount_cents) / 100:.2f}".replace(".", "_")
    filename = (f"{txn.transaction_date.isoformat()}_{vendor_slug}_"
                 f"{amount_str}{ext}")
    out_path = output_dir / filename
    counter = 1
    while out_path.exists():
        out_path = output_dir / f"{out_path.stem}-{counter}{ext}"
        counter += 1
    out_path.write_bytes(att.data)
    return out_path.name


def _derive_period_label(
    excel_path: Path, transactions: list[Transaction],
) -> str:
    """Sniff Q1-2026 / 2026-Q1 / Jan-2026 uit excel-naam, anders quartaal
    afleiden uit jongste transactie."""
    name = excel_path.stem.lower()
    m = re.search(r"(q[1-4])[\s\-_]*(\d{4})", name) or \
        re.search(r"(\d{4})[\s\-_]*(q[1-4])", name)
    if m:
        groups = sorted(m.groups(), key=lambda g: g.startswith("q"))
        return f"{groups[1].upper()}-{groups[0]}"
    if transactions:
        d = max(t.transaction_date for t in transactions)
        q = (d.month - 1) // 3 + 1
        return f"Q{q}-{d.year}"
    return "unlabeled"
