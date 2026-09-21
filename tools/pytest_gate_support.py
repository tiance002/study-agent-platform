"""Shared JUnit statistics for release gates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class JUnitStats:
    tests: int
    failures: int
    errors: int
    skipped: int


def read_junit_stats(path: Path) -> JUnitStats:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return JUnitStats(
        tests=sum(int(suite.attrib.get("tests", "0")) for suite in suites),
        failures=sum(int(suite.attrib.get("failures", "0")) for suite in suites),
        errors=sum(int(suite.attrib.get("errors", "0")) for suite in suites),
        skipped=sum(int(suite.attrib.get("skipped", "0")) for suite in suites),
    )
