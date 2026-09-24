"""Validate project-local Agent skills and their Markdown references."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / ".agents" / "skills"
GUIDE = ROOT / "docs" / "agent-skills.md"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _read_frontmatter(path: Path, errors: list[str]) -> dict[str, object] | None:
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        errors.append(f"{path.relative_to(ROOT)}: 缺少 YAML frontmatter 起始标记")
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        errors.append(f"{path.relative_to(ROOT)}: 缺少 YAML frontmatter 结束标记")
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        errors.append(f"{path.relative_to(ROOT)}: frontmatter YAML 无效：{exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{path.relative_to(ROOT)}: frontmatter 顶层必须为映射")
        return None
    return data


def _check_links(markdown_path: Path, errors: list[str]) -> None:
    content = markdown_path.read_text(encoding="utf-8")
    for raw_target in LINK_PATTERN.findall(content):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        local_target = (markdown_path.parent / unquote(parsed.path)).resolve()
        if not local_target.exists():
            relative_source = markdown_path.relative_to(ROOT)
            errors.append(f"{relative_source}: 本地链接目标不存在：{parsed.path}")


def check(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    skills_dir = root / ".agents" / "skills"
    guide = root / "docs" / "agent-skills.md"
    if not skills_dir.is_dir():
        return ["找不到项目技能目录：.agents/skills"]
    if not guide.is_file():
        errors.append("找不到技能使用指南：docs/agent-skills.md")

    skill_dirs = sorted(path for path in skills_dir.iterdir() if path.is_dir())
    if not skill_dirs:
        errors.append(".agents/skills 下没有技能目录")

    names: set[str] = set()
    markdown_files: list[Path] = []
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"{skill_dir.relative_to(root)}: 缺少 SKILL.md")
            continue

        metadata = _read_frontmatter(skill_file, errors)
        if metadata is not None:
            name = metadata.get("name")
            description = metadata.get("description")
            if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
                errors.append(f"{skill_file.relative_to(root)}: name 必须为小写 kebab-case")
            elif name != skill_dir.name:
                errors.append(
                    f"{skill_file.relative_to(root)}: name {name!r} 与目录名不一致"
                )
            if isinstance(name, str):
                if name in names:
                    errors.append(f"技能名称重复：{name}")
                names.add(name)
            if not isinstance(description, str) or not description.strip():
                errors.append(f"{skill_file.relative_to(root)}: description 不能为空")

        markdown_files.extend(skill_dir.rglob("*.md"))

    if guide.is_file():
        markdown_files.append(guide)
    for markdown_file in markdown_files:
        _check_links(markdown_file, errors)

    return errors


def main() -> int:
    errors = check()
    for error in errors:
        print(f"[error] {error}")
    if errors:
        print(f"[fail] {len(errors)} 个问题需要修复")
        return 1
    skill_count = sum(1 for path in SKILLS_DIR.iterdir() if path.is_dir())
    print(f"[ok] 项目 Agent skills 校验通过（{skill_count} 个技能）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
