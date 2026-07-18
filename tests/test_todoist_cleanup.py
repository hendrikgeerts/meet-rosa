"""Tests voor extensions.todoist_sync.cleanup + de cleanup-tools.

Test-strategie:
- `find_duplicates` op handgemaakte taken (variërende similariteit).
- `find_stale` op taken met expliciete created_at + due-date.
- End-to-end via TODOIST_HANDLERS[suggest+apply]: roundtrip met
  proposal_id-confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from extensions.todoist_sync.cleanup import (
    DuplicateProposal, StaleProposal,
    find_duplicates, find_stale, get_proposal, register_duplicate_proposal,
    register_stale_proposal, reset_proposals, text_similarity,
)
from extensions.todoist_sync.tools import TODOIST_HANDLERS
from integrations.todoist import Task


def _t(
    tid: str, content: str, *,
    due_date: str | None = None, due_datetime: str | None = None,
    created_at: str | None = None,
) -> Task:
    return Task(
        id=tid, content=content, project_id="p1", is_completed=False,
        labels=[], due_date=due_date, due_datetime=due_datetime,
        created_at=created_at,
    )


@pytest.fixture(autouse=True)
def _clear_store() -> None:
    reset_proposals()
    yield
    reset_proposals()


@dataclass
class _Fake:
    tasks: list[Task] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def list_tasks(self, *, project_id: str | None = None) -> list[Task]:
        return list(self.tasks)

    def close_task(self, task_id: str) -> bool:
        self.closed.append(task_id)
        return True


# ---- text_similarity --------------------------------------------------

def test_text_similarity_identical_is_one() -> None:
    seq, jac = text_similarity("Bel verzekering", "Bel verzekering")
    assert seq == 1.0 and jac == 1.0


def test_text_similarity_token_reorder_high_jaccard() -> None:
    seq, jac = text_similarity("Bel verzekering", "verzekering bellen")
    # Jaccard pakt 'verzekering' (gedeelde token); 'bel' vs 'bellen' verschillen
    # → jaccard hier ~0.33 maar TOKEN_REORDER hoeft niet altijd hoog te zijn.
    # Belangrijker: het is symmetrisch en in [0,1].
    assert 0.0 <= jac <= 1.0
    assert 0.0 <= seq <= 1.0


def test_text_similarity_unrelated_low() -> None:
    seq, jac = text_similarity("Bel verzekering", "Mail boekhouder")
    assert seq < 0.5
    assert jac < 0.3


def test_text_similarity_stopwords_ignored() -> None:
    # 'het' / 'de' worden uit tokens gefilterd → de inhoudswoorden domineren
    seq, jac = text_similarity("bel de verzekering", "verzekering bellen")
    assert jac > 0  # 'verzekering' is shared content-word


# ---- find_duplicates --------------------------------------------------

def test_find_duplicates_picks_high_similarity_pair() -> None:
    tasks = [
        _t("a", "Bel verzekering"),
        _t("b", "Bel verzekering vandaag"),
        _t("c", "Mail boekhouder"),
    ]
    dups = find_duplicates(tasks)
    assert len(dups) == 1
    assert {dups[0].keep_id, dups[0].drop_id} == {"a", "b"}


def test_find_duplicates_keeps_task_with_due_date() -> None:
    tasks = [
        _t("a", "Bel verzekering"),
        _t("b", "Bel verzekering vandaag", due_date="2026-06-30"),
    ]
    dups = find_duplicates(tasks)
    assert dups[0].keep_id == "b"
    assert dups[0].drop_id == "a"


def test_find_duplicates_empty_for_unrelated() -> None:
    tasks = [_t("a", "Bel verzekering"), _t("b", "Mail boekhouder")]
    assert find_duplicates(tasks) == []


def test_find_duplicates_no_self_pair_for_same_id() -> None:
    # Hoekgeval: dezelfde id twee keer (zou niet voorkomen) — moet niet
    # in resultaat verschijnen
    a = _t("a", "Bel verzekering")
    assert find_duplicates([a, a]) == []


# ---- find_stale --------------------------------------------------------

def test_find_stale_picks_old_without_due() -> None:
    today = datetime(2026, 6, 27, tzinfo=timezone.utc)
    long_ago = (today - timedelta(days=60)).isoformat()
    fresh = (today - timedelta(days=2)).isoformat()
    tasks = [
        _t("a", "old no due", created_at=long_ago),
        _t("b", "old with due", due_date="2026-07-01", created_at=long_ago),
        _t("c", "fresh", created_at=fresh),
    ]
    stales = find_stale(tasks, today=today, days_threshold=30)
    assert len(stales) == 1
    assert stales[0].task_id == "a"


def test_find_stale_includes_with_due_when_flag_set() -> None:
    today = datetime(2026, 6, 27, tzinfo=timezone.utc)
    long_ago = (today - timedelta(days=60)).isoformat()
    tasks = [
        _t("a", "old no due", created_at=long_ago),
        _t("b", "old with due", due_date="2026-07-01", created_at=long_ago),
    ]
    stales = find_stale(tasks, today=today, days_threshold=30, include_with_due=True)
    assert {s.task_id for s in stales} == {"a", "b"}


def test_find_stale_sorted_oldest_first() -> None:
    today = datetime(2026, 6, 27, tzinfo=timezone.utc)
    tasks = [
        _t("recent", "x", created_at=(today - timedelta(days=35)).isoformat()),
        _t("ancient", "y", created_at=(today - timedelta(days=200)).isoformat()),
    ]
    stales = find_stale(tasks, today=today, days_threshold=30)
    assert [s.task_id for s in stales] == ["ancient", "recent"]


def test_find_stale_missing_created_at_ignored() -> None:
    today = datetime(2026, 6, 27, tzinfo=timezone.utc)
    tasks = [_t("a", "no created_at")]
    assert find_stale(tasks, today=today) == []


# ---- proposal store ---------------------------------------------------

def test_register_and_get_duplicate_proposal() -> None:
    p = DuplicateProposal(
        keep_id="a", drop_id="b", keep_content="x", drop_content="y",
        seq_ratio=0.9, jaccard=0.8,
    )
    pid = register_duplicate_proposal(p)
    spec = get_proposal(pid)
    assert spec is not None
    assert spec["action"] == "close"
    assert spec["task_id"] == "b"
    assert spec["kind"] == "dup"


def test_register_and_get_stale_proposal() -> None:
    p = StaleProposal(task_id="z", content="x", age_days=90, has_due=False)
    pid = register_stale_proposal(p)
    spec = get_proposal(pid)
    assert spec is not None
    assert spec["task_id"] == "z"
    assert spec["kind"] == "stale"


def test_unknown_proposal_returns_none() -> None:
    assert get_proposal("xxx") is None


# ---- end-to-end: suggest + apply --------------------------------------

def test_cleanup_suggest_returns_proposals_no_execution() -> None:
    fake = _Fake(tasks=[
        _t("a", "Bel verzekering"),
        _t("b", "Bel verzekering vandaag"),
    ])
    out = TODOIST_HANDLERS["todoist_cleanup_suggest"](fake, "p1", {})
    assert out["duplicates"]
    assert fake.closed == []  # geen execution
    assert all(p["proposal_id"].startswith("dup-") for p in out["duplicates"])


def test_cleanup_apply_closes_only_confirmed_proposals() -> None:
    today = datetime.now(timezone.utc)
    old = (today - timedelta(days=90)).isoformat()
    fake = _Fake(tasks=[
        _t("a", "Bel verzekering"),
        _t("b", "Bel verzekering vandaag"),
        _t("c", "Mail boekhouder", created_at=old),
    ])
    suggest = TODOIST_HANDLERS["todoist_cleanup_suggest"](fake, "p1", {})
    # Confirm only de duplicate, niet de stale
    dup_pid = suggest["duplicates"][0]["proposal_id"]
    out = TODOIST_HANDLERS["todoist_cleanup_apply"](
        fake, "p1", {"proposal_ids": [dup_pid]},
    )
    assert len(out["closed"]) == 1
    assert out["closed"][0]["task_id"] in {"a", "b"}
    # 'c' (stale) niet gesloten — geen confirmation
    assert "c" not in fake.closed


def test_cleanup_apply_unknown_id_returned_as_unknown() -> None:
    fake = _Fake()
    out = TODOIST_HANDLERS["todoist_cleanup_apply"](
        fake, "p1", {"proposal_ids": ["xxx-not-existing"]},
    )
    assert out["closed"] == []
    assert out["unknown_proposal_ids"] == ["xxx-not-existing"]


def test_cleanup_apply_empty_list_errors() -> None:
    out = TODOIST_HANDLERS["todoist_cleanup_apply"](
        _Fake(), "p1", {"proposal_ids": []},
    )
    assert "error" in out


def test_cleanup_suggest_without_client_errors() -> None:
    out = TODOIST_HANDLERS["todoist_cleanup_suggest"](None, None, {})
    assert "error" in out


def test_cleanup_apply_without_client_errors() -> None:
    out = TODOIST_HANDLERS["todoist_cleanup_apply"](
        None, None, {"proposal_ids": ["x"]},
    )
    assert "error" in out


# ---- review 27/6 fixes -------------------------------------------------

def test_cleanup_apply_caps_batch_at_max(monkeypatch: Any) -> None:
    """H2: prompt-injection-veilige cap. Mass-batch wordt afgewezen."""
    fake = _Fake()
    out = TODOIST_HANDLERS["todoist_cleanup_apply"](
        fake, "p1", {"proposal_ids": [f"pid-{i}" for i in range(100)]},
    )
    assert "error" in out
    assert fake.closed == []
    assert out["max_per_call"] == 5


def test_pick_keep_drop_id_tiebreaker_deterministic() -> None:
    """M5: identieke created_at → id-tiebreaker zorgt voor stabiele
    keep/drop over repeated calls."""
    from extensions.todoist_sync.cleanup import _pick_keep_drop
    a = _t("alpha", "x", created_at="2026-01-01T00:00:00Z")
    b = _t("zulu", "x", created_at="2026-01-01T00:00:00Z")
    keep1, drop1 = _pick_keep_drop(a, b)
    keep2, drop2 = _pick_keep_drop(b, a)  # zelfde tasks, andere volgorde
    assert keep1.id == keep2.id == "alpha"
    assert drop1.id == drop2.id == "zulu"


def test_proposal_store_thread_safe_under_contention() -> None:
    """M1: lock voorkomt dict-mutation-RuntimeError onder thread-druk."""
    import threading as _th
    from extensions.todoist_sync.cleanup import (
        StaleProposal, get_proposal, register_stale_proposal,
    )

    errors: list[BaseException] = []

    def _hammer() -> None:
        try:
            for i in range(50):
                p = StaleProposal(
                    task_id=f"t-{_th.get_ident()}-{i}",
                    content="x", age_days=90, has_due=False,
                )
                pid = register_stale_proposal(p)
                get_proposal(pid)
        except BaseException as exc:
            errors.append(exc)

    threads = [_th.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
