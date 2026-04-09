# Pane — Results Summary

**Context window manager for LLMs. 85% token savings. Zero retrieval LLM calls.**

Built by [Waleed Abdullah](https://github.com/wabdull) | [github.com/wabdull/Pane](https://github.com/wabdull/Pane) | `pip install pane-llm`

---

## The Problem

Without context management, every turn re-sends the entire conversation history. Input cost grows **quadratically** with session length. A 200-turn conversation burns 10M+ input tokens — most of it irrelevant context the model has to attend to.

## The Solution

Pane manages the context window. Topics load on demand, decay via TTL when unreferenced, and unload automatically. Entity-scoped facts travel with their topics. The LLM emits ~100-200 tokens of metadata per turn — entities, categories, facts — which powers deterministic tag-intersection retrieval in SQLite. **Zero additional LLM calls.**

## Measured Results

**200-turn eval** with a real LLM speaker (claude-sonnet-4-6), math tutoring across 8 domains with 3 returns to prior topics:

| Metric | Without Pane | With Pane |
|---|---|---|
| Total input tokens | 10,075,256 | 1,379,092 |
| Total session cost (Sonnet) | $32.12 | $6.03 |
| Context at turn 200 | 97,769 tokens | 6,318 tokens |
| **Total savings** | — | **85% tokens, 81% cost** |

Context stays bounded at 3K-12K tokens regardless of conversation length. Without Pane, it grows linearly to 98K by turn 200.

### Savings over session length

| Turn | With Pane | Without Pane | Saving |
|---|---|---|---|
| 20 | 4,312 | 9,002 | 52% |
| 60 | 5,184 | 29,584 | 82% |
| 100 | 4,956 | 49,842 | 90% |
| 200 | 6,318 | 97,769 | 94% |

The gap only widens. A 400-turn session would cost ~$6 with Pane vs ~$120+ without.

## Quality

Domain returns work correctly. At turn 141, when the user returned to calculus after 40 turns of other topics, Pane loaded 8 topic summaries and 23 entity facts spanning BOTH a derivatives phase (turns 41-60) and an integrals phase (turns 101-120). The model referenced prior learning and built on established knowledge.

97.5% speaker metadata compliance across 200 turns. 114 unit tests. Three evaluation tiers (compliance, integration, long-session) all passing.

## How It Works

1. LLM responds normally + emits metadata (entities, categories, facts) — ~100-200 extra output tokens
2. Metadata stored in SQLite — topics grouped by entity + category fingerprint
3. Next turn: recall matches topics by tag intersection, loads summaries + facts
4. Stale topics decay via TTL (default 3 turns), unloading their entity facts
5. Subtopics (same entities, different work type) decay independently

No embeddings. No keyword extraction. No reranking. No retrieval LLM calls.

## Architecture Decisions (from ~20 research iterations)

| Decision | Evidence |
|---|---|
| Kill embeddings | Cosine similarity caps at ~46% on conversational data |
| Kill reranking | Every strategy tested reduced accuracy vs raw candidates |
| Kill LLM retrieval calls | Expensive, slow, unnecessary when speaker emits tags |
| Use speaker-emitted metadata | Free (~100-200 tokens), accurate, no post-processing |
| Fixed fingerprints for identity | Prevents entity bloat; tags grow separately for retrieval |
| Decrement-only TTL | Resets come from on_stop (knows the active subtopic), not from recall (can't distinguish subtopics with shared entities) |

## Technical Details

- **114 unit tests** covering TTL, facts, recall, grouping, lifecycle (cross-session, long-session)
- **3-tier eval framework**: Tier 1 (speaker compliance, 120/120), Tier 2 (integration, 6/6), Tier 3 (200-turn long session, 85% savings)
- **Middleware chat** with full context control (`python -m pane`) + **web UI** (`python -m pane.web`)
- **Claude Code hooks** for additive context injection
- **Published to PyPI**: `pip install pane-llm`
- **Research repo**: [github.com/wabdull/Reverie](https://github.com/wabdull/Reverie) (~20 iterations, 168 commits)

---

*Built by one person in ~3 days of focused work with a few hundred dollars of API cost.*
