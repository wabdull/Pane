# Pane

Context window manager for LLMs. Memory for free.

**114 tests** | **85% total token savings** | **$6 vs $32 for 200 turns** | **zero retrieval LLM calls**

## The Problem

LLMs forget everything between sessions. Context windows grow linearly — by turn 50, you're paying for 50 turns of input on every API call, most of it irrelevant. The cost isn't just per-turn — it's **quadratic over the session** because every turn re-sends the entire history. A 200-turn conversation without context management burns 10M+ input tokens.

Compaction (summarizing old context) is lossy AND expensive. Every existing memory system adds LLM calls for retrieval, extraction, or both.

## What Pane Does

Pane manages what's in the context window — loading relevant history when needed, unloading stale context when it's not. Nothing is ever lost. Nothing is ever summarized away.

The core insight: **the LLM is already generating a response.** Have it emit a few lines of metadata alongside that response — entities mentioned, facts learned, work type. That metadata costs ~100-200 output tokens per turn and powers a deterministic retrieval system with zero additional LLM calls.

## Measured Results (Tier 3 Eval — 200 turns, real LLM speaker)

```
                    Input        Output       Total
  Without Pane: 10,075,256      126,059   10,201,315
  With Pane:     1,379,092      126,059    1,505,151
  Saved:         8,696,164                 8,696,164

  Total savings: 85%  |  Cost: $6.03 vs $32.12 (81% reduction)
```

200 turns of math tutoring across 8 domains with 3 returns to prior topics. Context stays bounded at ~3K-12K tokens while full replay grows to 98K. Domain returns reload relevant facts from the DB — at turn 141, the model loaded facts from BOTH a derivatives phase (turn 41-60) and an integrals phase (turn 101-120) simultaneously.

Savings progression:

| Turn | Pane Context | Without Pane | Saving |
|---|---|---|---|
| 20 | 4,312 | 9,002 | 52% |
| 60 | 5,184 | 29,584 | 82% |
| 100 | 4,956 | 49,842 | 90% |
| 140 | 10,450 | 72,625 | 86% |
| 200 | 6,318 | 97,769 | 94% |

### How it works

```
User sends message
    │
    ▼
[Recall] Search memory DB by entity tags → soft-load matching topics
    │
    ▼
[TTL] Decrement all loaded topics → unload expired (TTL=0)
    │
    ▼
[Build context] User facts + active entity facts + loaded topic summaries
    │
    ▼
LLM responds + emits metadata (entities, categories, facts)
    │
    ▼
[Group] Two-axis fingerprint check → extend or new topic
    │
    ▼
[Store] Save facts, tags, messages → mark active topic loaded (TTL reset)
```

### What loads, what unloads

| Layer | Load trigger | Unload trigger |
|---|---|---|
| User facts (name, role, prefs) | Always loaded | Never |
| Entity facts (cpp.exceptions, postgres.downtime) | Entity's topic loaded | All topics with that entity decay to TTL 0 |
| Topic summaries | Entity tag match on user message | TTL countdown (default 3 unreferenced turns) |
| Raw messages | On demand (summary unavailable) | Topic unloads |

### Two-axis topic grouping

Topics are grouped by **entity fingerprint** (what you're working on) and **category fingerprint** (what type of work). Fingerprints are **fixed at creation** — they represent the topic's identity. Tags grow cumulatively for retrieval.

```
auth-session,cpp  | architecture   ← designing the system
auth-session,cpp  | testing        ← writing tests (SUBTOPIC SPLIT)
admin-dashboard   | debugging      ← different domain entirely (DOMAIN SHIFT)
```

Same entities + same categories → extend. Same entities + different categories → subtopic split. Different entities → new topic. Each subtopic decays independently via its own TTL.

Overlap threshold (50%) prevents related-but-different domains (trig → geometry) from merging via a single shared entity.

### Middleware chat + web UI

```bash
# Terminal REPL
python -m pane --db math.db --system "You are a math tutor"

# Web UI with live context panel
python -m pane.web --db math.db --port 3000
```

The middleware gives Pane full control of the context window — not additive-only like hooks. Every turn shows exact API token counts, cache hits, loaded topics, and savings vs full replay.

## What's Killed (and why)

| Approach | Finding |
|---|---|
| Embeddings | Proven ceiling — recurring events embed identically, cosine can't distinguish instances |
| Keyword extraction | Noisy — stems from code blocks pollute results |
| LLM retrieval calls | Expensive, slow, adds latency per turn |
| Reranking | Always hurts — narrowing candidates then reranking loses the right answer more than it promotes it |
| Compaction | Lossy AND expensive — clear and reload from DB instead |

## What's Kept (and why)

| Approach | Rationale |
|---|---|
| Entity + category tags | Speaker emits them for free alongside response generation. High precision. |
| Entity-scoped facts | Instant lookup. `cpp.exceptions = "disallowed"` loads when any cpp topic is active. |
| Summaries on topic resolution | 50 tokens replaces 15K of raw conversation. Speaker emits at transition time. |
| TTL-based topic management | Context stays focused automatically. No manual "forget this" needed. |
| Two-axis subtopic grouping | Entity fingerprint (fixed identity) + category fingerprint. Independent decay. |
| Fixed fingerprints + tag growth | Fingerprint = identity (for grouping). Tags = content (for retrieval). No bloat. |

## Testing

**114 unit tests** across five modules:
- TTL mechanics (13 tests) — decrement-only model, mark_loaded resets
- Entity facts (20 tests) — CRUD, scoping, domain-switch scenarios
- Recall + context loading (23 tests) — entity extraction, tag intersection, ranking
- Topic grouping (31 tests) — fingerprints, extension, subtopic splits, overlap threshold
- **Lifecycle (27 tests)** — full load/unload cycle, cross-session reload, subtopic independent decay, long-session savings (50 turns: 82% savings, 100 turns: >50% savings), fixed fingerprint behavior

**Tier 1 eval** — speaker compliance (120/120 checks):
8 checks per turn × 15 turns: JSON validity, required fields, entity specificity, category discipline (work types not domain nouns), fact format, summary scaling.

**Tier 2 eval** — integration (6/6 checks):
10-turn conversation through real grouping pipeline with live LLM. Validates subtopic splits, summary attribution, multi-domain separation.

**Tier 3 eval** — 200-turn long session:
Full math tutoring journey across 8 domains with returns. Measures total API cost, context bounding, cross-session reload quality. **85% total token savings, 81% cost reduction.**

All tiers run via `python evals/run.py`. Tier 1 gates Tier 2. Tier 3 runs separately.

## Quick Start

### Middleware (full context control)

```bash
pip install pane-llm[web]

# Terminal
python -m pane --db my.db --system "You are a helpful assistant"

# Web UI
python -m pane.web --db my.db --port 3000
```

### Claude Code hooks (additive context injection)

Copy `examples/claude-code/` into your project:

```
your-project/
  .claude/
    settings.json          ← hook configuration
    hooks/
      on_message.py        ← recall + TTL + entity fact loading
      on_stop.py           ← topic grouping + fact storage
      on_compact.py        ← re-injects memory after compaction
    memory/                ← runtime data (DB, stats, log)
  CLAUDE.md                ← speaker metadata instructions
```

Inspect the DB anytime:
```bash
python scripts/inspect_db.py .claude/memory/pane.db --facts --stats
```

## Research

Built through systematic experimentation (~20 architecture iterations) across retrieval approaches. Each version had a thesis, an experiment, and a quantitative result. Research repo: [github.com/wabdull/Reverie](https://github.com/wabdull/Reverie)

Key findings:

- **Model quality > retrieval algorithm.** A better LLM reading raw content beats clever retrieval with a weaker model.
- **Embeddings have a structural ceiling.** Recurring events embed identically. Cosine similarity caps retrieval at ~46%.
- **Reranking always hurts.** Every strategy tested reduced accuracy vs. the raw candidate set.
- **Entity extraction at the wrong abstraction level hurts.** Batch-extracted entities performed worse than keyword-only baselines (58.4% vs 64.2%).
- **The speaker already understands.** Capturing metadata during response generation is free and accurate. Extracting after the fact is expensive and lossy.
- **Pane's value is cumulative, not per-turn.** Without context management, input cost grows quadratically with session length. With Pane, it's bounded. The longer the session, the bigger the gap.

## Architecture

<img src="docs/architecture.svg" alt="Pane Architecture" width="100%">

## License

MIT
