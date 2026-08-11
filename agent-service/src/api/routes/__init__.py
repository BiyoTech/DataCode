"""Aggregate API routers."""

from fastapi import APIRouter

from src.api.routes.chat import router as chat_router
from src.api.routes.events import router as events_router
from src.api.routes.health import router as health_router
from src.api.routes.sessions import router as sessions_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(sessions_router)
api_router.include_router(events_router)
api_router.include_router(chat_router)
