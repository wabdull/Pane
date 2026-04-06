"""Tests for topic grouping — entity fingerprint, overlap matching, extension."""

import pytest

from pane.schema import (
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    get_entities_from_loaded_topics,
    get_most_recent_topic,
    get_topic_messages,
    get_topics_by_tags,
    mark_loaded,
    parse_fingerprint,
    save_messages,
    save_topic,
    set_topic_summary,
)


@pytest.fixture
def db():
    conn = create_db(":memory:")
    yield conn
    conn.close()


# ── entity_fingerprint / parse_fingerprint ────────────────────

def test_fingerprint_sorted():
    assert entity_fingerprint(["cpp", "auth-session"]) == "auth-session,cpp"
    assert entity_fingerprint(["z", "a", "m"]) == "a,m,z"


def test_fingerprint_normalizes():
    assert entity_fingerprint(["  CPP  ", "Auth-Session"]) == "auth-session,cpp"


def test_fingerprint_dedupes():
    assert entity_fingerprint(["cpp", "CPP", " cpp "]) == "cpp"


def test_fingerprint_empty():
    assert entity_fingerprint([]) == ""
    assert entity_fingerprint(None) == ""
    assert entity_fingerprint(["", "  ", None]) == ""


def test_parse_fingerprint_roundtrip():
    fp = entity_fingerprint(["cpp", "auth-session", "python"])
    assert parse_fingerprint(fp) == {"cpp", "auth-session", "python"}


def test_parse_fingerprint_empty():
    assert parse_fingerprint("") == set()
    assert parse_fingerprint(None) == set()


# ── get_most_recent_topic ─────────────────────────────────────

def test_get_most_recent_topic_empty(db):
    assert get_most_recent_topic(db) is None


def test_get_most_recent_topic_returns_last(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "a"}])
    save_topic(db, window, "T1", m[0][0], m[0][0], entities=["cpp"])
    m = save_messages(db, window, [{"role": "user", "content": "b"}])
    save_topic(db, window, "T2", m[0][0], m[0][0], entities=["python"])
    t = get_most_recent_topic(db)
    assert t["title"] == "T2"
    assert t["entity_fingerprint"] == "python"


# ── save_topic with fingerprint ───────────────────────────────

def test_save_topic_stores_fingerprint(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0],
                     entities=["cpp", "auth-session"])
    t = get_most_recent_topic(db)
    assert t["id"] == tid
    assert t["entity_fingerprint"] == "auth-session,cpp"


def test_save_topic_no_entities(db):
    """Topic created with no entities has empty fingerprint."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "empty", m[0][0], m[0][0])
    t = get_most_recent_topic(db)
    assert t["entity_fingerprint"] == ""


# ── extend_topic ──────────────────────────────────────────────

def test_extend_topic_updates_end_range(db):
    window = create_window(db)
    m1 = save_messages(db, window, [{"role": "user", "content": "first"}])
    tid = save_topic(db, window, "T", m1[0][0], m1[0][0], entities=["cpp"])

    m2 = save_messages(db, window, [{"role": "user", "content": "second"}])
    extend_topic(db, tid, new_end_message_id=m2[0][0])

    t = get_most_recent_topic(db)
    assert t["start_message_id"] == m1[0][0]
    assert t["end_message_id"] == m2[0][0]


def test_extend_topic_merges_entities(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0], entities=["cpp"])

    m2 = save_messages(db, window, [{"role": "user", "content": "y"}])
    extend_topic(db, tid, new_end_message_id=m2[0][0],
                 new_entities=["python", "auth-session"])

    t = get_most_recent_topic(db)
    assert parse_fingerprint(t["entity_fingerprint"]) == {
        "cpp", "python", "auth-session"
    }


def test_extend_topic_is_idempotent_on_existing_entity(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0],
                     entities=["cpp", "python"])

    m2 = save_messages(db, window, [{"role": "user", "content": "y"}])
    extend_topic(db, tid, new_end_message_id=m2[0][0], new_entities=["cpp"])

    t = get_most_recent_topic(db)
    assert parse_fingerprint(t["entity_fingerprint"]) == {"cpp", "python"}


def test_extend_topic_merges_tags(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0],
                     entities=["cpp"], tags=["entity:cpp", "cat:backend"])

    m2 = save_messages(db, window, [{"role": "user", "content": "y"}])
    extend_topic(db, tid, new_end_message_id=m2[0][0],
                 new_entities=["python"],
                 new_tags=["entity:python", "cat:frontend"])

    matches = get_topics_by_tags(db, ["entity:python", "cat:backend",
                                       "cat:frontend"])
    assert len(matches) == 1
    assert matches[0]["topic_id"] == tid
    assert matches[0]["match_count"] == 3


def test_extend_topic_updates_title(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "old title", m[0][0], m[0][0],
                     entities=["cpp"])
    extend_topic(db, tid, new_end_message_id=m[0][0], new_title="new title")
    t = get_most_recent_topic(db)
    assert t["title"] == "new title"


def test_extend_topic_noop_on_missing(db):
    """Extending a nonexistent topic is a no-op, not a crash."""
    extend_topic(db, "nonexistent-id", new_end_message_id=5,
                 new_entities=["cpp"])
    assert get_most_recent_topic(db) is None


# ── set_topic_summary ─────────────────────────────────────────

def test_set_topic_summary(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0], entities=["cpp"])
    set_topic_summary(db, tid, "cpp work went well")
    t = get_most_recent_topic(db)
    assert t["summary"] == "cpp work went well"


def test_set_topic_summary_strips(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0], entities=["cpp"])
    set_topic_summary(db, tid, "  padded  \n")
    t = get_most_recent_topic(db)
    assert t["summary"] == "padded"


# ── get_topic_messages across extended range ─────────────────

def test_get_topic_messages_after_extension(db):
    """Extending the end_message_id should pull in later messages."""
    window = create_window(db)
    m1 = save_messages(db, window, [
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first response"},
    ])
    tid = save_topic(db, window, "T", m1[0][0], m1[-1][0], entities=["cpp"])

    m2 = save_messages(db, window, [
        {"role": "user", "content": "second user"},
        {"role": "assistant", "content": "second response"},
    ])
    extend_topic(db, tid, new_end_message_id=m2[-1][0])

    msgs = get_topic_messages(db, tid)
    assert len(msgs) == 4
    assert msgs[0]["content"] == "first user"
    assert msgs[-1]["content"] == "second response"


# ── get_topics_by_tags recency tiebreaker ─────────────────────

def test_topics_ranked_by_recency_on_tiebreak(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "a"}])
    old_tid = save_topic(db, window, "old", m[0][0], m[0][0],
                         entities=["cpp"], tags=["entity:cpp"])

    m = save_messages(db, window, [{"role": "user", "content": "b"}])
    new_tid = save_topic(db, window, "new", m[0][0], m[0][0],
                         entities=["cpp"], tags=["entity:cpp"])

    matches = get_topics_by_tags(db, ["entity:cpp"])
    # Both match 1 tag each — newer should rank first
    assert matches[0]["topic_id"] == new_tid
    assert matches[1]["topic_id"] == old_tid


# ── overlap logic (integration with topic decision) ──────────

# ── Two-axis grouping (entity + category) ────────────────────

def test_save_topic_stores_category_fingerprint(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "T", m[0][0], m[0][0],
               entities=["cpp"], categories=["architecture", "backend"])
    t = get_most_recent_topic(db)
    assert t["category_fingerprint"] == "architecture,backend"


def test_extend_topic_merges_categories(db):
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    tid = save_topic(db, window, "T", m[0][0], m[0][0],
                     entities=["cpp"], categories=["architecture"])
    m2 = save_messages(db, window, [{"role": "user", "content": "y"}])
    extend_topic(db, tid, new_end_message_id=m2[0][0],
                 new_categories=["testing"])
    t = get_most_recent_topic(db)
    assert parse_fingerprint(t["category_fingerprint"]) == {"architecture", "testing"}


def test_subtopic_shift_same_entities_different_categories(db):
    """Same entity overlap + different categories = subtopic shift (new row)."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "T1", m[0][0], m[0][0],
               entities=["cpp"], categories=["architecture"])

    prior = get_most_recent_topic(db)
    prior_ent = parse_fingerprint(prior["entity_fingerprint"])
    prior_cat = parse_fingerprint(prior["category_fingerprint"])

    new_ent = {"cpp"}  # overlap
    new_cat = {"testing"}  # no overlap

    ent_continues = bool(new_ent & prior_ent)
    cat_continues = bool(new_cat & prior_cat)

    assert ent_continues is True  # same domain
    assert cat_continues is False  # different sub-thread


def test_same_entities_same_categories_extends(db):
    """Both axes overlap = extend."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "T1", m[0][0], m[0][0],
               entities=["cpp"], categories=["architecture"])

    prior = get_most_recent_topic(db)
    prior_ent = parse_fingerprint(prior["entity_fingerprint"])
    prior_cat = parse_fingerprint(prior["category_fingerprint"])

    new_ent = {"cpp"}
    new_cat = {"architecture"}

    assert bool(new_ent & prior_ent) is True
    assert bool(new_cat & prior_cat) is True


def test_empty_entity_set_treated_as_continuation(db):
    """Empty entities this turn = 'no change' on entity axis."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "T1", m[0][0], m[0][0],
               entities=["cpp"], categories=["architecture"])

    prior = get_most_recent_topic(db)
    prior_ent = parse_fingerprint(prior["entity_fingerprint"])

    new_ent = set()  # empty = user didn't name an entity
    # "not new_ent" is True, so ent_continues = True
    ent_continues = not new_ent or bool(new_ent & prior_ent)
    assert ent_continues is True


def test_empty_category_set_treated_as_continuation(db):
    """Empty categories this turn = 'no change' on category axis."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "T1", m[0][0], m[0][0],
               entities=["cpp"], categories=["architecture"])

    prior = get_most_recent_topic(db)
    prior_cat = parse_fingerprint(prior["category_fingerprint"])

    new_cat = set()
    cat_continues = not new_cat or bool(new_cat & prior_cat)
    assert cat_continues is True


# ── get_entities_from_loaded_topics ───────────────────────────

def test_entities_derived_from_loaded_topics(db):
    """Active entities = union of fingerprints across loaded topics."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    t1 = save_topic(db, window, "T1", m[0][0], m[0][0],
                     entities=["cpp", "auth-session"])
    m = save_messages(db, window, [{"role": "user", "content": "y"}])
    t2 = save_topic(db, window, "T2", m[0][0], m[0][0],
                     entities=["postgres", "payment-webhook"])
    mark_loaded(db, t1)
    mark_loaded(db, t2)
    active = get_entities_from_loaded_topics(db)
    assert set(active) == {"cpp", "auth-session", "postgres", "payment-webhook"}


def test_entities_drop_when_topic_unloads(db):
    """Unloading the only topic with cpp should drop cpp from active."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    t1 = save_topic(db, window, "T1", m[0][0], m[0][0], entities=["cpp"])
    m = save_messages(db, window, [{"role": "user", "content": "y"}])
    t2 = save_topic(db, window, "T2", m[0][0], m[0][0], entities=["python"])
    mark_loaded(db, t1)
    mark_loaded(db, t2)
    assert "cpp" in get_entities_from_loaded_topics(db)

    # Unload t1 (cpp)
    db.execute("DELETE FROM loaded_topics WHERE topic_id = ?", (t1,))
    db.commit()
    active = get_entities_from_loaded_topics(db)
    assert "cpp" not in active
    assert "python" in active


def test_shared_entity_survives_partial_unload(db):
    """If two subtopics share cpp, unloading one keeps cpp active."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    t1 = save_topic(db, window, "S1", m[0][0], m[0][0],
                     entities=["cpp", "auth-session"], categories=["architecture"])
    m = save_messages(db, window, [{"role": "user", "content": "y"}])
    t2 = save_topic(db, window, "S2", m[0][0], m[0][0],
                     entities=["cpp", "auth-session"], categories=["testing"])
    mark_loaded(db, t1)
    mark_loaded(db, t2)

    # Unload first subtopic
    db.execute("DELETE FROM loaded_topics WHERE topic_id = ?", (t1,))
    db.commit()
    active = get_entities_from_loaded_topics(db)
    assert "cpp" in active  # survived via t2
    assert "auth-session" in active


def test_no_loaded_topics_means_no_active_entities(db):
    assert get_entities_from_loaded_topics(db) == []


# ── overlap logic (single-axis, original tests) ──────────────

def test_overlap_detection_matches_prior(db):
    """Non-empty intersection with prior topic's fingerprint."""
    window = create_window(db)
    m = save_messages(db, window, [{"role": "user", "content": "x"}])
    save_topic(db, window, "T1", m[0][0], m[0][0],
               entities=["cpp", "auth-session"])

    prior = get_most_recent_topic(db)
    prior_set = parse_fingerprint(prior["entity_fingerprint"])

    # Subset overlap
    assert bool(prior_set & {"cpp"})
    # Shared element only
    assert bool(prior_set & {"cpp", "python"})
    # Empty current (drift) — overlap is empty but drift rule extends anyway
    assert not bool(prior_set & set())
    # No overlap
    assert not bool(prior_set & {"python", "postgres"})
