"""Run all eval tiers in sequence.

Tier 1 (speaker compliance) must pass before Tier 2 (integration) runs.
Aborts early if Tier 1 fails, saving the cost of a Tier 2 API run.

Run:
    python evals/run.py
    python evals/run.py --model claude-opus-4-6
    python evals/run.py --save-dir evals/results/2026-04-05
    python evals/run.py --tier1-only
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")


def run_tier1(model, save_path, verbose):
    """Run Tier 1 compliance. Returns (passed, total, results_dict)."""
    from tier1_compliance import run_conversation, print_summary, CONVERSATION

    results = run_conversation(model, verbose)
    print_summary(results)

    checks = [c for r in results for c in r["checks"]]
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)

    if save_path:
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    return passed, total, results


def run_tier2(model, save_path, verbose):
    """Run Tier 2 integration. Returns (passed, total, results_dict)."""
    from tier2_integration import run_conversation as run_t2

    results = run_t2(model, verbose)
    checks = results.get("checks", [])
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)

    if save_path:
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    return passed, total, results


def main():
    parser = argparse.ArgumentParser(description="Run Pane eval tiers")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--save-dir", default=None,
                        help="directory to save results (tier1.json, tier2.json)")
    parser.add_argument("--tier1-only", action="store_true",
                        help="run only Tier 1, skip Tier 2")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Put your key in .env")
        sys.exit(1)

    # Ensure we can import the tier modules
    sys.path.insert(0, str(Path(__file__).parent))

    save_dir = None
    if args.save_dir:
        save_dir = Path(args.save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

    total_in = 0
    total_out = 0

    # ── Tier 1 ────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(" TIER 1 — Speaker Compliance")
    print("=" * 72 + "\n")

    t1_save = str(save_dir / "tier1.json") if save_dir else None
    t1_start = time.time()
    t1_passed, t1_total, t1_results = run_tier1(args.model, t1_save, args.verbose)
    t1_time = time.time() - t1_start

    t1_tokens = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"]
                    for r in t1_results)
    total_in += sum(r["usage"]["input_tokens"] for r in t1_results)
    total_out += sum(r["usage"]["output_tokens"] for r in t1_results)

    t1_pass = t1_passed == t1_total

    if not t1_pass:
        print(f"\n{'=' * 72}")
        print(f" TIER 1 FAILED ({t1_passed}/{t1_total})")
        print(f" Aborting — fix speaker compliance before running Tier 2.")
        print(f"{'=' * 72}")
        sys.exit(1)

    if args.tier1_only:
        print(f"\n{'=' * 72}")
        print(f" TIER 1 PASSED ({t1_passed}/{t1_total})  |  "
              f"{t1_time:.0f}s  |  {t1_tokens:,} tokens")
        print(f" --tier1-only: skipping Tier 2")
        print(f"{'=' * 72}")
        return

    # ── Tier 2 ────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(" TIER 2 — Integration (subtopic grouping)")
    print("=" * 72 + "\n")

    t2_save = str(save_dir / "tier2.json") if save_dir else None
    t2_start = time.time()
    t2_passed, t2_total, t2_results = run_tier2(args.model, t2_save, args.verbose)
    t2_time = time.time() - t2_start

    t2_tokens = t2_results.get("tokens", {})
    total_in += t2_tokens.get("input", 0)
    total_out += t2_tokens.get("output", 0)

    # ── Summary ───────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f" RESULTS")
    print(f"{'=' * 72}")
    print(f"  Tier 1 (compliance):   {t1_passed}/{t1_total}  "
          f"{'PASS' if t1_pass else 'FAIL'}  ({t1_time:.0f}s)")
    print(f"  Tier 2 (integration):  {t2_passed}/{t2_total}  "
          f"{'PASS' if t2_passed == t2_total else 'FAIL'}  ({t2_time:.0f}s)")
    print(f"  Total tokens:          {total_in + total_out:,} "
          f"({total_in:,} in / {total_out:,} out)")

    total_time = t1_time + t2_time
    print(f"  Total time:            {total_time:.0f}s")

    if save_dir:
        print(f"  Results saved to:      {save_dir}/")
    print(f"{'=' * 72}")

    if t2_passed < t2_total:
        sys.exit(1)


if __name__ == "__main__":
    main()
