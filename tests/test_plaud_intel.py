"""Tests voor extensions.plaud_intel — JSON-parser, due-text helper,
end-to-end analyze_pending met fake Ollama."""
from __future__ import annotations

import sqlite3
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from extensions.open_loops.schema import init_open_loops_schema, list_open
from extensions.plaud_intel.analyze import (
    AnalyzeTranscriptError, _due_from_text, _parse_loose_json,
    analyze_pending, analyze_transcript,
)
from extensions.plaud_intel.schema import (
    MeetingAnalysis, find_unanalyzed_transcripts, init_plaud_meetings_schema,
)

TZ = ZoneInfo("Europe/Amsterdam")


# --- _parse_loose_json ----------------------------------------------------

def test_parse_full_object() -> None:
    raw = """
    {
      "summary": "Korte sync over offerte",
      "participants": ["Piet"],
      "decisions": ["We sturen vandaag offerte"],
      "actions_for_hendrik": [{"title": "Offerte aanpassen", "due_text": "morgen"}],
      "actions_for_others": [{"who": "Piet", "title": "Goedkeuring vragen", "due_text": "vrijdag"}],
      "open_questions": ["Past 3000 EUR?"]
    }"""
    a = _parse_loose_json(raw)
    assert a.summary == "Korte sync over offerte"
    assert a.participants == ["Piet"]
    assert len(a.actions_for_hendrik) == 1
    assert a.actions_for_others[0]["who"] == "Piet"


def test_parse_with_code_fence() -> None:
    raw = '```json\n{"summary":"x","participants":[],"decisions":[],"actions_for_hendrik":[],"actions_for_others":[],"open_questions":[]}\n```'
    a = _parse_loose_json(raw)
    assert a.summary == "x"


def test_parse_garbage_returns_safe_default() -> None:
    a = _parse_loose_json("nothing parseable here")
    assert "onleesbaar" in a.summary
    assert a.participants == []


def test_parse_truncates_lists() -> None:
    raw = ('{"summary":"x","decisions":["d1","d2","d3","d4","d5","d6","d7"],'
           '"open_questions":["q1","q2","q3","q4","q5"],'
           '"actions_for_hendrik":[],"actions_for_others":[]}')
    a = _parse_loose_json(raw)
    assert len(a.decisions) == 5
    assert len(a.open_questions) == 3


# --- _due_from_text -------------------------------------------------------

def test_due_morgen() -> None:
    base = int(datetime(2026, 4, 22, 10, 0, tzinfo=TZ).timestamp())
    out = _due_from_text("morgen", base)
    assert out == int(datetime(2026, 4, 23, 10, 0, tzinfo=TZ).timestamp())


def test_due_overmorgen() -> None:
    base = int(datetime(2026, 4, 22, 10, 0, tzinfo=TZ).timestamp())
    out = _due_from_text("overmorgen", base)
    assert out == int(datetime(2026, 4, 24, 10, 0, tzinfo=TZ).timestamp())


def test_due_volgende_week() -> None:
    base = int(datetime(2026, 4, 22, 10, 0, tzinfo=TZ).timestamp())  # wo
    out = _due_from_text("volgende week", base)
    assert out == int(datetime(2026, 4, 29, 10, 0, tzinfo=TZ).timestamp())


def test_due_weekday_skips_today() -> None:
    """Op een woensdag → 'woensdag' = volgende woensdag (over 7 dagen)."""
    base = int(datetime(2026, 4, 22, 10, 0, tzinfo=TZ).timestamp())  # wo
    out = _due_from_text("woensdag", base)
    assert out == int(datetime(2026, 4, 29, 10, 0, tzinfo=TZ).timestamp())


def test_due_weekday_in_future_same_week() -> None:
    """Op een woensdag → 'vrijdag' = vrijdag van DEZE week (over 2 dagen)."""
    base = int(datetime(2026, 4, 22, 10, 0, tzinfo=TZ).timestamp())  # wo
    out = _due_from_text("vrijdag", base)
    assert out == int(datetime(2026, 4, 24, 10, 0, tzinfo=TZ).timestamp())


def test_due_over_n_dagen() -> None:
    base = int(datetime(2026, 4, 22, 10, 0, tzinfo=TZ).timestamp())
    out = _due_from_text("over 5 dagen", base)
    assert out == int(datetime(2026, 4, 27, 10, 0, tzinfo=TZ).timestamp())


def test_due_null_or_unknown_returns_none() -> None:
    base = int(_time.time())
    assert _due_from_text(None, base) is None
    assert _due_from_text("null", base) is None
    assert _due_from_text("", base) is None
    assert _due_from_text("misschien ooit", base) is None


# --- analyze_transcript with fake Ollama ----------------------------------

@dataclass
class _Block:
    type: str = "text"
    text: str = ""


@dataclass
class _Resp:
    content: list[Any] = field(default_factory=list)


@dataclass
class _FakeOllama:
    response_text: str = ""
    last_call: dict[str, Any] | None = None
    def chat(self, **kwargs: Any) -> _Resp:
        self.last_call = kwargs
        return _Resp(content=[_Block(text=self.response_text)])


def test_analyze_returns_safe_default_on_empty_body() -> None:
    a = analyze_transcript("", _FakeOllama(response_text="{}"))
    assert "leeg" in a.summary


def test_analyze_passes_body_to_ollama() -> None:
    fake = _FakeOllama(response_text='{"summary":"x","participants":[]}')
    analyze_transcript("Hier is een transcript over offerte", fake)
    user = fake.last_call["messages"][0]["content"]
    assert "transcript over offerte" in user


# --- analyze_pending end-to-end -------------------------------------------

@pytest.fixture
def db_with_transcript(tmp_path: Path) -> Path:
    p = tmp_path / "db.sqlite"
    init_open_loops_schema(p)
    init_plaud_meetings_schema(p)
    # Plaud transcripts table komt van integrations.plaud — dupliceer schema
    # hier zodat de test geen Plaud-import nodig heeft.
    with sqlite3.connect(p) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS plaud_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                title TEXT,
                body TEXT NOT NULL,
                recorded_at INTEGER,
                ingested_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            );
        """)
        c.execute(
            "INSERT INTO plaud_transcripts (source_path, content_hash, title, body, recorded_at) "
            "VALUES (?,?,?,?,?)",
            ("/tmp/m1.txt", "h1", "Sync met Piet",
             "Hendrik en Piet bespreken de offerte. Hendrik gaat de offerte morgen "
             "aanpassen. Piet stuurt vrijdag de goedkeuring.",
             int(_time.time())),
        )
    return p


def test_analyze_pending_creates_meeting_and_loops(db_with_transcript: Path) -> None:
    fake = _FakeOllama(response_text='''
    {
      "summary": "Sync over offerte",
      "participants": ["Piet"],
      "decisions": ["Offerte morgen aangepast"],
      "actions_for_hendrik": [{"title": "Offerte aanpassen", "due_text": "morgen"}],
      "actions_for_others": [{"who": "Piet", "title": "Goedkeuring sturen", "due_text": "vrijdag"}],
      "open_questions": []
    }''')
    n = analyze_pending(db_with_transcript, fake)
    assert n == 1

    with sqlite3.connect(db_with_transcript) as c:
        c.row_factory = sqlite3.Row
        meeting = c.execute("SELECT * FROM plaud_meetings").fetchone()
        assert meeting["summary"] == "Sync over offerte"
        assert meeting["actions_count"] == 2

        loops = list_open(c)
        kinds = {l["kind"] for l in loops}
        assert "meeting_action_self" in kinds
        assert "meeting_action_other" in kinds
        # both loops should have due_at set
        assert all(l["due_at"] for l in loops)


def test_analyze_pending_idempotent(db_with_transcript: Path) -> None:
    """Tweede tick met dezelfde transcripten → 0 nieuwe meetings, geen
    dubbele open loops."""
    fake = _FakeOllama(response_text='{"summary":"x","participants":[],"decisions":[],"actions_for_hendrik":[{"title":"doe X"}],"actions_for_others":[],"open_questions":[]}')
    n1 = analyze_pending(db_with_transcript, fake)
    n2 = analyze_pending(db_with_transcript, fake)
    assert n1 == 1
    assert n2 == 0
    with sqlite3.connect(db_with_transcript) as c:
        loops = list_open(c)
        assert len(loops) == 1


def test_analyze_transcript_raises_on_ollama_failure() -> None:
    """Ollama-faal moet als AnalyzeTranscriptError naar boven komen zodat
    analyze_pending GEEN fallback-meeting inserteert (die zou retry blokkeren)."""
    @dataclass
    class _FailingOllama:
        def chat(self, **kwargs: Any) -> _Resp:
            raise RuntimeError("connection refused")
    with pytest.raises(AnalyzeTranscriptError):
        analyze_transcript("Niet-lege transcript.", _FailingOllama())


def test_analyze_pending_leaves_transcript_pending_on_failure(db_with_transcript: Path) -> None:
    """Als de Ollama-call faalt: geen meeting-rij, transcript blijft in
    unanalyzed-queue; volgende tick probeert opnieuw."""
    @dataclass
    class _FailingOllama:
        def chat(self, **kwargs: Any) -> _Resp:
            raise RuntimeError("ollama busy")
    n = analyze_pending(db_with_transcript, _FailingOllama())
    assert n == 0
    with sqlite3.connect(db_with_transcript) as c:
        meetings = c.execute("SELECT COUNT(*) FROM plaud_meetings").fetchone()[0]
        assert meetings == 0

    # Tweede tick met werkende Ollama → transcript wordt alsnog opgepakt
    ok = _FakeOllama(response_text='{"summary":"x","participants":[],"decisions":[],"actions_for_hendrik":[{"title":"doe Y"}],"actions_for_others":[],"open_questions":[]}')
    n2 = analyze_pending(db_with_transcript, ok)
    assert n2 == 1


def test_analyze_pending_skips_empty_body(tmp_path: Path) -> None:
    p = tmp_path / "db.sqlite"
    init_open_loops_schema(p)
    init_plaud_meetings_schema(p)
    with sqlite3.connect(p) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS plaud_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                title TEXT, body TEXT NOT NULL, recorded_at INTEGER,
                ingested_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            );""")
        c.execute("INSERT INTO plaud_transcripts (source_path, content_hash, title, body) VALUES (?,?,?,?)",
                  ("/tmp/x.txt", "h", "x", "   "))
    fake = _FakeOllama(response_text='{}')
    assert analyze_pending(p, fake) == 0
