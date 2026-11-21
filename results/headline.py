"""Recompute the headline Terminal-Bench 4 numbers from tb4-per-task.csv. Python 3 standard library only.

Usage: python3 results/headline.py [path/to/tb4-per-task.csv] [--by-category]

Each score is passed trials over all trials, across every task (66 tasks per run).
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

ROWS = [
    # label, columns, how many trials each column holds per task
    ("Mastra Code + Claude Opus 5.5, goal mode", ["opus_goal_run1", "opus_goal_run2", "opus_goal_run3"], 1),
    ("Mastra Code + GPT-6.1 Sol, goal mode", ["sol_goal_run1", "sol_goal_run2", "sol_goal_run3"], 1),
    ("Mastra Code + Claude Opus 5.5, no goal mode", ["opus_nogoal_nov5_run1"], 1),
    ("Mastra Code + GPT-6.1 Sol, no goal mode", ["sol_nogoal_nov5_run1", "sol_nogoal_nov5_run2"], 1),
    ("Claude Code + Claude Opus 5.5 (leaderboard)", ["claude_code_opus55_passes_of_5"], 5),
    ("Codex + GPT-6.1 Sol (leaderboard)", ["codex_sol_passes_of_5"], 5),
]


def score(rows, columns, per_column):
    passed = sum(int(row[c]) for row in rows for c in columns)
    total = len(rows) * len(columns) * per_column
    return passed, total


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else Path(__file__).with_name("tb4-per-task.csv")
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    print(f"{len(rows)} tasks\n")
    print(f"{'configuration':<46} {'passed':>9} {'score':>7}  per run")
    for label, columns, per_column in ROWS:
        passed, total = score(rows, columns, per_column)
        runs = ", ".join(str(sum(int(r[c]) for r in rows)) for c in columns) if per_column == 1 else "-"
        print(f"{label:<46} {passed:>4}/{total:<4} {100 * passed / total:>6.2f}%  {runs}")

    if "--by-category" in argv:
        groups = defaultdict(list)
        for row in rows:
            groups[row["category"]].append(row)
        print()
        header = ["category", "tasks"] + [label.split(" (")[0].replace("Mastra Code + ", "MC + ") for label, _, _ in ROWS]
        print(" | ".join(header))
        for category in sorted(groups):
            cells = [category, str(len(groups[category]))]
            for _, columns, per_column in ROWS:
                passed, total = score(groups[category], columns, per_column)
                cells.append(f"{100 * passed / total:.1f}%")
            print(" | ".join(cells))


if __name__ == "__main__":
    main(sys.argv[1:])
