"""Background memory summarization after agent runs."""

import asyncio
import os

from src.context.session import SessionContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

_SUMMARIZE_THRESHOLD_DEFAULT = 10


async def memory_post_process(session: SessionContext) -> None:
    """Check strategy and trigger memory summarization if threshold met."""
    try:
        threshold = _SUMMARIZE_THRESHOLD_DEFAULT
        config_path = os.path.join(session.workspace_path, ".agent", "config.yaml")
        if os.path.exists(config_path):
            import yaml

            def _read_config():
                with open(config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}

            ws_config = await asyncio.to_thread(_read_config)
            memory_cfg = ws_config.get("agent", {}).get("memory", {})
            if not memory_cfg.get("enabled", True):
                return
            threshold = memory_cfg.get("auto_summarize_after_turns", _SUMMARIZE_THRESHOLD_DEFAULT)

        last_summarized = session.last_summarized_message_count or 0
        if session.message_count - last_summarized < threshold:
            return

        logger.info(
            "memory_post_process_triggered",
            session_id=session.session_id,
            message_count=session.message_count,
            last_summarized=last_summarized,
            threshold=threshold,
        )

        # Order matters: run the LLM summarization FIRST, and only advance
        # last_summarized_message_count after it succeeds. If we advance the
        # counter before the LLM call and the call then fails, the trigger
        # condition (message_count - last_summarized < threshold) would never
        # be satisfied again — the session would be permanently skipped.
        from src.tools.memory_summarize_session import _run_summarize_session

        await _run_summarize_session(
            workspace_path=session.workspace_path,
            session_id=session.session_id,
        )

        session.last_summarized_message_count = session.message_count
        await _update_last_summarized_count(session.session_id, session.message_count)
    except Exception as e:
        logger.error("memory_post_process_error", session_id=session.session_id, error=str(e))


async def _update_last_summarized_count(session_id: str, count: int) -> None:
    from sqlalchemy import update

    from src.context.database import SessionRow, get_session_factory

    try:
        async with get_session_factory()() as db:
            stmt = (
                update(SessionRow)
                .where(SessionRow.id == session_id)
                .values(last_summarized_message_count=count)
            )
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
        logger.warning("update_last_summarized_count_failed", session_id=session_id, error=str(e))
