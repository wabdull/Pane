"""Lifecycle tests — full load/unload cycle through the pipeline.

Simulates multiple turns running both the on_stop pipeline (save/group)
and the on_message pipeline (recall/inject). Verifies that:
  1. Topics load into context when relevant
  2. Topics decay and unload after TTL expires
  3. Entity facts appear when their topic is loaded
  4. Entity facts disappear when their topic unloads
  5. Context size shrinks as stale topics decay
  6. Subtopic splits load/unload independently
  7. Returning to a prior domain reloads its context
"""

import pytest

from pane.recall import recall, load_context, format_facts
from pane.schema import (
    DEFAULT_TTL,
    USER_ENTITY,
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    fingerprint_overlaps,
    get_entities_from_loaded_topics,
    get_facts_for_entities,
    get_loaded_topic_ids,
    get_loaded_topics_with_ttl,
    get_most_recent_topic,
    mark_loaded,
    parse_fingerprint,
    save_entity,
    save_entity_fact,
    save_messages,
    save_topic,
    set_topic_summary,
    tick_ttl,
)


@pytest.fixture
def db():
    conn = create_db(":memory:")
    yield conn
    conn.close()


# ── Helpers: simulate the on_stop and on_message pipelines ────

def simulate_on_stop(db, window_id, user_msg, assistant_msg,
                     entities, categories, facts=None, summary=""):
    """Simulate what on_stop.py does: save messages, group topic, save facts."""
    # Save messages
    messages = [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": assistant_msg},
    ]
    saved = save_messages(db, window_id, messages)
    new_start, new_end = saved[0][0], saved[-1][0]

    # Normalize
    ent_list = [(e or "").lower().strip() for e in entities if (e or "").strip()]
    cat_list = [(c or "").lower().strip() for c in categories if (c or "").strip()]
    ent_set = set(ent_list)
    cat_set = set(cat_list)
    tags = [f"entity:{e}" for e in ent_list] + [f"cat:{c}" for c in cat_list]

    # Register entities
    for e in ent_list:
        if len(e) > 1:
            save_entity(db, e, entity_type="unknown", aliases=[e])
    for c in cat_list:
        if c and len(c) > 1:
            save_entity(db, c, entity_type="category", aliases=[c])

    # Two-axis grouping (mirrors on_stop.py)
    most_recent = get_most_recent_topic(db)
    is_drift = not ent_set and not cat_set

    if most_recent is None:
        title = entity_fingerprint(ent_set) or "general"
        topic_id = save_topic(
            db, window_id, title=title,
            start_message_id=new_start, end_message_id=new_end,
            tags=tags, entities=ent_list, categories=cat_list,
        )
    elif is_drift:
        extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                     new_tags=tags)
        topic_id = most_recent["id"]
    else:
        ent_continues = fingerprint_overlaps(
            ent_set, most_recent["entity_fingerprint"])
        cat_continues = fingerprint_overlaps(
            cat_set, most_recent["category_fingerprint"])

        if ent_continues and cat_continues:
            extend_topic(
                db, most_recent["id"], new_end_message_id=new_end,
                new_tags=tags,
            )
            topic_id = most_recent["id"]
        else:
            if summary:
                set_topic_summary(db, most_recent["id"], summary)
            title = entity_fingerprint(ent_set) or "general"
            topic_id = save_topic(
                db, window_id, title=title,
                start_message_id=new_start, end_message_id=new_end,
                tags=tags, entities=ent_list, categories=cat_list,
            )

    # Save facts
    for fact in (facts or []):
        entity_name = (fact.get("entity") or USER_ENTITY).strip().lower()
        key = (fact.get("key") or "").strip()
        value = (fact.get("value") or "").strip()
        if entity_name and key and value:
            save_entity_fact(db, entity_name, key, value)
            if entity_name != USER_ENTITY:
                save_entity(db, entity_name, entity_type="unknown",
                            aliases=[entity_name])

    # Reset TTL for active topic. tick_ttl only decrements — resets
    # happen here where we know the specific active topic.
    # - New topic: mark_loaded
    # - Non-drift extend: mark_loaded (user actively working on this)
    # - Drift extend: DON'T reset (gradual decay on silence)
    is_new = (most_recent is None or topic_id != most_recent.get("id"))
    if is_new or not is_drift:
        mark_loaded(db, topic_id)
    return topic_id


def simulate_on_message(db, user_msg):
    """Simulate what on_message.py does: recall, tick_ttl, build context.
    Returns the context string that would be injected.
    """
    result = recall(user_msg, db)
    tick_ttl(db)

    loaded_topic_ids = get_loaded_topic_ids(db)
    active = get_entities_from_loaded_topics(db)
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)

    parts = []
    if facts:
        parts.append(format_facts(facts))
    if loaded_topic_ids:
        raw = load_context(loaded_topic_ids, db)
        if raw:
            parts.append(raw)

    context = "[MEMORY]\n" + "\n\n".join(parts) if parts else ""
    return context, active, loaded_topic_ids


# ── Tests ─────────────────────────────────────────────────────

class TestTopicLoadUnload:
    """Topics load when matched, decay over turns, and unload at TTL 0."""

    def test_topic_loads_on_first_mention(self, db):
        window = create_window(db)
        simulate_on_stop(db, window, "working on auth-session in cpp",
                         "sure, lets work on that",
                         ["cpp", "auth-session"], ["architecture"])
        ctx, active, loaded = simulate_on_message(db, "auth-session patterns")
        assert len(loaded) >= 1
        assert "auth-session" in ctx or "cpp" in ctx

    def test_topic_decays_when_unreferenced(self, db):
        window = create_window(db)
        # Turn 1: create topic about cpp
        simulate_on_stop(db, window, "working on cpp auth-session",
                         "ok", ["cpp", "auth-session"], ["architecture"])

        # Turns 2-6: unrelated drift (no cpp/auth-session mentions)
        for i in range(DEFAULT_TTL + 1):
            simulate_on_stop(db, window, f"unrelated message {i}",
                             "ok", [], [])
            simulate_on_message(db, f"unrelated message {i}")

        # After DEFAULT_TTL+1 unreferenced turns, topic should be unloaded
        ctx, active, loaded = simulate_on_message(db, "random question")
        assert "cpp" not in active
        assert "auth-session" not in active

    def test_topic_stays_loaded_when_referenced(self, db):
        window = create_window(db)
        simulate_on_stop(db, window, "working on cpp auth-session",
                         "ok", ["cpp", "auth-session"], ["architecture"])

        # Keep referencing cpp for many turns
        for i in range(DEFAULT_TTL * 3):
            simulate_on_stop(db, window, f"more cpp work {i}",
                             "ok", ["cpp", "auth-session"], ["architecture"])
            ctx, active, loaded = simulate_on_message(db, "cpp auth-session")

        # Still loaded after 15 turns of continuous reference
        assert "cpp" in active
        assert len(loaded) >= 1

    def test_context_shrinks_as_topics_unload(self, db):
        window = create_window(db)
        # Create two topics
        simulate_on_stop(db, window, "working on cpp auth-session",
                         "designing the session store with versioned tokens",
                         ["cpp", "auth-session"], ["architecture"])
        simulate_on_stop(db, window, "now working on python admin-dashboard",
                         "checking dark mode rollout status",
                         ["python", "admin-dashboard"], ["debugging"],
                         summary="cpp auth: versioned tokens, no exceptions")

        # Both should be loaded initially
        ctx_both, _, loaded_both = simulate_on_message(db, "admin-dashboard")
        assert len(loaded_both) >= 2

        # Let cpp topic decay by only referencing admin-dashboard
        for i in range(DEFAULT_TTL + 1):
            simulate_on_stop(db, window, f"more dashboard work {i}",
                             "ok", ["python", "admin-dashboard"], ["debugging"])
            simulate_on_message(db, "admin-dashboard")

        ctx_after, active_after, loaded_after = simulate_on_message(
            db, "admin-dashboard")
        assert len(loaded_after) < len(loaded_both)
        # cpp content should be gone even if admin-dashboard grew
        assert "cpp" not in active_after
        assert "auth-session" not in active_after


class TestEntityFactLifecycle:
    """Entity facts load when their topic loads, vanish when it unloads."""

    def test_entity_facts_load_with_topic(self, db):
        window = create_window(db)
        simulate_on_stop(db, window, "working on cpp",
                         "ok", ["cpp"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "exceptions",
                                 "value": "disallowed at acme"}])

        ctx, active, _ = simulate_on_message(db, "cpp work")
        assert "cpp" in active
        assert "disallowed" in ctx

    def test_entity_facts_disappear_on_unload(self, db):
        window = create_window(db)
        simulate_on_stop(db, window, "working on cpp",
                         "ok", ["cpp"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "exceptions",
                                 "value": "disallowed at acme"}])

        # Verify loaded
        ctx, active, _ = simulate_on_message(db, "cpp work")
        assert "disallowed" in ctx

        # Decay the topic completely with unrelated turns
        for i in range(DEFAULT_TTL + 2):
            simulate_on_stop(db, window, f"unrelated {i}",
                             "ok", [], [])
            simulate_on_message(db, f"unrelated {i}")

        ctx_after, active_after, _ = simulate_on_message(db, "random question")
        assert "cpp" not in active_after
        assert "disallowed" not in ctx_after

    def test_user_facts_always_present(self, db):
        window = create_window(db)
        save_entity_fact(db, USER_ENTITY, "name", "Waleed")
        save_entity_fact(db, USER_ENTITY, "role", "engineer")

        # Even with nothing loaded, user facts should be in context
        ctx, _, _ = simulate_on_message(db, "hello")
        assert "Waleed" in ctx
        assert "engineer" in ctx

    def test_entity_facts_switch_on_domain_change(self, db):
        window = create_window(db)
        # Set up two domains with different facts
        simulate_on_stop(db, window, "working on cpp",
                         "ok", ["cpp"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "style",
                                 "value": "snake_case"}])

        simulate_on_stop(db, window, "now python admin-dashboard",
                         "ok", ["python", "admin-dashboard"], ["debugging"],
                         facts=[{"entity": "python", "key": "linter",
                                 "value": "ruff"}])

        # Both should be present initially (both topics loaded)
        ctx, active, _ = simulate_on_message(db, "python admin-dashboard")
        assert "ruff" in ctx  # python fact

        # Let cpp decay
        for i in range(DEFAULT_TTL + 2):
            simulate_on_stop(db, window, f"more python {i}",
                             "ok", ["python", "admin-dashboard"], ["debugging"])
            simulate_on_message(db, "python admin-dashboard")

        ctx_after, active_after, _ = simulate_on_message(
            db, "python admin-dashboard")
        assert "ruff" in ctx_after      # python fact still present
        assert "snake_case" not in ctx_after  # cpp fact gone
        assert "cpp" not in active_after


class TestSubtopicLifecycle:
    """Subtopics (same entities, different categories) decay independently."""

    def test_subtopics_decay_independently(self, db):
        """Subtopics with shared entities decay independently because
        tick_ttl only decrements and mark_loaded resets only the specific
        topic that on_stop extended.
        """
        window = create_window(db)
        # Subtopic 1: architecture
        simulate_on_stop(db, window, "designing auth-session in cpp",
                         "ok", ["cpp", "auth-session"], ["architecture"])

        # Subtopic 2: testing (same entities, different category)
        simulate_on_stop(db, window, "writing tests for auth-session",
                         "ok", ["cpp", "auth-session"], ["testing"],
                         summary="architecture: versioned tokens")

        _, _, loaded_both = simulate_on_message(db, "cpp auth-session")
        assert len(loaded_both) == 2  # both subtopics loaded

        # Keep working on testing — only testing subtopic gets mark_loaded
        for i in range(DEFAULT_TTL):
            simulate_on_stop(db, window, f"more tests {i}",
                             "ok", ["cpp", "auth-session"], ["testing"])
            simulate_on_message(db, "cpp auth-session")

        _, _, loaded_after = simulate_on_message(db, "cpp auth-session")
        # Architecture subtopic decayed, testing survived
        assert len(loaded_after) < len(loaded_both)

    def test_subtopics_decay_on_domain_shift(self, db):
        """Subtopics DO decay when the entire domain shifts (no shared
        entity tags with the new domain).
        """
        window = create_window(db)
        simulate_on_stop(db, window, "designing cpp",
                         "ok", ["cpp"], ["architecture"])
        simulate_on_stop(db, window, "testing cpp",
                         "ok", ["cpp"], ["testing"],
                         summary="architecture done")

        _, _, loaded = simulate_on_message(db, "cpp")
        assert len(loaded) == 2

        # Shift to a completely different domain
        simulate_on_stop(db, window, "python admin-dashboard",
                         "ok", ["python", "admin-dashboard"], ["debugging"],
                         summary="cpp testing done")

        # Let cpp subtopics decay
        for i in range(DEFAULT_TTL + 1):
            simulate_on_stop(db, window, f"python work {i}",
                             "ok", ["python", "admin-dashboard"], ["debugging"])
            simulate_on_message(db, "python admin-dashboard")

        _, active, loaded_after = simulate_on_message(db, "python")
        assert "cpp" not in active  # both cpp subtopics decayed

    def test_entity_facts_survive_subtopic_decay(self, db):
        """If two subtopics share entities, facts survive partial unload."""
        window = create_window(db)
        simulate_on_stop(db, window, "designing cpp",
                         "ok", ["cpp"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "exceptions",
                                 "value": "disallowed"}])

        simulate_on_stop(db, window, "testing cpp",
                         "ok", ["cpp"], ["testing"],
                         summary="architecture done")

        # Both loaded — cpp facts present
        ctx, active, _ = simulate_on_message(db, "cpp")
        assert "disallowed" in ctx

        # Let architecture subtopic decay, testing continues
        for i in range(DEFAULT_TTL):
            simulate_on_stop(db, window, f"more testing {i}",
                             "ok", ["cpp"], ["testing"])
            simulate_on_message(db, "cpp testing")

        # cpp facts should STILL be present (testing subtopic keeps cpp alive)
        ctx_after, active_after, _ = simulate_on_message(db, "cpp")
        assert "cpp" in active_after
        assert "disallowed" in ctx_after


class TestReturnToPriorDomain:
    """Returning to a previously-discussed domain reloads its context."""

    def test_return_reloads_topic(self, db):
        window = create_window(db)
        # Discuss cpp
        simulate_on_stop(db, window, "working on cpp auth-session",
                         "designing the session store",
                         ["cpp", "auth-session"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "style",
                                 "value": "snake_case"}])

        # Switch to python (cpp starts decaying)
        simulate_on_stop(db, window, "now python admin-dashboard",
                         "ok", ["python", "admin-dashboard"], ["debugging"],
                         summary="cpp: session store designed, snake_case")

        # Let cpp fully decay
        for i in range(DEFAULT_TTL + 2):
            simulate_on_stop(db, window, f"python work {i}",
                             "ok", ["python", "admin-dashboard"], ["debugging"])
            simulate_on_message(db, "python admin-dashboard")

        # Verify cpp is gone
        ctx_gone, active_gone, _ = simulate_on_message(db, "random")
        assert "cpp" not in active_gone

        # NOW return to cpp — should reload from DB
        simulate_on_stop(db, window, "back to cpp auth-session",
                         "ok", ["cpp", "auth-session"], ["architecture"])
        ctx_back, active_back, loaded_back = simulate_on_message(
            db, "cpp auth-session")

        assert "cpp" in active_back
        assert len(loaded_back) >= 1

    def test_return_reloads_entity_facts(self, db):
        window = create_window(db)
        simulate_on_stop(db, window, "cpp work",
                         "ok", ["cpp"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "exceptions",
                                 "value": "disallowed at acme"}])

        # Switch away, let decay
        simulate_on_stop(db, window, "python work",
                         "ok", ["python"], ["debugging"],
                         summary="cpp: no exceptions")
        for i in range(DEFAULT_TTL + 2):
            simulate_on_stop(db, window, f"python {i}",
                             "ok", ["python"], ["debugging"])
            simulate_on_message(db, "python")

        # cpp facts gone
        ctx_gone, _, _ = simulate_on_message(db, "random")
        assert "disallowed" not in ctx_gone

        # Return to cpp
        simulate_on_stop(db, window, "back to cpp",
                         "ok", ["cpp"], ["architecture"])
        ctx_back, _, _ = simulate_on_message(db, "cpp")

        # Facts should be back
        assert "disallowed" in ctx_back


class TestContextContent:
    """Verify the actual content of injected context is correct."""

    def test_summary_loads_not_raw_when_available(self, db):
        window = create_window(db)
        simulate_on_stop(db, window, "cpp architecture work",
                         "long detailed response about session tokens "
                         "and versioning strategies and error handling",
                         ["cpp"], ["architecture"])

        # Close this topic with a summary
        simulate_on_stop(db, window, "switching to python",
                         "ok", ["python"], ["debugging"],
                         summary="cpp: versioned session tokens, no exceptions")

        # The cpp topic has a summary now. When it's loaded,
        # it should show the summary, not the raw messages
        ctx, _, _ = simulate_on_message(db, "cpp")
        assert "versioned session tokens" in ctx

    def test_multiple_domain_facts_coexist(self, db):
        """When two domains are both loaded, both sets of facts appear."""
        window = create_window(db)
        save_entity_fact(db, USER_ENTITY, "name", "Waleed")
        simulate_on_stop(db, window, "cpp work",
                         "ok", ["cpp"], ["architecture"],
                         facts=[{"entity": "cpp", "key": "style",
                                 "value": "snake_case"}])
        simulate_on_stop(db, window, "also postgres",
                         "ok", ["postgres"], ["performance"],
                         facts=[{"entity": "postgres", "key": "version",
                                 "value": "17.2"}])

        ctx, active, _ = simulate_on_message(db, "cpp and postgres")
        assert "Waleed" in ctx        # user facts
        assert "snake_case" in ctx     # cpp facts
        assert "17.2" in ctx           # postgres facts

    def test_empty_db_returns_no_context(self, db):
        ctx, active, loaded = simulate_on_message(db, "hello")
        assert ctx == ""
        assert active == []
        assert loaded == []
