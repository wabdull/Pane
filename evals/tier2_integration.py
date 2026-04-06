"""Tier 2 integration test — end-to-end subtopic grouping with a real speaker.

Runs a scripted conversation against the Anthropic API, feeds each turn's
metadata through the actual Pane grouping pipeline, then inspects the
resulting DB for correct behavior:

  1. Topic rows created (not 1 monolith, not 1 per turn)
  2. At least one subtopic split (same entity fingerprint, different categories)
  3. Summaries on closed topics
  4. Entity facts derived from loaded topics

This is the test that proves subtopics WORK end-to-end — not just that
the speaker emits valid JSON (Tier 1) or that the demo runs with hand-fed
data, but that a real speaker's natural categorization triggers the right
grouping behavior.

Setup:
    pip install -e ".[evals]"
    # put your key in .env

Run:
    python evals/tier2_integration.py
    python evals/tier2_integration.py --model claude-opus-4-6
    python evals/tier2_integration.py --save evals/results/tier2_run1.json
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: anthropic SDK not installed. Run: pip install -e '.[evals]'")
    sys.exit(1)

from pane.schema import (
    DEFAULT_TTL,
    USER_ENTITY,
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    get_all_topics,
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
from pane.recall import recall

CLAUDE_MD_PATH = REPO_ROOT / "examples" / "claude-code" / "CLAUDE.md"
CLAUDE_MD = CLAUDE_MD_PATH.read_text(encoding="utf-8")

SYSTEM_PROMPT = CLAUDE_MD + """

---
TEST ENVIRONMENT NOTE:
You do not have the Write tool. Include turn.json inline at the END of
every response inside a fenced code block labeled `turn.json`:

```turn.json
{
  "entities": [...],
  "categories": [...],
  "facts": [...],
  "summary": "...",
  "tools_used": []
}
```

Always include this block, every turn, no exceptions.
"""

# Conversation designed to trigger:
#   Turns 1-3: same entities + same categories -> EXTEND
#   Turn 4:    same entities + different categories -> SUBTOPIC SPLIT
#   Turn 5:    extend the new subtopic
#   Turn 6:    different entities entirely -> DOMAIN SHIFT
#   Turn 7:    extend
#   Turn 8:    different entities -> DOMAIN SHIFT again
#   Turn 9:    extend
#   Turn 10:   drift
CONVERSATION = [
    # Sub-thread 1: cpp auth-session ARCHITECTURE
    "i'm working on the auth-session refactor in cpp. what invalidation pattern should we use?",
    "lets go with token versioning. how should we structure the session store?",
    "ok, and error handling? we can't use exceptions at this company.",
    # SUBTOPIC SHIFT: same entities, shift to TESTING
    "alright architecture is settled. now lets write tests for the session handler.",
    "should we mock the token store or use a real db for integration tests?",
    # DOMAIN SHIFT: different entities
    "switching gears — the admin-dashboard in python needs dark mode pushed to 100%.",
    "where did we leave the rollout? any blockers?",
    # DOMAIN SHIFT: different entities again
    "now the payment-webhook postgres query is timing out. lets look at the query plan.",
    "we need an index but can only do schema changes during the downtime window.",
    # DRIFT
    "ok, that covers everything for today.",
]


def extract_turn_json(text):
    m = re.search(r"```turn\.json\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def process_turn(db, window_id, user_msg, assistant_msg, metadata):
    """Simulate the on_stop pipeline: save messages, group topic, save facts."""
    entities = metadata.get("entities", [])
    categories = metadata.get("categories", [])
    facts = metadata.get("facts", [])
    summary = metadata.get("summary", "")

    current_entities = [(e or "").lower().strip() for e in entities if (e or "").strip()]
    current_categories = [(c or "").lower().strip() for c in categories if (c or "").strip()]
    current_ent_set = set(current_entities)
    current_cat_set = set(current_categories)

    # Save messages
    messages = []
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    if assistant_msg:
        messages.append({"role": "assistant", "content": assistant_msg})
    if not messages:
        messages.append({"role": "assistant", "content": "[turn]"})
    saved = save_messages(db, window_id, messages)
    new_start, new_end = saved[0][0], saved[-1][0]

    tags = [f"entity:{e}" for e in current_entities]
    tags += [f"cat:{c}" for c in current_categories]

    # Entity registry
    for ent in current_entities:
        if len(ent) > 1:
            save_entity(db, ent, entity_type="unknown", aliases=[ent])
    for cat in current_categories:
        if cat and len(cat) > 1:
            save_entity(db, cat, entity_type="category", aliases=[cat])

    # Two-axis grouping (mirrors on_stop.py)
    most_recent = get_most_recent_topic(db)
    is_drift = not current_ent_set and not current_cat_set

    if most_recent is None:
        title = entity_fingerprint(current_ent_set) or "general"
        topic_id = save_topic(db, window_id, title=title,
                              start_message_id=new_start, end_message_id=new_end,
                              tags=tags, entities=current_entities,
                              categories=current_categories)
        action = "new"
    elif is_drift:
        extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                     new_tags=tags)
        topic_id = most_recent["id"]
        action = "extend"
    else:
        prior_ent = parse_fingerprint(most_recent["entity_fingerprint"])
        prior_cat = parse_fingerprint(most_recent["category_fingerprint"])
        ent_continues = not current_ent_set or bool(current_ent_set & prior_ent)
        cat_continues = not current_cat_set or bool(current_cat_set & prior_cat)

        if ent_continues and cat_continues:
            merged_title = entity_fingerprint(prior_ent | current_ent_set) or \
                           most_recent["title"]
            extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                         new_entities=list(current_ent_set),
                         new_categories=list(current_cat_set),
                         new_tags=tags, new_title=merged_title)
            topic_id = most_recent["id"]
            action = "extend"
        else:
            if summary:
                set_topic_summary(db, most_recent["id"], summary)
            title = entity_fingerprint(current_ent_set) or "general"
            topic_id = save_topic(db, window_id, title=title,
                                  start_message_id=new_start, end_message_id=new_end,
                                  tags=tags, entities=current_entities,
                                  categories=current_categories)
            action = "subtopic" if ent_continues else "new"

    # Facts
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        entity_name = (fact.get("entity") or USER_ENTITY).strip().lower()
        key = (fact.get("key") or "").strip()
        value = (fact.get("value") or "").strip()
        if entity_name and key and value:
            save_entity_fact(db, entity_name, key, value)
            if entity_name != USER_ENTITY:
                save_entity(db, entity_name, entity_type="unknown",
                            aliases=[entity_name])

    mark_loaded(db, topic_id)
    return topic_id, action


def run_conversation(model, verbose):
    client = Anthropic()
    messages = []

    # Set up DB
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    db = create_db(db_path)
    window_id = create_window(db)

    print(f"Tier 2 integration test  |  model: {model}  |  {len(CONVERSATION)} turns")
    print("=" * 72)

    turn_log = []

    for i, user_msg in enumerate(CONVERSATION, 1):
        messages.append({"role": "user", "content": user_msg})

        try:
            response = client.messages.create(
                model=model,
                system=SYSTEM_PROMPT,
                messages=messages,
                max_tokens=2000,
            )
        except Exception as e:
            print(f"\n[Turn {i}] API ERROR: {e}")
            break

        assistant_text = response.content[0].text
        messages.append({"role": "assistant", "content": assistant_text})

        metadata = extract_turn_json(assistant_text)
        if metadata is None:
            print(f"\n[Turn {i}] WARN: no turn.json in response")
            turn_log.append({"turn": i, "msg": user_msg, "metadata": None,
                             "action": "skip"})
            continue

        # Simulate on_message: recall + tick_ttl
        result = recall(user_msg, db)
        matched = [t["id"] for t, _ in result.topics[:5]]
        tick_ttl(db, matched)

        # Process through pipeline
        topic_id, action = process_turn(db, window_id, user_msg,
                                        assistant_text, metadata)

        ent_fp = metadata.get("entities", [])
        cat_list = metadata.get("categories", [])

        print(f"  [Turn {i:2d}] {action:8s}  entities: {ent_fp}  "
              f"categories: {cat_list}")

        if verbose and metadata:
            print(f"           {json.dumps(metadata, indent=2)[:200]}...")

        turn_log.append({
            "turn": i, "msg": user_msg, "action": action,
            "entities": ent_fp, "categories": cat_list,
            "summary_emitted": bool(metadata.get("summary")),
            "topic_id": topic_id,
            "usage": {"input": response.usage.input_tokens,
                      "output": response.usage.output_tokens},
        })

    # ── Post-run DB inspection ────────────────────────────────
    print("\n" + "=" * 72)
    print("DB INSPECTION")
    print("=" * 72)

    all_topics = get_all_topics(db)
    loaded = get_loaded_topics_with_ttl(db)
    active_ents = get_entities_from_loaded_topics(db)

    print(f"\nTopic rows: {len(all_topics)}")
    for t in all_topics:
        cat_fp = t.get("category_fingerprint", "")
        span = t["end_message_id"] - t["start_message_id"] + 1
        has_sum = "yes" if t.get("summary") else "no"
        print(f"  {t['entity_fingerprint']:30s} | cat: {cat_fp:25s} "
              f"| {span:2d} msgs | summary: {has_sum}")

    print(f"\nLoaded topics: {len(loaded)}")
    for tid, ttl in loaded:
        t = db.execute("SELECT title, entity_fingerprint, category_fingerprint "
                       "FROM topics WHERE id = ?", (tid,)).fetchone()
        print(f"  TTL={ttl}  {t['entity_fingerprint']} | {t['category_fingerprint']}")

    print(f"\nDerived active entities: {active_ents}")

    # ── Validation checks ─────────────────────────────────────
    print("\n" + "=" * 72)
    print("VALIDATION")
    print("=" * 72)

    checks = []

    # 1. Topic count: not 1 monolith, not 1-per-turn
    n = len(all_topics)
    ok = 2 <= n <= len(CONVERSATION) - 1
    checks.append(("topic_count_reasonable",
                    ok,
                    f"{n} rows for {len(CONVERSATION)} turns"
                    + (" (too few or too many)" if not ok else "")))

    # 2. At least one subtopic split: same entity_fp, different category_fp
    fingerprints = [(t["entity_fingerprint"], t.get("category_fingerprint", ""))
                    for t in all_topics]
    subtopic_found = False
    for i_t in range(len(fingerprints)):
        for j_t in range(i_t + 1, len(fingerprints)):
            if (fingerprints[i_t][0] == fingerprints[j_t][0] and
                    fingerprints[i_t][0] != "" and
                    fingerprints[i_t][1] != fingerprints[j_t][1]):
                subtopic_found = True
                break
    checks.append(("subtopic_split_detected",
                    subtopic_found,
                    None if subtopic_found else
                    "no two rows share entity_fp with different category_fp"))

    # 3. Closed topics have summaries (all except the last row)
    closed = all_topics[:-1]
    closed_with_summary = [t for t in closed if t.get("summary")]
    ok = len(closed_with_summary) >= len(closed) * 0.5  # at least half
    checks.append(("closed_topics_have_summaries",
                    ok,
                    f"{len(closed_with_summary)}/{len(closed)} closed topics "
                    f"have summaries"))

    # 4. Multiple distinct entity fingerprints (proves domain shifts happened)
    distinct_ent_fps = {t["entity_fingerprint"] for t in all_topics if t["entity_fingerprint"]}
    ok = len(distinct_ent_fps) >= 2
    checks.append(("multiple_domains",
                    ok,
                    f"{len(distinct_ent_fps)} distinct entity fingerprints: "
                    f"{sorted(distinct_ent_fps)}"))

    # 5. Active entities derived from loaded topics are non-empty
    checks.append(("active_entities_derived",
                    len(active_ents) > 0,
                    f"derived: {active_ents}" if active_ents else "empty"))

    # 6. Last topic (still open) has NO summary
    last = all_topics[-1] if all_topics else None
    if last:
        ok = not last.get("summary")
        checks.append(("last_topic_open",
                        ok,
                        "last topic has summary (should be open)"
                        if not ok else None))

    print()
    passed = 0
    for name, ok, detail in checks:
        mark = " OK " if ok else "MISS"
        msg = f"  -- {detail}" if detail else ""
        print(f"  {mark}  {name}{msg}")
        if ok:
            passed += 1

    total = len(checks)
    print(f"\n  {passed}/{total} checks passed")

    # Token usage
    in_tok = sum(t["usage"]["input"] for t in turn_log if "usage" in t)
    out_tok = sum(t["usage"]["output"] for t in turn_log if "usage" in t)
    print(f"  tokens: {in_tok} in / {out_tok} out (total {in_tok + out_tok})")

    db.close()
    os.remove(db_path)

    return {
        "turns": turn_log,
        "topics": [dict(t) for t in all_topics],
        "checks": [(n, o, d) for n, o, d in checks],
        "tokens": {"input": in_tok, "output": out_tok},
    }


def main():
    parser = argparse.ArgumentParser(
        description="Tier 2 integration test: subtopic grouping with real speaker")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--save", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Put your key in .env")
        sys.exit(1)

    results = run_conversation(args.model, args.verbose)

    if args.save:
        out_path = Path(args.save)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
