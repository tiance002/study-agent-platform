"""Run the round 6 release gate with PostgreSQL as a hard prerequisite."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "backend"))
sys.path.insert(0, str(repo_root / "backend" / "tests"))
import pg_support  # noqa: E402
from pytest_gate_support import read_junit_stats  # noqa: E402


def main() -> int:
    if not pg_support.reachable():
        print("ROUND6_GATE_BLOCKED: PostgreSQL is not reachable; no pass is reported.")
        return 2
    temp_root = Path(
        os.environ.get("ROUND6_GATE_TEMP_ROOT", tempfile.gettempdir())
    ) / "study-plan-pytest"
    temp_root.mkdir(exist_ok=True)
    environment = os.environ.copy()
    for variable in ("TEMP", "TMP", "TMPDIR"):
        environment[variable] = str(temp_root)
    gate_dir = repo_root / "var" / "round6-gate"
    gate_dir.mkdir(parents=True, exist_ok=True)
    commands = [
        [sys.executable, "-m", "pytest", "backend/tests", "-q", "-o", "addopts=", f"--junitxml={gate_dir / 'full.xml'}"],
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests",
            "-m",
            "postgres",
            "-q",
            "-o",
            "addopts=",
            f"--junitxml={gate_dir / 'postgres.xml'}",
        ],
    ]
    for command in commands:
        result = subprocess.run(command, check=False, cwd=repo_root, env=environment)
        if result.returncode != 0:
            print(f"ROUND6_GATE_FAILED: {' '.join(command)}")
            return result.returncode
    postgres_stats = read_junit_stats(gate_dir / "postgres.xml")
    if postgres_stats.tests <= 0 or postgres_stats.failures or postgres_stats.errors or postgres_stats.skipped:
        print(f"ROUND6_GATE_FAILED: invalid PostgreSQL evidence {postgres_stats}")
        return 1
    print("ROUND6_GATE_PASSED: full suite and PostgreSQL suite completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
