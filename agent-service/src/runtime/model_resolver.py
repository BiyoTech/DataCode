"""LLM model resolution for agent runs."""

import threading
import typing as t

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Cache of OpenAIChatCompletionsModel instances keyed by
# (api_key, base_url, model_name). AsyncOpenAI internally maintains an HTTP
# connection pool; creating a fresh client per resolve_model() call leaks
# connections and adds latency. The cache is process-wide and guarded by a
# lock so the (rare) concurrent first-call for the same config doesn't create
# two clients.
_model_cache: t.Dict[t.Tuple[str, str, str], t.Any] = {}
_model_cache_lock = threading.Lock()


def resolve_model(model_id: str) -> t.Optional[t.Any]:
    """Resolve a model_id to an openai-agents compatible Model object.

    Raises:
        ValueError: When a custom base_url is configured without an api_key.
    """
    if not model_id:
        model_cfg = settings.llm.get_default_model()
    else:
        model_cfg = settings.llm.get_model(model_id)
        if not model_cfg:
            logger.warning("model_not_found", requested=model_id, fallback="default")
            model_cfg = settings.llm.get_default_model()

    # A custom endpoint (Azure / self-hosted / proxy) requires an api_key.
    # Previously this fell through to "sk-placeholder", which always produced
    # a 401 — fail fast with an actionable message instead.
    if model_cfg.base_url and not model_cfg.api_key:
        raise ValueError(
            f"Model '{model_cfg.id}' has base_url='{model_cfg.base_url}' but no "
            f"api_key configured. Set it via env "
            f"AGENT_SERVICE_LLM_API_KEY__{model_cfg.id.upper()} or in "
            f"conf/agent-service.yaml."
        )

    if model_cfg.base_url or model_cfg.api_key:
        from openai import AsyncOpenAI
        from agents import OpenAIChatCompletionsModel

        cache_key = (model_cfg.api_key or "", model_cfg.base_url or "", model_cfg.name)
        cached = _model_cache.get(cache_key)
        if cached is not None:
            return cached

        with _model_cache_lock:
            # Double-check under the lock: another thread may have created it.
            cached = _model_cache.get(cache_key)
            if cached is not None:
                return cached

            client = AsyncOpenAI(
                api_key=model_cfg.api_key,
                base_url=model_cfg.base_url or None,
            )
            model = OpenAIChatCompletionsModel(
                model=model_cfg.name,
                openai_client=client,
            )
            _model_cache[cache_key] = model
            logger.info(
                "model_client_created",
                model_name=model_cfg.name,
                base_url=model_cfg.base_url or "default",
                cache_size=len(_model_cache),
            )
            return model

    # No custom endpoint: return the model name string and let the SDK use its
    # default OpenAI client (which respects the OPENAI_API_KEY env var).
    return model_cfg.name
