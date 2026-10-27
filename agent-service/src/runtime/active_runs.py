"""Registry of in-flight chat runs — local handles + distributed visibility.

`active_chat_runs` keeps the LIVE task handle: only the replica that spawned
the run can await/cancel it. The companion `DistributedRunRegistry` makes the
run's EXISTENCE visible to every replica and routes cancellation back to the
owner, so:

- a second POST /chat for a session is refused wherever it lands (409), not
  just on the replica already running it;
- DELETE /cancel reaches the owning replica through a Redis broadcast even
  when the request is load-balanced elsewhere.

Degradation: without Redis both properties narrow to single-replica scope,
which is the behaviour the service had before HA. Nothing raises.

The registry keys are session ids: one chat run per session at a time, which
is the existing (local) invariant, now enforced globally.
"""

import asyncio
import typing as t

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Live task handles, local to this replica:
#   session_id -> {"task": asyncio.Task, "result": streamed_result, "agent_name": str}
active_chat_runs: dict[str, dict[str, t.Any]] = {}

# How long a run's Redis registration survives WITHOUT a refresh. This is the
# crash-recovery bound and nothing else: a live run keeps its registration
# fresh through the heartbeat below, so the TTL does not have to outlive the
# longest plausible chat turn — it only has to outlive one refresh interval.
# Keeping it short bounds how long a session stays locked out after a replica
# dies mid-run (the user's only symptom is "a chat run is already in progress").
_RUN_TTL_SECONDS = 600
_RUN_REFRESH_SECONDS = 180

_registry = None  # DistributedRunRegistry, built lazily (needs a running loop)


def _cancel_local_run(session_id: str) -> bool:
    """Cancel the run on THIS replica. Returns False when it does not own it."""
    run_info = active_chat_runs.get(session_id)
    if not run_info:
        return False
    streamed = run_info.get("result")
    if streamed is not None and hasattr(streamed, "cancel"):
        streamed.cancel()
    task = run_info.get("task")
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


def _get_registry():
    """Lazily build the registry (import inside to avoid cycles at module load)."""
    global _registry
    if _registry is None:
        from datacode_common.redis import DistributedRunRegistry

        _registry = DistributedRunRegistry(
            domain="agent", cancel_local=_cancel_local_run
        )
    return _registry


async def start_run_registry() -> None:
    """Start listening for remote cancel requests (service startup hook)."""
    reg = _get_registry()
    try:
        await reg.start()
    except Exception as exc:
        # Startup must not depend on Redis availability.
        logger.warning("run_registry_start_failed", error=str(exc))


async def stop_run_registry() -> None:
    """Release the registry's subscription (service shutdown hook)."""
    if _registry is not None:
        await _registry.stop()


async def register_chat_run(session_id: str) -> bool:
    """Register a run starting on this replica.

    Returns False when a run for the session already exists on ANY replica —
    callers translate that into the same 409 the local check produces.
    """
    try:
        return await _get_registry().register(
            session_id, ttl_seconds=_RUN_TTL_SECONDS
        )
    except Exception:
        # Redis-side failure: degrade to local-only protection (the caller's
        # local dict check is still authoritative on this replica).
        return True


async def claim_chat_run(session_id: str) -> None:
    """Claim a session for a run this replica MUST execute anyway.

    Resume paths (approval granted, forwarded "continue") are not refusable:
    the SDK run state lives in this process and nowhere else. The claim exists
    only so a concurrent POST /chat elsewhere is refused while the resumed run
    is in flight; losing the race changes nothing, so the result is ignored.
    """
    await register_chat_run(session_id)


def track_chat_run(session_id: str, task: asyncio.Task, *, agent_name: str) -> None:
    """Own a run's bookkeeping for its entire life.

    One place is responsible for the three facts that must stay in sync with a
    running task: the local handle (so cancel can reach it), the registration
    TTL (refreshed while the task lives), and the release of that registration
    when the task ends.

    The release deliberately does NOT depend on ``active_chat_runs`` still
    holding this task. The entry is rewritten during the run (each Runner
    segment records its streamed result), and any path that dropped it early
    used to skip the release entirely — leaking the Redis key and locking the
    session out until the TTL expired.
    """
    active_chat_runs[session_id] = {
        "task": task,
        "result": None,
        "agent_name": agent_name,
    }
    heartbeat = asyncio.create_task(
        _keep_registered(session_id, task), name=f"run_heartbeat:{session_id}"
    )
    task.add_done_callback(
        lambda finished: _release_chat_run(session_id, finished, heartbeat)
    )


async def _keep_registered(session_id: str, task: asyncio.Task) -> None:
    """Refresh the run's registration TTL until its task completes."""
    try:
        while not task.done():
            await asyncio.sleep(_RUN_REFRESH_SECONDS)
            if task.done():
                return
            await refresh_chat_run(session_id)
    except asyncio.CancelledError:
        pass


def _release_chat_run(
    session_id: str, task: asyncio.Task, heartbeat: asyncio.Task
) -> None:
    """Drop the local handle, release the registration, surface failures."""
    heartbeat.cancel()

    current = active_chat_runs.get(session_id)
    # A newer run already owns the session (forwarded resume racing the tail of
    # this task): leave both its handle and its registration alone.
    superseded = current is not None and current.get("task") is not task
    if not superseded:
        active_chat_runs.pop(session_id, None)
        # Done callbacks are sync; the Redis release is fire-and-forget. The
        # CAS release only deletes a key this replica owns.
        asyncio.get_running_loop().create_task(unregister_chat_run(session_id))

    if task.cancelled():
        logger.info("chat_run_cancelled", session_id=session_id)
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "chat_run_failed", session_id=session_id, error=str(exc), exc_info=exc
        )


async def refresh_chat_run(session_id: str) -> None:
    """Extend the run's registration (each new turn of a long session)."""
    try:
        await _get_registry().refresh(session_id, ttl_seconds=_RUN_TTL_SECONDS)
    except Exception:
        pass  # degraded: TTL simply runs shorter


async def unregister_chat_run(session_id: str) -> None:
    """Remove this replica's registration (run finished or was cancelled)."""
    try:
        await _get_registry().unregister(session_id)
    except Exception:
        pass  # TTL reclaims the key if unregister cannot reach Redis


async def is_chat_running_remotely(session_id: str) -> bool:
    """Report whether a run exists on another replica.

    Local runs are the caller's fast path (the dict lookup); this answers the
    question the dict cannot: is a run live anywhere else?
    """
    try:
        return await _get_registry().exists(session_id)
    except Exception:
        return False  # degraded: pretend no remote run exists


async def cancel_chat_run(session_id: str) -> bool:
    """Cancel the session's run wherever it lives.

    Local first (synchronous with the task handle), then broadcast to other
    replicas. True means cancelled locally or a cancel request was delivered;
    False means no live run was found anywhere.

    Cancel is also the user's escape hatch from a stale registration, so when
    nothing is running here the registration is released as well. The CAS
    release only removes a key this replica owns: a run owned by another
    replica keeps its key and is released by its own completion path.
    """
    try:
        cancelled = await _get_registry().cancel(session_id)
    except Exception:
        return _cancel_local_run(session_id)  # degraded: local only

    if session_id not in active_chat_runs:
        await unregister_chat_run(session_id)
    return cancelled
