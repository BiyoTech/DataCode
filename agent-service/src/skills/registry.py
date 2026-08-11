"""Skill Registry — manages skill registration, lookup, and lazy content loading.

Skills are on-demand knowledge packs (SKILL.md markdown files) that agents
load via the ``load_skill`` tool when needed, rather than stuffing domain
knowledge into the system prompt.  This keeps the prompt lean and makes
skills maintainable by non-developers.

Pattern follows the existing ToolRegistry / HandoffFilterRegistry convention:
class-level dict + classmethod decorator-style register().

Skill locations:
  - Builtin: src/skills/builtin/<skill_name>/SKILL.md  (service code)
  - Installed: .agent/skills/installed/<skill_name>/SKILL.md (workspace-level, from session scaffold)
  - Custom: .agent/skills/custom/<skill_name>/SKILL.md (user-defined)
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Maximum length for the skill index section injected into the system prompt
MAX_INDEX_CHARS = 2000


@dataclass
class SkillInfo:
    """Metadata + lazy-loaded content for a registered skill."""

    name: str
    """Unique skill identifier (e.g. 'sqlmesh_reference')."""

    description: str
    """One-line summary shown in the skill index (e.g. 'SQLMesh syntax & model kind reference')."""

    trigger_keywords: t.List[str] = field(default_factory=list)
    """Keywords / intents that suggest this skill is needed (e.g. ['model kind', 'syntax'])."""

    file_path: str = ""
    """Absolute path to the SKILL.md file (content loaded on first access)."""

    _content: str | None = field(default=None, repr=False, init=False)
    """Cached content, loaded lazily from file_path on first get()."""

    def get_content(self) -> str:
        """Return the full SKILL.md content, loading from disk on first access."""
        if self._content is not None:
            return self._content
        if not self.file_path or not Path(self.file_path).exists():
            logger.warning("skill_file_missing", name=self.name, path=self.file_path)
            return ""
        try:
            self._content = Path(self.file_path).read_text(encoding="utf-8")
            logger.info("skill_content_loaded", name=self.name, chars=len(self._content))
        except Exception as e:
            logger.error("skill_load_failed", name=self.name, error=str(e))
            self._content = ""
        return self._content


class SkillRegistry:
    """Central registry of all available skills.

    Usage::

        @SkillRegistry.register(
            "sqlmesh_reference",
            description="SQLMesh syntax & model kind reference",
            trigger_keywords=["syntax", "model kind", "INCREMENTAL_BY_TIME_RANGE"],
            file_path="src/skills/builtin/sqlmesh_reference/SKILL.md",
        )
        def _sqlmesh_reference() -> SkillInfo: ...

        # Startup
        SkillRegistry._load_builtin()

        # Prompt injection
        section = SkillRegistry.get_index_section()
    """

    _skills: t.Dict[str, SkillInfo] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    @classmethod
    def register(
        cls,
        name: str,
        *,
        description: str = "",
        trigger_keywords: t.Optional[t.List[str]] = None,
        file_path: str = "",
    ) -> t.Callable:
        """Decorator: register a skill with metadata.

        Args:
            name: Unique skill identifier.
            description: One-line summary for the index section.
            trigger_keywords: Keywords that hint this skill is relevant.
            file_path: Path to SKILL.md (absolute or relative to project root).

        The decorated factory function is called once at registration time.
        """
        def decorator(factory: t.Callable[[], None]) -> t.Callable[[], None]:
            info = SkillInfo(
                name=name,
                description=description,
                trigger_keywords=trigger_keywords or [],
                file_path=file_path,
            )
            cls._skills[name] = info
            logger.info(
                "skill_registered",
                name=name,
                has_file_path=bool(file_path),
            )
            return factory

        return decorator

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    @classmethod
    def get(cls, name: str) -> SkillInfo | None:
        """Look up a skill by name. Returns None if not registered."""
        return cls._skills.get(name)

    @classmethod
    def has(cls, name: str) -> bool:
        """Check if a skill name is registered."""
        return name in cls._skills

    @classmethod
    def list_all(cls) -> t.List[str]:
        """Return all registered skill names."""
        return sorted(cls._skills.keys())

    # ------------------------------------------------------------------
    # Index section for prompt injection
    # ------------------------------------------------------------------

    @classmethod
    def get_index_section(cls) -> str:
        """Generate a compact skill index for injection into the system prompt.

        Returns an empty string when no skills are registered.
        """
        if not cls._skills:
            return ""

        lines = [
            "",
            "## Available Skills",
            "",
            "When you need domain knowledge, call `load_skill(name)` to load it on demand.",
            "Skills are NOT loaded by default — only load when needed for the current task.",
            "",
        ]

        for name in sorted(cls._skills.keys()):
            info = cls._skills[name]
            # Compact one-line entry
            desc = info.description or "(no description)"
            triggers = (
                " | ".join(info.trigger_keywords[:5])
                if info.trigger_keywords
                else ""
            )
            line = f"- **{name}**: {desc}"
            if triggers:
                line += f"  [triggers: {triggers}]"
            lines.append(line)

        result = "\n".join(lines)
        if len(result) > MAX_INDEX_CHARS:
            result = result[:MAX_INDEX_CHARS] + "\n...(skill index truncated)"
        return result

    # ------------------------------------------------------------------
    # Builtin skill loading (called at service startup)
    # ------------------------------------------------------------------

    @classmethod
    def _load_builtin(cls) -> None:
        """Scan src/skills/builtin/*/SKILL.md  and register all builtin skills.

        Called once at service startup after all @SkillRegistry.register
        decorators in src/skills/__init__.py have fired.
        """
        builtin_dir = Path(__file__).parent / "builtin"
        if not builtin_dir.is_dir():
            logger.warning("builtin_skills_dir_missing", path=str(builtin_dir))
            return

        for skill_dir in sorted(builtin_dir.iterdir()):
            if not skill_dir.is_dir():
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                logger.debug("skill_md_missing", dir=str(skill_dir))
                continue

            name = skill_dir.name
            if name in cls._skills:
                # Already registered via decorator — update file_path if needed
                existing = cls._skills[name]
                if not existing.file_path:
                    existing.file_path = str(skill_md)
                    logger.info(
                        "skill_file_path_updated",
                        name=name,
                        path=str(skill_md),
                    )
            else:
                # Auto-register: infer metadata from SKILL.md front-matter or fallback
                logger.warning(
                    "skill_not_registered_via_decorator",
                    name=name,
                    hint="Consider adding @SkillRegistry.register decorator in __init__.py",
                )

        logger.info("builtin_skills_loaded", count=len(cls._skills))
