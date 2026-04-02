"""Recall — tag intersection retrieval, zero LLM calls.

Two modes, checked in order:
  1. Fact lookup: exact match against fact store
  2. Topic search: entity + category tag intersection

No keywords. No embeddings. No LLM calls.
Tags come from what the speaker emitted during conversation.
"""

import re
from pane.types import RecallResult
from pane.schema import get_facts, get_all_topics, get_topic_messages, get_topics_by_tags, build_alias_map


def extract_entities(query, alias_map):
    """Find entity mentions in query using the alias registry.
    Checks longest aliases first to match "dr. chen" before "chen".
    """
    query_lower = query.lower()
    found = set()
    for alias in sorted(alias_map.keys(), key=len, reverse=True):
        if len(alias) > 1 and alias in query_lower:
            found.add(alias_map[alias])
    return found


def recall(query, db):
    """Search memory. Checks facts first, then topics.
    Returns RecallResult.
    """
    # Mode 1: Fact lookup
    fact_result = _check_facts(query, db)
    if fact_result:
        return RecallResult(mode="fact", answer=fact_result, n_results=1)

    # Mode 2: Topic search
    topic_results = _search_topics(query, db)
    if topic_results:
        return RecallResult(mode="topic", topics=topic_results, n_results=len(topic_results))

    return RecallResult(mode="not_found")


def _check_facts(query, db):
    """Check if query matches a stored fact."""
    facts = get_facts(db)
    if not facts:
        return None
    query_lower = query.lower()
    for fact_name, fact_value in facts.items():
        if re.search(r'\b' + re.escape(fact_name) + r'\b', query_lower):
            return f"{fact_name}: {fact_value}"
    return None


def _search_topics(query, db):
    """Tag intersection search. Returns list of (topic_dict, score)."""
    alias_map = build_alias_map(db)
    query_entities = extract_entities(query, alias_map)

    if not query_entities:
        return []

    # Build tags to search for
    tags = [f"entity:{e}" for e in query_entities]

    # Also search categories — if "health" is an entity, check category tags too
    tags.extend(f"cat:{e}" for e in query_entities)

    candidates = get_topics_by_tags(db, tags)
    if not candidates:
        return []

    all_topics = {t["id"]: t for t in get_all_topics(db)}
    results = []
    for c in candidates:
        topic = all_topics.get(c["topic_id"])
        if not topic:
            continue
        score = c["match_count"]
        results.append((topic, score))

    results.sort(key=lambda x: -x[1])
    return results


def load_context(topic_ids, db, max_tokens=30000, use_summary=True):
    """Load topics as formatted context.

    Default: loads summaries (compact). Set use_summary=False for raw messages.
    """
    blocks = []
    tokens_used = 0

    for tid in topic_ids:
        topic = db.execute("SELECT title, summary FROM topics WHERE id = ?", (tid,)).fetchone()
        if not topic:
            continue

        if use_summary and topic["summary"]:
            block = f"[{topic['title']}] {topic['summary']}"
        else:
            messages = get_topic_messages(db, tid)
            if not messages:
                continue
            lines = [f"--- {topic['title']} ---"]
            for m in messages:
                role = "[User]" if m["role"] == "user" else "[AI]"
                lines.append(f"{role} {m['content']}")
            block = "\n".join(lines)

        block_tokens = len(block) // 4
        if max_tokens is not None and tokens_used + block_tokens > max_tokens:
            break

        blocks.append(block)
        tokens_used += block_tokens

    return "\n\n".join(blocks)
