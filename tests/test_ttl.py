"""Tests for TTL-based topic loading/unloading."""

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
    tick_ttl(db, [])  # decrements to 4
    assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1
    mark_loaded(db, "topic-a")
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


def test_mark_loaded_respects_custom_ttl(db):
    mark_loaded(db, "topic-a", max_ttl=3)
    assert ttl_of(db, "topic-a") == 3


# ── tick_ttl: basic decrement ────────────────────────────────

def test_tick_decrements_unreferenced(db):
    mark_loaded(db, "topic-a")
    tick_ttl(db, [])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1


def test_tick_resets_referenced(db):
    mark_loaded(db, "topic-a")
    tick_ttl(db, [])
    tick_ttl(db, [])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL - 2
    tick_ttl(db, ["topic-a"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


def test_tick_loads_new_referenced_topic(db):
    # A topic referenced for the first time should be loaded at max TTL
    assert ttl_of(db, "topic-a") is None
    tick_ttl(db, ["topic-a"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


# ── tick_ttl: unload at zero ─────────────────────────────────

def test_tick_unloads_at_zero(db):
    mark_loaded(db, "topic-a", max_ttl=2)
    tick_ttl(db, [])  # 1
    assert ttl_of(db, "topic-a") == 1
    tick_ttl(db, [])  # 0 → unloaded
    assert ttl_of(db, "topic-a") is None


def test_full_decay_cycle(db):
    """Load a topic, let it decay to zero over DEFAULT_TTL turns."""
    mark_loaded(db, "topic-a")
    for i in range(DEFAULT_TTL - 1):
        tick_ttl(db, [])
        assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1 - i
    tick_ttl(db, [])  # final tick → unloaded
    assert ttl_of(db, "topic-a") is None
    assert get_loaded_topic_ids(db) == []


def test_reference_prevents_decay(db):
    """Repeatedly referenced topic stays loaded indefinitely."""
    mark_loaded(db, "topic-a")
    for _ in range(50):
        tick_ttl(db, ["topic-a"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


# ── tick_ttl: mixed behavior ─────────────────────────────────

def test_mixed_referenced_and_decaying(db):
    """Referenced topic stays at max while unreferenced decrements."""
    mark_loaded(db, "topic-a")
    mark_loaded(db, "topic-b")
    tick_ttl(db, ["topic-a"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL
    assert ttl_of(db, "topic-b") == DEFAULT_TTL - 1


def test_topic_switch_scenario(db):
    """Talk about A for 3 turns, then switch to B — A decays, B persists."""
    # Turns 1-3: talking about A
    for _ in range(3):
        tick_ttl(db, ["topic-a"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL

    # Turns 4 through 4+DEFAULT_TTL: talking about B, A decays
    for i in range(DEFAULT_TTL):
        tick_ttl(db, ["topic-b"])
    assert ttl_of(db, "topic-b") == DEFAULT_TTL
    assert ttl_of(db, "topic-a") is None  # A aged out


def test_referenced_id_not_previously_loaded(db):
    """A topic referenced for the first time loads without prior mark_loaded."""
    tick_ttl(db, ["topic-a", "topic-b"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL
    assert ttl_of(db, "topic-b") == DEFAULT_TTL


# ── tick_ttl: edge cases ─────────────────────────────────────

def test_empty_loaded_set_with_empty_references(db):
    """No-op: empty loaded, empty references. Should not crash."""
    result = tick_ttl(db, [])
    assert result == []


def test_empty_loaded_set_with_new_reference(db):
    """Empty loaded + one reference → loads that one."""
    result = tick_ttl(db, ["topic-a"])
    assert result == ["topic-a"]


def test_none_referenced_ids(db):
    """None should be handled as empty list."""
    mark_loaded(db, "topic-a")
    tick_ttl(db, None)
    assert ttl_of(db, "topic-a") == DEFAULT_TTL - 1


def test_duplicate_ids_in_referenced(db):
    """Same id passed twice shouldn't break anything."""
    tick_ttl(db, ["topic-a", "topic-a"])
    assert ttl_of(db, "topic-a") == DEFAULT_TTL


def test_get_loaded_topic_ids_ordered_by_ttl(db):
    """Highest TTL first."""
    mark_loaded(db, "topic-old")
    tick_ttl(db, [])  # topic-old → 4
    tick_ttl(db, [])  # topic-old → 3
    mark_loaded(db, "topic-new")  # topic-new → 5
    ids = get_loaded_topic_ids(db)
    assert ids[0] == "topic-new"
    assert ids[1] == "topic-old"
