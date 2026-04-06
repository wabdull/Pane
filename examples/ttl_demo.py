"""Pane demo — three tracks in action.

  1. Topics (conversational history) -> TTL-managed, tolerate drift
  2. Entity facts (domain rules)     -> hard-switch, replace on mention
  3. Topic grouping                  -> consecutive turns with overlapping
                                        entities extend one topic row; disjoint
                                        entities open a new row.

Each turn, we simulate the on_message + on_stop hook pipeline:
  recall -> tick_ttl -> group_into_topic -> derive active entities.

Run:
    PYTHONPATH=src python examples/ttl_demo.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pane.recall import recall
from pane.schema import (
    DEFAULT_TTL,
    USER_ENTITY,
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    get_entities_from_loaded_topics,
    get_facts_for_entities,
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


def seed_entities_and_facts(db):
    """Create the entity registry + user/domain/project facts."""
    entity_defs = [
        ("cpp", "tool", ["cpp", "c++"]),
        ("python", "tool", ["python"]),
        ("postgres", "tool", ["postgres", "postgresql"]),
        ("auth-session", "project", ["auth-session", "auth session"]),
        ("admin-dashboard", "project", ["admin-dashboard", "admin dashboard"]),
        ("payment-webhook", "project", ["payment-webhook", "payment webhook"]),
    ]
    for name, etype, aliases in entity_defs:
        save_entity(db, name, entity_type=etype, aliases=aliases)

    # User (always loaded)
    save_entity_fact(db, USER_ENTITY, "name", "Waleed")
    save_entity_fact(db, USER_ENTITY, "role", "staff engineer")
    save_entity_fact(db, USER_ENTITY, "timezone", "PST")

    # Tools
    save_entity_fact(db, "cpp", "exceptions", "disallowed at acme")
    save_entity_fact(db, "cpp", "standard", "c++20")
    save_entity_fact(db, "cpp", "style", "snake_case members")
    save_entity_fact(db, "python", "version", "3.13")
    save_entity_fact(db, "python", "linter", "ruff")
    save_entity_fact(db, "python", "tests", "pytest")
    save_entity_fact(db, "postgres", "version", "17.2")
    save_entity_fact(db, "postgres", "prod_rule", "no ALTER without pg_repack")
    save_entity_fact(db, "postgres", "downtime", "Sun 2-4am UTC")

    # Projects
    save_entity_fact(db, "auth-session", "status", "blocked on security review")
    save_entity_fact(db, "auth-session", "owner", "waleed")
    save_entity_fact(db, "admin-dashboard", "route", "/admin")
    save_entity_fact(db, "admin-dashboard", "rollout", "60% of tenants")
    save_entity_fact(db, "payment-webhook", "endpoint", "/webhooks/payment")
    save_entity_fact(db, "payment-webhook", "blocker", "missing index")


def group_turn(db, window_id, user_msg, extracted_entities,
               turn_categories, summary=""):
    """Simulate on_stop's two-axis grouping logic.
    Returns (topic_id, action, title).
    """
    saved = save_messages(db, window_id, [{"role": "user", "content": user_msg}])
    new_start, new_end = saved[0][0], saved[-1][0]

    ent_set = set(extracted_entities)
    cat_set = set(turn_categories)
    tags = [f"entity:{e}" for e in ent_set] + [f"cat:{c}" for c in cat_set]
    is_drift = not ent_set and not cat_set

    most_recent = get_most_recent_topic(db)

    if most_recent is None:
        title = entity_fingerprint(ent_set) or "general"
        tid = save_topic(db, window_id, title=title,
                         start_message_id=new_start, end_message_id=new_end,
                         tags=tags, entities=list(ent_set),
                         categories=list(cat_set))
        return tid, "NEW", title

    if is_drift:
        extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                     new_tags=tags)
        return most_recent["id"], "EXTEND", most_recent["title"]

    prior_ent = parse_fingerprint(most_recent["entity_fingerprint"])
    prior_cat = parse_fingerprint(most_recent["category_fingerprint"])

    # Empty axis this turn = "no change" on that axis
    ent_continues = not ent_set or bool(ent_set & prior_ent)
    cat_continues = not cat_set or bool(cat_set & prior_cat)

    if ent_continues and cat_continues:
        merged_title = entity_fingerprint(prior_ent | ent_set) or \
                       most_recent["title"]
        extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                     new_entities=list(ent_set), new_categories=list(cat_set),
                     new_tags=tags, new_title=merged_title)
        return most_recent["id"], "EXTEND", merged_title
    else:
        if summary:
            set_topic_summary(db, most_recent["id"], summary)
        shift = "SUBTOPIC" if ent_continues else "NEW"
        title = entity_fingerprint(ent_set) or "general"
        tid = save_topic(db, window_id, title=title,
                         start_message_id=new_start, end_message_id=new_end,
                         tags=tags, entities=list(ent_set),
                         categories=list(cat_set))
        return tid, shift, title


def print_turn(turn_num, user_msg, extracted, action, title, topic_id,
                active, facts, loaded, prev_loaded, db):
    current_loaded = {e for e in active if e in facts and e != USER_ENTITY}
    dropped = prev_loaded - current_loaded
    added = current_loaded - prev_loaded
    change = ""
    if added or dropped:
        parts = []
        if dropped:
            parts.append(f"drop {','.join(sorted(dropped))}")
        if added:
            parts.append(f"load {','.join(sorted(added))}")
        change = "   [SWITCH: " + "; ".join(parts) + "]"

    print(f"\n+-- Turn {turn_num} " + "-" * 48 + change)
    print(f"| User: {user_msg}")
    print(f"| Extracted entities: {sorted(extracted) if extracted else '(none)'}")
    print("|")

    # Topic action
    action_tag = f"[{action}]"
    print(f"| {action_tag:8s}  topic {topic_id[:8]}  fingerprint: {title}")
    print("|")

    # User + active entity facts
    user_kvs = facts.get(USER_ENTITY, [])
    print(f"| [ALWAYS]  user: "
          f"{', '.join(f'{k}={v}' for k, v in user_kvs)}")
    entity_facts_loaded = {
        n: kvs for n, kvs in facts.items() if n != USER_ENTITY
    }
    if entity_facts_loaded:
        for n in sorted(entity_facts_loaded):
            print(f"| [ACTIVE]  {n}:")
            for k, v in entity_facts_loaded[n]:
                print(f"|             {k}: {v}")
    else:
        print("| [ACTIVE]  (no entity facts yet)")
    print("|")

    # Topic TTL bars
    print("| [TOPICS]  (TTL bar)")
    if not loaded:
        print("|             (window empty)")
    else:
        for tid, ttl in loaded:
            row = db.execute(
                "SELECT title FROM topics WHERE id = ?", (tid,)
            ).fetchone()
            bar = "#" * ttl + "." * (DEFAULT_TTL - ttl)
            print(f"|             [{bar}] TTL={ttl}  {row['title']}")
    print("+" + "-" * 59)

    return current_loaded


def simulate_turn(db, window_id, user_msg, summary, turn_entities,
                  turn_categories, turn_num, prev_loaded):
    # on_message: recall + TTL + active entities
    # In real usage, recall extracts entities from the user message.
    # Here we also accept speaker-provided entities (turn_entities)
    # because the speaker sees full context and knows "what pattern?"
    # is still about auth-session even when the user doesn't name it.
    result = recall(user_msg, db)
    # Merge recall-extracted + speaker-provided entities
    all_entities = sorted(set(result.entities) | set(turn_entities))

    matched_topic_ids = [t["id"] for t, _score in result.topics[:5]]
    tick_ttl(db, matched_topic_ids)

    # on_stop: group into topic row (two-axis: entity + category)
    tid, action, title = group_turn(db, window_id, user_msg,
                                    all_entities, turn_categories,
                                    summary=summary)
    mark_loaded(db, tid)

    # Active entities derived from loaded topics (facts follow topics)
    active = get_entities_from_loaded_topics(db)
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)
    loaded = get_loaded_topics_with_ttl(db)

    return print_turn(turn_num, user_msg, all_entities, action, title, tid,
                       active, facts, loaded, prev_loaded, db)


def main():
    db_path = os.path.join(os.path.dirname(__file__), "ttl_demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    db = create_db(db_path)
    seed_entities_and_facts(db)
    window = create_window(db)

    print("=" * 60)
    print(" Pane Demo - TTL + hard-switch + topic grouping")
    print("=" * 60)
    print("\nSeeded entities (specific, fact-attachable):")
    print("  user, cpp, python, postgres,")
    print("  auth-session, admin-dashboard, payment-webhook")
    print(f"\nDEFAULT_TTL = {DEFAULT_TTL} turns")
    print("\nWatch:")
    print("  [NEW]      = disjoint entity set -> new domain")
    print("  [SUBTOPIC] = same entities, different categories -> new sub-thread")
    print("  [EXTEND]   = entity+category overlap OR drift -> extends current")
    print("  [SWITCH]   = entity hard-switch drops old facts, loads new")

    # Conversation: (message, summary, speaker_entities, categories).
    # Speaker entities = what the speaker would emit in turn.json (it sees
    # full context and knows the topic even when user doesn't name it).
    # Categories drive subtopic splits within the same entity domain.
    conversation = [
        # Sub-thread 1: cpp auth-session ARCHITECTURE
        ("working on auth-session refactor in cpp at acme",
         "", ["cpp", "auth-session"], ["architecture"]),
        ("what pattern should we use for session invalidation?",
         "", ["cpp", "auth-session"], ["architecture"]),
        ("do we have a test harness ready?",
         "", ["cpp", "auth-session"], ["architecture"]),
        # SUBTOPIC SHIFT: same entities, different category (architecture -> testing)
        ("lets write unit tests for the session handler",
         "auth-session architecture: picked version-counter + session store "
         "pattern, no-exceptions via result types",
         ["cpp", "auth-session"], ["testing"]),
        ("mock the token store or use a real db?",
         "", ["cpp", "auth-session"], ["testing"]),
        # DOMAIN SHIFT: new entities entirely
        ("actually let me check admin-dashboard in python",
         "auth-session testing: mock token store, 3 test cases for "
         "invalidation flow",
         ["python", "admin-dashboard"], ["frontend"]),
        ("where did we leave dark mode rollout?",
         "", ["python", "admin-dashboard"], ["frontend"]),
        # DOMAIN SHIFT: new entities
        ("now the payment-webhook postgres timeout is urgent",
         "admin-dashboard: dark mode at 60%, push to 100% next sprint",
         ["postgres", "payment-webhook"], ["database"]),
        ("query plan shows seq scan",
         "", ["postgres", "payment-webhook"], ["database"]),
        # Drift — extends current thread
        ("quiet moment", "", [], []),
        ("nothing else to report", "", [], []),
    ]

    prev_loaded = set()
    for i, (msg, summary, ents, cats) in enumerate(conversation, 1):
        prev_loaded = simulate_turn(db, window, msg, summary, ents, cats,
                                    i, prev_loaded)

    # Print final topic inventory
    print("\n" + "=" * 60)
    print(" Final topic inventory (what got stored)")
    print("=" * 60)
    rows = db.execute(
        "SELECT title, entity_fingerprint, category_fingerprint, "
        "start_message_id, end_message_id, "
        "substr(summary, 1, 60) as sum_snippet FROM topics "
        "ORDER BY start_message_id"
    ).fetchall()
    for r in rows:
        span = r["end_message_id"] - r["start_message_id"] + 1
        cats = r["category_fingerprint"] or "(none)"
        print(f"  * {r['entity_fingerprint']}  |  cats: {cats}  ({span} msgs)")
        if r["sum_snippet"]:
            print(f"    summary: {r['sum_snippet']}...")
    print(f"\n  {len(rows)} topic rows for {len(conversation)} turns")

    db.close()
    os.remove(db_path)


if __name__ == "__main__":
    main()
