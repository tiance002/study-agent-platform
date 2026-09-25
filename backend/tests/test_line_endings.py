"""文本文件的行尾必须与 `.gitattributes` 一致。

`.gitattributes` 声明 `* text=auto eol=lf`，只有 `.cmd` / `.bat` 例外（必须 CRLF）。

## 为什么需要一条测试来守它

因为 **git 自己不会报**。`core.autocrlf=true` 会在 `add` 时把行尾差异规范化掉，
于是 `git diff` 看起来完全正常 —— 但「下次 checkout」「换一台机器」
「关掉 autocrlf」任一条成立，整文件 diff 就会冒出来，把真实改动淹没。
这与契约生成曾经的 `generated_at` 噪音是同一类问题：
**它不报错，只是让 review 变得不可靠。**

## 它不是假想的

写这条守卫的当天，用 Python 脚本改源码时忘了指定 `newline=""`，
一次就把四个文件变成了 CRLF —— 其中一个（`budget/ledger.py`）
和另一个（`core/evidence_issues.py`）还是**上一轮**就已经被污染、并已提交的。
靠人眼看着 `git diff` 根本发现不了。
"""

from __future__ import annotations

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 跟随 `.gitattributes` 的 `eol=lf`。
LF_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".md", ".yaml", ".yml", ".ini", ".toml",
        ".json", ".sql", ".mako", ".txt", ".html", ".css", ".js",
    }
)
#: `.gitattributes` 明确要求 CRLF：cmd.exe 按系统代码页逐字节解析，
#: 行尾与编码都参与解析，错了就会执行字节残片。
CRLF_SUFFIXES = frozenset({".cmd", ".bat"})

SKIP_DIRS = frozenset(
    {
        ".venv", "__pycache__", ".git", "var", "node_modules",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".agents", ".codex",
        ".planning", ".serena", ".trae",
    }
)
#: 测试报告是 pytest-html 生成的产物（已在 `.gitignore` 里）。
SKIP_FILES = frozenset({"pytest_html_report.html"})


def _candidate_files(suffixes: frozenset[str]):
    for directory, child_dirs, filenames in os.walk(ROOT):
        child_dirs[:] = sorted(
            name
            for name in child_dirs
            if name not in SKIP_DIRS and not name.endswith(".egg-info")
        )
        for filename in sorted(filenames):
            path = pathlib.Path(directory) / filename
            if path.suffix.lower() in suffixes and path.name not in SKIP_FILES:
                yield path


@pytest.mark.invariant
def test_text_files_use_lf():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _candidate_files(LF_SUFFIXES)
        if b"\r\n" in path.read_bytes()
    ]
    assert not offenders, (
        "以下文件含 CRLF，应为 LF。注意：用 Python 脚本改文件时必须指定 "
        f"`newline=\"\"`（或直接写二进制），否则一次就污染整个文件：{offenders}"
    )


@pytest.mark.invariant
def test_windows_scripts_use_crlf_and_stay_ascii():
    """`.cmd` 必须 CRLF 且纯 ASCII。

    这两条是 cmd.exe 的硬要求：它按**系统代码页**逐字节解析批处理文件，
    编码或行尾不对都会让解析偏移错位，报出看不懂的「不是内部或外部命令」。
    `chcp 65001` 救不了 —— 错位在它生效之前就发生了。
    中文说明应当放 README，而不是写进脚本。
    """
    problems: list[str] = []
    found = 0
    for path in _candidate_files(CRLF_SUFFIXES):
        found += 1
        data = path.read_bytes()
        rel = str(path.relative_to(ROOT))
        if b"\r\n" not in data:
            problems.append(f"{rel}：不是 CRLF")
        try:
            data.decode("ascii")
        except UnicodeDecodeError:
            problems.append(f"{rel}：含非 ASCII 字节")
    assert found, "没有找到任何 .cmd/.bat 文件，测试前提不成立（路径或排除规则写错了？）"
    assert not problems, problems


#: 零宽类不可见字符。它们不会报错，只会让"看起来一样"的两段文本不相等。
_INVISIBLE_CHARS = {
    "零宽空格 ZWSP": "\u200b",
    "零宽非连接符 ZWNJ": "\u200c",
    "零宽连接符 ZWJ": "\u200d",
    "单词连接符 WJ": "\u2060",
    "BOM": "\ufeff",
}


@pytest.mark.invariant
def test_text_files_contain_no_invisible_characters():
    """源码与文档不得含零宽类不可见字符。

    为什么这值得一条机械守卫：**它们不可见，review 抓不到**。危害有三 ——

    1. 字符串比较会莫名失败："看起来一样"的两个字符串不相等；
    2. 中文输入法切换时会悄悄插入它们，而错误现场离原因很远；
    3. 它们让 diff 出现无法解释的改动。

    它不是假想的：写这条守卫的当天，我在一个注释里打进了一个零宽空格。
    靠眼睛是永远发现不了的 —— 只能靠机械检查。
    """
    offenders: dict[str, list[str]] = {}
    for path in _candidate_files(LF_SUFFIXES):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [name for name, char in _INVISIBLE_CHARS.items() if char in text]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, f"发现不可见字符（它们只能靠机械检查发现）：{offenders}"
