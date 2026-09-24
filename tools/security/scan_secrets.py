"""秘密内容扫描。

**为什么需要它**：此前的"密钥检查"只查文件名（`.env` / `.pem` / `.key`），
它拦不住把 token 直接写进源码、配置或文档 —— 而那才是最常见的泄露方式。

设计取舍：
- **不依赖外部工具**，因此 CI 里不需要额外安装，也不会因工具版本漂移而失效。
- **白名单是显式的**：占位值（`dev-only-*`、`change-me`、`placeholder`、`example` 等）
  被允许，其余一律报错。宁可偶尔误报，也不要漏报。
- **变量引用不是字面密钥**：`$VAR` / `${VAR}` 形式的 shell/环境变量引用
  （如部署脚本把密码经变量拼进 DSN）在仓库中不含任何秘密内容，不按泄露处理。
- 扫描器**跳过自身**：它的规则定义里必然出现 "password" 之类的词。

用法：

```bash
python tools/security/scan_secrets.py              # 扫描 git 跟踪的文件
python tools/security/scan_secrets.py --all        # 扫描工作区全部文件（含未跟踪）
python tools/security/scan_secrets.py --path docs  # 只扫某个目录
```
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()

# 二进制与已知无需扫描的扩展名
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".db", ".sqlite3", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2",
    ".pyc", ".whl",
}

# 显式允许的占位值。命中这些片段的匹配不算泄露。
PLACEHOLDER_MARKERS = (
    "dev-only",
    "dev_only",
    "change-me",
    "change_me",
    "changeme",
    "placeholder",
    "example",
    "your-",
    "your_",
    "xxx",
    "redacted",
    "not-for-production",
    "dummy",
    "fake",
    "sample",
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack Token", re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Private Key Block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    (
        "Credential Assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token)\b"
            r"\s*[:=]\s*['\"](?!\$)[^'\"]{8,}['\"]"
        ),
    ),
    (
        "Connection String With Password",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:\s/]+:(?!\$)[^@\s/]+@"
        ),
    ),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [
        REPO_ROOT / item
        for item in result.stdout.decode("utf-8", errors="replace").split("\0")
        if item
    ]


def all_files() -> list[Path]:
    skipped_dirs = {".git", "node_modules", "__pycache__", ".venv", "var", ".pytest_cache"}
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skipped_dirs for part in path.parts):
            continue
        files.append(path)
    return files


def is_placeholder(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """返回 `(行号, 规则名, 该行片段)`。"""
    if path.suffix.lower() in SKIP_SUFFIXES or path.resolve() == SELF:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if is_placeholder(line):
            continue
        for name, pattern in PATTERNS:
            if pattern.search(line):
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                findings.append((number, name, snippet))
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描疑似明文凭据")
    parser.add_argument("--all", action="store_true", help="扫描工作区全部文件")
    parser.add_argument("--path", type=Path, default=None, help="只扫描指定子目录")
    args = parser.parse_args(argv)

    if args.path is not None:
        target = (REPO_ROOT / args.path).resolve()
        files = [p for p in target.rglob("*") if p.is_file()]
    else:
        files = all_files() if args.all else tracked_files()

    total_findings = 0
    for path in sorted(files):
        for number, rule, snippet in scan_file(path):
            total_findings += 1
            try:
                relative = path.relative_to(REPO_ROOT)
            except ValueError:
                relative = path
            print(f"[secret] {relative}:{number}  [{rule}]")
            print(f"         {snippet}")

    if total_findings:
        print(
            f"\n[fail] 发现 {total_findings} 处疑似明文凭据。\n"
            f"       若确认是占位值，请在值中包含 dev-only / change-me / placeholder 等标记；\n"
            f"       否则请移入环境变量或密钥管理系统（项目红线：密钥不进仓库）。"
        )
        return 1

    print(f"[ok]   已扫描 {len(files)} 个文件，未发现明文凭据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
