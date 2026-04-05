"""Pane demo — three tracks in action.

  1. Topics (conversational history) -> TTL-managed, tolerate drift
  2. Entity facts (domain rules)     -> hard-switch, replace on mention
  3. Topic grouping                  -> consecutive turns with overlapping
                                        entities extend one topic row; disjoint
                                        entities open a new row.

Each turn, we simulate the on_message + on_stop hook pipeline:
  recall -> tick_ttl -> set_active_entities -> group_into_topic.

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
    get_active_entities,
    get_facts_for_entities,
    get_loaded_topics_with_ttl,
    get_most_recent_topic,
    mark_loaded,
    parse_fingerprint,
    save_entity,
    save_entity_fact,
    save_messages,
    save_topic,
    set_active_entities,
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


def group_turn(db, window_id, user_msg, extracted_entities, summary=""):
    """Simulate on_stop's grouping logic. Returns (topic_id, action, title)."""
    # Save the message(s) for this turn
    saved = save_messages(db, window_id, [{"role": "user", "content": user_msg}])
    new_start, new_end = saved[0][0], saved[-1][0]

    current_set = set(extracted_entities)
    tags = [f"entity:{e}" for e in current_set]

    most_recent = get_most_recent_topic(db)

    if most_recent is None:
        title = entity_fingerprint(current_set) or "general"
        tid = save_topic(db, window_id, title=title,
                          start_message_id=new_start, end_message_id=new_end,
                          tags=tags, entities=list(current_set))
        return tid, "NEW", title

    prior_set = parse_fingerprint(most_recent["entity_fingerprint"])
    overlap = bool(current_set & prior_set)

    if not current_set or overlap:
        merged = prior_set | current_set
        new_title = entity_fingerprint(merged) or most_recent["title"]
        extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                      new_entities=list(current_set), new_tags=tags,
                      new_title=new_title)
        return most_recent["id"], "EXTEND", new_title
    else:
        # Topic transition — close prior with summary, open new
        if summary:
            set_topic_summary(db, most_recent["id"], summary)
        title = entity_fingerprint(current_set) or "general"
        tid = save_topic(db, window_id, title=title,
                          start_message_id=new_start, end_message_id=new_end,
                          tags=tags, entities=list(current_set))
        return tid, "NEW", title


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


def simulate_turn(db, window_id, user_msg, summary, turn_num, prev_loaded):
    # on_message: recall + TTL + active entities
    result = recall(user_msg, db)
    matched_topic_ids = [t["id"] for t, _score in result.topics[:5]]
    tick_ttl(db, matched_topic_ids)
    set_active_entities(db, result.entities)

    # on_stop: group into topic row
    tid, action, title = group_turn(db, window_id, user_msg,
                                      result.entities, summary=summary)
    mark_loaded(db, tid)

    active = get_active_entities(db)
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)
    loaded = get_loaded_topics_with_ttl(db)

    return print_turn(turn_num, user_msg, result.entities, action, title, tid,
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
    print("  [NEW]    = first turn or disjoint entity set -> new topic row")
    print("  [EXTEND] = entity overlap OR drift turn -> extends prior topic")
    print("  [SWITCH] = entity hard-switch drops old facts, loads new")

    # Conversation (message, summary).
    # Summary is only emitted on the TRANSITION turn — the one where the
    # speaker is starting a new disjoint work area. It describes the PRIOR
    # thread being closed.
    conversation = [
        # Thread 1 — cpp + auth-session (extends through drift)
        ("working on auth-session refactor in cpp at acme", ""),
        ("what pattern should we use for session invalidation?", ""),
        ("do we have a test harness ready?", ""),
        ("ok proceed with the redesign", ""),
        # HARD SWITCH -> new topic (python + admin-dashboard).
        # Summary closes Thread 1.
        ("actually let me check admin-dashboard in python first",
         "auth-session refactor in cpp: picked version-counter + session "
         "store pattern, no-exceptions policy applied via result types"),
        ("where did we leave dark mode rollout?", ""),
        ("push it to 100% tenants next sprint", ""),
        # HARD SWITCH -> new topic. Summary closes Thread 2.
        ("now the payment-webhook postgres timeout is urgent",
         "admin-dashboard: confirmed dark mode rollout push to 100% "
         "planned for next sprint"),
        ("query plan shows seq scan", ""),
        ("add the index during sunday downtime window", ""),
        # Drift — extends Thread 3
        ("quiet moment", ""),
        ("nothing else to report", ""),
    ]

    prev_loaded = set()
    for i, (msg, summary) in enumerate(conversation, 1):
        prev_loaded = simulate_turn(db, window, msg, summary, i, prev_loaded)

    # Print final topic inventory
    print("\n" + "=" * 60)
    print(" Final topic inventory (what got stored)")
    print("=" * 60)
    rows = db.execute(
        "SELECT title, entity_fingerprint, start_message_id, end_message_id, "
        "substr(summary, 1, 60) as sum_snippet FROM topics "
        "ORDER BY start_message_id"
    ).fetchall()
    for r in rows:
        span = r["end_message_id"] - r["start_message_id"] + 1
        print(f"  * {r['entity_fingerprint']}  (spans {span} messages)")
        if r["sum_snippet"]:
            print(f"    summary: {r['sum_snippet']}...")
    print(f"\n  {len(rows)} topic rows for {len(conversation)} turns")

    db.close()
    os.remove(db_path)


if __name__ == "__main__":
    main()
