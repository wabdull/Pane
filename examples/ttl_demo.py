"""Pane demo - two parallel tracks.

  1. Topics (conversational history) -> TTL-managed, tolerate drift
  2. Entity facts (domain rules)     -> hard-switch, replace on mention

Scenario: a developer juggling three work threads across three domains.
Watch topics decay via TTL while domain facts hard-switch on mention.

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
    get_active_entities,
    get_facts_for_entities,
    get_loaded_topics_with_ttl,
    save_entity,
    save_entity_fact,
    save_messages,
    save_topic,
    set_active_entities,
    tick_ttl,
)


def seed_db(db):
    """Create topics, entities, and entity-scoped facts."""
    window = create_window(db)

    # Register entities. Entities are specific, fact-attachable identifiers.
    # Generic nouns like "dashboard" or "session" go in categories, not here.
    entity_defs = [
        # Tools (domains)
        ("cpp", "tool", ["cpp", "c++"]),
        ("python", "tool", ["python"]),
        ("postgres", "tool", ["postgres", "postgresql"]),
        # Specific things (compound names make them fact-attachable)
        ("auth-session", "project", ["auth-session", "auth session"]),
        ("admin-dashboard", "project", ["admin-dashboard", "admin dashboard"]),
        ("payment-webhook", "project", ["payment-webhook", "payment webhook"]),
    ]
    for name, etype, aliases in entity_defs:
        save_entity(db, name, entity_type=etype, aliases=aliases)

    # User-scoped facts (always loaded)
    save_entity_fact(db, USER_ENTITY, "name", "Waleed")
    save_entity_fact(db, USER_ENTITY, "role", "staff engineer")
    save_entity_fact(db, USER_ENTITY, "timezone", "PST")

    # Domain-scoped facts (hard-switch on mention)
    save_entity_fact(db, "cpp", "exceptions", "disallowed at acme")
    save_entity_fact(db, "cpp", "standard", "c++20")
    save_entity_fact(db, "cpp", "style", "snake_case members")

    save_entity_fact(db, "python", "version", "3.13")
    save_entity_fact(db, "python", "linter", "ruff")
    save_entity_fact(db, "python", "tests", "pytest")

    save_entity_fact(db, "postgres", "version", "17.2")
    save_entity_fact(db, "postgres", "prod_rule", "no ALTER without pg_repack")
    save_entity_fact(db, "postgres", "downtime", "Sun 2-4am UTC")

    # Specific-project facts (attached to the compound-name entities)
    save_entity_fact(db, "auth-session", "status", "blocked on security review")
    save_entity_fact(db, "auth-session", "owner", "waleed")

    save_entity_fact(db, "admin-dashboard", "route", "/admin")
    save_entity_fact(db, "admin-dashboard", "rollout", "60% of tenants")

    save_entity_fact(db, "payment-webhook", "endpoint", "/webhooks/payment")
    save_entity_fact(db, "payment-webhook", "blocker", "missing index")

    # Pre-existing topic conversations
    # Categories (cat:) are broad themes; entities (entity:) are specific.
    topics = [
        {
            "title": "Auth session refactor",
            "summary": "Moving auth-session handling to cpp service. Blocked on review.",
            "tags": ["entity:cpp", "entity:auth-session", "cat:backend", "cat:auth"],
            "content": "We're rewriting auth-session in the cpp service.",
        },
        {
            "title": "Admin dashboard dark mode",
            "summary": "Python/React work. Dark mode rollout 60% complete.",
            "tags": ["entity:python", "entity:admin-dashboard", "cat:frontend", "cat:dashboard"],
            "content": "admin-dashboard dark mode shipped for 60% of tenants.",
        },
        {
            "title": "Payment webhook timeout",
            "summary": "Postgres query taking 8s on payment-webhook. Needs index.",
            "tags": ["entity:postgres", "entity:payment-webhook", "cat:backend", "cat:webhook"],
            "content": "The payment-webhook postgres query times out at 8s.",
        },
    ]

    for t in topics:
        saved = save_messages(
            db, window, [{"role": "user", "content": t["content"]}]
        )
        mid = saved[0][0]
        save_topic(db, window, t["title"], mid, mid,
                   summary=t["summary"], tags=t["tags"])


def print_window(turn_num, user_msg, active, facts, loaded_topics, db,
                 prev_loaded_entities):
    """Render the context window for this turn."""
    # Active entities that actually have facts (ignore user, already always-loaded)
    current_loaded = {e for e in active if e in facts and e != USER_ENTITY}
    dropped = prev_loaded_entities - current_loaded
    added = current_loaded - prev_loaded_entities
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
    print("|")

    # User facts (always loaded)
    user_facts = facts.get(USER_ENTITY, [])
    print(f"| [ALWAYS]  user: "
          f"{', '.join(f'{k}={v}' for k, v in user_facts)}")

    # Entity facts (hard-switch)
    entity_facts_loaded = {
        name: kvs for name, kvs in facts.items()
        if name != USER_ENTITY
    }
    if entity_facts_loaded:
        for name in sorted(entity_facts_loaded):
            print(f"| [ACTIVE]  {name}:")
            for key, value in entity_facts_loaded[name]:
                print(f"|             {key}: {value}")
    else:
        print("| [ACTIVE]  (no entity facts — no specific entity named yet)")
    print("|")

    # Topics (TTL)
    if not loaded_topics:
        print("| [TOPICS]  (window empty)")
    else:
        print("| [TOPICS]  (TTL bar)")
        for tid, ttl in loaded_topics:
            row = db.execute(
                "SELECT title FROM topics WHERE id = ?", (tid,)
            ).fetchone()
            title = row["title"] if row else tid[:8]
            bar = "#" * ttl + "." * (DEFAULT_TTL - ttl)
            print(f"|             [{bar}] TTL={ttl}  {title}")
    print("+" + "-" * 59)

    return current_loaded


def simulate_turn(db, user_msg, turn_num, prev_loaded_entities):
    """Run one turn: recall -> tick_ttl -> set_active_entities -> render."""
    result = recall(user_msg, db)

    # Topic TTL track (tolerates drift)
    matched_topic_ids = [t["id"] for t, _score in result.topics[:5]]
    tick_ttl(db, matched_topic_ids)

    # Entity hard-switch track (sticky with replacement)
    set_active_entities(db, result.entities)

    active = get_active_entities(db)
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)
    loaded_topics = get_loaded_topics_with_ttl(db)

    return print_window(turn_num, user_msg, active, facts, loaded_topics, db,
                        prev_loaded_entities)


def main():
    db_path = os.path.join(os.path.dirname(__file__), "ttl_demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    db = create_db(db_path)
    seed_db(db)

    print("=" * 60)
    print(" Pane Demo - Topics (TTL) + Entity Facts (hard-switch)")
    print("=" * 60)
    print("\nSeeded entities (specific, fact-attachable):")
    print("  user            - name, role, timezone  (always loaded)")
    print("  cpp             - exceptions, standard, style")
    print("  python          - version, linter, tests")
    print("  postgres        - version, prod_rule, downtime")
    print("  auth-session    - status, owner")
    print("  admin-dashboard - route, rollout")
    print("  payment-webhook - endpoint, blocker")
    print("\nCategories (broad, retrieval only, no facts):")
    print("  backend, frontend, auth, dashboard, webhook")
    print("\n3 topics: Auth session refactor / Admin dashboard dark mode /")
    print("          Payment webhook timeout")
    print(f"\nDEFAULT_TTL = {DEFAULT_TTL} turns")
    print("\nWatch: [SWITCH] markers show entity hard-switches.")
    print("       User facts stay loaded the entire time.")
    print("       Entity facts drop IMMEDIATELY when another entity is named.")

    conversation = [
        "Where did we leave the auth-session refactor in cpp?",    # cpp + auth-session
        "What pattern did we settle on?",                          # drift, sticky
        "Do we have a test for it?",                               # drift
        "Actually let me check the admin-dashboard in python.",    # HARD SWITCH
        "I forget where we left off on it.",                       # drift, sticky
        "Circling back - rewrite this cpp auth-session logic.",    # HARD SWITCH back
        "Handlers need snake_case right?",                         # drift, sticky
        "Let me look at the payment-webhook postgres timeout.",    # HARD SWITCH
        "The query plan looks expensive.",                         # drift, sticky
        "Need to add an index during the downtime window.",        # drift
        "Quiet moment.",                                           # drift
        "Nothing to report.",                                      # drift
    ]

    prev_loaded_entities = set()
    for i, msg in enumerate(conversation, 1):
        prev_loaded_entities = simulate_turn(db, msg, i, prev_loaded_entities)

    print("\n" + "=" * 60)
    print(" End of demo")
    print("=" * 60)

    db.close()
    os.remove(db_path)


if __name__ == "__main__":
    main()
