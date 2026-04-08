"""Tests for TTL-based topic loading/unloading.

tick_ttl only decrements. Resets come from mark_loaded (called by on_stop
after grouping identifies the specific active topic).
"""

import pytest

from pane.schema import (
    DEFAULT_TTL,
    create_db,
    get_loaded_topic_ids,
    get_loaded_topics_with_ttl,
    mark_loaded,
    tick_ttl,
)


@pytest.fixture
def db():
    conn = create_db(":memory:")
    yield conn
    conn.close()


def ttl_of(db, topic_id):
    """Helper: return current TTL for a topic, or None if unloaded."""
    for tid, ttl in get_loaded_topics_with_ttl(db):
        if tid == topic_id:
            return ttl
    return None


# ── mark_loaded ──────────────────────────────────────────────

def test_mark_loaded_sets_max_ttl(db):
    mark_loaded(db, "topic-a")
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


def test_mark_loaded_resets_existing_ttl(db):
    mark_loaded(db, "topic-a")
    tick_ttl(db)  # decrements to 4
    assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1
    mark_loaded(db, "topic-a")
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


def test_mark_loaded_respects_custom_ttl(db):
    mark_loaded(db, "topic-a", max_ttl=3)
    assert ttl_of(db, "topic-a") == 3


# ── tick_ttl: decrement-only ─────────────────────────────────

def test_tick_decrements_all(db):
    mark_loaded(db, "topic-a")
    mark_loaded(db, "topic-b")
    tick_ttl(db)
    assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1
    assert ttl_of(db, "topic-b") == DEFAULT_TTL - 1


def test_tick_unloads_at_zero(db):
    mark_loaded(db, "topic-a", max_ttl=2)
    tick_ttl(db)  # 1
    assert ttl_of(db, "topic-a") == 1
    tick_ttl(db)  # 0 → unloaded
    assert ttl_of(db, "topic-a") is None


def test_full_decay_cycle(db):
    """Load a topic, let it decay to zero over DEFAULT_TTL turns."""
    mark_loaded(db, "topic-a")
    for i in range(DEFAULT_TTL - 1):
        tick_ttl(db)
        assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1 - i
    tick_ttl(db)  # final tick → unloaded
    assert ttl_of(db, "topic-a") is None
    assert get_loaded_topic_ids(db) == []


# ── mark_loaded prevents decay ───────────────────────────────

def test_mark_loaded_each_turn_prevents_decay(db):
    """Simulates on_stop resetting the active topic each turn."""
    mark_loaded(db, "topic-a")
    for _ in range(50):
        tick_ttl(db)
        mark_loaded(db, "topic-a")  # on_stop resets active topic
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


def test_only_active_topic_survives(db):
    """One topic gets mark_loaded each turn, the other decays."""
    mark_loaded(db, "topic-a")
    mark_loaded(db, "topic-b")
    for _ in range(DEFAULT_TTL):
        tick_ttl(db)
        mark_loaded(db, "topic-a")  # only A is active
    assert ttl_of(db, "topic-a") == DEFAULT_TTL
    assert ttl_of(db, "topic-b") is None  # B decayed


# ── topic switch scenario ────────────────────────────────────

def test_topic_switch_scenario(db):
    """Talk about A, then switch to B — A decays, B persists."""
    mark_loaded(db, "topic-a")
    # Turns 1-3: working on A
    for _ in range(3):
        tick_ttl(db)
        mark_loaded(db, "topic-a")
    assert ttl_of(db, "topic-a") == DEFAULT_TTL

    # Switch to B
    mark_loaded(db, "topic-b")
    # Turns 4 through 4+DEFAULT_TTL: working on B, A decays
    for _ in range(DEFAULT_TTL):
        tick_ttl(db)
        mark_loaded(db, "topic-b")
    assert ttl_of(db, "topic-b") == DEFAULT_TTL
    assert ttl_of(db, "topic-a") is None  # A aged out


# ── edge cases ───────────────────────────────────────────────

def test_empty_loaded_set(db):
    """No-op: empty loaded, just decrement nothing."""
    result = tick_ttl(db)
    assert result == []


def test_none_handling(db):
    """tick_ttl with empty DB should not crash."""
    tick_ttl(db)
    assert get_loaded_topic_ids(db) == []


def test_get_loaded_topic_ids_ordered_by_ttl(db):
    """Highest TTL first."""
    mark_loaded(db, "topic-old")
    tick_ttl(db)  # topic-old → 4
    tick_ttl(db)  # topic-old → 3
    mark_loaded(db, "topic-new")  # topic-new → 5
    ids = get_loaded_topic_ids(db)
    assert ids[0] == "topic-new"
    assert ids[1] == "topic-old"


def test_drift_causes_gradual_decay(db):
    """If nothing calls mark_loaded (drift turns), topic decays."""
    mark_loaded(db, "topic-a")
    # Simulate 5 drift turns — no mark_loaded
    for _ in range(DEFAULT_TTL):
        tick_ttl(db)
    assert ttl_of(db, "topic-a") is None
