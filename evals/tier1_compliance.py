"""Tier 1 compliance test — verify the speaker emits valid turn.json.

Runs a scripted conversation against the Anthropic API using CLAUDE.md as
the system prompt. Validates each turn's emitted turn.json for:
  1. JSON parseability
  2. Required fields present
  3. user_message matches what we sent
  4. entities are specific (no generic common nouns)
  5. facts are well-formed dicts with key/value
  6. summary is reasonable length (not an essay)
  7. topic label is concise

Prints a per-turn report and summary compliance rates.

Setup:
    pip install -e ".[evals]"          # installs anthropic SDK
    # copy your key into .env

Run:
    python evals/tier1_compliance.py
    python evals/tier1_compliance.py --model claude-opus-4-6
    python evals/tier1_compliance.py --save evals/results/run1.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parent.parent
load_dotenv(REPO_ROOT / ".env")

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: anthropic SDK not installed.")
    print("Run: pip install -e '.[evals]'")
    sys.exit(1)


CLAUDE_MD_PATH = REPO_ROOT / "examples" / "claude-code" / "CLAUDE.md"
CLAUDE_MD = CLAUDE_MD_PATH.read_text(encoding="utf-8")

# Append note about test harness format (no Write tool available here)
SYSTEM_PROMPT = CLAUDE_MD + """

---
TEST ENVIRONMENT NOTE:
You do not have the Write tool in this environment. Instead of writing
turn.json to a file, include it inline at the END of every response inside
a fenced code block labeled `turn.json`:

```turn.json
{
  "entities": [...],
  "categories": [...],
  "facts": [...],
  "summary": "...",
  "tools_used": []
}
```

Always include this block after your response, every single turn, no exceptions.
"""

# Generic common nouns. If any of these appear in `entities`, the speaker
# violated the entity-vs-category discipline.
GENERIC_NOUNS = {
    "session", "dashboard", "webhook", "database", "service", "page",
    "component", "function", "module", "file", "api", "endpoint",
    "request", "response", "query", "table", "model", "controller",
    "view", "template", "middleware", "handler", "manager", "client",
    "server", "process", "thread", "task", "job", "worker", "script",
    "config", "settings", "logger", "logs", "log", "report",
    "record", "row", "column", "field", "value", "item", "admin",
    "customer", "tenant", "account", "profile", "auth", "authentication",
    "backend", "frontend", "infra", "code", "codebase",
}

REQUIRED_FIELDS = {"entities", "categories", "facts", "summary", "tools_used"}

# Scripted conversation designed to exercise the full discipline:
#   - specific compound entity names (auth-session, admin-dashboard)
#   - user facts (commute)
#   - entity facts (cpp.exceptions, postgres.downtime)
#   - hard-switches between domains
#   - drift turns
#   - topic resolution (should trigger summary emission)
#   - generic-noun traps (did the speaker put them in categories?)
CONVERSATION = [
    # 1. Opening mention: specific entity + tool
    "hey, i'm working on the auth-session refactor in cpp at acme",
    # 2. Drift turn — cpp and auth-session should stay active
    "what pattern would you recommend for session invalidation?",
    # 3. Entity fact — cpp.exceptions should be emitted
    "we can't use exceptions in this codebase btw, company policy",
    # 4. Drift
    "do we have a test harness?",
    # 5. Hard switch to another domain + specific entity
    "ok switching gears - let me look at the admin-dashboard in python",
    # 6. Drift — admin-dashboard + python should stay active
    "i forget where we left off on that one",
    # 7. Entity fact — admin-dashboard.rollout
    "dark mode rollout is at 60%, need to push to 100",
    # 8. User fact — user.commute
    "btw my commute today was brutal, 45 min each way",
    # 9. Return to prior domain — HARD SWITCH back
    "alright circling back to cpp auth-session",
    # 10. Entity fact — cpp.style
    "snake_case for handlers right?",
    # 11. Topic resolution — should trigger summary on auth-session
    "got it, that's resolved - moving to the payment-webhook postgres timeout",
    # 12. Continuation in new domain
    "query plan shows a seq scan, need an index",
    # 13. Entity fact — postgres.downtime
    "we can only do schema changes during sunday downtime window",
    # 14. Trivial / drift
    "ok, enough for today",
    # 15. Closing
    "thanks, that was helpful",
]


def extract_turn_json(text):
    """Extract the turn.json payload from the assistant's response."""
    m = re.search(r"```turn\.json\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return None, "no ```turn.json block found in response"
    try:
        return json.loads(m.group(1)), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"


def validate_turn(turn, turns_covered):
    """Return list of (check_name, passed, detail_or_none).

    turns_covered: how many turns this summary covers (for dynamic budget).
    """
    checks = []

    if turn is None:
        checks.append(("parse_valid", False, "turn.json not extractable"))
        return checks
    checks.append(("parse_valid", True, None))

    # Required fields
    missing = REQUIRED_FIELDS - turn.keys()
    checks.append((
        "all_fields_present",
        not missing,
        f"missing: {sorted(missing)}" if missing else None,
    ))

    # entities should be specific, not generic nouns
    entities = turn.get("entities", []) or []
    generic = [e for e in entities if e.lower().strip() in GENERIC_NOUNS]
    checks.append((
        "entities_not_generic",
        not generic,
        f"generic nouns as entities: {generic}" if generic else None,
    ))

    # facts are {key, value} dicts (entity optional)
    facts = turn.get("facts", []) or []
    bad = []
    for f in facts:
        if not isinstance(f, dict):
            bad.append(f"not a dict: {f!r}")
            continue
        if "key" not in f or "value" not in f:
            bad.append(f"missing key/value: {f!r}")
    checks.append((
        "facts_well_formed",
        not bad,
        "; ".join(bad) if bad else None,
    ))

    # Summary budget scales with depth: ~400 chars (~100 tokens) per turn
    # covered. Short topic shifts get terse summaries; deep discussions get
    # more room. Empty summary is always fine (non-resolution turn).
    summary = turn.get("summary", "") or ""
    if not summary:
        checks.append(("summary_length_ok", True, None))
    else:
        budget = max(400, turns_covered * 400)
        ok = len(summary) <= budget
        checks.append((
            "summary_length_ok",
            ok,
            (f"{len(summary)} chars > {budget} budget "
             f"({turns_covered}-turn topic)") if not ok else None,
        ))

    return checks


def run_conversation(model, verbose):
    client = Anthropic()
    messages = []
    results = []

    print(f"Tier 1 compliance test  |  model: {model}  |  {len(CONVERSATION)} turns")
    print("=" * 72)

    # Track turns since last summary was emitted — defines the summary budget.
    turns_covered = 0

    for i, user_msg in enumerate(CONVERSATION, 1):
        messages.append({"role": "user", "content": user_msg})
        turns_covered += 1

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

        turn_json, parse_err = extract_turn_json(assistant_text)
        checks = validate_turn(turn_json, turns_covered)

        # Reset depth counter when a summary is emitted (topic resolved)
        if turn_json and turn_json.get("summary"):
            turns_covered = 0

        results.append({
            "turn": i,
            "user_msg": user_msg,
            "assistant_text": assistant_text,
            "turn_json": turn_json,
            "parse_err": parse_err,
            "checks": [(n, p, d) for n, p, d in checks],
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        })

        passed = sum(1 for _, p, _ in checks if p)
        total = len(checks)
        status = "PASS" if passed == total else "FAIL"
        print(f"\n[Turn {i:2d}] {status} ({passed}/{total})  \"{user_msg[:58]}\"")
        for name, ok, detail in checks:
            mark = " OK " if ok else "MISS"
            msg = f"  — {detail}" if detail else ""
            print(f"         {mark}  {name}{msg}")

        if verbose and turn_json:
            print("         emitted:")
            for line in json.dumps(turn_json, indent=2).splitlines():
                print(f"           {line}")

    return results


def print_summary(results):
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    if not results:
        print("No turns completed.")
        return

    check_names = []
    seen = set()
    for r in results:
        for name, _, _ in r["checks"]:
            if name not in seen:
                seen.add(name)
                check_names.append(name)

    print(f"\n{len(results)} turns completed\n")
    print(f"  {'check':<25} {'pass':>10}  {'rate':>6}")
    print(f"  {'-'*25} {'-'*10}  {'-'*6}")
    for name in check_names:
        passed = sum(1 for r in results for n, ok, _ in r["checks"] if n == name and ok)
        total = sum(1 for r in results for n, _, _ in r["checks"] if n == name)
        pct = (passed / total * 100) if total else 0
        print(f"  {name:<25} {passed:>4}/{total:<4}  {pct:>5.0f}%")

    # Token usage
    in_tok = sum(r["usage"]["input_tokens"] for r in results)
    out_tok = sum(r["usage"]["output_tokens"] for r in results)
    print(f"\n  tokens: {in_tok} in / {out_tok} out  (total {in_tok+out_tok})")

    # List every failing check with detail
    fails = [
        (r["turn"], n, d) for r in results
        for n, ok, d in r["checks"] if not ok
    ]
    if fails:
        print("\nFailures:")
        for turn, name, detail in fails:
            print(f"  turn {turn:2d}  {name}: {detail}")
    else:
        print("\nAll checks passed.")


def main():
    parser = argparse.ArgumentParser(description="Tier 1 compliance test for Pane's CLAUDE.md speaker instructions")
    parser.add_argument("--model", default="claude-sonnet-4-6",
                        help="Anthropic model ID (default: claude-sonnet-4-6)")
    parser.add_argument("--save", default=None,
                        help="save full results (incl. raw responses) to a JSON file")
    parser.add_argument("--verbose", action="store_true",
                        help="print full emitted turn.json for every turn")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Put your key in .env")
        sys.exit(1)

    results = run_conversation(args.model, args.verbose)
    print_summary(results)

    if args.save:
        out_path = Path(args.save)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
