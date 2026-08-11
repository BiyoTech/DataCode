"""Tool: memory_summarize_session — auto-summarize current session into L1+L2.

Reads recent conversation messages, extracts key events via LLM, and writes
both L1 index entries and L2 summaries. Typically triggered when the session
message count exceeds the auto_summarize_after_turns threshold.
"""

import json
import uuid
import typing as t

from agents import RunContextWrapper, function_tool
from sqlalchemy import select, desc

from src.context.database import (
    MemoryEventRow,
    MemorySummaryRow,
    MessageRow,
    get_session_factory,
)
from src.context.tool_context import ToolContext
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Prompt template for LLM-based summarization
_SUMMARIZE_PROMPT = """Analyze the following conversation and extract key events.

For each key event, provide:
1. event_key: A short descriptive title (max 100 chars)
2. event_type: One of "decision", "action", "insight", "error"
3. summary: A 2-3 sentence description of what happened and why it matters
4. context: A JSON object with relevant metadata (e.g. files involved, tools used)

Return a JSON array of objects with these fields. Extract 1-5 most important events.

Conversation:
{conversation}

Return ONLY valid JSON array, no markdown formatting."""


async def _run_summarize_session(
    workspace_path: str,
    session_id: str,
    max_messages: int = 50,
) -> str:
    """Core summarization logic — callable from both tool decorator and background worker.

    Reads recent messages, extracts key events via LLM, and persists L1+L2 entries.

    Args:
        workspace_path: Current workspace root path.
        session_id: Session ID to summarize.
        max_messages: Maximum number of recent messages to include.

    Returns:
        Summary of what was extracted and stored.
    """
    # Step 1: Load recent messages from current session
    session_factory = get_session_factory()
    async with session_factory() as db_session:
        stmt = (
            select(MessageRow)
            .where(MessageRow.session_id == session_id)
            .order_by(desc(MessageRow.id))
            .limit(max_messages)
        )
        result = await db_session.execute(stmt)
        messages = list(reversed(result.scalars().all()))

    if not messages:
        return "No messages found in current session to summarize."

    # Step 2: Format conversation for LLM
    conversation_lines = []
    for msg in messages:
        content = msg.content[:1000] if msg.content else "(empty)"
        conversation_lines.append(f"[{msg.role}]: {content}")
    conversation_text = "\n".join(conversation_lines)

    # Step 3: Call LLM for extraction using configured summarization model
    from openai import AsyncOpenAI
    from src.config import settings

    # Resolve API credentials from the model config referenced by summarization settings
    summ_cfg = settings.memory_summarization
    model_config = settings.llm.get_model(summ_cfg.model)
    if model_config is None:
        logger.warning("summarization_model_not_found", model_id=summ_cfg.model)
        model_config = settings.llm.get_default_model()

    client = AsyncOpenAI(
        api_key=model_config.api_key,
        base_url=model_config.base_url or None,
    )

    prompt = _SUMMARIZE_PROMPT.format(conversation=conversation_text[:8000])
    try:
        response = await client.chat.completions.create(
            model=summ_cfg.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=summ_cfg.temperature,
            max_tokens=summ_cfg.max_tokens,
        )
        raw_output = response.choices[0].message.content or "[]"
    except Exception as e:
        logger.error("memory_summarize_llm_failed", error=str(e))
        return f"Error: LLM summarization failed — {str(e)}"

    # Step 4: Parse LLM output
    try:
        cleaned = raw_output.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        events_data: t.List[t.Dict] = json.loads(cleaned.strip())
    except (json.JSONDecodeError, IndexError) as e:
        logger.warning("memory_summarize_parse_failed", raw_output=raw_output[:200], error=str(e))
        return "Error: Failed to parse LLM summarization output."

    if not events_data:
        return "LLM extracted no key events from this session."

    # Step 5: Persist L1 + L2 entries
    created_events = []
    async with session_factory() as db_session:
        for item in events_data[:5]:  # Cap at 5 events
            event_key = item.get("event_key", "Unknown event")[:512]
            event_type = item.get("event_type", "action")
            if event_type not in {"decision", "action", "insight", "error"}:
                event_type = "action"

            # L1: Create event index entry
            event_id = str(uuid.uuid4())
            event_row = MemoryEventRow(
                id=event_id,
                workspace_path=workspace_path,
                session_id=session_id,
                event_key=event_key,
                event_type=event_type,
            )
            db_session.add(event_row)

            # L2: Create summary entry
            summary_text = item.get("summary", event_key)
            context_data = item.get("context", {})
            summary_id = str(uuid.uuid4())
            summary_row = MemorySummaryRow(
                id=summary_id,
                event_id=event_id,
                workspace_path=workspace_path,
                summary=summary_text,
                context_json=json.dumps(context_data, ensure_ascii=False),
            )
            db_session.add(summary_row)

            created_events.append(f"- [{event_type}] {event_key}")

        await db_session.commit()

    logger.info(
        "memory_session_summarized",
        session_id=session_id,
        events_created=len(created_events),
    )

    result_lines = [
        f"Session summarized: {len(created_events)} key events extracted and stored.",
        "",
        "Events created:",
    ] + created_events

    return "\n".join(result_lines)


@ToolRegistry.register(
    "memory_summarize_session",
    level=1,
    description="Auto-summarize current session into L1+L2 memory entries",
)
@function_tool
async def memory_summarize_session(
    ctx: RunContextWrapper[ToolContext],
    max_messages: int = 50,
) -> str:
    """Summarize the current session's conversation into L1 index + L2 summary entries.

    Thin wrapper around _run_summarize_session that extracts context from the tool call.

    Args:
        max_messages: Maximum number of recent messages to include in summarization.
            Defaults to 50.

    Returns:
        Summary of what was extracted and stored.
    """
    return await _run_summarize_session(
        workspace_path=ctx.context.workspace_path,
        session_id=ctx.context.session_id,
        max_messages=max_messages,
    )
