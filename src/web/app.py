"""Lokaal audit-dashboard + project-CRUD.

Twee soorten audit-records:
  - egress-YYYY-MM-DD.jsonl  → metadata per externe call (geen content)
  - payloads-YYYY-MM-DD.jsonl → redacted payload + Claude response per call
                                (alleen als settings.log_payloads=true)

Plus `/projects` voor CRUD over de `projects` tabel — the user kan daar
actieve initiatives toevoegen, status veranderen, deadlines schuiven en
keywords editen die de aggregator gebruikt om mail/decisions/loops aan
projecten te koppelen.

Bind alleen op 127.0.0.1 + Host-header allowlist. Een loopback-bind alleen
beschermt NIET tegen DNS-rebinding: een willekeurige website die the user
bezoekt kan via low-TTL A-records eerst naar attacker-IP resolven, dan
naar 127.0.0.1, en dan POST'en naar dashboard endpoints. De Host-header
arriveert dan als de attacker-domeinnaam — door alleen 127.0.0.1 /
localhost te accepteren breken we die aanval (ISO_AUDIT 2026-05
CRITICAL-B).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

# Host-header allowlist: blokkeert DNS-rebinding van willekeurige sites
# naar het dashboard. Loopback-bind alléén dekt LAN-toegang, niet
# browser-tab van the user. Zie ISO_AUDIT 2026-05 CRITICAL-B.
_ALLOWED_HOSTS = frozenset({
    "127.0.0.1:8080",
    "localhost:8080",
    "127.0.0.1",
    "localhost",
})


class HostHeaderAllowlistMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        host = (request.headers.get("host") or "").lower()
        if host not in _ALLOWED_HOSTS:
            log.warning(
                "dashboard: rejected request with Host=%r path=%s — possible DNS-rebind",
                host, request.url.path,
            )
            return PlainTextResponse(
                "Host not allowed. Dashboard accepts only 127.0.0.1 / localhost.",
                status_code=403,
            )
        return await call_next(request)

from extensions.projects.aggregator import project_status as _project_status
from extensions.projects.schema import (
    VALID_STATUS,
    delete_project,
    get_project,
    insert_project,
    list_projects,
    update_project,
)
from extensions.receipt_collector.schema import (
    VALID_SOURCE_KIND,
    list_vendor_strategies,
    upsert_vendor_strategy,
)
from extensions.receipt_collector.schema import (
    get_run as _get_receipt_run,
)
from extensions.receipt_collector.schema import (
    list_run_items as _list_run_items,
)
from extensions.receipt_collector.schema import (
    list_runs as _list_receipt_runs,
)
from integrations.imap import (
    ImapAccount,
    ImapClient,
    ImapFolders,
)
from integrations.imap import (
    delete_password as _imap_delete_password,
)
from integrations.imap import (
    get_password as _imap_get_password,
)
from integrations.imap import (
    load_accounts as _imap_load,
)
from integrations.imap import (
    save_accounts as _imap_save,
)
from integrations.imap import (
    set_password as _imap_set_password,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(
    audit_dir: Path,
    db_path: Path | None = None,
    imap_yaml: Path | None = None,
    ollama: Any | None = None,
) -> FastAPI:
    audit_dir = audit_dir.resolve()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["format_ts"] = _format_ts

    app = FastAPI(title="pa-agent audit", docs_url=None, redoc_url=None)
    app.add_middleware(HostHeaderAllowlistMiddleware)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        dates = _available_dates(audit_dir)
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        latest = dates[0] if dates else today
        return templates.TemplateResponse(request, "index.html", {
            "dates": dates,
            "latest": latest,
        })

    @app.get("/api/cost", response_class=JSONResponse)
    def api_cost() -> JSONResponse:
        """Current-month Claude spend + last 30 days per-day breakdown."""
        from core.cost_tracker import current_month_cost, daily_series
        month = current_month_cost(db_path)
        daily = daily_series(db_path, days=30)
        from core.config import load_settings
        try:
            budget = load_settings().monthly_anthropic_budget_usd
        except Exception:
            budget = 0.0
        return JSONResponse({
            "month": {
                "calls": month.calls,
                "tokens_in": month.tokens_in,
                "tokens_out": month.tokens_out,
                "usd": round(month.usd, 4),
            },
            "budget_usd": budget,
            "daily": daily,
        })

    @app.get("/cost", response_class=HTMLResponse)
    def cost_page(request: Request) -> HTMLResponse:
        from core.cost_tracker import current_month_cost, daily_series
        month = current_month_cost(db_path)
        daily = daily_series(db_path, days=30)
        from core.config import load_settings
        try:
            budget = load_settings().monthly_anthropic_budget_usd
        except Exception:
            budget = 0.0
        pct = int(100 * month.usd / budget) if budget else 0
        return templates.TemplateResponse(request, "cost.html", {
            "month": month, "budget": budget, "pct": pct, "daily": daily,
        })

    @app.get("/audit", response_class=HTMLResponse)
    def audit(
        request: Request,
        date: str = Query(default_factory=lambda: datetime.now(TZ).strftime("%Y-%m-%d")),
        task: str | None = Query(default=None),
        label: str | None = Query(default=None),
        backend: str | None = Query(default=None),
    ) -> HTMLResponse:
        if not _is_safe_date(date):
            return templates.TemplateResponse(request, "error.html",
                                              {"msg": "ongeldige datum"}, status_code=400)
        egress = _read_jsonl(audit_dir / f"egress-{date}.jsonl")
        payloads = _read_jsonl(audit_dir / f"payloads-{date}.jsonl")
        merged = _merge(egress, payloads)
        if task:
            merged = [r for r in merged if r.get("task") == task]
        if label:
            merged = [r for r in merged if r.get("label") == label]
        if backend:
            merged = [r for r in merged if r.get("backend") == backend]

        # Newest first
        merged.sort(key=lambda r: r.get("ts", ""), reverse=True)

        dates = _available_dates(audit_dir)
        return templates.TemplateResponse(request, "audit.html", {
            "date": date,
            "task": task or "",
            "label": label or "",
            "backend": backend or "",
            "records": merged,
            "dates": dates,
            "all_tasks": sorted({r.get("task", "") for r in merged if r.get("task")}),
            "all_labels": sorted({r.get("label", "") for r in merged if r.get("label")}),
            "all_backends": sorted({r.get("backend", "") for r in merged if r.get("backend")}),
        })

    @app.get("/audit/{date}/{idx}", response_class=HTMLResponse)
    def detail(request: Request, date: str, idx: int) -> HTMLResponse:
        if not _is_safe_date(date):
            return templates.TemplateResponse(request, "error.html",
                                              {"msg": "ongeldige datum"}, status_code=400)
        egress = _read_jsonl(audit_dir / f"egress-{date}.jsonl")
        payloads = _read_jsonl(audit_dir / f"payloads-{date}.jsonl")
        merged = _merge(egress, payloads)
        merged.sort(key=lambda r: r.get("ts", ""), reverse=True)
        if idx < 0 or idx >= len(merged):
            return templates.TemplateResponse(request, "error.html",
                                              {"msg": "geen record op index"}, status_code=404)
        rec = merged[idx]
        leak_hits = _scan_for_leaks(rec)
        return templates.TemplateResponse(request, "detail.html", {
            "date": date,
            "idx": idx,
            "rec": rec,
            "rec_pretty": json.dumps(rec, ensure_ascii=False, indent=2, default=str),
            "leak_hits": leak_hits,
        })

    @app.get("/api/audit", response_class=JSONResponse)
    def api_audit(date: str = Query(default_factory=lambda: datetime.now(TZ).strftime("%Y-%m-%d"))) -> JSONResponse:
        if not _is_safe_date(date):
            return JSONResponse({"error": "invalid date"}, status_code=400)
        return JSONResponse({
            "date": date,
            "egress": _read_jsonl(audit_dir / f"egress-{date}.jsonl"),
            "payloads": _read_jsonl(audit_dir / f"payloads-{date}.jsonl"),
        })

    # --- Projects CRUD ---------------------------------------------------
    # Disabled if db_path is not provided (audit-only setup).

    if db_path is not None:
        _register_project_routes(app, templates, db_path)
        _register_vendor_routes(app, templates, db_path)
        _register_run_routes(app, templates, db_path)
        _register_wishes_routes(app, templates, db_path)
        _register_loops_routes(app, templates, db_path)
        _register_ceo_routes(app, templates, db_path)
        _register_vip_routes(app, templates, db_path)
        _register_uptime_routes(app, templates, db_path)
        _register_sales_routes(app, templates, db_path)

    if imap_yaml is not None:
        _register_imap_routes(app, templates, imap_yaml)

    # Suggest-endpoints werken zonder db (alleen Llama-call) — outside db_path gate.
    _register_llm_suggest_routes(app, ollama)

    return app


def _register_llm_suggest_routes(app: FastAPI, ollama: Any | None) -> None:
    """JSON-endpoints voor browser-fetch JS: vraag Llama om suggestions."""
    from core.llm_helpers import llm_json_array, llm_short_text

    @app.post("/api/suggest/project-keywords")
    async def suggest_keywords(payload: dict[str, Any]) -> JSONResponse:
        if ollama is None:
            return JSONResponse({"error": "ollama not available"}, status_code=503)
        title = str(payload.get("title", "")).strip()
        description = str(payload.get("description", "")).strip()
        if not title:
            return JSONResponse({"error": "title required"}, status_code=400)
        prompt = (
            "Je krijgt een project-titel en optionele beschrijving. "
            "Stel 3-5 zoektermen voor (kort, lower-case, alpha) die de "
            "project-aggregator kan gebruiken om mail / decisions / loops "
            "aan dit project te koppelen.\n\n"
            "Output STRIKT als JSON-array van strings. Voorbeeld: "
            '["pa-agent", "rosa", "assistant"]'
        )
        result = llm_json_array(
            ollama, system=prompt,
            user=f"Title: {title}\nDescription: {description or '(none)'}",
        )
        keywords = [str(k).strip() for k in (result or []) if str(k).strip()][:8]
        return JSONResponse({"keywords": keywords})

    @app.post("/api/suggest/vendor-email-hint")
    async def suggest_vendor_email(payload: dict[str, Any]) -> JSONResponse:
        if ollama is None:
            return JSONResponse({"error": "ollama not available"}, status_code=503)
        name = str(payload.get("name", "")).strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        prompt = (
            "Je krijgt een vendor-naam (bv. 'Datadog', 'AWS', 'OpenAI'). "
            "Geef de meest waarschijnlijke Gmail-search query om bonnen "
            "van die vendor te vinden. Format: 'from:billing@vendor.com' "
            "(één regel, geen prose).\n\n"
            "Als je het echt niet weet, antwoord 'unknown'."
        )
        text = llm_short_text(
            ollama, system=prompt,
            user=f"Vendor: {name}", max_tokens=60,
        )
        if not text or text.strip().lower().startswith("unknown"):
            return JSONResponse({"hint": None,
                                  "note": "ollama kon geen suggestie geven"})
        # Strip leading/trailing quotes/punctuation
        hint = text.strip().strip("'\"").split("\n")[0]
        return JSONResponse({"hint": hint})

    return app


def _register_project_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:

    @app.get("/projects", response_class=HTMLResponse)
    def projects_index(
        request: Request,
        status: str = Query(default=""),
        company: str = Query(default=""),
        message: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            rows = list_projects(
                conn,
                status=(status or None),
                company=(company or None),
            )
        formatted = [_format_project(r) for r in rows]
        return templates.TemplateResponse(request, "projects_list.html", {
            "projects": formatted,
            "status": status, "company": company,
            "message": message,
        })

    @app.get("/projects/new", response_class=HTMLResponse)
    def projects_new(request: Request, error: str = Query(default="")) -> HTMLResponse:
        return templates.TemplateResponse(request, "projects_form.html", {
            "project": None, "error": error,
        })

    @app.post("/projects/new")
    def projects_create(
        title: str = Form(...),
        slug: str = Form(default=""),
        company: str = Form(default=""),
        owner: str = Form(default=""),
        status: str = Form(default="active"),
        deadline: str = Form(default=""),
        keywords: str = Form(default=""),
        description: str = Form(default=""),
    ) -> RedirectResponse:
        slug_final = slug.strip() or _slugify(title)
        if not slug_final:
            return RedirectResponse(
                "/projects/new?error=could+not+derive+slug", status_code=303,
            )
        if status not in VALID_STATUS:
            return RedirectResponse(
                f"/projects/new?error=invalid+status:+{status}", status_code=303,
            )
        deadline_at = _parse_form_deadline(deadline)
        kws = [k.strip() for k in keywords.split(",") if k.strip()]
        try:
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                insert_project(
                    conn,
                    slug=slug_final, title=title.strip(),
                    description=(description.strip() or None),
                    company=(company.strip() or None),
                    owner=(owner.strip() or None),
                    status=status,
                    keywords=kws,
                    deadline_at=deadline_at,
                )
        except sqlite3.IntegrityError:
            return RedirectResponse(
                f"/projects/new?error=slug+already+exists:+{slug_final}",
                status_code=303,
            )
        return RedirectResponse(
            f"/projects?message=created+{slug_final}", status_code=303,
        )

    @app.get("/projects/{slug}", response_class=HTMLResponse)
    def projects_detail(request: Request, slug: str) -> HTMLResponse:
        if not _is_safe_slug(slug):
            return templates.TemplateResponse(request, "error.html",
                                               {"msg": "ongeldige slug"}, status_code=400)
        result = _project_status(db_path, slug=slug, days_back=30)
        if "error" in result:
            return templates.TemplateResponse(request, "error.html",
                                               {"msg": result["error"]}, status_code=404)
        return templates.TemplateResponse(request, "projects_detail.html", {
            "project": _format_project(result["project"]),
            "recent_comm": result["recent_comm"],
            "recent_decisions": result["recent_decisions"],
            "open_loops": result["open_loops"],
            "upcoming_events": result["upcoming_events"],
            "matched_terms": result["matched_terms"],
            "days_back": 30,
        })

    @app.get("/projects/{slug}/edit", response_class=HTMLResponse)
    def projects_edit(
        request: Request, slug: str, error: str = Query(default=""),
    ) -> HTMLResponse:
        if not _is_safe_slug(slug):
            return templates.TemplateResponse(request, "error.html",
                                               {"msg": "ongeldige slug"}, status_code=400)
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            proj = get_project(conn, slug=slug)
        if not proj:
            return templates.TemplateResponse(request, "error.html",
                                               {"msg": "project not found"}, status_code=404)
        return templates.TemplateResponse(request, "projects_form.html", {
            "project": _format_project(proj), "error": error,
        })

    @app.post("/projects/{slug}/edit")
    def projects_update(
        slug: str,
        title: str = Form(...),
        company: str = Form(default=""),
        owner: str = Form(default=""),
        status: str = Form(default="active"),
        deadline: str = Form(default=""),
        keywords: str = Form(default=""),
        description: str = Form(default=""),
    ) -> RedirectResponse:
        if not _is_safe_slug(slug):
            return RedirectResponse("/projects", status_code=303)
        if status not in VALID_STATUS:
            return RedirectResponse(
                f"/projects/{slug}/edit?error=invalid+status:+{status}",
                status_code=303,
            )
        kws = [k.strip() for k in keywords.split(",") if k.strip()]
        deadline_at = _parse_form_deadline(deadline) if deadline.strip() else None
        clear_deadline = not deadline.strip()
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            proj = get_project(conn, slug=slug)
            if not proj:
                return RedirectResponse("/projects", status_code=303)
            update_project(
                conn,
                project_id=proj["id"],
                title=title.strip(),
                description=(description.strip() or None),
                company=(company.strip() or None),
                owner=(owner.strip() or None),
                status=status,
                keywords=kws,
                deadline_at=deadline_at,
                clear_deadline=clear_deadline,
            )
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/delete")
    def projects_delete(slug: str) -> RedirectResponse:
        if not _is_safe_slug(slug):
            return RedirectResponse("/projects", status_code=303)
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            proj = get_project(conn, slug=slug)
            if proj:
                delete_project(conn, proj["id"])
        return RedirectResponse(
            f"/projects?message=deleted+{slug}", status_code=303,
        )


def _register_vendor_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """CRUD voor vendor_strategies — Rosa's geheugen waar bonnen vandaan komen."""

    @app.get("/vendors", response_class=HTMLResponse)
    def vendors_index(
        request: Request,
        message: str = Query(default=""),
        error: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            vendors = list_vendor_strategies(conn)
            unknowns_count = _latest_unknown_count(conn)
        formatted = [_format_vendor(v) for v in vendors]
        return templates.TemplateResponse(request, "vendors_list.html", {
            "vendors": formatted,
            "unknowns_count": unknowns_count,
            "message": message, "error": error,
        })

    @app.get("/vendors/new", response_class=HTMLResponse)
    def vendors_new(
        request: Request,
        prefill_name: str = Query(default=""),
        prefill_alias: str = Query(default=""),
        prefill_amount: str = Query(default=""),
        prefill_date: str = Query(default=""),
        error: str = Query(default=""),
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, "vendors_form.html", {
            "vendor": None, "error": error,
            "prefill_name": prefill_name,
            "prefill_alias": prefill_alias,
            "prefill_amount": prefill_amount,
            "prefill_date": prefill_date,
        })

    @app.post("/vendors/new")
    def vendors_create(
        name: str = Form(...),
        source_kind: str = Form(...),
        aliases: str = Form(default=""),
        email_query_hint: str = Form(default=""),
        portal_url: str = Form(default=""),
        portal_notes: str = Form(default=""),
    ) -> RedirectResponse:
        if source_kind not in VALID_SOURCE_KIND:
            return RedirectResponse(
                f"/vendors/new?error=invalid+source_kind:+{source_kind}",
                status_code=303,
            )
        kws = [k.strip() for k in aliases.split(",") if k.strip()]
        try:
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                upsert_vendor_strategy(
                    conn, name=name.strip(), source_kind=source_kind,
                    aliases=kws,
                    email_query_hint=(email_query_hint.strip() or None),
                    portal_url=(portal_url.strip() or None),
                    portal_notes=(portal_notes.strip() or None),
                )
        except ValueError as e:
            return RedirectResponse(
                f"/vendors/new?error={_url_safe(str(e))}", status_code=303,
            )
        return RedirectResponse(
            f"/vendors?message=saved+{_url_safe(name)}", status_code=303,
        )

    @app.get("/vendors/{vendor_id}/edit", response_class=HTMLResponse)
    def vendors_edit(
        request: Request, vendor_id: int, error: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            row = conn.execute(
                "SELECT * FROM vendor_strategies WHERE id=?", (vendor_id,),
            ).fetchone()
            if not row:
                return templates.TemplateResponse(request, "error.html",
                                                   {"msg": "vendor not found"}, status_code=404)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM vendor_strategies WHERE id=?", (vendor_id,),
            ).fetchone()
        from extensions.receipt_collector.schema import _vendor_to_dict
        vendor = _format_vendor(_vendor_to_dict(row))
        return templates.TemplateResponse(request, "vendors_form.html", {
            "vendor": vendor, "error": error,
        })

    @app.post("/vendors/{vendor_id}/edit")
    def vendors_update(
        vendor_id: int,
        name: str = Form(...),
        source_kind: str = Form(...),
        aliases: str = Form(default=""),
        email_query_hint: str = Form(default=""),
        portal_url: str = Form(default=""),
        portal_notes: str = Form(default=""),
    ) -> RedirectResponse:
        if source_kind not in VALID_SOURCE_KIND:
            return RedirectResponse(
                f"/vendors/{vendor_id}/edit?error=invalid+source_kind",
                status_code=303,
            )
        kws = [k.strip() for k in aliases.split(",") if k.strip()]
        # upsert by name — als de naam wijzigt maakt 't een nieuwe rij; we
        # voorkomen rename door de bestaande naam te respecteren.
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            row = conn.execute(
                "SELECT name FROM vendor_strategies WHERE id=?", (vendor_id,),
            ).fetchone()
            if not row:
                return RedirectResponse("/vendors", status_code=303)
            existing_name = row[0]
            target_name = name.strip()
            if target_name != existing_name:
                # Naamwijziging: upsert overschrijft niet, dus update direct
                conn.execute(
                    "UPDATE vendor_strategies SET name=?, aliases=?, "
                    "source_kind=?, email_query_hint=?, portal_url=?, "
                    "portal_notes=?, updated_at=strftime('%s','now') "
                    "WHERE id=?",
                    (target_name, _json_encode(kws), source_kind,
                     email_query_hint.strip() or None,
                     portal_url.strip() or None,
                     portal_notes.strip() or None,
                     vendor_id),
                )
            else:
                upsert_vendor_strategy(
                    conn, name=target_name, source_kind=source_kind,
                    aliases=kws,
                    email_query_hint=(email_query_hint.strip() or None),
                    portal_url=(portal_url.strip() or None),
                    portal_notes=(portal_notes.strip() or None),
                )
        return RedirectResponse(
            f"/vendors?message=updated+{_url_safe(target_name)}",
            status_code=303,
        )

    @app.post("/vendors/{vendor_id}/delete")
    def vendors_delete(vendor_id: int) -> RedirectResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            conn.execute("DELETE FROM vendor_strategies WHERE id=?",
                          (vendor_id,))
        return RedirectResponse("/vendors?message=deleted", status_code=303)

    @app.get("/vendors/import-unknowns", response_class=HTMLResponse)
    def vendors_import_unknowns(request: Request) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            conn.row_factory = sqlite3.Row
            run_row = conn.execute(
                "SELECT id FROM receipt_runs ORDER BY id DESC LIMIT 1",
            ).fetchone()
            if not run_row:
                return templates.TemplateResponse(request, "error.html",
                                                   {"msg": "geen runs in DB"}, status_code=404)
            run_id = run_row[0]
            rows = conn.execute(
                "SELECT vendor_raw, COUNT(*) c, "
                "MAX(amount_cents) last_amount, "
                "MAX(transaction_date) last_date "
                "FROM receipt_run_items "
                "WHERE run_id=? AND status='unknown_vendor' "
                "GROUP BY vendor_raw ORDER BY c DESC, vendor_raw",
                (run_id,),
            ).fetchall()
        unknowns = []
        for r in rows:
            unknowns.append({
                "vendor_raw": r["vendor_raw"],
                "count": r["c"],
                "last_amount": abs(r["last_amount"]) / 100.0,
                "last_date": _format_ts_date(r["last_date"]),
                "suggested_name": _suggest_canonical_name(r["vendor_raw"]),
            })
        return templates.TemplateResponse(request, "vendors_import.html", {
            "unknowns": unknowns, "run_id": run_id,
        })


def _register_run_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """Read-only views op receipt_runs + per-run items + attachment-download."""

    @app.get("/receipt-runs", response_class=HTMLResponse)
    def runs_index(request: Request) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            runs = _list_receipt_runs(conn, limit=50)
        formatted = [_format_run(r) for r in runs]
        from pathlib import Path
        home_prefix = str(Path.home()) + "/"
        return templates.TemplateResponse(request, "runs_list.html", {
            "runs": formatted,
            "home_prefix": home_prefix,
        })

    @app.get("/receipt-runs/{run_id}", response_class=HTMLResponse)
    def runs_detail(
        request: Request, run_id: int,
        status: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            run = _get_receipt_run(conn, run_id)
            if not run:
                return templates.TemplateResponse(request, "error.html",
                                                   {"msg": "run not found"}, status_code=404)
            items = _list_run_items(conn, run_id)
        if status:
            items = [i for i in items if i["status"] == status]
        return templates.TemplateResponse(request, "runs_detail.html", {
            "run": _format_run(run),
            "items": [_format_run_item(i) for i in items],
            "status_filter": status,
        })

    @app.get("/receipt-runs/{run_id}/attachment/{filename}")
    def runs_attachment(run_id: int, filename: str):  # type: ignore[no-untyped-def]
        from fastapi.responses import FileResponse
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            run = _get_receipt_run(conn, run_id)
        if not run:
            return JSONResponse({"error": "run not found"}, status_code=404)
        # Path-safety: alleen filename, geen .. of /
        if "/" in filename or ".." in filename:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        target = Path(run["output_dir"]) / filename
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(str(target))


def _register_imap_routes(
    app: FastAPI, templates: Jinja2Templates, imap_yaml: Path,
) -> None:
    """Dashboard CRUD voor IMAP-accounts. Wachtwoorden gaan direct naar
    macOS Keychain (service 'pa-agent-imap'); de yaml bevat alleen
    host/port/folders/username. Form post over 127.0.0.1, geen externe
    blootstelling."""

    @app.get("/imap-accounts", response_class=HTMLResponse)
    def imap_index(
        request: Request,
        message: str = Query(default=""),
        error: str = Query(default=""),
    ) -> HTMLResponse:
        accounts = _imap_load(imap_yaml)
        rows = []
        for a in accounts:
            rows.append({
                "name": a.name, "label": a.label,
                "host": a.host, "port": a.port,
                "username": a.username,
                "smtp_host": a.smtp_host,
                "enabled": a.enabled,
                "has_password": _imap_get_password(a) is not None,
            })
        return templates.TemplateResponse(request, "imap_list.html", {
            "accounts": rows, "message": message, "error": error,
        })

    @app.get("/imap-accounts/new", response_class=HTMLResponse)
    def imap_new(request: Request, error: str = Query(default="")) -> HTMLResponse:
        return templates.TemplateResponse(request, "imap_form.html", {
            "account": None, "error": error,
        })

    @app.post("/imap-accounts/new")
    def imap_create(
        name: str = Form(...),
        label: str = Form(default=""),
        host: str = Form(...),
        port: int = Form(default=993),
        ssl: str = Form(default=""),
        username: str = Form(...),
        password: str = Form(...),
        folder_inbox: str = Form(default="INBOX"),
        folder_sent: str = Form(default="Sent"),
        enabled: str = Form(default=""),
        smtp_host: str = Form(default=""),
        smtp_port: int = Form(default=587),
        smtp_starttls: str = Form(default=""),
        from_address: str = Form(default=""),
        from_name: str = Form(default=""),
    ) -> RedirectResponse:
        name = name.strip()
        if not _is_safe_imap_name(name):
            return RedirectResponse(
                "/imap-accounts/new?error=invalid+name+(letters/cijfers/dash)",
                status_code=303,
            )
        existing = _imap_load(imap_yaml)
        if any(a.name == name for a in existing):
            return RedirectResponse(
                f"/imap-accounts/new?error=name+already+exists:+{name}",
                status_code=303,
            )
        new_account = ImapAccount(
            name=name,
            label=(label.strip() or name),
            host=host.strip(),
            port=int(port),
            ssl=bool(ssl),
            username=username.strip(),
            folders=ImapFolders(
                inbox=(folder_inbox.strip() or "INBOX"),
                sent=(folder_sent.strip() or "Sent"),
            ),
            enabled=bool(enabled),
            smtp_host=(smtp_host.strip() or None),
            smtp_port=int(smtp_port),
            smtp_use_starttls=bool(smtp_starttls),
            from_address=(from_address.strip() or None),
            from_name=(from_name.strip() or None),
        )
        _imap_save(imap_yaml, [*existing, new_account])
        _imap_set_password(new_account, password)

        ok, err = _imap_test(new_account, password)
        if not ok:
            return RedirectResponse(
                f"/imap-accounts?error=saved+but+test+failed:+{_url_safe(err)}",
                status_code=303,
            )
        return RedirectResponse(
            f"/imap-accounts?message=created+and+tested:+{name}",
            status_code=303,
        )

    @app.get("/imap-accounts/{name}/edit", response_class=HTMLResponse)
    def imap_edit(
        request: Request, name: str, error: str = Query(default=""),
    ) -> HTMLResponse:
        if not _is_safe_imap_name(name):
            return templates.TemplateResponse(request, "error.html",
                                               {"msg": "ongeldige naam"}, status_code=400)
        accounts = _imap_load(imap_yaml)
        acc = next((a for a in accounts if a.name == name), None)
        if not acc:
            return templates.TemplateResponse(request, "error.html",
                                               {"msg": "account not found"}, status_code=404)
        return templates.TemplateResponse(request, "imap_form.html", {
            "account": acc, "error": error,
        })

    @app.post("/imap-accounts/{name}/edit")
    def imap_update(
        name: str,
        label: str = Form(default=""),
        host: str = Form(...),
        port: int = Form(default=993),
        ssl: str = Form(default=""),
        username: str = Form(...),
        password: str = Form(default=""),
        folder_inbox: str = Form(default="INBOX"),
        folder_sent: str = Form(default="Sent"),
        enabled: str = Form(default=""),
        smtp_host: str = Form(default=""),
        smtp_port: int = Form(default=587),
        smtp_starttls: str = Form(default=""),
        from_address: str = Form(default=""),
        from_name: str = Form(default=""),
    ) -> RedirectResponse:
        if not _is_safe_imap_name(name):
            return RedirectResponse("/imap-accounts", status_code=303)
        accounts = _imap_load(imap_yaml)
        idx = next((i for i, a in enumerate(accounts) if a.name == name), -1)
        if idx < 0:
            return RedirectResponse("/imap-accounts", status_code=303)

        updated = ImapAccount(
            name=name,
            label=(label.strip() or name),
            host=host.strip(),
            port=int(port),
            ssl=bool(ssl),
            username=username.strip(),
            folders=ImapFolders(
                inbox=(folder_inbox.strip() or "INBOX"),
                sent=(folder_sent.strip() or "Sent"),
            ),
            enabled=bool(enabled),
            smtp_host=(smtp_host.strip() or None),
            smtp_port=int(smtp_port),
            smtp_use_starttls=bool(smtp_starttls),
            from_address=(from_address.strip() or None),
            from_name=(from_name.strip() or None),
        )
        accounts[idx] = updated
        _imap_save(imap_yaml, accounts)
        if password.strip():
            _imap_set_password(updated, password)
        return RedirectResponse(
            f"/imap-accounts?message=updated+{name}", status_code=303,
        )

    @app.post("/imap-accounts/{name}/test")
    def imap_test_route(name: str) -> RedirectResponse:
        if not _is_safe_imap_name(name):
            return RedirectResponse("/imap-accounts", status_code=303)
        accounts = _imap_load(imap_yaml)
        acc = next((a for a in accounts if a.name == name), None)
        if not acc:
            return RedirectResponse(
                f"/imap-accounts?error=not+found:+{name}", status_code=303,
            )
        pw = _imap_get_password(acc)
        if not pw:
            return RedirectResponse(
                f"/imap-accounts?error=no+password+in+Keychain+for+{name}",
                status_code=303,
            )
        ok, err = _imap_test(acc, pw)
        if ok:
            return RedirectResponse(
                f"/imap-accounts?message=test+OK:+{name}", status_code=303,
            )
        return RedirectResponse(
            f"/imap-accounts?error=test+failed+for+{name}:+{_url_safe(err)}",
            status_code=303,
        )

    @app.post("/imap-accounts/{name}/delete")
    def imap_delete_route(name: str) -> RedirectResponse:
        if not _is_safe_imap_name(name):
            return RedirectResponse("/imap-accounts", status_code=303)
        accounts = _imap_load(imap_yaml)
        acc = next((a for a in accounts if a.name == name), None)
        if acc:
            _imap_delete_password(acc)
            remaining = [a for a in accounts if a.name != name]
            _imap_save(imap_yaml, remaining)
        return RedirectResponse(
            f"/imap-accounts?message=deleted+{name}", status_code=303,
        )


def _imap_test(account: ImapAccount, password: str) -> tuple[bool, str]:
    try:
        client = ImapClient(account, password)
        client.test_connection()
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def _url_safe(s: str) -> str:
    """Quote voor query-string. Houdt het simpel voor read-back in flash."""
    import urllib.parse as _u
    return _u.quote_plus(s)[:200]


_IMAP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,59}$")


def _is_safe_imap_name(s: str) -> bool:
    return bool(_IMAP_NAME_RE.match(s))


# --- helpers --------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,59}$")


def _is_safe_date(s: str) -> bool:
    return bool(_DATE_RE.match(s))


def _is_safe_slug(s: str) -> bool:
    return bool(_SLUG_RE.match(s))


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60]


def _parse_form_deadline(value: str) -> int | None:
    s = (value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _format_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M")


def _format_project(p: dict[str, Any]) -> dict[str, Any]:
    out = dict(p)
    if p.get("deadline_at"):
        out["deadline"] = datetime.fromtimestamp(p["deadline_at"], TZ).date().isoformat()
    out["created"] = datetime.fromtimestamp(p["created_at"], TZ).date().isoformat()
    out["updated"] = datetime.fromtimestamp(p["updated_at"], TZ).date().isoformat()
    return out


def _format_vendor(v: dict[str, Any]) -> dict[str, Any]:
    out = dict(v)
    if v.get("last_used_at"):
        out["last_used_date"] = datetime.fromtimestamp(v["last_used_at"], TZ).date().isoformat()
    else:
        out["last_used_date"] = None
    return out


def _format_run(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    if r.get("started_at"):
        out["started"] = datetime.fromtimestamp(r["started_at"], TZ).strftime("%Y-%m-%d %H:%M")
    if r.get("completed_at"):
        out["completed"] = datetime.fromtimestamp(r["completed_at"], TZ).strftime("%Y-%m-%d %H:%M")
    else:
        out["completed"] = None
    if r.get("date_window_start"):
        out["window_start_date"] = datetime.fromtimestamp(r["date_window_start"], TZ).date().isoformat()
    if r.get("date_window_end"):
        out["window_end_date"] = datetime.fromtimestamp(r["date_window_end"], TZ).date().isoformat()
    return out


def _format_run_item(i: dict[str, Any]) -> dict[str, Any]:
    out = dict(i)
    out["amount_eur"] = i["amount_cents"] / 100.0
    out["transaction_date_iso"] = datetime.fromtimestamp(i["transaction_date"], TZ).date().isoformat()
    return out


def _format_ts_date(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), TZ).date().isoformat()


def _latest_unknown_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM receipt_run_items "
        "WHERE run_id=(SELECT MAX(id) FROM receipt_runs) "
        "AND status='unknown_vendor'",
    ).fetchone()
    return int(row[0] if row else 0)


def _suggest_canonical_name(vendor_raw: str) -> str:
    """Heuristiek voor pre-fill in vendor-form: pak het 'echte' deel weg."""
    import re
    s = re.sub(r"^\d{3,6}\s*-\s*", "", vendor_raw.strip())
    s = re.sub(r"\s*\((?:cc|pin|bank|sepa)\)\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"(?i)(paypal\s*\*|ideal\s+|sepa[\s\-_]+)", "", s)
    s = re.sub(r"\s+(?:NL|LU|DE|FR|UK|US)\s+\d.*$", "", s)
    s = re.sub(r"\s+\d{4,}.*$", "", s)
    return " ".join(s.split()[:3]).strip()


def _json_encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        log.exception("could not read %s", path)
    return out


def _merge(egress: list[dict[str, Any]], payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match egress + payload records on (ts, task) — same call wrote both."""
    payload_by_key = {(p.get("ts"), p.get("task")): p for p in payloads}
    out: list[dict[str, Any]] = []
    for e in egress:
        key = (e.get("ts"), e.get("task"))
        p = payload_by_key.get(key, {})
        merged = {**e}
        # Payload-only fields we want surfaced in the list view:
        merged["backend"] = p.get("backend", "claude")
        # Detail-view fields:
        merged["system_redacted"] = p.get("system_redacted")
        merged["messages_redacted"] = p.get("messages_redacted")
        merged["response_text"] = p.get("response_text")
        merged["has_payload"] = key in payload_by_key
        out.append(merged)
    # Payload records without matching egress (rare — maybe gateway crashed
    # between the two writes). Still show them.
    seen_keys = {(e.get("ts"), e.get("task")) for e in egress}
    for p in payloads:
        if (p.get("ts"), p.get("task")) in seen_keys:
            continue
        out.append({
            **p,
            "event": "payload_only",
            "has_payload": True,
        })
    return out


# Patterns we'd consider PII leaking past the redactor. Same shape as
# privacy/preflight.py but more permissive (we want to surface anything
# suspicious for human review).
_LEAK_PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "url_with_token": re.compile(r"https?://[^\s'\"<>]+[?&](?:token|key|auth|sig)=[^\s'\"<>]+"),
    "phone": re.compile(r"(?<!\w)(?:\+\d{1,3}[\s\-]?)?(?:\(?0\d{1,3}\)?[\s\-]?)?\d(?:[\s\-]?\d){7,11}(?!\w)"),
}


def _scan_for_leaks(rec: dict[str, Any]) -> list[dict[str, str]]:
    """Run leak-patterns over redacted system + messages + response.
    Returns list of {field, category, sample}. Empty = clean."""
    hits: list[dict[str, str]] = []

    def _scan(field: str, text: str) -> None:
        if not text:
            return
        for cat, rx in _LEAK_PATTERNS.items():
            for m in rx.finditer(text):
                hits.append({"field": field, "category": cat, "sample": m.group(0)[:80]})

    _scan("system_redacted", rec.get("system_redacted", "") or "")
    msgs = rec.get("messages_redacted") or []
    for i, m in enumerate(msgs if isinstance(msgs, list) else []):
        c = m.get("content") if isinstance(m, dict) else ""
        if isinstance(c, str):
            _scan(f"messages[{i}]", c)
        elif isinstance(c, list):
            for j, b in enumerate(c):
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        _scan(f"messages[{i}].blocks[{j}].text", str(b.get("text", "")))
                    elif b.get("type") == "tool_result":
                        _scan(f"messages[{i}].blocks[{j}].tool_result",
                              str(b.get("content", "")))
    _scan("response_text", rec.get("response_text", "") or "")
    return hits


def _available_dates(audit_dir: Path) -> list[str]:
    dates: set[str] = set()
    if not audit_dir.exists():
        return []
    for p in audit_dir.iterdir():
        m = re.match(r"^(?:egress|payloads)-(\d{4}-\d{2}-\d{2})\.jsonl$", p.name)
        if m:
            dates.add(m.group(1))
    return sorted(dates, reverse=True)


def _register_wishes_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """CRUD voor config_wishes — the user's structurele preferences."""
    from datetime import datetime as _dt

    from extensions.config_wishes.schema import (
        VALID_STATUS as _WISH_VALID_STATUS,
    )
    from extensions.config_wishes.schema import (
        list_wishes,
        update_wish_status,
    )

    def _fmt(w: dict) -> dict:
        out = dict(w)
        if w.get("created_at"):
            out["created"] = _dt.fromtimestamp(w["created_at"]).strftime("%Y-%m-%d %H:%M")
        if w.get("resolved_at"):
            out["resolved"] = _dt.fromtimestamp(w["resolved_at"]).strftime("%Y-%m-%d %H:%M")
        return out

    @app.get("/wishes", response_class=HTMLResponse)
    def wishes_index(
        request: Request,
        status: str = Query(default=""),
        message: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            if status:
                rows = list_wishes(conn, status=status, limit=200)
            else:
                # Default-view: open + wip eerst, daarna done/dismissed achteraan
                rows = list_wishes(conn, status="open", limit=200)
                rows += list_wishes(conn, status="wip", limit=200)
                rows += list_wishes(conn, status="done", limit=50)
                rows += list_wishes(conn, status="dismissed", limit=50)
        return templates.TemplateResponse(request, "wishes_list.html", {
            "wishes": [_fmt(w) for w in rows],
            "status": status,
            "message": message,
            "valid_statuses": list(_WISH_VALID_STATUS),
        })

    @app.post("/wishes/{wish_id}/status")
    def wishes_set_status(wish_id: int,
                            new_status: str = Form(...)) -> RedirectResponse:
        if new_status not in _WISH_VALID_STATUS:
            return RedirectResponse("/wishes?message=invalid+status", status_code=303)
        try:
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                update_wish_status(conn, wish_id, new_status)
        except ValueError as e:
            return RedirectResponse(f"/wishes?message={e}", status_code=303)
        return RedirectResponse(
            f"/wishes?message=wish+{wish_id}+→+{new_status}", status_code=303,
        )


def _register_loops_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """Read-only dashboard voor open_loops + status-toggle (close/snooze)."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from extensions.open_loops.schema import (
        close_loop,
        list_open,
        snooze_loop,
    )

    def _fmt(r: dict) -> dict:
        out = dict(r)
        if r.get("created_at"):
            out["created"] = _dt.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
            age_seconds = int(_dt.now().timestamp()) - r["created_at"]
            out["age_days"] = age_seconds // 86400
        if r.get("due_at"):
            out["due"] = _dt.fromtimestamp(r["due_at"]).strftime("%Y-%m-%d %H:%M")
        return out

    @app.get("/loops", response_class=HTMLResponse)
    def loops_index(
        request: Request,
        kind: str = Query(default=""),
        who: str = Query(default=""),
        message: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            rows = list_open(
                conn,
                kind=(kind or None),
                who=(who or None),
                limit=200,
            )
        # Group by kind voor visuele scheiding in template
        by_kind: dict[str, list] = {
            "incoming_question": [], "incoming_task": [],
            "outgoing_request": [], "meeting_action_self": [],
            "meeting_action_other": [], "_other": [],
        }
        for r in rows:
            bucket = r["kind"] if r["kind"] in by_kind else "_other"
            by_kind[bucket].append(_fmt(r))
        return templates.TemplateResponse(request, "loops_list.html", {
            "by_kind": by_kind,
            "total": len(rows),
            "kind": kind, "who": who,
            "message": message,
        })

    @app.post("/loops/{loop_id}/close")
    def loops_close(loop_id: int) -> RedirectResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            close_loop(conn, loop_id, via="manual")
        return RedirectResponse(
            f"/loops?message=loop+{loop_id}+closed", status_code=303,
        )

    @app.post("/loops/{loop_id}/snooze")
    def loops_snooze(loop_id: int,
                       days: int = Form(default=7)) -> RedirectResponse:
        until = int((_dt.now() + _td(days=max(1, days))).timestamp())
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            snooze_loop(conn, loop_id, until_unix=until)
        return RedirectResponse(
            f"/loops?message=loop+{loop_id}+snoozed+{days}d", status_code=303,
        )


def _register_ceo_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """CEO-dashboard: single-page synthese over alle signals."""
    from web.ceo_aggregator import build_ceo_snapshot

    @app.get("/ceo", response_class=HTMLResponse)
    def ceo(request: Request) -> HTMLResponse:
        # Locate okrs.yaml relative to db_path's parent/parent/config
        okrs_path = db_path.parent.parent / "config" / "okrs.yaml"
        snapshot = build_ceo_snapshot(
            db_path, okrs_path=okrs_path if okrs_path.exists() else None,
        )
        return templates.TemplateResponse(request, "ceo_dashboard.html", {
            "snap": snapshot,
        })


def _register_vip_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """VIP-relationship-monitor dashboard /vip + suggester /vip/suggest."""
    from web.vip_aggregator import build_vip_snapshot
    from web.vip_suggester import (
        append_to_yaml,
        load_existing_vips,
        suggest_vips,
    )

    def _vip_path() -> Path:
        return db_path.parent.parent / "config" / "vip_contacts.yaml"

    @app.get("/vip", response_class=HTMLResponse)
    def vip(request: Request, message: str = Query(default="")) -> HTMLResponse:
        snapshot = build_vip_snapshot(db_path, _vip_path())
        return templates.TemplateResponse(request, "vip_list.html", {
            "snap": snapshot, "message": message,
        })

    @app.get("/vip/suggest", response_class=HTMLResponse)
    def vip_suggest(request: Request) -> HTMLResponse:
        from core.config import load_settings
        settings = load_settings()
        emails, domains = load_existing_vips(_vip_path())
        sug = suggest_vips(
            db_path,
            own_domains=settings.own_email_domains,
            existing_emails=emails, existing_domains=domains,
        )
        return templates.TemplateResponse(request, "vip_suggest.html", {
            "persons": sug["persons"],
            "orgs": sug["orgs"],
        })

    @app.post("/vip/suggest")
    async def vip_suggest_apply(request: Request) -> RedirectResponse:
        form = await request.form()
        # Velden komen binnen als 'person_<email>=on' en 'org_<domain>=on'
        # Plus per-row 'tier_person_<email>' en 'name_person_<email>'
        # Plus 'tier_org_<domain>' en 'name_org_<domain>'.
        new_persons: list[dict] = []
        new_orgs: list[dict] = []
        for key, value in form.multi_items():
            if key.startswith("person_") and value == "on":
                email = key[len("person_"):]
                tier = (form.get(f"tier_person_{email}") or "B").upper()
                name = (form.get(f"name_person_{email}") or email.split("@")[0]).strip()
                new_persons.append({"email": email, "tier": tier, "name": name})
            elif key.startswith("org_") and value == "on":
                domain = key[len("org_"):]
                tier = (form.get(f"tier_org_{domain}") or "B").upper()
                name = (form.get(f"name_org_{domain}") or domain).strip()
                new_orgs.append({"domain": domain, "tier": tier, "name": name})
        added_p, added_o = append_to_yaml(
            _vip_path(),
            new_persons=new_persons, new_orgs=new_orgs,
        )
        msg = f"toegevoegd: {added_p} personen + {added_o} organisaties"
        return RedirectResponse(f"/vip?message={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Uptime dashboard
# ---------------------------------------------------------------------------

def _register_uptime_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """Read-only dashboard met state + events per target."""
    import time as _time

    from extensions.uptime.schema import list_targets_state, recent_events

    @app.get("/uptime", response_class=HTMLResponse)
    def uptime_index(request: Request) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            targets = list_targets_state(conn)
            events = recent_events(conn, limit=30)
        return templates.TemplateResponse(request, "uptime_list.html", {
            "targets": targets,
            "events": events,
            "now_ts": int(_time.time()),
            "fail_threshold_hint": "2",  # informatief; per-target staat in config
        })


# ---------------------------------------------------------------------------

def _register_sales_routes(
    app: FastAPI, templates: Jinja2Templates, db_path: Path,
) -> None:
    """Sales pipeline dashboard: lijst + filters + detail + actie-knoppen.

    Read-mostly. Mutaties (snooze / set_status / forget) gaan via kleine
    POST-endpoints die naar dezelfde lijst terug-redirecten.
    """
    import time as _time
    from datetime import datetime as _dt

    from extensions.sales.briefing import compute_sales_pulse
    from extensions.sales.schema import (
        VALID_PROSPECT_TYPES,
        VALID_STATUSES,
        VALID_TARGETS,
    )
    from extensions.sales.storage import (
        forget_account,
        get_account,
        insert_touchpoint,
        list_accounts,
        list_touchpoints,
        snooze_account,
        update_account,
    )

    TARGET_LABELS = {
        "adl_video":    ("ADL", "adl_video — eindgebruikers narrowcasting NL"),
        "dst_connect":  ("DST", "dst_connect — AV-resellers"),
        "ds_templates": ("DS",  "ds_templates — eindgebruikers + CMS-vendors"),
        "multi":        ("MULTI", "multi — meerdere targets"),
    }

    def _fmt_account(row: dict) -> dict:
        a = dict(row)
        a["target_label"] = TARGET_LABELS.get(a["target"], (a["target"], a["target"]))[0]
        a["target_title"] = TARGET_LABELS.get(a["target"], (a["target"], a["target"]))[1]
        now = int(_time.time())
        nt = a.get("next_touch_at")
        if nt:
            days = (int(nt) - now) // 86400
            a["next_touch_human"] = (
                "vandaag" if abs(days) < 1
                else f"in {days}d" if days > 0
                else f"{-days}d over tijd"
            )
            a["next_touch_overdue"] = days < 0
        else:
            a["next_touch_human"] = "—"
            a["next_touch_overdue"] = False
        lt = a.get("last_touch_at")
        a["last_touch_human"] = (
            _dt.fromtimestamp(int(lt)).strftime("%Y-%m-%d") if lt else "—"
        )
        return a

    @app.get("/sales", response_class=HTMLResponse)
    def sales_index(
        request: Request,
        target: str = Query(default=""),
        status: str = Query(default=""),
        sector: str = Query(default=""),
        message: str = Query(default=""),
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            rows = list_accounts(
                conn,
                target=(target or None),
                status=(status or None),
                limit=500,
            )
            # Counts per target/status voor de pipeline-summary
            conn.row_factory = sqlite3.Row
            pipeline_rows = conn.execute(
                "SELECT target, status, COUNT(*) AS n, "
                "       SUM(COALESCE(estimated_value_eur,0)) AS total_value "
                "FROM sales_accounts "
                "WHERE status NOT IN ('won','lost') "
                "GROUP BY target, status "
                "ORDER BY target, status"
            ).fetchall()
            sectors = [r[0] for r in conn.execute(
                "SELECT DISTINCT sector FROM sales_accounts "
                "WHERE sector IS NOT NULL ORDER BY sector"
            ).fetchall()]

        # Server-side sector filter (storage.list_accounts heeft 'm niet)
        if sector:
            rows = [r for r in rows if r.get("sector") == sector]

        formatted = [_fmt_account(r) for r in rows]
        # Sorteer: overdue eerst, dan kansrijk/offerte, dan rest
        order = {"offerte": 0, "kansrijk": 1, "nurturing": 2,
                  "koud": 3, "snoozed": 4, "won": 5, "lost": 6}
        formatted.sort(key=lambda a: (
            0 if a["next_touch_overdue"] else 1,
            order.get(a["status"], 99),
            a["next_touch_at"] or 0,
        ))

        pipeline = {}
        for r in pipeline_rows:
            t = r["target"]
            pipeline.setdefault(t, {})[r["status"]] = {
                "count": int(r["n"]),
                "value": int(r["total_value"] or 0),
            }

        return templates.TemplateResponse(request, "sales_list.html", {
            "accounts": formatted,
            "target": target, "status": status, "sector": sector,
            "all_targets": sorted(VALID_TARGETS),
            "all_statuses": sorted(VALID_STATUSES),
            "all_sectors": sectors,
            "pipeline": pipeline,
            "target_labels": TARGET_LABELS,
            "total_shown": len(formatted),
            "message": message,
        })

    @app.get("/sales/top3", response_class=HTMLResponse)
    def sales_top3(request: Request) -> HTMLResponse:
        # compute_sales_pulse is pure read — geen trigger-consumptie
        pulse, _ = compute_sales_pulse(db_path)
        return templates.TemplateResponse(request, "sales_top3.html", {
            "top_three": pulse.get("top_three", []),
            "pipeline_snapshot": pulse.get("pipeline_snapshot", {}),
            "target_labels": TARGET_LABELS,
        })

    @app.get("/sales/{account_id}", response_class=HTMLResponse)
    def sales_detail(
        request: Request, account_id: int,
    ) -> HTMLResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            acc = get_account(conn, account_id)
            touchpoints = list_touchpoints(conn, account_id, limit=50) if acc else []
        if acc is None:
            return templates.TemplateResponse(request, "error.html", {
                "message": f"Sales-account {account_id} bestaat niet",
            }, status_code=404)
        return templates.TemplateResponse(request, "sales_detail.html", {
            "acc": _fmt_account(acc),
            "touchpoints": touchpoints,
            "target_labels": TARGET_LABELS,
            "all_statuses": sorted(VALID_STATUSES),
            "all_prospect_types": sorted(VALID_PROSPECT_TYPES),
        })

    @app.post("/sales/{account_id}/snooze")
    def sales_snooze(
        account_id: int, days: int = Form(default=14),
    ) -> RedirectResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            snooze_account(conn, account_id, days=max(1, days))
        return RedirectResponse(
            f"/sales/{account_id}?message=snoozed+{days}d", status_code=303,
        )

    @app.post("/sales/{account_id}/set_status")
    def sales_set_status(
        account_id: int, status: str = Form(...),
    ) -> RedirectResponse:
        if status not in VALID_STATUSES:
            return RedirectResponse(
                f"/sales/{account_id}?message=invalid+status", status_code=303,
            )
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            update_account(conn, account_id, status=status)
        return RedirectResponse(
            f"/sales/{account_id}?message=status+={status}", status_code=303,
        )

    @app.post("/sales/{account_id}/log_touchpoint")
    def sales_log_touchpoint(
        account_id: int,
        channel: str = Form(...),
        summary: str = Form(default=""),
        outcome: str = Form(default=""),
    ) -> RedirectResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            insert_touchpoint(
                conn, account_id=account_id, channel=channel,
                summary=(summary.strip() or None),
                outcome=(outcome.strip() or None),
            )
        return RedirectResponse(
            f"/sales/{account_id}?message=touchpoint+logged", status_code=303,
        )

    @app.post("/sales/{account_id}/forget")
    def sales_forget(
        account_id: int, reason: str = Form(default=""),
    ) -> RedirectResponse:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            forgotten = forget_account(conn, account_id)
        if forgotten:
            try:
                from core.audit import log_admin_action
                log_admin_action(
                    action="sales_account_forget", actor="dashboard",
                    from_value={
                        "id": forgotten["id"], "naam": forgotten["naam"],
                        "kvk": forgotten.get("kvk"),
                        "contact_email": forgotten.get("primary_contact_email"),
                        "target": forgotten.get("target"),
                        "status": forgotten.get("status"),
                    },
                    reason=reason or "via /sales dashboard",
                )
            except Exception:
                pass
        return RedirectResponse(
            "/sales?message=account+verwijderd", status_code=303,
        )
