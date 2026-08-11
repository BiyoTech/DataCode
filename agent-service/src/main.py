"""Agent Service entry point — app factory and lifespan."""

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
    logger.info("agent_service_starting", ws_port=settings.ws_port)
    logger.info("registered_agents", agents=AgentRegistry.list_agents())
    logger.info("registered_skills", skills=SkillRegistry.list_all())
    yield
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
