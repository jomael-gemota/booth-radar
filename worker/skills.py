"""
skills.py  –  Booth Radar's Hermes skills (worker/hermes-skills/booth-radar/*/SKILL.md).

In mcp mode Hermes finds the skills through `skills.external_dirs` and loads them
with skill_view. In inline mode Hermes can't use them with the <tool> protocol, so
their text is added to the system prompt instead. The files are the single source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "hermes-skills" / "booth-radar"
TOOLSET = "mcp-booth-radar"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    frontmatter: str
    path: Path


def _field(frontmatter: str, key: str) -> str:
    match = re.search(rf"^\s*{key}:\s*(.+)$", frontmatter, re.MULTILINE)
    return match.group(1).strip() if match else ""


def load() -> list[Skill]:
    """Every skill, with site-scraping (the base strategy) first."""
    found: list[Skill] = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        frontmatter = match.group(1) if match else ""
        found.append(Skill(
            name=_field(frontmatter, "name"),
            description=_field(frontmatter, "description"),
            body=text[match.end():].strip() if match else text.strip(),
            frontmatter=frontmatter,
            path=path,
        ))
    return sorted(found, key=lambda s: s.name != "site-scraping")


def problems(skill: Skill, tools: set[str]) -> list[str]:
    """What is wrong with a skill file: missing frontmatter, or tools that don't exist."""
    issues = []
    if not skill.name:
        issues.append("no name")
    elif skill.name != skill.path.parent.name:
        issues.append(f"name {skill.name!r} differs from its folder {skill.path.parent.name!r}")
    if not skill.description:
        issues.append("no description")
    if TOOLSET not in skill.frontmatter:
        issues.append(f"requires_toolsets does not list {TOOLSET}")
    for called in sorted(set(re.findall(r"`([a-z_]+)\(", skill.body)) - tools):
        issues.append(f"calls unknown tool {called!r}")
    return issues
