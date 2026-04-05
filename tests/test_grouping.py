"""Tests for topic grouping — entity fingerprint, overlap matching, extension."""

import pytest

from pane.schema import (
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    get_most_recent_topic,
    get_topic_messages,
    get_topics_by_tags,
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
