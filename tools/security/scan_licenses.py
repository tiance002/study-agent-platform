"""依赖许可证扫描门。

## 目的不是「合规盖章」

这个门要解决一个很具体的问题：**引入带分发义务的依赖时，必须当场知道**，
而不是等发布前才发现整个服务要开源。

## 判定规则（与行为严格一致）

| 情况 | 结果 |
|---|---|
| 只有宽松许可（MIT / BSD / Apache / ISC / PSF / Unlicense / CC0） | 通过 |
| 出现 GPL / AGPL / LGPL 且**没有**宽松选项 | **失败** |
| 同时存在宽松与强 copyleft（双许可） | 通过，但打印提示 |
| **弱 copyleft**（MPL / EPL —— 文件级） | 通过，但打印义务提示 |
| **无法判定**（元数据缺失，或是有文本但归不了类） | **失败** |

最后一行是刻意的：无法判定**不等于安全**。早期版本让它静默通过，
等于扫描器自带一个看不见的洞 —— 任何新出现的、规则没覆盖的许可证
都会从那里溜走。现在它必须被处理一次（换成宽松依赖，或登记豁免）。

弱 copyleft 为什么不直接失败：MPL / EPL 的义务按**文件**触发，
修改了那些文件才需要回馈，作为依赖使用通常不触发。但也不能像早期版本
那样把它归成"宽松" —— 那是在替使用者做一个未经验证的判断。
所以：不阻塞，但必须显式提示。

## 豁免

`tools/security/license_allowlist.txt`，一行一个包名，**必须**跟 `#` 理由。
空理由会被当作错误 —— 「不允许无理由豁免」如果只是写在注释里，就等于没有。

用法：

```bash
python tools/security/scan_licenses.py
```
"""

from __future__ import annotations

import re
import sys
from enum import StrEnum
from importlib.metadata import distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "tools" / "security" / "license_allowlist.txt"

COPYLEFT_PATTERNS = (
    re.compile(r"\bA?GPL\b", re.IGNORECASE),
    re.compile(r"\bLGPL\b", re.IGNORECASE),
    # 全名形态必须一起覆盖：`GNU Lesser General Public License` 里
    # 既没有单独的 `GPL` 词，也没有 `LGPL` 连写，只写 GPL 会漏掉它。
    re.compile(
        r"GNU\s+(Affero\s+|Lesser\s+|Library\s+)?General\s+Public\s+License",
        re.IGNORECASE,
    ),
)

# 文件级弱 copyleft：不阻塞发布，但必须提示。
WEAK_COPYLEFT_PATTERNS = (
    re.compile(r"\bMPL\b", re.IGNORECASE),
    re.compile(r"Mozilla\s+Public\s+License", re.IGNORECASE),
    re.compile(r"\bEPL\b", re.IGNORECASE),
    re.compile(r"Eclipse\s+Public\s+License", re.IGNORECASE),
)

PERMISSIVE_MARKERS = (
    # 许可名称
    "MIT",
    "BSD",
    "APACHE",
    "ISC",
    "PSF",
    "PYTHON SOFTWARE FOUNDATION",
    "UNLICENSE",
    "CC0",
    "PUBLIC DOMAIN",
    # 许可**全文**的特征句。
    # 很多包的 License 字段写的是全文而不是名称，而 MIT / BSD 全文里
    # 压根没有 "MIT" / "BSD" 字样 —— 只认名称会让这些包全部掉进「无法判定」。
    "PERMISSION IS HEREBY GRANTED",                        # MIT 全文开头
    "REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS",   # BSD 全文开头
    "THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS",  # BSD 免责段
    "PERMISSION TO USE, COPY, MODIFY, AND DISTRIBUTE",     # 部分 MIT 变体
)


class Verdict(StrEnum):
    """许可判定结果。抽成独立函数是为了能被测试覆盖 —— 校验器本身也得被校验。"""

    PERMISSIVE = "permissive"
    WEAK_COPYLEFT = "weak_copyleft"
    COPYLEFT = "copyleft"
    DUAL = "dual"
    UNKNOWN = "unknown"


def classify_license(text: str) -> Verdict:
    """把许可文本判成一类。

    ⚠️ 关键分支：既不含 copyleft、也不含任何宽松标记的文本（例如
    `Zope Public License`、`Server Side Public License`）必须归入 UNKNOWN。
    早期版本会把它静默放过 —— 那等于扫描器有个看不见的洞，
    任何规则没覆盖的新许可证都会从那里溜走。
    """
    if not text.strip():
        return Verdict.UNKNOWN

    upper = text.upper()
    has_copyleft = any(pattern.search(text) for pattern in COPYLEFT_PATTERNS)
    has_weak = any(pattern.search(text) for pattern in WEAK_COPYLEFT_PATTERNS)
    has_permissive = any(marker in upper for marker in PERMISSIVE_MARKERS)

    if has_copyleft and has_permissive:
        return Verdict.DUAL
    if has_copyleft:
        return Verdict.COPYLEFT
    if has_weak:
        return Verdict.WEAK_COPYLEFT
    if not has_permissive:
        return Verdict.UNKNOWN
    return Verdict.PERMISSIVE


def license_text(dist) -> str:
    """尽量从元数据里拼出许可信息。不同打包工具的字段位置不一样。"""
    meta = dist.metadata
    parts: list[str] = []
    for key in ("License", "License-Expression"):
        value = meta.get(key)
        if value and value.strip() and value.strip().upper() != "UNKNOWN":
            parts.append(value.strip())
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            parts.append(classifier.removeprefix("License ::").strip())
    return " | ".join(parts)


def load_allowlist() -> dict[str, str]:
    """解析豁免清单。每条**必须**写明理由，否则直接报错。"""
    if not ALLOWLIST.exists():
        return {}

    allowed: dict[str, str] = {}
    for lineno, raw in enumerate(ALLOWLIST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, reason = line.partition("#")
        name = name.strip().lower()
        if not name:
            continue
        if not separator or not reason.strip():
            raise ValueError(
                f"{ALLOWLIST.name} 第 {lineno} 行：豁免必须写明理由。"
                f"格式为 `<包名> # <为什么可以接受>`。不允许无理由豁免。"
            )
        allowed[name] = reason.strip()
    return allowed


def main() -> int:
    try:
        allowed = load_allowlist()
    except ValueError as exc:
        print(f"[FAIL] 豁免清单格式错误：{exc}")
        return 1

    failures: list[str] = []
    undecided: list[str] = []
    dual: list[str] = []
    weak: list[str] = []

    for dist in distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        if name.lower() in allowed:
            continue

        text = license_text(dist)
        verdict = classify_license(text)

        if verdict is Verdict.COPYLEFT:
            failures.append(f"{name} ({text[:80]})")
        elif verdict is Verdict.DUAL:
            dual.append(f"{name} ({text[:80]})")
        elif verdict is Verdict.WEAK_COPYLEFT:
            weak.append(f"{name} ({text[:80]})")
        elif verdict is Verdict.UNKNOWN:
            undecided.append(f"{name} ({text[:60] or '无许可元数据'})")

    if failures:
        print(f"[FAIL] 发现 {len(failures)} 个强 copyleft 依赖，会带来分发义务：")
        for item in failures:
            print(f"  - {item}")
        print()
        print("  处理方式（择一）：")
        print("    1. 换用宽松许可的替代实现；")
        print("    2. 确属可接受，在 tools/security/license_allowlist.txt 登记并写明理由。")
        return 1

    if undecided:
        print(f"[FAIL] {len(undecided)} 个依赖的许可无法自动判定：")
        for item in undecided:
            print(f"  - {item}")
        print()
        print("  无法判定不等于安全 —— 这是一个必须处理一次的分支。")
        print("  请确认其许可条款，然后在 tools/security/license_allowlist.txt 登记理由，")
        print("  或换用元数据完整的替代实现。")
        return 1

    if dual:
        print(f"[WARN] {len(dual)} 个依赖为双许可（含宽松选项，按宽松那条使用）：")
        for item in dual:
            print(f"  - {item}")

    if weak:
        print(f"[WARN] {len(weak)} 个依赖为弱 copyleft（文件级义务）：")
        for item in weak:
            print(f"  - {item}")
        print("      只要不修改其源文件，作为依赖使用通常不触发回馈义务；")
        print("      若确实需要修改，请先确认条款。")

    print(f"[OK] 许可证扫描通过：无强 copyleft，无法判定 0 项，{len(allowed)} 条豁免")
    return 0


if __name__ == "__main__":
    sys.exit(main())
