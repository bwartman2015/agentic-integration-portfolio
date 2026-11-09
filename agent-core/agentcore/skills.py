"""SKILLS (concept 7): reusable agent capabilities stored as files.

Each skill is skills/<name>/SKILL.md with a small frontmatter block:

    ---
    name: dlq-replay-runbook
    description: What it does and when to use it.
    trigger: dlq, dead letter, replay, 503
    ---
    # Steps
    - Read ...  - Call a tool ...  - Save to file ...
    # Example input / Example output / Notes

Progressive disclosure: the agent always sees the catalog (name + description); a skill's
full body is injected only when its trigger matches the request or the agent calls the
built-in `load_skill` tool. Agents may WRITE new skills, but only as drafts in
skills/_drafts/ that a human promotes (guardrail).
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from .act import Tool

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    trigger: list[str] = field(default_factory=list)
    path: str = ""

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(t and t in low for t in self.trigger)


def parse_skill(text: str, path: str = "") -> Skill:
    m = _FM.match(text)
    if not m:
        raise ValueError(f"{path or 'skill'}: missing --- frontmatter ---")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    for req in ("name", "description"):
        if not meta.get(req):
            raise ValueError(f"{path or 'skill'}: frontmatter needs {req!r}")
    trig = [t.strip().lower() for t in meta.get("trigger", "").split(",") if t.strip()]
    return Skill(meta["name"], meta["description"], m.group(2).strip(), trig, path)


class SkillLibrary:
    def __init__(self, skills: list[Skill] | None = None, drafts_dir: str | None = None):
        self.skills = {s.name: s for s in (skills or [])}
        self.drafts_dir = drafts_dir

    @classmethod
    def from_dir(cls, path: str | pathlib.Path) -> "SkillLibrary":
        root = pathlib.Path(path)
        skills = [parse_skill(f.read_text(), str(f)) for f in sorted(root.glob("*/SKILL.md"))
                  if not f.parent.name.startswith("_")]
        return cls(skills, drafts_dir=str(root / "_drafts"))

    def catalog(self) -> list[dict[str, str]]:
        return [{"name": s.name, "description": s.description} for s in self.skills.values()]

    def select(self, text: str, limit: int = 2) -> list[Skill]:
        return [s for s in self.skills.values() if s.matches(text)][:limit]

    def load_skill_tool(self) -> Tool:
        def load_skill(name: str) -> dict[str, Any]:
            s = self.skills.get(name)
            if not s:
                raise KeyError(f"no skill named {name!r}; available: {sorted(self.skills)}")
            return {"name": s.name, "instructions": s.body}
        return Tool("load_skill", "Load the full instructions of a skill from the skills catalog",
                    handler=load_skill, params={"name": "string"}, required=["name"],
                    scope=None, side_effect="read", category="file")

    def write_draft_tool(self, scope: str = "skills.write") -> Tool:
        """Lets an agent capture a learned procedure as a DRAFT skill (write -> approval gate)."""
        def draft_skill(name: str, description: str, trigger: str, body: str) -> dict[str, str]:
            if not self.drafts_dir:
                raise RuntimeError("no drafts_dir configured")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", name):
                raise ValueError("skill name must be kebab-case")
            d = pathlib.Path(self.drafts_dir) / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {description}\ntrigger: {trigger}\n---\n{body}\n")
            return {"draft": str(d / "SKILL.md"), "status": "awaiting human review"}
        return Tool("draft_skill", "Save a new reusable procedure as a draft skill for human review",
                    handler=draft_skill, params={"name": "string", "description": "string",
                                                 "trigger": "string", "body": "string"},
                    required=["name", "description", "trigger", "body"], scope=scope,
                    side_effect="write", category="file")
