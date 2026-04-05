"""Tests for recall — entity extraction, topic search, context formatting."""

import pytest

from pane.recall import (
    extract_entities,
    format_facts,
    load_context,
    recall,
)
from pane.schema import (
    USER_ENTITY,
    build_alias_map,
    create_db,
    create_window,
    save_entity,
    save_entity_fact,
    save_messages,
    save_topic,
)


@pytest.fixture
def db():
    conn = create_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def seeded_db(db):
    """DB with a small entity registry and a few topics."""
    save_entity(db, "cpp", "tool", ["cpp", "c++"])
    save_entity(db, "python", "tool", ["python"])
    save_entity(db, "sarah", "person", ["sarah", "sarah chen"])
    save_entity(db, "dr. chen", "person", ["dr. chen", "chen"])
    save_entity(db, USER_ENTITY, "self", ["user", "me"])

    window = create_window(db)
    msgs = save_messages(db, window, [{"role": "user", "content": "first"}])
    save_topic(db, window, "cpp work", msgs[0][0], msgs[0][0],
               summary="rewriting auth in cpp", tags=["entity:cpp", "cat:backend"])

    msgs = save_messages(db, window, [{"role": "user", "content": "second"}])
    save_topic(db, window, "python UI", msgs[0][0], msgs[0][0],
               summary="dashboard dark mode", tags=["entity:python", "cat:frontend"])

    msgs = save_messages(db, window, [{"role": "user", "content": "third"}])
    save_topic(db, window, "1:1 with sarah", msgs[0][0], msgs[0][0],
               summary="career chat", tags=["entity:sarah", "cat:work"])
    return db


# ── extract_entities ─────────────────────────────────────────

def test_extract_entities_basic(seeded_db):
    amap = build_alias_map(seeded_db)
    found = extract_entities("some cpp work", amap)
    assert "cpp" in found


def test_extract_entities_via_alias(seeded_db):
    amap = build_alias_map(seeded_db)
    found = extract_entities("testing c++ code", amap)
    assert "cpp" in found


def test_extract_entities_longest_alias_wins(seeded_db):
    """'dr. chen' should match as 'dr. chen', not just 'chen'."""
    amap = build_alias_map(seeded_db)
    found = extract_entities("met with dr. chen today", amap)
    assert "dr. chen" in found


def test_extract_entities_excludes_user(seeded_db):
    """The user entity is filtered out — it's always implicitly present."""
    amap = build_alias_map(seeded_db)
    found = extract_entities("what about me and user stuff", amap)
    assert USER_ENTITY not in found


def test_extract_entities_empty_query(seeded_db):
    amap = build_alias_map(seeded_db)
    assert extract_entities("", amap) == set()


def test_extract_entities_no_matches(seeded_db):
    amap = build_alias_map(seeded_db)
    found = extract_entities("random unrelated text", amap)
    assert found == set()


def test_extract_entities_case_insensitive(seeded_db):
    amap = build_alias_map(seeded_db)
    found = extract_entities("SARAH says hi", amap)
    assert "sarah" in found


def test_extract_entities_multiple(seeded_db):
    amap = build_alias_map(seeded_db)
    found = extract_entities("cpp and python question for sarah", amap)
    assert {"cpp", "python", "sarah"} <= found


def test_extract_entities_skips_single_char_aliases(seeded_db):
    """Single-char aliases are too noisy to match reliably."""
    save_entity(seeded_db, "x", "tool", ["x"])
    amap = build_alias_map(seeded_db)
    # 'x' would substring-match almost any text — must be excluded
    found = extract_entities("exploring stuff", amap)
    assert "x" not in found


# ── recall ───────────────────────────────────────────────────

def test_recall_returns_topic_mode_when_matched(seeded_db):
    r = recall("something cpp related", seeded_db)
    assert r.mode == "topic"
    assert "cpp" in r.entities
    assert len(r.topics) >= 1
    assert r.n_results == len(r.topics)


def test_recall_returns_entity_mode_when_no_topics(db):
    """Entity mentioned but no matching topic → entity mode, empty topics."""
    save_entity(db, "cpp", "tool", ["cpp"])
    r = recall("working on cpp", db)
    assert r.mode == "entity"
    assert r.entities == ["cpp"]
    assert r.topics == []


def test_recall_returns_not_found_when_empty(db):
    r = recall("random text", db)
    assert r.mode == "not_found"
    assert r.entities == []
    assert r.topics == []


def test_recall_entities_sorted(seeded_db):
    r = recall("python and cpp and sarah", seeded_db)
    assert r.entities == sorted(r.entities)


def test_recall_topics_ranked_by_match_count(db):
    """Topic matching more query tags ranks higher."""
    save_entity(db, "cpp", "tool", ["cpp"])
    save_entity(db, "python", "tool", ["python"])
    window = create_window(db)
    msgs = save_messages(db, window, [{"role": "user", "content": "x"}])
    # Topic A has both tags, Topic B has only one
    save_topic(db, window, "Both", msgs[0][0], msgs[0][0],
               tags=["entity:cpp", "entity:python"])
    msgs = save_messages(db, window, [{"role": "user", "content": "y"}])
    save_topic(db, window, "One", msgs[0][0], msgs[0][0],
               tags=["entity:cpp"])

    r = recall("cpp python", db)
    assert r.topics[0][0]["title"] == "Both"
    assert r.topics[0][1] >= r.topics[1][1]  # scores sorted desc


# ── load_context ─────────────────────────────────────────────

def test_load_context_uses_summary_by_default(seeded_db):
    from pane.schema import get_all_topics
    topic_ids = [t["id"] for t in get_all_topics(seeded_db)]
    ctx = load_context(topic_ids, seeded_db)
    assert "rewriting auth in cpp" in ctx
    assert "dashboard dark mode" in ctx


def test_load_context_raw_messages_when_summary_disabled(seeded_db):
    from pane.schema import get_all_topics
    topic_ids = [t["id"] for t in get_all_topics(seeded_db)]
    ctx = load_context(topic_ids, seeded_db, use_summary=False)
    # Raw mode renders role labels
    assert "[User]" in ctx or "first" in ctx


def test_load_context_respects_max_tokens(seeded_db):
    from pane.schema import get_all_topics
    topic_ids = [t["id"] for t in get_all_topics(seeded_db)]
    # Tiny budget should cut off after first topic
    ctx = load_context(topic_ids, seeded_db, max_tokens=5)
    # At most one of the three summaries made it in
    summary_hits = sum(
        s in ctx for s in
        ["rewriting auth in cpp", "dashboard dark mode", "career chat"]
    )
    assert summary_hits <= 1


def test_load_context_skips_missing_topic_ids(seeded_db):
    ctx = load_context(["nonexistent-id"], seeded_db)
    assert ctx == ""


def test_load_context_empty_list(seeded_db):
    assert load_context([], seeded_db) == ""


def test_load_context_falls_back_to_raw_when_no_summary(db):
    """Topic with empty summary should render raw messages instead."""
    save_entity(db, "x", "tool", ["x"])
    window = create_window(db)
    msgs = save_messages(db, window, [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "reply here"},
    ])
    tid = save_topic(db, window, "No Summary",
                     msgs[0][0], msgs[-1][0], summary="", tags=["entity:x"])
    ctx = load_context([tid], db)
    # Fallback renders role-labeled raw messages
    assert "No Summary" in ctx
    assert "hello there" in ctx


# ── format_facts ─────────────────────────────────────────────

def test_format_facts_empty():
    assert format_facts({}) == ""


def test_format_facts_single_entity():
    out = format_facts({"user": [("commute", "35 min"), ("role", "eng")]})
    assert "[user]" in out
    assert "commute: 35 min" in out
    assert "role: eng" in out


def test_format_facts_multiple_entities_sorted():
    out = format_facts({
        "zebra": [("a", "1")],
        "apple": [("b", "2")],
    })
    # entities should render in alphabetical order
    assert out.index("[apple]") < out.index("[zebra]")
