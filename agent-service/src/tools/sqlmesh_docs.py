"""Tool: sqlmesh_docs — searchable SQLMesh documentation index.

Builds an in-memory inverted index from the sqlmesh_docs/ Markdown files
at module load time. Provides keyword search, topic browsing, and file-level
lookup for Model Agent / Design Agent to consult precise syntax, configuration,
macros, and engine adapter reference on demand.
"""

import re
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

from agents import RunContextWrapper, function_tool

from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "of", "in", "on",
    "at", "to", "for", "with", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "and",
    "but", "or", "not", "no", "nor", "so", "if", "then", "than", "that",
    "this", "these", "those", "it", "its", "each", "all", "every", "any",
    "both", "few", "more", "most", "other", "some", "such", "only", "own",
    "same", "about", "also", "how", "just", "very", "too", "here", "there",
    "when", "where", "why", "how", "which", "who", "whom",
})

EXCERPT_CHARS = 500
MAX_QUERY_CHARS = 200
MAX_RESULTS = 10
DEFAULT_RESULTS = 5
MAX_OUTPUT_CHARS = 4000

# Regex for SQLMesh technical terms (UPPER_SNAKE, multi_word_snake, @macros, etc.)
_TECH_TERM_RE = re.compile(
    r'\b(?:[A-Z][A-Z_]{2,}|[a-z]+_[a-z]+_[a-z]+|@\w+)\b'
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DocSection:
    """A document section identified by its Markdown heading (## / ### / ####)."""

    title: str
    level: int  # 2 = ##, 3 = ###, 4 = ####
    file_path: str  # relative to sqlmesh_docs/
    category: str  # top-level dir name
    content: str  # trimmed excerpt
    keywords: t.List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Predefined topic → glob patterns
# ---------------------------------------------------------------------------

_TOPIC_PATTERNS: t.Dict[str, t.List[str]] = {
    "concepts":       ["concepts/**/*.md"],
    "guides":         ["guides/**/*.md"],
    "integrations":   ["integrations/**/*.md"],
    "reference":      ["reference/**/*.md"],
    "faq":            ["faq/**/*.md"],
    "engines":        ["integrations/engines/**/*.md"],
    "macros":         ["concepts/macros/**/*.md"],
    "models":         ["concepts/models/**/*.md"],
    "model_kinds":    ["concepts/models/model_kinds.md"],
    "configuration":  ["guides/configuration.md", "reference/configuration.md"],
    "cli":            ["reference/cli.md"],
    "testing":        ["concepts/tests.md", "guides/testing.md"],
}

_TOPIC_DESCRIPTIONS: t.Dict[str, str] = {
    "concepts":      "Core SQLMesh concepts",
    "guides":        "How-to guides and workflows",
    "integrations":  "Third-party integrations (dbt, engines)",
    "reference":     "CLI, configuration, API reference",
    "faq":           "Frequently asked questions",
    "engines":       "Database engine adapters",
    "macros":        "Jinja macros & SQLMesh built-in macros",
    "models":        "Model definitions and kinds",
    "model_kinds":   "Model kind reference (FULL, INCREMENTAL, SCD_TYPE_2, ...)",
    "configuration": "Global + model configuration",
    "cli":           "CLI command reference",
    "testing":       "Tests and audits",
}


# ---------------------------------------------------------------------------
# DocIndex — singleton, built at module load
# ---------------------------------------------------------------------------


class DocIndex:
    """In-memory searchable index of all SQLMesh documentation files."""

    _instance: t.Optional["DocIndex"] = None

    def __init__(
        self,
        sections: t.List[DocSection],
        file_list: t.Dict[str, t.List[str]],
    ) -> None:
        self.sections = sections
        self.file_list = file_list  # category → [relative file paths]
        self._keyword_index: t.Dict[str, t.List[int]] = {}
        self._build_keyword_index()

    # ---- public class methods ----

    @classmethod
    def get(cls) -> "DocIndex":
        """Return the singleton index, building it on first access."""
        if cls._instance is None:
            cls._instance = cls._build()
        return cls._instance

    @classmethod
    def refresh(cls) -> "DocIndex":
        """Rebuild index (e.g. after docs update)."""
        cls._instance = cls._build()
        logger.info("sqlmesh_docs_index_refreshed")
        return cls._instance

    # ---- internal builders ----

    @classmethod
    def _build(cls) -> "DocIndex":
        docs_dir = Path(__file__).parent.parent / "sqlmesh_docs"
        if not docs_dir.is_dir():
            logger.warning("sqlmesh_docs_dir_missing", path=str(docs_dir))
            return cls(sections=[], file_list={})

        sections: t.List[DocSection] = []
        file_list: t.Dict[str, t.List[str]] = {}

        for topic, patterns in _TOPIC_PATTERNS.items():
            paths: t.List[str] = []
            for pattern in patterns:
                for md_file in sorted(docs_dir.glob(pattern)):
                    rel = str(md_file.relative_to(docs_dir))
                    paths.append(rel)
                    sections.extend(_parse_file(md_file, docs_dir, rel))
            if paths:
                file_list[topic] = paths

        logger.info(
            "sqlmesh_docs_index_built",
            files=sum(len(v) for v in file_list.values()),
            sections=len(sections),
        )
        return cls(sections=sections, file_list=file_list)

    def _build_keyword_index(self) -> None:
        """Build inverted index: keyword → list of section indices."""
        for i, sec in enumerate(self.sections):
            for kw in sec.keywords:
                self._keyword_index.setdefault(kw, []).append(i)

    # ---- search / query methods ----

    def search(self, query: str, limit: int = DEFAULT_RESULTS) -> t.List[DocSection]:
        """Keyword search across all indexed sections."""
        limit = min(limit, MAX_RESULTS)
        query = query[:MAX_QUERY_CHARS]
        query_terms = set(
            t for t in query.lower().split() if t not in _STOPWORDS
        )
        if not query_terms:
            return []

        scored: t.List[t.Tuple[int, int]] = []  # (score, section_index)
        seen: t.Set[int] = set()

        for i, sec in enumerate(self.sections):
            # Title match bonus
            title_lower = sec.title.lower()
            title_hits = sum(1 for t in query_terms if t in title_lower)
            # Keyword match
            kw_hits = sum(
                1 for t in query_terms
                if any(t in kw for kw in sec.keywords)
            )
            # Level bonus: h2 gets higher weight
            level_bonus = 3 if sec.level == 2 else 1

            score = title_hits * 5 + kw_hits * 2 + level_bonus
            if score > 0 and i not in seen:
                scored.append((score, i))
                seen.add(i)

        scored.sort(key=lambda x: x[0], reverse=True)
        return [self.sections[i] for _, i in scored[:limit]]

    def get_by_topic(self, topic: str) -> t.List[DocSection]:
        """Return all sections belonging to a topic."""
        patterns = _TOPIC_PATTERNS.get(topic, [])
        if not patterns:
            return []

        prefix_set: t.Set[str] = set()
        for p in patterns:
            prefix = p.replace("**/*.md", "").replace("*.md", "").rstrip("/")
            prefix_set.add(prefix)

        return [
            s for s in self.sections
            if any(s.file_path.startswith(p) for p in prefix_set)
        ]

    def get_by_file(self, file_path: str) -> t.List[DocSection]:
        """Return all sections from a specific file."""
        return [s for s in self.sections if s.file_path == file_path]

    def list_topics(self) -> str:
        """Return a formatted table of available topics."""
        lines = ["## Available Documentation Topics\n"]
        lines.append("| Topic | Files | Description |")
        lines.append("|-------|-------|-------------|")
        for topic in sorted(self.file_list.keys()):
            count = len(self.file_list[topic])
            desc = _TOPIC_DESCRIPTIONS.get(topic, "")
            lines.append(f"| {topic} | {count} files | {desc} |")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# File parser
# ---------------------------------------------------------------------------


def _parse_file(
    file_path: Path,
    docs_dir: Path,
    rel_path: str,
) -> t.List[DocSection]:
    """Parse a Markdown file into sections keyed by ##/###/#### headings."""
    category = rel_path.split("/", 1)[0]

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("sqlmesh_docs_parse_failed", file=rel_path, error=str(e))
        return []

    sections: t.List[DocSection] = []
    current_title: str = ""
    current_level: int = 0
    current_lines: t.List[str] = []

    for line in lines:
        stripped = line.strip()
        # Detect heading: ## / ### / #### (skip single # which is the page title)
        if stripped.startswith("#") and not stripped.startswith("# "):
            # Save previous section
            if current_title and current_lines:
                sections.append(
                    _make_section(current_title, current_level, current_lines,
                                  rel_path, category)
                )
            # Start new section
            level = len(stripped) - len(stripped.lstrip("#"))
            if level <= 1:
                current_title = ""
                current_level = 0
                current_lines = []
                continue
            current_title = stripped.lstrip("#").strip()
            current_level = min(level, 4)  # cap at h4
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_title and current_lines:
        sections.append(
            _make_section(current_title, current_level, current_lines,
                          rel_path, category)
        )

    return sections


def _make_section(
    title: str,
    level: int,
    lines: t.List[str],
    rel_path: str,
    category: str,
) -> DocSection:
    """Build a DocSection from raw lines."""
    body = "".join(lines)
    excerpt = body.strip()[:EXCERPT_CHARS]
    keywords = _extract_keywords(title, excerpt)
    return DocSection(
        title=title,
        level=level,
        file_path=rel_path,
        category=category,
        content=excerpt,
        keywords=keywords,
    )


def _extract_keywords(title: str, content: str) -> t.List[str]:
    """Extract technical keywords from a section title and body."""
    words: t.Set[str] = set()

    # Title words
    for w in re.findall(r'\w+', title.lower()):
        if len(w) > 1 and w not in _STOPWORDS:
            words.add(w)

    # Technical terms from content
    for match in _TECH_TERM_RE.finditer(content[:500]):
        term = match.group(0).lower()
        if term not in _STOPWORDS:
            words.add(term)

    # SQLMesh-specific terms (common in code blocks)
    for term in ("model", "kind", "cron", "grain", "column", "table",
                 "view", "seed", "macro", "audit", "plan", "schema",
                 "incremental", "partition", "connection", "gateway"):
        if term in content.lower():
            words.add(term)

    return sorted(words)


# ---------------------------------------------------------------------------
# Tool decorator
# ---------------------------------------------------------------------------


@ToolRegistry.register(
    "sqlmesh_docs",
    level=1,
    description="Search SQLMesh documentation: syntax, model kinds, macros, "
                "engine configurations, CLI reference, and design patterns",
)
@function_tool
async def sqlmesh_docs(
    ctx: RunContextWrapper[ToolContext],
    query: str = "",
    topic: str = "",
    file_path: str = "",
    list_topics: bool = False,
    limit: int = DEFAULT_RESULTS,
) -> str:
    """Search SQLMesh documentation for syntax, configuration, and best practices.

    Use this tool when you need precise SQLMesh knowledge that isn't in your
    system prompt — model kind parameters, macro syntax, engine-specific
    connection config, CLI commands, etc.

    Args:
        query: Keyword search (natural language or technical terms).
               e.g. "INCREMENTAL_BY_TIME_RANGE time_column"
        topic: Browse all sections under a topic.
               Valid: concepts, guides, integrations, reference, faq,
               engines, macros, models, model_kinds, configuration, cli, testing
        file_path: Get all sections from a specific file.
               e.g. "concepts/models/model_kinds.md"
        list_topics: Set to true to see all available documentation topics.
        limit: Max results (default 5, max 10).

    Returns:
        Formatted search results or topic listing.
    """
    idx = DocIndex.get()

    # Check if index is empty (docs directory missing)
    if not idx.sections and not idx.file_list:
        return (
            "Documentation index is not available. "
            "The sqlmesh_docs/ directory may be missing or empty."
        )

    # Mode 1: list topics
    if list_topics:
        return idx.list_topics()

    # Mode 2: browse by file
    if file_path:
        sections = idx.get_by_file(file_path)
        if not sections:
            return (
                f"File '{file_path}' not found in documentation index.\n\n"
                f"Use `list_topics=true` to see available topics and files."
            )
        return _format_sections(
            sections[:limit],
            title=f"File: {file_path}",
            show_score=False,
            limit=limit,
        )

    # Mode 3: browse by topic
    if topic:
        if topic not in _TOPIC_PATTERNS:
            available = ", ".join(sorted(_TOPIC_PATTERNS.keys()))
            return (
                f"Unknown topic '{topic}'.\n\n"
                f"Available topics: {available}\n\n"
                f"Use `list_topics=true` for descriptions."
            )
        sections = idx.get_by_topic(topic)
        if not sections:
            return (
                f"No sections found for topic '{topic}'.\n\n"
                f"Use `list_topics=true` to see available topics."
            )
        # For topic browsing, return h2 sections first, then h3
        h2 = [s for s in sections if s.level == 2]
        h3 = [s for s in sections if s.level >= 3]
        ordered = h2 + h3
        return _format_sections(
            ordered[:limit],
            title=f"Topic: {topic}",
            show_score=False,
            limit=limit,
        )

    # Mode 4: keyword search (default)
    if not query:
        return (
            "Please provide a search query, topic name, file path, "
            "or set list_topics=true.\n\n"
            "Examples:\n"
            '  - sqlmesh_docs(query="INCREMENTAL_BY_TIME_RANGE time_column")\n'
            '  - sqlmesh_docs(topic="model_kinds")\n'
            '  - sqlmesh_docs(file_path="concepts/models/model_kinds.md")\n'
            '  - sqlmesh_docs(list_topics=true)\n'
        )

    results = idx.search(query, limit=limit)
    if not results:
        return (
            f"No results found for '{query}'.\n\n"
            f"Try different keywords or use `list_topics=true` "
            f"to see available documentation."
        )

    return _format_sections(
        results, title=f'Search: "{query}"', show_score=True, limit=limit,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_sections(
    sections: t.List[DocSection],
    title: str,
    show_score: bool = False,
    limit: int = DEFAULT_RESULTS,
) -> str:
    """Format sections as a readable Markdown result."""
    lines = [f"## {title}", ""]
    total = 0

    for i, sec in enumerate(sections[:limit]):
        header = f"### {i + 1}. {sec.title}"
        meta = f"**File**: {sec.file_path}  |  **Category**: {sec.category}"
        excerpt = sec.content[:EXCERPT_CHARS]

        block = f"{header}\n{meta}\n\n{excerpt}"
        if total + len(block) > MAX_OUTPUT_CHARS:
            lines.append(f"... ({len(sections) - i} more results omitted)")
            break

        lines.append(block)
        lines.append("")
        total += len(block) + 2

    if len(sections) > limit:
        lines.append(f"({len(sections) - limit} more results not shown)")
    elif len(sections) > 0 and total <= MAX_OUTPUT_CHARS:
        lines.append(f"({len(sections)} results total)")

    return "\n".join(lines)
