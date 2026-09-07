"""Agent Service entry point — app factory and lifespan."""

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from src.agents.registry import AgentRegistry, load_all_agents
from src.api.routes import api_router
from src.config import settings
from src.context.database import close_db, init_db
from src.skills import SkillRegistry
from src.tools.registry import load_all_tools
from src.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    load_all_agents()
    load_all_tools()
    SkillRegistry._load_builtin()
    await init_db()
    # Cross-replica coordination: makes chat runs visible to every replica and
    # routes cancellation to whichever replica owns the run. Degrades to
    # single-replica behaviour when Redis is unavailable.
    from src.runtime.active_runs import start_run_registry, stop_run_registry
    from src.runtime.session_coordination import get_session_coordination

    await start_run_registry()
    # Session-state coordination (approval/resume routing, cache invalidation)
    # shares the same degradation doctrine: Redis down means local-only.
    await get_session_coordination().start()

    # Session data retention (design 05 §2.3): timeline/memory events are pure
    # append streams; this is their expiry.
    retention_task = None
    if settings.retention_enabled:
        from src.context.retention import get_session_retention

        retention_task = asyncio.create_task(
            get_session_retention().start_periodic(settings.retention_interval)
        )

    logger.info("agent_service_starting", ws_port=settings.ws_port)
    logger.info("registered_agents", agents=AgentRegistry.list_agents())
    logger.info("registered_skills", skills=SkillRegistry.list_all())
    try:
        yield
    finally:
        if retention_task is not None:
            retention_task.cancel()
        await get_session_coordination().stop()
        await stop_run_registry()
        await close_db()
        logger.info("agent_service_stopping")


def create_app() -> FastAPI:
    application = FastAPI(
        title="Agent Service",
        description="AI Agent orchestration with ACP v2 SSE + HTTP protocol",
        version="0.4.0",
        lifespan=lifespan,
    )
    application.include_router(api_router)
    return application


app = create_app()


if __name__ == "__main__":
    setup_logging()
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.ws_port,
        reload=True,
    )
