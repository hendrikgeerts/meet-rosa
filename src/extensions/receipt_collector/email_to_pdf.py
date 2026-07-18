"""Email-body → evidence-PDF render.

Sommige vendors (Datadog via Netsuite, Userback, Fira Barcelona, noo.ma,
Clicks via Stripe) sturen wel een factuur-mail maar ZONDER PDF-bijlage —
de invoice-info staat in de mail-body. Voor de receipt-collector
genereren we dan een evidence-PDF die als attachment wordt opgeslagen,
zodat de accountant het in dezelfde flow ontvangt als reguliere PDFs.

`looks_like_invoice` is de gate: zonder factuur-keywords renderen we
niet (geen ruis voor newsletters / order-confirmations zonder bedrag).
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

log = logging.getLogger(__name__)

_INVOICE_KEYWORDS = (
    "invoice", "factuur", "receipt", "amount due", "total due",
    "subtotal", "amount paid", "payment receipt", "billing",
    "you paid", "subscription receipt", "credit memo", "creditnota",
)


def looks_like_invoice(body: str) -> bool:
    if not body:
        return False
    tl = body.lower()
    return any(k in tl for k in _INVOICE_KEYWORDS)


def render_email_as_pdf(
    *,
    headers: dict[str, str],
    body_html: str | None = None,
    body_text: str | None = None,
) -> bytes | None:
    """Render een minimale evidence-PDF (header + body). Returns None bij
    fout. Body wordt als platte tekst weergegeven; HTML wordt gestript."""
    body = body_text or ""
    if not body and body_html:
        body = _strip_html(body_html)
    if not body.strip():
        return None
    try:
        from fpdf import FPDF
    except ImportError:
        log.warning("fpdf2 niet geïnstalleerd — sla email-PDF render over")
        return None

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Title
    pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(pdf.epw, 8, _safe("Email-receipt (gerenderde evidence)"),
              new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    # Header block — single multi_cell per regel ("From: ..."), simpel
    # en robuust tegen fpdf2 horizontal-space issues.
    pdf.set_font("Helvetica", size=10)
    for label in ("From", "To", "Date", "Subject"):
        val = headers.get(label) or headers.get(label.lower()) or ""
        if not val:
            continue
        pdf.multi_cell(pdf.epw, 5, _safe(f"{label}: {val[:300]}"))
    pdf.ln(2)
    pdf.set_draw_color(180, 180, 180)
    y = pdf.get_y()
    pdf.line(10, y, 200, y)
    pdf.ln(3)

    # Body — capped to ~10k chars to avoid runaway PDFs
    pdf.set_font("Helvetica", size=9)
    pdf.multi_cell(pdf.epw, 4.5, _safe(body[:10000]))

    try:
        return bytes(pdf.output())
    except Exception:
        log.exception("fpdf2 render failed")
        return None


def _safe(s: str) -> str:
    """fpdf2 default Helvetica is latin-1 only. Strip non-encodable chars
    om UnicodeEncodeError te vermijden — voor evidence-PDF is verlies van
    een paar emoji's acceptabel."""
    return s.encode("latin-1", "replace").decode("latin-1")


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "head"):
            self.skip_depth += 1
        if tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")
        if tag == "td":
            self.parts.append("  ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)


def _strip_html(html: str) -> str:
    p = _HTMLToText()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", "", html)  # fallback regex strip
    text = "".join(p.parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
