"""Tests for entity-scoped facts and active entity (hard-switch) behavior."""

import pytest

from pane.schema import (
    USER_ENTITY,
    clear_active_entities,
    create_db,
    get_active_entities,
    get_entity_facts,
    get_facts_for_entities,
    save_entity_fact,
    set_active_entities,
)


@pytest.fixture
def db():
    conn = create_db(":memory:")
    yield conn
    conn.close()


# ── save_entity_fact / get_entity_facts ──────────────────────

def test_save_and_get_single_fact(db):
    save_entity_fact(db, "user", "commute", "35 min")
    assert get_entity_facts(db, "user") == [("commute", "35 min")]


def test_entity_owns_many_facts(db):
    save_entity_fact(db, "cpp", "exceptions", "disallowed")
    save_entity_fact(db, "cpp", "style", "snake_case")
    save_entity_fact(db, "cpp", "standard", "c++20")
    facts = get_entity_facts(db, "cpp")
    assert len(facts) == 3
    assert ("exceptions", "disallowed") in facts
    assert ("style", "snake_case") in facts


def test_upsert_overwrites_value(db):
    save_entity_fact(db, "user", "commute", "35 min")
    save_entity_fact(db, "user", "commute", "45 min")  # commute got worse
    assert get_entity_facts(db, "user") == [("commute", "45 min")]


def test_facts_isolated_per_entity(db):
    """Same key across different entities should not collide."""
    save_entity_fact(db, "cpp", "status", "blocked-on-review")
    save_entity_fact(db, "java", "status", "archived")
    assert get_entity_facts(db, "cpp") == [("status", "blocked-on-review")]
    assert get_entity_facts(db, "java") == [("status", "archived")]


def test_empty_fields_rejected(db):
    save_entity_fact(db, "", "key", "value")
    save_entity_fact(db, "user", "", "value")
    save_entity_fact(db, "user", "key", "")
    assert get_entity_facts(db, "user") == []


def test_entity_name_normalized(db):
    save_entity_fact(db, "  CPP  ", "exceptions", "disallowed")
    assert get_entity_facts(db, "cpp") == [("exceptions", "disallowed")]


def test_get_facts_for_multiple_entities(db):
    save_entity_fact(db, "user", "commute", "35 min")
    save_entity_fact(db, "cpp", "exceptions", "disallowed")
    save_entity_fact(db, "java", "version", "17")

    facts = get_facts_for_entities(db, ["user", "cpp"])
    assert "user" in facts
    assert "cpp" in facts
    assert "java" not in facts


def test_get_facts_for_entities_empty(db):
    assert get_facts_for_entities(db, []) == {}
    assert get_facts_for_entities(db, None) == {}


def test_get_facts_missing_entity(db):
    """Asking for an entity with no facts returns it missing from the dict."""
    save_entity_fact(db, "user", "commute", "35 min")
    facts = get_facts_for_entities(db, ["user", "nonexistent"])
    assert list(facts.keys()) == ["user"]


# ── active_entities: set / get / clear ───────────────────────

def test_active_entities_empty_by_default(db):
    assert get_active_entities(db) == []


def test_set_active_entities_simple(db):
    set_active_entities(db, ["cpp", "postgres"])
    active = get_active_entities(db)
    assert set(active) == {"cpp", "postgres"}


def test_set_active_entities_replaces(db):
    """Setting a new set replaces the old one entirely (hard switch)."""
    set_active_entities(db, ["cpp", "postgres"])
    set_active_entities(db, ["java"])
    assert get_active_entities(db) == ["java"]


def test_set_active_entities_empty_is_noop(db):
    """Empty mentions = drift turn = keep previous set (sticky)."""
    set_active_entities(db, ["cpp"])
    set_active_entities(db, [])
    assert get_active_entities(db) == ["cpp"]
    set_active_entities(db, None)
    assert get_active_entities(db) == ["cpp"]


def test_set_active_entities_filters_user(db):
    """The user entity is never stored in active_entities — it's always loaded."""
    set_active_entities(db, ["user", "cpp"])
    active = get_active_entities(db)
    assert "user" not in active
    assert "cpp" in active


def test_set_active_entities_only_user_is_noop(db):
    """If user is the only thing mentioned, nothing changes (nothing to add)."""
    set_active_entities(db, ["cpp"])
    set_active_entities(db, ["user"])  # should NOT clear cpp
    assert get_active_entities(db) == ["cpp"]


def test_clear_active_entities(db):
    set_active_entities(db, ["cpp", "postgres"])
    clear_active_entities(db)
    assert get_active_entities(db) == []


def test_set_active_entities_normalizes(db):
    set_active_entities(db, ["  CPP  ", "Java"])
    active = get_active_entities(db)
    assert set(active) == {"cpp", "java"}


def test_set_active_entities_deduplicates(db):
    set_active_entities(db, ["cpp", "CPP", "  cpp"])
    active = get_active_entities(db)
    assert active == ["cpp"]


# ── hard-switch scenarios (Option B: sticky with replacement) ─

def test_domain_switch_scenario(db):
    """Real-world pattern: work in cpp, drift, switch to java, drift."""
    # Seed some facts
    save_entity_fact(db, USER_ENTITY, "name", "Waleed")
    save_entity_fact(db, "cpp", "exceptions", "disallowed")
    save_entity_fact(db, "java", "version", "17")

    # Turn 1: mention cpp
    set_active_entities(db, ["cpp"])
    facts = get_facts_for_entities(db, [USER_ENTITY] + get_active_entities(db))
    assert "cpp" in facts
    assert USER_ENTITY in facts
    assert "java" not in facts

    # Turn 2: drift (no entity mention) → cpp stays
    set_active_entities(db, [])
    facts = get_facts_for_entities(db, [USER_ENTITY] + get_active_entities(db))
    assert "cpp" in facts  # sticky!

    # Turn 3: mention java → cpp gone, java in
    set_active_entities(db, ["java"])
    facts = get_facts_for_entities(db, [USER_ENTITY] + get_active_entities(db))
    assert "cpp" not in facts
    assert "java" in facts
    assert USER_ENTITY in facts  # always loaded


def test_user_facts_always_loaded_without_active_set(db):
    """Even with empty active_entities, user facts must still load."""
    save_entity_fact(db, USER_ENTITY, "commute", "35 min")
    facts = get_facts_for_entities(db, [USER_ENTITY])
    assert facts[USER_ENTITY] == [("commute", "35 min")]
