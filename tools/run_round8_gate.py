"""Round 8 release gate with explicit PostgreSQL and browser evidence."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "backend"))
sys.path.insert(0, str(repo_root / "backend" / "tests"))
sys.path.insert(0, str(repo_root / "tools"))
import pg_support  # noqa: E402
from pytest_gate_support import read_junit_skips, read_junit_stats  # noqa: E402


def run(command: list[str], environment: dict[str, str]) -> int:
    print("$", " ".join(command))
    return subprocess.run(command, check=False, cwd=repo_root, env=environment).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--artifacts", default="var/round8-browser-gate")
    parser.add_argument("--invite", default="round8-preview-invite")
    args = parser.parse_args()

    if not pg_support.reachable():
        print("ROUND8_GATE_BLOCKED: PostgreSQL is not reachable.")
        return 2

    gate_dir = repo_root / "var" / "round8-gate"
    gate_dir.mkdir(parents=True, exist_ok=True)
    configured_temp_root = os.environ.get("ROUND8_GATE_TEMP_ROOT")
    temp_root = (
        Path(configured_temp_root)
        if configured_temp_root
        else repo_root / "var" / "round8-gate" / f"tmp-{os.getpid()}"
    )
    temp_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for variable in ("TEMP", "TMP", "TMPDIR"):
        environment[variable] = str(temp_root)

    python = sys.executable
    full_xml = gate_dir / "full.xml"
    postgres_xml = gate_dir / "postgres.xml"
    commands = [
        [python, "-m", "pytest", "backend/tests", "-q", "-o", "addopts=", f"--junitxml={full_xml}"],
        [
            python,
            "-m",
            "pytest",
            "backend/tests",
            "-m",
            "postgres",
            "-q",
            "-o",
            "addopts=",
            f"--junitxml={postgres_xml}",
        ],
        [python, "-m", "ruff", "check", "backend", "tools"],
        [python, "-m", "mypy", "backend/app"],
        [python, "-m", "compileall", "-q", "backend", "tools", "alembic"],
        ["node", "--check", "frontend/api-client.js"],
        ["node", "--check", "frontend/views.js"],
        ["node", "--check", "frontend/commands.js"],
        ["node", "--check", "frontend/project-state.js"],
        ["node", "--check", "frontend/app.js"],
        [python, "tools/skills/gen_contracts.py", "--all", "--check"],
        [python, "tools/check_round8_browser.py", "--base-url", args.base_url, "--artifacts", args.artifacts, "--invite", args.invite],
        [python, "tools/check_round8_process_recovery.py"],
    ]
    for command in commands:
        if run(command, environment) != 0:
            print("ROUND8_GATE_FAILED:", " ".join(command))
            return 1

    full_stats = read_junit_stats(full_xml)
    full_skips = read_junit_skips(full_xml)
    if full_stats.tests <= 0 or full_stats.failures or full_stats.errors:
        print(f"ROUND8_GATE_FAILED: invalid full-suite evidence {full_stats}")
        return 1
    print(f"FULL_PYTEST_EVIDENCE: {full_stats}")
    for detail in full_skips:
        print("FULL_PYTEST_SKIP:", detail)

    postgres_stats = read_junit_stats(postgres_xml)
    if postgres_stats.tests <= 0 or postgres_stats.failures or postgres_stats.errors or postgres_stats.skipped:
        print(f"ROUND8_GATE_FAILED: invalid PostgreSQL evidence {postgres_stats}")
        return 1
    diff_check = subprocess.run(
        ["git", "diff", "--check"], check=False, cwd=repo_root, env=environment
    )
    if diff_check.returncode != 0:
        print("ROUND8_GATE_FAILED: git diff --check")
        return 1
    print(f"ROUND8_GATE_PASSED: PostgreSQL evidence {postgres_stats}; browser and static checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
