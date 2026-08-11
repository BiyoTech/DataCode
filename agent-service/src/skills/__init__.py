"""Skills module — on-demand knowledge packs for agents.

Exports:
  SkillRegistry   — central skill index with decorator-style registration
  load_skill       — function_tool for agents to load skill content on demand
"""

from agents import RunContextWrapper, function_tool

from src.context.tool_context import ToolContext
from src.skills.registry import SkillRegistry, SkillInfo
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["SkillRegistry", "SkillInfo", "load_skill"]


# ---------------------------------------------------------------------------
# load_skill — the agent-facing tool
# ---------------------------------------------------------------------------


@ToolRegistry.register(
    "load_skill",
    level=1,
    description="Load a skill's full knowledge content on demand. "
                "Skills contain domain knowledge, workflows, and best practices "
                "that are NOT loaded by default — call this only when you need "
                "the specific knowledge for the current task.",
)
@function_tool
async def load_skill(
    ctx: RunContextWrapper[ToolContext],
    skill_name: str,
) -> str:
    """Load a skill's complete SKILL.md content into the conversation context.

    Use this tool to acquire specialized domain knowledge that is only needed
    for the current task. Skills are progressive-disclosure — they stay out of
    context until explicitly loaded, saving tokens.

    Args:
        skill_name: Name of the skill to load (e.g. 'sqlmesh_reference',
                    'sqlmesh_modeling').  Refer to the "Available Skills" section
                    in your system prompt for the full list.

    Returns:
        The complete skill content, or an error message if the skill is not found.
    """
    del ctx  # ToolContext not needed for skill loading

    info = SkillRegistry.get(skill_name)
    if info is None:
        available = ", ".join(SkillRegistry.list_all()) or "(none registered)"
        logger.warning("skill_not_found", requested=skill_name, available=available)
        return (
            f"Skill '{skill_name}' not found.\n\n"
            f"Available skills: {available}\n"
            f"Tip: check the 'Available Skills' section in your system prompt "
            f"for the correct skill names."
        )

    content = info.get_content()
    if not content:
        return f"Skill '{skill_name}' exists but has no content. The SKILL.md file may be missing or empty."

    logger.info("skill_loaded", name=skill_name, chars=len(content))
    return content


# ---------------------------------------------------------------------------
# Builtin skill registrations — decorators fire when this module is imported
# ---------------------------------------------------------------------------


@SkillRegistry.register(
    "sqlmesh_reference",
    description="SQLMesh syntax, model kind, macro, engine, CLI, and configuration reference",
    trigger_keywords=[
        "model kind", "syntax", "INCREMENTAL_BY_TIME_RANGE", "FULL",
        "SCD_TYPE_2", "macro", "engine", "CLI", "configuration",
        "reference", "audit", "test",
    ],
    file_path=str(
        __import__("pathlib").Path(__file__).parent
        / "builtin"
        / "sqlmesh_reference"
        / "SKILL.md"
    ),
)
def _register_sqlmesh_reference() -> None:
    """No-op — registration is done via decorator side-effect."""
    pass


@SkillRegistry.register(
    "sqlmesh_modeling",
    description="Step-by-step guide for creating, modifying, and deploying SQLMesh models",
    trigger_keywords=[
        "create model", "modify model", "edit model", "apply patch",
        "new model", "model workflow", "write model", "preview plan",
        "apply plan", "model kind", "grain", "description",
    ],
    file_path=str(
        __import__("pathlib").Path(__file__).parent
        / "builtin"
        / "sqlmesh_modeling"
        / "SKILL.md"
    ),
)
def _register_sqlmesh_modeling() -> None:
    """No-op — registration is done via decorator side-effect."""
    pass


@SkillRegistry.register(
    "ontology_modeling",
    description="Step-by-step guide for authoring the semantic-layer ontology "
                "(entities, relationships, metrics) under {workspace}/ontology/ "
                "and validating it",
    trigger_keywords=[
        "ontology", "semantic layer", "entity", "relationship", "metric",
        "business concept", "ai_context", "verbalizes", "maps_to",
        "ontology validate",
    ],
    file_path=str(
        __import__("pathlib").Path(__file__).parent
        / "builtin"
        / "ontology_modeling"
        / "SKILL.md"
    ),
)
def _register_ontology_modeling() -> None:
    """No-op — registration is done via decorator side-effect."""
    pass


@SkillRegistry.register(
    "ontology_query",
    description="Consume the semantic-layer ontology (entities, relationships, metrics) "
                "and translate it into correct analytical SQL over physical tables",
    trigger_keywords=[
        "ontology", "semantic layer", "entity", "relationship", "metric",
        "business term", "concept to sql", "generate sql", "which table",
        "join", "aggregate",
    ],
    file_path=str(
        __import__("pathlib").Path(__file__).parent
        / "builtin"
        / "ontology_query"
        / "SKILL.md"
    ),
)
def _register_ontology_query() -> None:
    """No-op — registration is done via decorator side-effect."""
    pass


@SkillRegistry.register(
    "analysis_workflow",
    description="Step-by-step context-retrieval workflow for answering an analytical "
                "question with SQL (ontology first, then knowledge/ad-hocs/models)",
    trigger_keywords=[
        "analysis", "analyze", "explore data", "ad-hoc", "query workflow",
        "how many", "trend", "breakdown", "report", "insight",
    ],
    file_path=str(
        __import__("pathlib").Path(__file__).parent
        / "builtin"
        / "analysis_workflow"
        / "SKILL.md"
    ),
)
def _register_analysis_workflow() -> None:
    """No-op — registration is done via decorator side-effect."""
    pass


@SkillRegistry.register(
    "analysis_reporting",
    description="Present analytical findings as image-and-text Markdown: conclusion-first "
                "structure, compact tables, and echarts/mermaid chart blocks",
    trigger_keywords=[
        "chart", "visualization", "echarts", "graph", "plot", "report",
        "markdown", "table", "trend chart", "pie", "bar chart",
    ],
    file_path=str(
        __import__("pathlib").Path(__file__).parent
        / "builtin"
        / "analysis_reporting"
        / "SKILL.md"
    ),
)
def _register_analysis_reporting() -> None:
    """No-op — registration is done via decorator side-effect."""
    pass

