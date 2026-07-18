"""Tests voor core.timezone — active_timezone via app_state met
caching, en de bind/current_tz helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from core import app_state
from core import timezone as tz_mod


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "app.db"
    app_state.init_app_state_schema(p)
    return p


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path: Path):
    """Elke test krijgt een schone bind-state + cache."""
    tz_mod.invalidate_cache()
    tz_mod._default_db_path = None  # type: ignore[attr-defined]
    tz_mod._default_tz_name = "Europe/Amsterdam"  # type: ignore[attr-defined]
    yield
    tz_mod.invalidate_cache()


# --- app_state -----------------------------------------------------

def test_app_state_get_returns_default_when_missing(db: Path) -> None:
    assert app_state.get(db, key="nope", default="fallback") == "fallback"


def test_app_state_set_and_get(db: Path) -> None:
    app_state.set_value(db, key="foo", value="bar")
    assert app_state.get(db, key="foo") == "bar"


def test_app_state_set_none_deletes(db: Path) -> None:
    app_state.set_value(db, key="x", value="123")
    app_state.set_value(db, key="x", value=None)
    assert app_state.get(db, key="x", default="GONE") == "GONE"


# --- active_tz / set_active_timezone ------------------------------

def test_active_tz_returns_default_when_unset(db: Path) -> None:
    tz = tz_mod.active_tz(db_path=db)
    assert str(tz) == "Europe/Amsterdam"


def test_set_then_get_via_active_tz(db: Path) -> None:
    tz_mod.set_active_timezone(db_path=db, name="America/Los_Angeles")
    assert str(tz_mod.active_tz(db_path=db)) == "America/Los_Angeles"


def test_set_invalid_raises(db: Path) -> None:
    with pytest.raises(ValueError):
        tz_mod.set_active_timezone(db_path=db, name="Mars/Olympus_Mons")


def test_reset_via_alias(db: Path) -> None:
    tz_mod.set_active_timezone(db_path=db, name="Asia/Tokyo")
    assert str(tz_mod.active_tz(db_path=db)) == "Asia/Tokyo"
    tz_mod.set_active_timezone(db_path=db, name="home")
    assert str(tz_mod.active_tz(db_path=db)) == "Europe/Amsterdam"


def test_reset_via_none(db: Path) -> None:
    tz_mod.set_active_timezone(db_path=db, name="Asia/Tokyo")
    tz_mod.set_active_timezone(db_path=db, name=None)
    assert str(tz_mod.active_tz(db_path=db)) == "Europe/Amsterdam"


def test_cache_invalidated_on_set(db: Path) -> None:
    """Cache mag geen stale waarde laten zien direct na een set."""
    _ = tz_mod.active_tz(db_path=db)  # warm cache met default
    tz_mod.set_active_timezone(db_path=db, name="America/Los_Angeles")
    assert str(tz_mod.active_tz(db_path=db)) == "America/Los_Angeles"


def test_invalid_db_value_falls_back_to_default(db: Path) -> None:
    """Als de DB een corrupted waarde bevat: log + default."""
    app_state.set_value(db, key="active_timezone", value="NotAZone/Foo")
    assert str(tz_mod.active_tz(db_path=db, default="Europe/Amsterdam")) == "Europe/Amsterdam"


# --- bind + current_tz / now_local --------------------------------

def test_bind_makes_current_tz_work_without_path(db: Path) -> None:
    tz_mod.bind(db, default_timezone="Europe/Amsterdam")
    assert str(tz_mod.current_tz()) == "Europe/Amsterdam"
    tz_mod.set_active_timezone(db_path=db, name="Asia/Tokyo")
    assert str(tz_mod.current_tz()) == "Asia/Tokyo"


def test_now_local_returns_aware_datetime(db: Path) -> None:
    tz_mod.bind(db, default_timezone="Europe/Amsterdam")
    tz_mod.set_active_timezone(db_path=db, name="America/Los_Angeles")
    now = tz_mod.now_local()
    assert now.tzinfo is not None
    assert str(now.tzinfo) == "America/Los_Angeles"


def test_current_tz_without_bind_returns_default() -> None:
    """Bij tests / scripts die geen bind hebben gedaan: graceful default."""
    # _reset_module_state heeft _default_db_path al op None gezet
    assert str(tz_mod.current_tz()) == "Europe/Amsterdam"


def test_default_tz_name(db: Path) -> None:
    tz_mod.bind(db, default_timezone="Europe/Brussels")
    assert tz_mod.default_tz_name() == "Europe/Brussels"


# --- post-review fixes -------------------------------------------------

def test_alias_pst_resolves_to_la(db: Path) -> None:
    """M2: common abbreviations als 'PST' werken zonder dat Claude
    de IANA-vorm hoeft te weten."""
    tz_mod.set_active_timezone(db_path=db, name="PST")
    assert str(tz_mod.active_tz(db_path=db)) == "America/Los_Angeles"


def test_alias_jst_resolves_to_tokyo(db: Path) -> None:
    tz_mod.set_active_timezone(db_path=db, name="JST")
    assert str(tz_mod.active_tz(db_path=db)) == "Asia/Tokyo"


def test_alias_ist_resolves_to_india_kolkata(db: Path) -> None:
    """M1-context: India is +5:30 — alias mag fractional hour TZ
    accepteren."""
    tz_mod.set_active_timezone(db_path=db, name="IST")
    assert str(tz_mod.active_tz(db_path=db)) == "Asia/Kolkata"


def test_alias_case_insensitive(db: Path) -> None:
    tz_mod.set_active_timezone(db_path=db, name="pst")
    assert str(tz_mod.active_tz(db_path=db)) == "America/Los_Angeles"


def test_bind_twice_warns_but_keeps_new(db: Path, tmp_path: Path) -> None:
    """H4: silent re-bind met andere db_path moet niet failen, wel een
    warning loggen."""
    import logging as _logging
    other = tmp_path / "other.db"
    app_state.init_app_state_schema(other)
    tz_mod.bind(db, default_timezone="Europe/Amsterdam")
    with caplog_at_level(_logging.WARNING, "core.timezone") as records:
        tz_mod.bind(other, default_timezone="Europe/Amsterdam")
    assert any("rebinding" in r.getMessage() for r in records)


# helper voor caplog (lichter dan pytest-caplog dependency)
class _CapHandler(__import__("logging").Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def caplog_at_level(level, logger_name):
    import contextlib
    @contextlib.contextmanager
    def _ctx():
        import logging
        logger = logging.getLogger(logger_name)
        handler = _CapHandler()
        handler.setLevel(level)
        logger.addHandler(handler)
        prev_level = logger.level
        logger.setLevel(level)
        try:
            yield handler.records
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
    return _ctx()
