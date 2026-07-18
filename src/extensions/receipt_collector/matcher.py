"""Cross-source matcher voor één transaction.

Per Excel-regel:
1. Bouw zoekquery op basis van vendor + datum-window
2. Zoek in Gmail (gmail-syntax) + alle ingeschakelde IMAP accounts
3. Voor elke kandidaat: download attachment, extract amount uit PDF tekst,
   score op match (amount-exact + datum-nabijheid + vendor-naam in body)
4. Return best match (of None) + alternatieven

Vendor-strategie hint kan de zoek-query verfijnen (bv. `from:billing@aws...`)
maar is optional — zonder strategie zoeken we breed op vendor-naam.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from extensions.receipt_collector.parser import Transaction
from integrations.imap import ImapAccount

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class MatchCandidate:
    source: str                 # 'gmail' | 'imap:hendrikdpm' | etc
    message_id: str
    from_addr: str
    subject: str
    occurred_at: date
    attachments: list[Attachment] = field(default_factory=list)
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)


def search_gmail_for_transaction(
    gmail: Any, txn: Transaction, *,
    window_start: date, window_end: date,
    vendor_strategy: dict[str, Any] | None = None,
    search_vendor: str | None = None,
    max_results: int = 10,
) -> list[MatchCandidate]:
    """Zoek in Gmail. Gmail-syntax: 'has:attachment after:YYYY/MM/DD before:YYYY/MM/DD <vendor>'.

    `search_vendor` overrules de afgeleide vendor uit txn.vendor — voor
    multi-candidate search per transactie."""
    query = _build_gmail_query(txn, window_start, window_end, vendor_strategy,
                                 search_vendor=search_vendor)
    try:
        results = gmail.search(query=query, max_results=max_results)
    except Exception:
        log.exception("gmail search failed for txn %d (%s)",
                       txn.row_idx, txn.vendor)
        return []
    out: list[MatchCandidate] = []
    for r in results:
        msg_id = r.get("id")
        if not msg_id:
            continue
        try:
            msg = gmail.get_message_full(msg_id)
        except Exception:
            log.exception("gmail get_message_full failed for %s", msg_id)
            continue
        cand = _gmail_message_to_candidate(gmail, msg)
        if cand is not None:
            out.append(cand)
    return out


def _build_gmail_query(
    txn: Transaction, window_start: date, window_end: date,
    vendor_strategy: dict[str, Any] | None,
    search_vendor: str | None = None,
) -> str:
    parts = ["has:attachment",
             f"after:{window_start.strftime('%Y/%m/%d')}",
             f"before:{(window_end + timedelta(days=1)).strftime('%Y/%m/%d')}"]
    if vendor_strategy and vendor_strategy.get("email_query_hint"):
        parts.append(str(vendor_strategy["email_query_hint"]))
    else:
        if search_vendor:
            clean = search_vendor.strip()
        else:
            clean = _clean_vendor_for_search(txn.vendor)
        if clean:
            parts.append(f'"{clean}"')
    return " ".join(parts)


def _clean_vendor_for_search(raw: str) -> str:
    """Strip noise zodat alleen de vendor-naam overblijft. Voorbeelden:
      "50140 - Amazon (cc)"            → "Amazon"
      "50088 - Tidio LLC (cc)"         → "Tidio LLC"
      "PAYPAL *AMAZON LU 700000"       → "AMAZON"
      "AWS EMEA LUXEMBOURG LUX 700000" → "AWS"
      "SHELL HAZELDONK-W NL 0 RIJSBER" → "SHELL"
    """
    import re
    s = raw.strip()
    # Boekhouders-prefix: "NNNN - " of "NNNNN - " aan begin
    s = re.sub(r"^\d{3,6}\s*-\s*", "", s)
    # Boekhouders-suffix: " (cc)" / " (pin)" / " (bank)"
    s = re.sub(r"\s*\((?:cc|pin|bank|sepa)\)\s*$", "", s, flags=re.IGNORECASE)
    # Payment-rail noise. 'Mol*' is Mollie's merchant-prefix waarbij de
    # echte vendor erna staat (bv. 'Mol*ADL Video B V' → 'ADL Video').
    s = re.sub(r"(?i)(paypal\s*\*|mol\*|ideal\s+|sepa[\s\-_]+|incasso[\s\-_]+)", " ", s)
    # Strip merchant-locatie suffixen (HAZELDONK-W NL 0 RIJSBER, LUX 700000, etc)
    # Heuristiek: alles na een patroon van >=3-letter caps + 3+ digits/cap-tail dropt
    s = re.sub(r"\s+(?:NL|LU|DE|FR|UK|US)\s+\d.*$", "", s)
    s = re.sub(r"\s+\d{4,}.*$", "", s)
    # Strip dubbele spaties
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    # Pak max 2 woorden — meestal naam + bedrijfsvorm (bv. "Tidio LLC")
    words = s.split()
    return " ".join(words[:2])


def _gmail_message_to_candidate(
    gmail: Any, msg: dict[str, Any],
) -> MatchCandidate | None:
    headers = {h["name"].lower(): h["value"]
               for h in (msg.get("payload", {}).get("headers") or [])}
    date_str = headers.get("date", "")
    occurred = _parse_email_date(date_str) or date.today()
    attachments = _gmail_extract_attachments(gmail, msg)
    if not attachments:
        # Geen PDF-bijlage — probeer de mail-body als evidence-PDF te
        # renderen voor invoice-in-body cases (Datadog/Userback/Stripe etc).
        rendered = _gmail_render_evidence_pdf(gmail, msg, headers)
        if rendered is not None:
            attachments = [rendered]
        else:
            return None
    return MatchCandidate(
        source="gmail",
        message_id=str(msg.get("id", "")),
        from_addr=headers.get("from", ""),
        subject=headers.get("subject", ""),
        occurred_at=occurred,
        attachments=attachments,
    )


def _gmail_render_evidence_pdf(
    gmail: Any, msg: dict[str, Any], headers: dict[str, str],
) -> Attachment | None:
    """Render Gmail body als PDF-evidence wanneer er geen PDF-attachment was
    maar de mail invoice-keywords bevat. Returns None bij no-go."""
    from extensions.receipt_collector.email_to_pdf import (
        looks_like_invoice, render_email_as_pdf,
    )
    body_html, body_text = _gmail_extract_body(gmail, msg)
    haystack = f"{headers.get('subject','')}\n{body_text or ''}\n{body_html or ''}"
    if not looks_like_invoice(haystack):
        return None
    pdf_bytes = render_email_as_pdf(
        headers={
            "From": headers.get("from", ""),
            "To": headers.get("to", ""),
            "Date": headers.get("date", ""),
            "Subject": headers.get("subject", ""),
        },
        body_html=body_html,
        body_text=body_text,
    )
    if pdf_bytes is None:
        return None
    msg_id = str(msg.get("id", "") or "msg")
    return Attachment(
        filename=f"email-evidence-{msg_id}.pdf",
        mime_type="application/pdf",
        data=pdf_bytes,
    )


def _gmail_extract_body(
    gmail: Any, msg: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return (html, text) van de mail. Walks alle parts (niet alleen die
    met filename), prefereert text/html en text/plain bodies."""
    import base64
    html_parts: list[str] = []
    text_parts: list[str] = []

    def _walk_all(payload: dict[str, Any]) -> None:
        mime = (payload.get("mimeType") or "").lower()
        body = payload.get("body") or {}
        data = body.get("data")
        if data and mime in ("text/html", "text/plain"):
            try:
                raw = base64.urlsafe_b64decode(data + "===").decode(
                    "utf-8", errors="replace",
                )
            except Exception:
                raw = ""
            if raw:
                (html_parts if mime == "text/html" else text_parts).append(raw)
        for child in payload.get("parts") or []:
            _walk_all(child)

    _walk_all(msg.get("payload", {}))
    html = "\n".join(html_parts) or None
    text = "\n".join(text_parts) or None
    return html, text


def _gmail_extract_attachments(
    gmail: Any, msg: dict[str, Any],
) -> list[Attachment]:
    out: list[Attachment] = []
    for part in _walk_parts(msg.get("payload", {})):
        filename = part.get("filename") or ""
        mime = part.get("mimeType") or ""
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        if not filename or not attachment_id:
            continue
        if not _is_receipt_mime(mime, filename):
            continue
        try:
            data = gmail.get_attachment(
                message_id=msg["id"], attachment_id=attachment_id,
            )
        except Exception:
            log.exception("gmail attachment fetch failed for %s", filename)
            continue
        out.append(Attachment(filename=filename, mime_type=mime, data=data))
    return out


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if payload.get("filename"):
        out.append(payload)
    for child in payload.get("parts") or []:
        out.extend(_walk_parts(child))
    return out


def search_imap_for_transaction(
    account: ImapAccount, password: str, txn: Transaction, *,
    window_start: date, window_end: date,
    vendor_strategy: dict[str, Any] | None = None,
    search_vendor: str | None = None,
    max_results: int = 10,
) -> list[MatchCandidate]:
    """Server-side IMAP search met date-window + vendor-text. Geen lokale
    download van alle bodies — `imap-tools` AND() maakt dit native."""
    try:
        from imap_tools import AND, MailBox, MailBoxUnencrypted
        cls = MailBox if account.ssl else MailBoxUnencrypted
        mb = cls(account.host, port=account.port)
        mb.login(account.username, password)
    except Exception:
        log.exception("imap connect failed for %s", account.name)
        return []

    out: list[MatchCandidate] = []
    try:
        criteria_kwargs: dict[str, Any] = {
            "date_gte": window_start,
            "date_lt": window_end + timedelta(days=1),
        }
        if vendor_strategy and vendor_strategy.get("email_query_hint"):
            # email_query_hint kan bv. 'from:foo@bar.com' zijn — strip prefix
            hint = str(vendor_strategy["email_query_hint"])
            if hint.lower().startswith("from:"):
                criteria_kwargs["from_"] = hint[5:].strip()
        else:
            clean = (search_vendor.strip() if search_vendor
                     else _clean_vendor_for_search(txn.vendor))
            if clean:
                criteria_kwargs["body"] = clean
        criteria = AND(**criteria_kwargs)
        for msg in mb.fetch(criteria, limit=max_results, mark_seen=False, bulk=True):
            attachments = _imap_extract_attachments(msg)
            if not attachments:
                rendered = _imap_render_evidence_pdf(msg)
                if rendered is None:
                    continue
                attachments = [rendered]
            occurred = msg.date.date() if msg.date else date.today()
            out.append(MatchCandidate(
                source=f"imap:{account.name}",
                message_id=str(msg.uid or ""),
                from_addr=msg.from_ or "",
                subject=msg.subject or "",
                occurred_at=occurred,
                attachments=attachments,
            ))
    except Exception:
        log.exception("imap search failed for %s", account.name)
    finally:
        try:
            mb.logout()
        except Exception:
            pass
    return out


def _amount_search_strings(amount_cents: int) -> list[str]:
    """Genereer search-strings voor een bedrag in NL/EN format.
    Bijv: 12750 → ['127,50', '127.50']. Gebruikt door reverse-match."""
    if amount_cents == 0:
        return []
    amount_eur = abs(amount_cents) / 100.0
    nl = f"{amount_eur:.2f}".replace(".", ",")
    en = f"{amount_eur:.2f}"
    out = [nl]
    if en != nl:
        out.append(en)
    return out


def search_gmail_by_amount(
    gmail: Any, *,
    amount_cents: int, window_start: date, window_end: date,
    max_results: int = 20,
) -> list[MatchCandidate]:
    """Reverse-match: zoek emails met attachment + amount in date-window,
    zonder vendor-filter. Voor txns waar vendor-search niet werkt (bv.
    voorgeschoten betalingen, onbekende vendor-aliassen)."""
    queries = _amount_search_strings(amount_cents)
    seen_ids: set[str] = set()
    out: list[MatchCandidate] = []
    base = (f"has:attachment "
             f"after:{window_start.strftime('%Y/%m/%d')} "
             f"before:{(window_end + timedelta(days=1)).strftime('%Y/%m/%d')}")
    for amount_str in queries:
        query = f'{base} "{amount_str}"'
        try:
            results = gmail.search(query=query, max_results=max_results)
        except Exception:
            log.exception("gmail amount-search failed (%s)", amount_str)
            continue
        for r in results:
            msg_id = r.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            try:
                msg = gmail.get_message_full(msg_id)
            except Exception:
                log.exception("gmail get_message_full failed for %s", msg_id)
                continue
            cand = _gmail_message_to_candidate(gmail, msg)
            if cand is not None:
                out.append(cand)
    return out


def search_imap_by_amount(
    account: ImapAccount, password: str, *,
    amount_cents: int, window_start: date, window_end: date,
    max_results: int = 20,
) -> list[MatchCandidate]:
    """Reverse-match op IMAP: body-search op amount-strings + date-window.
    Geen vendor-filter."""
    queries = _amount_search_strings(amount_cents)
    if not queries:
        return []
    try:
        from imap_tools import AND, MailBox, MailBoxUnencrypted
        cls = MailBox if account.ssl else MailBoxUnencrypted
        mb = cls(account.host, port=account.port)
        mb.login(account.username, password)
    except Exception:
        log.exception("imap connect failed for %s", account.name)
        return []

    seen_uids: set[str] = set()
    out: list[MatchCandidate] = []
    try:
        for amount_str in queries:
            criteria = AND(
                date_gte=window_start,
                date_lt=window_end + timedelta(days=1),
                body=amount_str,
            )
            try:
                for msg in mb.fetch(criteria, limit=max_results,
                                       mark_seen=False, bulk=True):
                    uid = str(msg.uid or "")
                    if not uid or uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    attachments = _imap_extract_attachments(msg)
                    if not attachments:
                        continue
                    occurred = msg.date.date() if msg.date else date.today()
                    out.append(MatchCandidate(
                        source=f"imap:{account.name}",
                        message_id=uid,
                        from_addr=msg.from_ or "",
                        subject=msg.subject or "",
                        occurred_at=occurred,
                        attachments=attachments,
                    ))
            except Exception:
                log.exception("imap amount-search fetch failed for %s (%s)",
                                account.name, amount_str)
    finally:
        try:
            mb.logout()
        except Exception:
            pass
    return out


def _imap_extract_attachments(msg: Any) -> list[Attachment]:
    out: list[Attachment] = []
    for att in (msg.attachments or []):
        if not _is_receipt_mime(att.content_type or "", att.filename or ""):
            continue
        out.append(Attachment(
            filename=att.filename or "attachment",
            mime_type=att.content_type or "application/octet-stream",
            data=att.payload,
        ))
    return out


def _imap_render_evidence_pdf(msg: Any) -> Attachment | None:
    """Render IMAP body als PDF-evidence voor invoice-in-body cases."""
    from extensions.receipt_collector.email_to_pdf import (
        looks_like_invoice, render_email_as_pdf,
    )
    body_text = msg.text or ""
    body_html = msg.html or ""
    haystack = f"{msg.subject or ''}\n{body_text}\n{body_html}"
    if not looks_like_invoice(haystack):
        return None
    pdf_bytes = render_email_as_pdf(
        headers={
            "From": msg.from_ or "",
            "To": ", ".join(msg.to or []) if msg.to else "",
            "Date": str(msg.date) if msg.date else "",
            "Subject": msg.subject or "",
        },
        body_html=body_html or None,
        body_text=body_text or None,
    )
    if pdf_bytes is None:
        return None
    return Attachment(
        filename=f"email-evidence-{msg.uid or 'imap'}.pdf",
        mime_type="application/pdf",
        data=pdf_bytes,
    )


def _is_receipt_mime(mime: str, filename: str) -> bool:
    """Alleen PDF — een JPG/PNG is nooit een factuur in dit kanaal.
    the user's eis: image-attachments leveren alleen ruis op (logo's in
    signature, foto's in newsletters) en worden door pypdf niet gelezen."""
    mime_l = mime.lower()
    fn_l = filename.lower()
    return mime_l.startswith("application/pdf") or fn_l.endswith(".pdf")


def score_candidate(
    candidate: MatchCandidate, txn: Transaction,
) -> tuple[float, list[str]]:
    """Score 0.0-1.0 op:
    - amount-exact-match in PDF/text (0.5)
    - datum-nabijheid (0.3, lineair tussen 0 en 14 dagen)
    - vendor-naam in subject/from/body (0.2)
    """
    score = 0.0
    reasons: list[str] = []
    txn_amount_eur = abs(txn.amount_cents) / 100.0

    # Datum-score
    days_off = abs((candidate.occurred_at - txn.transaction_date).days)
    if days_off <= 14:
        date_score = 0.3 * (1.0 - days_off / 14.0)
        score += date_score
        reasons.append(f"date+{date_score:.2f} ({days_off}d off)")

    # Vendor-score
    vendor_l = txn.vendor.lower()
    vendor_clean = _clean_vendor_for_search(txn.vendor).lower()
    haystack = (candidate.subject + " " + candidate.from_addr).lower()
    if vendor_clean and vendor_clean in haystack:
        score += 0.2
        reasons.append("vendor in subject/from +0.20")
    elif vendor_l and len(vendor_l) >= 4 and vendor_l[:8] in haystack:
        score += 0.1
        reasons.append("vendor partial +0.10")

    # Amount-score (PDF text scan)
    for att in candidate.attachments:
        amt_found = _amount_in_pdf(att.data, txn_amount_eur)
        if amt_found:
            score += 0.5
            reasons.append(f"amount {txn_amount_eur:.2f} in {att.filename} +0.50")
            break

    return min(score, 1.0), reasons


def _amount_in_pdf(data: bytes, amount_eur: float) -> bool:
    """Open PDF, zoek naar amount-string. Tolerant voor formaat-variaties.
    Skipt non-PDF bytes silent — de mime-check filtert al maar header-
    validatie vangt edge-cases (corrupt download, mislabeled mime)."""
    if not data.startswith(b"%PDF"):
        return False
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        text = ""
        for page in reader.pages[:5]:  # max 5 pagina's, zat
            text += page.extract_text() or ""
    except Exception:
        return False
    text_clean = text.replace(",", ".").replace(" ", "")
    candidates = [
        f"{amount_eur:.2f}",
        f"{amount_eur:.2f}".replace(".", ","),
        f"€{amount_eur:.2f}",
        f"{amount_eur:.0f}.{int(round((amount_eur - int(amount_eur)) * 100)):02d}",
    ]
    for c in candidates:
        if c.replace(",", ".").replace(" ", "") in text_clean:
            return True
    return False


def _parse_email_date(s: str) -> date | None:
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None
