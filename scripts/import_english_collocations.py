"""Import bold collocations from the Cambridge `English Collocations in Use
Advanced` PDF into the `english_cards` table.

Heuristics — tuned against pages 10-14:
- Bold detection: font name contains "Bold"/"Black"/"Heavy" OR flags & 16.
- Size filter: keep spans with size 8-11; drop section headers (>=12) and
  unit titles (>=18) — those big bold spans become `unit_title` context
  for the cards that follow them on the page.
- Merge adjacent bold spans across line wraps (e.g. "adjourned" + "the
  meeting" → "adjourn the meeting") — any non-bold *word* span flushes
  the buffer; pure-punctuation spans do not.
- Drop instructional sentences and exercise headers via word-count and
  starting-verb filters (max 6 words, no trailing sentence-punctuation).

Run:
    python scripts/import_english_collocations.py \
        ~/Downloads/'English Collocations in Use Advanced (1).pdf'

Idempotent — duplicates (UNIQUE on collocation) are skipped.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

import fitz  # type: ignore[import-untyped]

# Make the in-repo sources importable when invoked from the scripts/ dir.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from core.config import load_settings  # noqa: E402
from extensions.english_practice.schema import (  # noqa: E402
    init_english_practice_schema,
    insert_card,
)

# --- heuristics ---------------------------------------------------------

# Span size ranges (in PDF points).
COLLOCATION_SIZE_RANGE = (8.0, 11.0)
HEADER_SIZE_MIN = 12.0            # "Strong collocations" → unit subheader
UNIT_TITLE_SIZE_MIN = 18.0        # "Strong, fixed and weak collocations"

# Words that mark exercise instructions (start of a bold instructional span).
INSTRUCTION_STARTERS = {
    "match", "correct", "improve", "write", "underline", "look",
    "choose", "replace", "translate", "complete", "fill", "find",
    "rewrite", "answer", "explain", "discuss", "decide", "give",
    "make", "use", "read", "tick", "circle", "rearrange", "sort",
}

MAX_WORDS = 6                     # collocations are short
MIN_WORDS = 2                     # singles are book-emphasis, not collocations
PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
SENTENCE_END_RE = re.compile(r"[.!?:]$")
WHITESPACE_RE = re.compile(r"\s+")

# Front-matter / back-matter unit titles to skip — you wants real
# collocations, not acknowledgements or the index.
META_UNIT_TITLES = {
    "acknowledgements", "using this book", "index", "references",
    "key", "menu", "contents", "introduction",
}


def is_bold(span: dict) -> bool:
    font = (span.get("font") or "").lower()
    flags = int(span.get("flags") or 0)
    if any(k in font for k in ("bold", "black", "heavy")):
        return True
    return bool(flags & 16)


def is_collocation_size(span: dict) -> bool:
    sz = float(span.get("size") or 0.0)
    lo, hi = COLLOCATION_SIZE_RANGE
    return lo <= sz <= hi


def looks_like_instruction(text: str) -> bool:
    """Filter exercise instructions and section names misclassified as
    collocations (e.g. 'Match the two parts of these collocations.')."""
    if SENTENCE_END_RE.search(text):
        return True
    first = text.split(" ", 1)[0].lower().rstrip(",;:")
    if first in INSTRUCTION_STARTERS:
        return True
    return False


LIGATURE_MAP = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}


_LIGATURE_SPLIT_RE = re.compile(
    r"(ﬀ|ﬃ|ﬄ|ﬁ|ﬂ)\s+([a-z]{1,4})\b",
)


def normalize(text: str) -> str:
    """Expand ligatures, fix mid-word ligature splits, collapse whitespace,
    strip outer punctuation."""
    # First, re-join words where the PDF split a ligature from its tail:
    # 'proﬁ ts' → 'profits', 'ﬁ erce' → 'fierce'. Done before expansion so
    # the unique ligature characters can serve as anchors.
    t = _LIGATURE_SPLIT_RE.sub(
        lambda m: LIGATURE_MAP[m.group(1)] + m.group(2), text,
    )
    for src, dst in LIGATURE_MAP.items():
        t = t.replace(src, dst)
    t = WHITESPACE_RE.sub(" ", t).strip()
    t = t.strip(" \t\n\r.,;:!?()[]\"'`—–-")
    return t


def is_real_collocation(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    n_words = len(t.split())
    if n_words < MIN_WORDS or n_words > MAX_WORDS:
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    if looks_like_instruction(t):
        return False
    return True


# --- PDF walker ---------------------------------------------------------

def extract_from_pdf(
    pdf_path: Path,
) -> list[dict]:
    """Walk the PDF and return [{collocation, context, page_no, unit_title}, ...]."""
    out: list[dict] = []
    seen_on_page: set[tuple[int, str]] = set()
    current_unit_title: str | None = None

    with fitz.open(pdf_path) as doc:
        for page_idx, page in enumerate(doc, start=1):
            d = page.get_text("dict")

            # First pass: find the biggest bold span on the page → unit title
            # (only update if we find one bigger than UNIT_TITLE_SIZE_MIN).
            page_unit_title = current_unit_title
            for block in d.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if not is_bold(span):
                            continue
                        sz = float(span.get("size") or 0.0)
                        text = (span.get("text") or "").strip()
                        if sz >= UNIT_TITLE_SIZE_MIN and text and not text.isdigit():
                            page_unit_title = text
                            break
            if page_unit_title and page_unit_title != current_unit_title:
                current_unit_title = page_unit_title

            # Second pass: collect collocations, with per-line context.
            for block in d.get("blocks", []):
                if block.get("type") != 0:
                    continue
                buffer: list[str] = []
                buffer_line_text: str | None = None

                def flush(line_text: str | None) -> None:
                    """Emit current buffer as a collocation candidate."""
                    if not buffer:
                        return
                    raw = " ".join(buffer)
                    norm = normalize(raw)
                    if is_real_collocation(norm):
                        ut = (current_unit_title or "").strip().lower()
                        if ut not in META_UNIT_TITLES:
                            key = (page_idx, norm.lower())
                            if key not in seen_on_page:
                                seen_on_page.add(key)
                                out.append({
                                    "collocation": norm,
                                    "context": (line_text or "").strip() or None,
                                    "page_no": page_idx,
                                    "unit_title": current_unit_title,
                                })
                    buffer.clear()

                for line in block.get("lines", []):
                    line_text = " ".join(
                        (s.get("text") or "") for s in line.get("spans", [])
                    ).strip()
                    for span in line.get("spans", []):
                        text = (span.get("text") or "").strip()
                        if not text:
                            continue
                        if is_bold(span) and is_collocation_size(span):
                            if PUNCT_ONLY_RE.match(text):
                                # bold punctuation — keep buffer alive,
                                # don't append
                                continue
                            buffer.append(text)
                            buffer_line_text = line_text
                        else:
                            # any non-bold span (or oversized bold header)
                            # flushes the buffer
                            flush(buffer_line_text)
                            buffer_line_text = None
                # end-of-block flush
                flush(buffer_line_text)
    return out


# --- DB import ----------------------------------------------------------

def import_into_db(db_path: Path, cards: list[dict]) -> tuple[int, int]:
    """Returns (inserted, skipped_dupes)."""
    init_english_practice_schema(db_path)
    inserted = 0
    dupes = 0
    with sqlite3.connect(db_path) as conn:
        for c in cards:
            rid = insert_card(
                conn,
                collocation=c["collocation"],
                context=c["context"],
                page_no=c["page_no"],
                unit_title=c["unit_title"],
            )
            if rid is None:
                dupes += 1
            else:
                inserted += 1
        conn.commit()
    return inserted, dupes


# --- CLI ----------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "pdf", type=Path,
        help="Path to the English Collocations PDF",
    )
    ap.add_argument(
        "--db", type=Path, default=None,
        help="Override DB path (default: from settings.yaml)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print extracted collocations without writing to DB",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Only show N collocations in dry-run output",
    )
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    cards = extract_from_pdf(args.pdf)
    print(f"Extracted {len(cards)} candidate collocations from {args.pdf.name}")

    if args.dry_run:
        for c in cards[: args.limit]:
            ut = c["unit_title"] or "-"
            ctx = c["context"] or ""
            print(f"  p{c['page_no']:>3} [{ut[:32]:32s}]  {c['collocation']}")
            if ctx:
                print(f"        ctx: {ctx[:120]}")
        return

    db_path = args.db
    if db_path is None:
        settings = load_settings()
        db_path = settings.db_path
    inserted, dupes = import_into_db(db_path, cards)
    print(f"Inserted: {inserted}   duplicates skipped: {dupes}")
    print(f"DB: {db_path}")


if __name__ == "__main__":
    main()
