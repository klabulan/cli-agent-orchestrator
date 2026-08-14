"""Session service for session-level operations.

This module provides session management functionality for CAO, where a "session"
corresponds to a tmux session that may contain multiple terminal windows (agents).

Session Hierarchy:
- Session: A tmux session (e.g., "cao-my-project")
  - Terminal: A tmux window within the session (e.g., "developer-abc123")
    - Provider: The CLI agent running in the terminal (e.g., KiroCliProvider)

Key Operations:
- list_sessions(): Get all CAO-managed sessions (filtered by SESSION_PREFIX)
- get_session(): Get session details including all terminal metadata
- delete_session(): Clean up session, providers, database records, and tmux session

Session Lifecycle:
1. create_terminal() with new_session=True creates a new tmux session
2. Additional terminals are added via create_terminal() with new_session=False
3. delete_session() removes the entire session and all contained terminals
"""

import logging
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.clients.tmux import TmuxLookupError
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateSessionEvent,
    PostKillSessionEvent,
)
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import clear_session_env
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

logger = logging.getLogger(__name__)


async def create_session(
    provider: str | None,
    agent_profile: str,
    session_name: str | None = None,
    working_directory: str | None = None,
    allowed_tools: list[str] | None = None,
    registry: PluginRegistry | None = None,
    env_vars: dict[str, str] | None = None,
    engine: KiroEngine | str | None = None,
    initial_message: str | None = None,
    initial_message_orchestration_type: OrchestrationType | None = None,
    model: str | None = None,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Terminal:
    """Create a new session by creating its initial terminal.

    ``env_vars`` are operator-forwarded env vars from ``cao launch --env``.
    They are persisted on the session record so every worker spawned later
    in the same session inherits them. See issue #248.

    When ``initial_message`` is provided, the initial terminal uses the
    existing deferred-init path so provider initialization and delivery can
    continue after the session response. Omitting it preserves the synchronous
    initialization behavior used by existing callers.
    On the deferred path, the ``post_create_session`` plugin event is dispatched
    before provider initialization and message delivery finish.

    ``group``/``metadata`` are the #432 discovery fields, set on the initial
    terminal at creation time (``group`` is also updatable later via
    ``PATCH /terminals/{id}/group``, ``metadata`` via the ``update_metadata``
    MCP tool).
    """
    if initial_message == "":
        raise ValueError("initial_message must not be empty")
    if initial_message is None and initial_message_orchestration_type is not None:
        raise ValueError("initial_message_orchestration_type requires initial_message")

    if provider is None:
        resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
    else:
        resolved_provider = provider

    terminal = await create_terminal(
        provider=resolved_provider,
        agent_profile=agent_profile,
        session_name=session_name,
        new_session=True,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
        registry=registry,
        env_vars=env_vars,
        engine=engine,
        defer_init=initial_message is not None,
        initial_message=initial_message,
        initial_message_orchestration_type=initial_message_orchestration_type,
        model=model,
        group=group,
        metadata=metadata,
    )
    dispatch_plugin_event(
        registry,
        "post_create_session",
        PostCreateSessionEvent(
            session_id=terminal.session_name,
            session_name=terminal.session_name,
        ),
    )
    return terminal


def list_sessions(strict: bool = False) -> List[Dict]:
    """List all CAO-managed sessions from the backend.

    ``strict`` (harness-control#840, the "unfixed half"): when False (the
    default, preserved for the SSE dashboard/fleet snapshot builders that are
    explicitly failure-isolated and want an empty snapshot on any backend
    hiccup), a ``TmuxLookupError`` -- the backend saying "I could not READ the
    tmux server", NOT "there are no sessions" -- is still degraded to ``[]``.

    When True (the ``GET /sessions`` route, which the harness-control gateway
    polls as its authoritative substrate-PRESENCE signal), that same
    "could-not-read" condition is RE-RAISED so the route can answer 5xx instead
    of a fabricated ``200 []``. A fabricated empty list is exactly what drove
    the #840 flap: the gateway read it as fleet-wide substrate loss and re-minted
    every live session's root terminal id every poll. A genuine empty list (the
    backend read fine and there really are no sessions) is unaffected either way;
    only the unreadable-backend case changes, and only in ``strict`` mode. The
    gateway already treats a non-404 error from this endpoint as "skip this poll,
    do not alarm" (its own positive-evidence recovery design), so surfacing the
    failure is strictly safer than hiding it behind an empty list.
    """
    try:
        tmux_sessions = get_backend().list_sessions()
        return [s for s in tmux_sessions if s["id"].startswith(SESSION_PREFIX)]
    except TmuxLookupError:
        if strict:
            # "Could not read the substrate" must never masquerade as "no
            # sessions" for the gateway's substrate-presence poll (#840).
            raise
        logger.error("Failed to list sessions: tmux listing unreadable (transient)")
        return []
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals."""
    try:
        backend = get_backend()
        # session_exists() is the AUTHORITATIVE existence check. On the tmux
        # backend it falls back to a direct `tmux has-session` probe when the
        # listing cannot be parsed (clients/tmux.py::session_exists), so it does
        # NOT spuriously answer False on a transient. If it says the session is
        # gone, that is a real 404.
        if not backend.session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        # harness-control#840 (the "listed but detail 404s" flap driver, fixed
        # here at the source). The pre-fix code additionally REQUIRED the session
        # to appear in a SECOND, independent list_sessions() round trip and 404'd
        # it otherwise. That is a TOCTOU with a live-session false-negative:
        # TmuxClient.list_sessions() swallows a transient generic tmux error to
        # [] (see its own body -- only a parse failure raises; everything else
        # returns an empty list), so a session that session_exists() *just*
        # confirmed LIVE could still 404 here purely because this redundant
        # listing momentarily came back empty -- while GET /sessions, which a
        # client polls a beat apart, still reported it. Downstream (the
        # harness-control gateway) read that "listed but detail 404s" as substrate
        # loss and tore live sessions down on wake (12 fatal give-ups on
        # 2026-08-13). A session already confirmed to exist is therefore NEVER
        # 404'd merely for being absent from this one snapshot: use its real
        # listing record when present, else synthesize a minimal one. status
        # defaults to "detached" for the synthesized case; it is cosmetic
        # (attached-clients flag) and the per-terminal status enriched below is
        # derived independently and is unaffected.
        # The session's existence is already authoritative (session_exists above).
        # This listing is only to enrich the cosmetic session_data (attached-clients
        # flag). harness-control#840: now that a transient tmux read raises
        # TmuxLookupError instead of degrading to [] (clients/tmux.py::_read_listing),
        # a hiccup here must NOT bubble up and 500 a session we JUST confirmed live --
        # that would re-open the very "confirmed live but detail errors" flap 56e67499
        # closed. Fall back to the synthesized record, exactly as for an absent-from-
        # snapshot session.
        try:
            listing = backend.list_sessions()
        except TmuxLookupError:
            listing = []
        session_data = next((s for s in listing if s["id"] == session_name), None)
        if session_data is None:
            session_data = {"id": session_name, "name": session_name, "status": "detached"}

        terminals = list_terminals_by_session(session_name)
        # Enrich each terminal with its live status. list_terminals_by_session
        # reads only the DB row (no status column), but callers monitoring an
        # orchestration — the web UI, and the cao-ops-mcp get_session_info tool
        # an external supervisor polls — need to distinguish
        # IDLE/PROCESSING/COMPLETED/ERROR per terminal. status_monitor is the
        # single source of truth and is backend-aware (tmux push vs herdr
        # native), so derive it here rather than persisting a stale column.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        for terminal in terminals:
            terminal["status"] = status_monitor.get_status(terminal["id"]).value
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(session_name: str, registry: PluginRegistry | None = None) -> Dict:
    """Delete session and cleanup.

    Returns:
        Dict with 'deleted' (list of deleted session names) and 'errors' (list of error dicts).
    """
    result: Dict = {"deleted": [], "errors": []}
    try:
        session_alive = get_backend().session_exists(session_name)

        from cli_agent_orchestrator.services import terminal_service

        terminals = list_terminals_by_session(session_name)

        # Clean up each terminal (snapshot, kill window, FIFO reader,
        # status buffer, provider, DB) via the event-driven teardown path.
        for terminal in terminals:
            try:
                terminal_service.delete_terminal(terminal["id"], registry=registry)
            except Exception as e:
                logger.warning(f"Failed to cleanup terminal {terminal['id']}: {e}")

        # Kill backend session only if it still exists
        if session_alive:
            get_backend().kill_session(session_name)

        # Drop the per-session forwarded-env mapping (issue #248). Safe
        # even when no vars were forwarded — the helper is a no-op then.
        clear_session_env(session_name)

        result["deleted"].append(session_name)
        logger.info(f"Deleted session: {session_name}")
        dispatch_plugin_event(
            registry,
            "post_kill_session",
            PostKillSessionEvent(session_id=session_name, session_name=session_name),
        )
        return result

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
