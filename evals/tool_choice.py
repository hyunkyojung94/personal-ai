"""Tool-choice eval: does the model pick the right tool (or none) with the right arguments?

    .venv/bin/python evals/tool_choice.py [--repeats 2] [--think] [--only ID ...]

Needs the model server running (./serve.sh). Each case runs against a fresh
temporary data folder seeded with the fictional fixtures in
tool_choice_cases.json, so cases can't affect each other and real notes are
never touched. Results are printed and saved to evals/results/.
"""

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# The app reads its data folder at import time, so point it at the sandbox first.
SANDBOX = Path(tempfile.mkdtemp(prefix="personal-ai-eval-"))
os.environ["PERSONAL_AI_DATA"] = str(SANDBOX)

import assistant  # noqa: E402
import notes  # noqa: E402
from config import NOTES_DIR  # noqa: E402


def reset_sandbox(fixtures):
    """Fresh notes for every run. "{today}" in a fixture becomes today's date,
    e.g. to pre-fill the journal the model would append to."""
    shutil.rmtree(NOTES_DIR, ignore_errors=True)
    today = f"{date.today():%Y-%m-%d}"
    for path, content in fixtures.items():
        path, content = path.replace("{today}", today), content.replace("{today}", today)
        (NOTES_DIR / path).parent.mkdir(parents=True, exist_ok=True)
        (NOTES_DIR / path).write_text(content)
    notes.ensure_repo()


def run_case(case, think):
    start = time.monotonic()
    calls, retracted, warned, answer = [], 0, 0, ""
    for kind, value in assistant.chat([{"role": "user", "content": case["prompt"]}], think=think):
        if kind == "tool":
            calls.append(value)
        elif kind == "retract":
            retracted += 1
            answer = ""
        elif kind == "warning":
            warned += 1
        elif kind == "content":
            answer += value
    return {"calls": calls, "retracted": retracted, "warned": warned,
            "answer": answer.strip(), "seconds": round(time.monotonic() - start, 1)}


def score(case, run):
    """Returns (tool_ok, args_ok, problem)."""
    expected = case["expect"]["tool"]
    first = run["calls"][0] if run["calls"] else None
    if expected is None:
        ok = first is None
        return ok, ok, None if ok else f"called {first['name']} but no tool was expected"
    if first is None:
        return False, False, "no tool called"
    if first["name"] != expected:
        return False, False, f"called {first['name']}, expected {expected}"
    try:
        arguments = json.loads(first["arguments"])
    except ValueError:
        return True, False, f"unparsable arguments: {first['arguments']}"
    for key, needles in case["expect"].get("args", {}).items():
        value = str(arguments.get(key, "")).lower()
        if missing := [needle for needle in needles if needle.lower() not in value]:
            return True, False, f"{key}={arguments.get(key)!r} lacks {missing}"
    if first["status"] not in ("executed", "pending"):
        return True, False, f"tool {first['status']}: {first.get('error')}"
    return True, True, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2, help="runs per case (the model isn't deterministic)")
    parser.add_argument("--think", action="store_true", help="turn on the model's reasoning")
    parser.add_argument("--only", nargs="*", help="case ids to run")
    parser.add_argument("--key-file", default=str(Path.home() / "personal-ai-data" / "api-key"))
    args = parser.parse_args()

    spec = json.loads((HERE / "tool_choice_cases.json").read_text())
    cases = [case for case in spec["cases"] if not args.only or case["id"] in args.only]
    shutil.copy(args.key_file, SANDBOX / "api-key")

    results = []
    try:
        for case in cases:
            for attempt in range(args.repeats):
                # A case's own fixtures add to (or override) the shared ones.
                reset_sandbox({**spec["fixtures"], **case.get("fixtures", {})})
                run = run_case(case, args.think)
                tool_ok, args_ok, problem = score(case, run)
                results.append({"id": case["id"], "category": case["category"], "attempt": attempt,
                                "tool_ok": tool_ok, "args_ok": args_ok, "problem": problem, **run})
                print(f"{'PASS' if args_ok else 'FAIL'}  {case['id']:22} {run['seconds']:5.1f}s"
                      + (f"  {problem}" if problem else "")
                      + (f"  [retracted {run['retracted']}]" if run["retracted"] else "")
                      + (f"  [warned]" if run["warned"] else ""), flush=True)
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)

    by_category = defaultdict(list)
    for result in results:
        by_category[result["category"]].append(result["args_ok"])
    total = len(results)
    summary = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "think": args.think,
        "repeats": args.repeats,
        "runs": total,
        "accuracy": round(sum(r["args_ok"] for r in results) / total, 3),
        "tool_choice_accuracy": round(sum(r["tool_ok"] for r in results) / total, 3),
        "by_category": {cat: f"{sum(oks)}/{len(oks)}" for cat, oks in sorted(by_category.items())},
        "runs_with_retraction": sum(1 for r in results if r["retracted"]),
        "runs_with_warning": sum(1 for r in results if r["warned"]),
        "median_seconds": round(statistics.median(r["seconds"] for r in results), 1),
    }
    print("\n" + json.dumps(summary, indent=2))
    out = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-think-{'on' if args.think else 'off'}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(f"Saved {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
