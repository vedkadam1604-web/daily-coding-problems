"""
Daily Coding Agent
------------------
Once a day this script asks Claude for a fresh coding problem plus a tested
Python solution, verifies the solution actually passes its tests, saves it to
problems/<date>-<slug>/, and updates the index in README.md.

The GitHub Actions workflow then commits and pushes the result.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent
PROBLEMS_DIR = ROOT / "problems"
HISTORY_FILE = ROOT / "history.json"
README = ROOT / "README.md"

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_ATTEMPTS = 3

# Rotates by day so the repo covers a broad range of DSA topics.
TOPICS = [
    "arrays and hashing", "two pointers", "sliding window", "stack",
    "binary search", "linked lists", "trees", "tries", "heaps / priority queues",
    "backtracking", "graphs (BFS/DFS)", "topological sort", "union find",
    "dynamic programming (1D)", "dynamic programming (2D)", "greedy",
    "intervals", "bit manipulation", "math and geometry", "string algorithms",
    "shortest paths (Dijkstra/Bellman-Ford)", "segment trees / Fenwick trees",
    "monotonic stack/queue", "prefix sums",
]
DIFFICULTIES = ["Easy", "Medium", "Medium", "Hard"]  # weighted toward Medium

SOLUTION_TOOL = {
    "name": "submit_solution",
    "description": "Submit an original coding problem with a verified Python solution.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short problem title"},
            "slug": {"type": "string", "description": "kebab-case slug, e.g. 'longest-balanced-substring'"},
            "difficulty": {"type": "string", "enum": ["Easy", "Medium", "Hard"]},
            "topic": {"type": "string"},
            "problem": {"type": "string", "description": "Full problem statement in Markdown, with examples and constraints"},
            "approach": {"type": "string", "description": "Markdown explanation of the approach and key insight"},
            "time_complexity": {"type": "string"},
            "space_complexity": {"type": "string"},
            "solution_code": {
                "type": "string",
                "description": "Complete Python 3 file: the solution function(s) plus an `if __name__ == '__main__':` "
                               "block with at least 5 assert-based tests (including edge cases) that prints 'ALL TESTS PASSED'. "
                               "Standard library only.",
            },
        },
        "required": ["title", "slug", "difficulty", "topic", "problem", "approach",
                     "time_complexity", "space_complexity", "solution_code"],
    },
}


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def run_tests(code: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        r = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr)[-3000:]
        return r.returncode == 0 and "ALL TESTS PASSED" in r.stdout, out
    except subprocess.TimeoutExpired:
        return False, "Timed out after 30s"
    finally:
        os.unlink(path)


def generate(topic: str, difficulty: str, past_titles: list[str]) -> dict:
    client = anthropic.Anthropic()
    prompt = (
        f"Create one ORIGINAL {difficulty} coding interview problem about **{topic}**, "
        "then solve it in clean, idiomatic, well-commented Python 3.\n\n"
        "Do not copy a LeetCode problem verbatim. Do not repeat any of these previous problems:\n"
        + "\n".join(f"- {t}" for t in past_titles[-60:])
        + "\n\nMake sure every test assertion is actually correct. Submit via the submit_solution tool."
    )
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            tools=[SOLUTION_TOOL],
            tool_choice={"type": "tool", "name": "submit_solution"},
            messages=messages,
        )
        tool_use = next(b for b in resp.content if b.type == "tool_use")
        data = tool_use.input
        ok, output = run_tests(data["solution_code"])
        print(f"Attempt {attempt}: tests {'passed' if ok else 'FAILED'}")
        if ok:
            return data
        # Feed the failure back so Claude can fix it.
        messages += [
            {"role": "assistant", "content": resp.content},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_use.id, "is_error": True,
                "content": f"The tests failed. Output:\n```\n{output}\n```\n"
                           "Fix the solution or the incorrect tests and resubmit.",
            }]},
        ]
    raise RuntimeError("Could not produce a passing solution today.")


def save(data: dict, today: dt.date) -> Path:
    slug = re.sub(r"[^a-z0-9-]+", "-", data["slug"].lower()).strip("-")[:60]
    folder = PROBLEMS_DIR / f"{today.isoformat()}-{slug}"
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "solution.py").write_text(data["solution_code"].rstrip() + "\n")
    (folder / "README.md").write_text(
        f"# {data['title']}\n\n"
        f"**Difficulty:** {data['difficulty']} &nbsp;|&nbsp; **Topic:** {data['topic']} "
        f"&nbsp;|&nbsp; **Date:** {today.isoformat()}\n\n"
        f"## Problem\n\n{data['problem'].strip()}\n\n"
        f"## Approach\n\n{data['approach'].strip()}\n\n"
        f"## Complexity\n\n- Time: {data['time_complexity']}\n- Space: {data['space_complexity']}\n\n"
        f"## Solution\n\nSee [`solution.py`](./solution.py). Run `python solution.py` to execute the tests.\n"
    )
    return folder


def update_readme(history: list[dict]) -> None:
    rows = "\n".join(
        f"| {h['date']} | [{h['title']}](problems/{h['folder']}/) | {h['difficulty']} | {h['topic']} |"
        for h in reversed(history)
    )
    counts = {d: sum(h["difficulty"] == d for h in history) for d in ("Easy", "Medium", "Hard")}
    README.write_text(
        "# Daily Coding Problems\n\n"
        "A new original problem and tested Python solution, added every day.\n\n"
        f"**Total solved:** {len(history)} &nbsp;·&nbsp; Easy {counts['Easy']} "
        f"&nbsp;·&nbsp; Medium {counts['Medium']} &nbsp;·&nbsp; Hard {counts['Hard']}\n\n"
        "| Date | Problem | Difficulty | Topic |\n|---|---|---|---|\n"
        f"{rows}\n"
    )


def main() -> None:
    today = dt.date.today()
    history = load_history()
    if any(h["date"] == today.isoformat() for h in history):
        print("Already solved a problem today — nothing to do.")
        return

    day_index = today.toordinal()
    topic = TOPICS[day_index % len(TOPICS)]
    difficulty = DIFFICULTIES[day_index % len(DIFFICULTIES)]
    print(f"Today: {difficulty} / {topic} (model: {MODEL})")

    data = generate(topic, difficulty, [h["title"] for h in history])
    folder = save(data, today)

    history.append({
        "date": today.isoformat(), "title": data["title"], "difficulty": data["difficulty"],
        "topic": data["topic"], "folder": folder.name,
    })
    HISTORY_FILE.write_text(json.dumps(history, indent=2) + "\n")
    update_readme(history)
    print(f"Saved {folder.relative_to(ROOT)}")

    # Expose the title to the workflow for the commit message.
    if gh_out := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_out, "a") as f:
            f.write(f"title={data['title']}\n")


if __name__ == "__main__":
    main()
