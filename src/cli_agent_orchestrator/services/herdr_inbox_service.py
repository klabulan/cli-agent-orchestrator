"""HerdrInboxService — socket event-based inbox delivery for herdr backend.

Replaces the pipe-pane + file watchdog approach with herdr's native socket API.
Subscribes to a broadcast pane.updated event (whose payload carries
agent_status) and delivers pending inbox messages when a pane transitions to
idle or done.

Design:
- Maintains a pane_id → terminal_id map for managed panes
- Subscribes once to a broadcast pane.updated (no pane_id) covering all panes,
  so a newly registered pane's events already arrive — registration updates the
  map only and never re-subscribes or forces a reconnect
- Reconnects with exponential backoff on socket disconnect
- Supplements with periodic pane read for kiro-cli (working >30s check)
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Callable, Dict, Optional, Set

from cli_agent_orchestrator.constants import (
    RECONCILE_ABSENT_GRACE_SECONDS,
    RECONCILE_ABSENT_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds


class HerdrInboxService:
    """Event-driven inbox delivery service using herdr socket API.

    Subscribes to agent status events for managed panes and delivers
    pending messages when agents become idle/done.
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        delivery_callback: Optional[Callable[[str], None]] = None,
        herdr_session: str = "cao",
    ) -> None:
        """Initialize the inbox service.

        Args:
            socket_path: Path to herdr socket. None = auto-detect from env.
            delivery_callback: Function to call for message delivery.
                Signature: callback(terminal_id) → checks and delivers pending messages.
            herdr_session: Name of the herdr session to connect to. Used to
                derive the default socket path and prefix CLI calls.
        """
        self._herdr_session = herdr_session
        self._socket_path = socket_path or self._default_socket_path(herdr_session)
        self._delivery_callback = delivery_callback

        # Managed pane tracking
        self._pane_to_terminal: Dict[str, str] = {}  # pane_id → terminal_id
        self._terminal_to_pane: Dict[str, str] = {}  # terminal_id → pane_id

        # Kiro-specific tracking for supplement check
        self._kiro_terminals: Set[str] = set()  # terminal_ids using kiro-cli
        self._working_since: Dict[str, float] = {}  # terminal_id → timestamp

        # Teardown grace: a terminal is reaped only after being AUTHORITATIVELY
        # confirmed-absent (its tab genuinely gone from a successfully-read
        # snapshot) across RECONCILE_ABSENT_THRESHOLD consecutive observations
        # AND for RECONCILE_ABSENT_GRACE_SECONDS of wall-clock. A transient read
        # failure is UNKNOWN and never advances these — see the module constants
        # and Tasks/20260814_readfail_never_dead/design.md.
        self._absent_since: Dict[str, float] = {}  # terminal_id → monotonic of first confirmed-absent
        self._absent_count: Dict[str, int] = {}  # terminal_id → consecutive confirmed-absent count

        # Workspace tracking for lifecycle events
        self._workspace_to_session: Dict[str, str] = {}  # workspace_id → session_name

        # Connection state
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._backoff = _BACKOFF_BASE

    @staticmethod
    def _default_socket_path(session_name: str = "cao") -> str:
        """Determine default herdr socket path for a named session.

        The default session (name ``"default"``) uses a flat path:
        ``~/.config/herdr/herdr.sock``.

        Named sessions use a sessions subdirectory:
        ``~/.config/herdr/sessions/<session_name>/herdr.sock``.

        Args:
            session_name: Herdr session name. Defaults to ``"cao"``.
        """
        import os
        from pathlib import Path

        # Check XDG_CONFIG_HOME first, fallback to ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        if session_name == "default":
            return f"{config_home}/herdr/herdr.sock"
        return f"{config_home}/herdr/sessions/{session_name}/herdr.sock"

    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None:
        """Register a terminal for event-based inbox delivery.

        Args:
            terminal_id: CAO terminal identifier
            pane_id: Current herdr compact pane_id
            is_kiro: Whether this terminal runs kiro-cli (enables supplement check)
        """
        self._pane_to_terminal[pane_id] = terminal_id
        self._terminal_to_pane[terminal_id] = pane_id
        if is_kiro:
            self._kiro_terminals.add(terminal_id)

        logger.info(f"Registered terminal {terminal_id} (pane={pane_id}, kiro={is_kiro})")

    def unregister_terminal(self, terminal_id: str) -> None:
        """Remove a terminal from managed set.

        Args:
            terminal_id: Terminal to unregister
        """
        pane_id = self._terminal_to_pane.pop(terminal_id, None)
        if pane_id:
            self._pane_to_terminal.pop(pane_id, None)
        self._kiro_terminals.discard(terminal_id)
        self._working_since.pop(terminal_id, None)
        logger.info(f"Unregistered terminal {terminal_id}")

    async def start(self) -> None:
        """Start the event loop: wait for first terminal, then connect and listen."""
        # Run DB cleanup before starting the socket loop so ghost records from
        # prior server runs are removed even when no terminals are registered yet.
        await self._startup_db_cleanup()
        kiro_task = asyncio.ensure_future(self._kiro_supplement_loop())
        try:
            await self._socket_loop()
        finally:
            kiro_task.cancel()

    async def _startup_db_cleanup(self) -> None:
        """Delete ghost DB terminals whose herdr tabs no longer exist.

        Runs once at server startup before any pane registrations.  Cannot
        rely on _pane_to_terminal (empty at startup) or _workspace_to_session
        (populated later by _reconcile).  Builds the workspace map directly
        from a herdr api snapshot.
        """
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            list_terminals_by_session,
        )

        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.debug("Startup DB cleanup: no snapshot, skipping")
            return

        # workspace_id -> label (= CAO session name). Skip malformed records.
        workspace_to_session = {
            ws["workspace_id"]: ws["label"]
            for ws in snapshot.get("workspaces", [])
            if ws.get("workspace_id") and ws.get("label")
        }

        # workspace_id -> set of live tab labels (= CAO window names)
        live_tabs_by_workspace: Dict[str, set] = {}
        for tab in snapshot.get("tabs", []):
            ws_id = tab.get("workspace_id", "")
            label = tab.get("label", "")
            if ws_id and label:
                live_tabs_by_workspace.setdefault(ws_id, set()).add(label)

        deleted = 0
        for ws_id, session_name in workspace_to_session.items():
            live_labels = live_tabs_by_workspace.get(ws_id, set())
            db_terminals = list_terminals_by_session(session_name)
            for term in db_terminals:
                window = term.get("tmux_window", "")
                if window and window not in live_labels:
                    logger.info(
                        f"Startup DB cleanup: deleting ghost terminal {term['id']} "
                        f"({session_name}:{window}) — tab not in herdr"
                    )
                    try:
                        delete_terminal(term["id"])
                        deleted += 1
                    except Exception as e:
                        logger.warning(
                            f"Startup DB cleanup: failed to delete ghost terminal "
                            f"{term['id']}: {e}"
                        )

        if deleted:
            logger.info(f"Startup DB cleanup: removed {deleted} ghost terminal(s)")
        else:
            logger.debug("Startup DB cleanup: no ghost terminals found")

    async def _kiro_supplement_loop(self) -> None:
        """Periodically check kiro terminals stuck in working state."""
        while True:
            await asyncio.sleep(10.0)
            try:
                await self.check_kiro_supplements()
            except Exception:
                logger.debug("Kiro supplement check error", exc_info=True)

    async def _socket_loop(self) -> None:
        """Connect to herdr socket and listen for events with reconnect.

        Defers connection until at least one terminal is registered. This avoids
        the disconnect/reconnect churn caused by herdr closing idle connections
        that have no active subscriptions.
        """
        while True:
            # Wait until there is at least one pane to subscribe to
            while not self._pane_to_terminal:
                await asyncio.sleep(0.5)

            try:
                await self._connect()

                # Reconcile map against live herdr state before subscribing
                await self._reconcile()

                # Subscribe to everything in ONE events.subscribe call: a single
                # broadcast pane.updated (no pane_id) plus the lifecycle events.
                # herdr resets the connection on a second events.subscribe, so
                # this must be a single combined call.
                await self._subscribe_all_events()

                self._backoff = _BACKOFF_BASE  # Reset backoff after successful setup

                # Listen for events
                await self._event_loop()

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

    def _fetch_snapshot(self) -> Optional[dict]:
        """Return herdr's full live session snapshot in one socket call.

        `herdr api snapshot` returns result.snapshot with panes[]/tabs[]/
        workspaces[]. Each pane carries pane_id, terminal_id, agent_status,
        tab_id, workspace_id; each tab carries tab_id, label, workspace_id;
        each workspace carries workspace_id, label. Replaces the former
        pane-list + workspace-list + tab-list subprocess fan-out.

        Returns None on any failure (non-zero exit, timeout, missing binary,
        or malformed output). This is the single entry point all snapshot reads
        route through, so it swallows the same broad error set as the file's
        other herdr-subprocess helpers rather than letting one bad response
        kill the socket loop.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "api", "snapshot"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                # repr() the stderr: it can echo user-controlled labels/args and
                # may contain newlines/control chars that would otherwise forge
                # log lines. Matches the escaping used elsewhere in the codebase.
                logger.warning("Snapshot: `api snapshot` failed: %r", result.stderr)
                return None
            snapshot = json.loads(result.stdout)["result"]["snapshot"]
            if not isinstance(snapshot, dict):
                logger.warning("Snapshot: result.snapshot is not a dict; ignoring")
                return None
            return snapshot
        except (
            subprocess.SubprocessError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as e:
            logger.warning(f"Snapshot: failed to fetch/parse: {e}")
            return None

    def _clear_absent(self, terminal_id: str) -> None:
        """Reset a terminal's confirmed-absent grace counters.

        Called the moment a terminal is observed live again (pane re-mapped, or
        its tab label present in a fresh snapshot), so a brief flap never
        accumulates toward teardown.
        """
        self._absent_since.pop(terminal_id, None)
        self._absent_count.pop(terminal_id, None)

    def _confirm_absent_or_defer(
        self, terminal_id: str, now: float, decided: Dict[str, bool]
    ) -> bool:
        """Record one AUTHORITATIVE confirmed-absent observation; decide teardown.

        Call ONLY when the tab is genuinely absent from a snapshot that was read
        SUCCESSFULLY (so absence is authoritative, not a read failure). Returns
        True when the terminal has now been confirmed-absent for at least
        RECONCILE_ABSENT_THRESHOLD consecutive observations AND at least
        RECONCILE_ABSENT_GRACE_SECONDS of wall-clock — the two-timer grace that
        keeps a single degraded read (or one partial-but-parsed snapshot) from
        destroying a live terminal. Returns False to defer (leave it, retry next
        reconcile).

        ``decided`` memoizes the verdict within a single reconcile pass so a
        terminal reachable from more than one loop (ghost-DB cross-check and the
        stale-pane diff) is counted at most once per pass.
        """
        if terminal_id in decided:
            return decided[terminal_id]

        self._absent_count[terminal_id] = self._absent_count.get(terminal_id, 0) + 1
        self._absent_since.setdefault(terminal_id, now)

        count = self._absent_count[terminal_id]
        elapsed = now - self._absent_since[terminal_id]
        tear_down = (
            count >= RECONCILE_ABSENT_THRESHOLD and elapsed >= RECONCILE_ABSENT_GRACE_SECONDS
        )
        if tear_down:
            # Fresh start if it somehow reappears after we tear it down.
            self._clear_absent(terminal_id)
        else:
            logger.info(
                "Reconcile: %s confirmed-absent (%d/%d obs, %.1f/%.1fs) — deferring teardown",
                terminal_id,
                count,
                RECONCILE_ABSENT_THRESHOLD,
                elapsed,
                RECONCILE_ABSENT_GRACE_SECONDS,
            )
        decided[terminal_id] = tear_down
        return tear_down

    async def _reconcile(self) -> None:
        """Reconcile _pane_to_terminal map against live herdr state.

        Prunes stale pane entries, deletes orphaned DB terminal records,
        and kills workspaces with zero live terminals.

        Teardown invariant: a transient tmux/herdr READ failure is UNKNOWN,
        never "dead". A terminal is reaped only after its tab is AUTHORITATIVELY
        confirmed absent from a successfully-read snapshot, and only after the
        RECONCILE_ABSENT_THRESHOLD/RECONCILE_ABSENT_GRACE_SECONDS grace. This is
        what stopped the 2026-08-14 read-failure fleet-death; see
        Tasks/20260814_readfail_never_dead/.
        """
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            get_terminal_metadata,
            list_terminals_by_session,
        )

        # One socket call replaces the former pane-list + workspace-list +
        # tab-list subprocess fan-out. All three data structures below are derived
        # from this single snapshot.
        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.warning("Reconcile: no snapshot, skipping")
            return

        # Live pane_ids (from snapshot.panes).
        live_pane_ids = {p["pane_id"] for p in snapshot.get("panes", []) if p.get("pane_id")}

        # workspace_id -> label (= CAO session name), from snapshot.workspaces.
        # Skip malformed records (missing id/label) rather than letting a
        # KeyError escape _reconcile and kill the socket loop — matches the
        # defensive .get() style used in the tabs loop below.
        self._workspace_to_session = {
            ws["workspace_id"]: ws["label"]
            for ws in snapshot.get("workspaces", [])
            if ws.get("workspace_id") and ws.get("label")
        }

        # workspace_id -> set of live tab labels (= CAO window names), from
        # snapshot.tabs. `all_live_tab_labels` is the flat union — the
        # AUTHORITATIVE liveness signal for a tab label, derived from THIS
        # already-successful snapshot read (no extra subprocess that could fail
        # open toward delete, as the old per-terminal `_label_still_live` call
        # did under load).
        live_tabs_by_workspace: Dict[str, set] = {}
        all_live_tab_labels: Set[str] = set()
        for tab in snapshot.get("tabs", []):
            ws_id = tab.get("workspace_id", "")
            label = tab.get("label", "")
            if ws_id and label:
                live_tabs_by_workspace.setdefault(ws_id, set()).add(label)
            if label:
                all_live_tab_labels.add(label)

        # Single monotonic clock + per-pass memo for every confirmed-absent
        # decision this reconcile makes (see _confirm_absent_or_defer).
        now = time.monotonic()
        absent_decided: Dict[str, bool] = {}

        # DB cross-check: find terminals in DB whose tab no longer exists in herdr.
        # This catches ghost records from previous server runs where _pane_to_terminal
        # starts empty (so the stale-pane diff below produces nothing).
        #
        # A tab absent from a SUCCESSFUL snapshot is authoritative — but still
        # routed through the grace gate so a single partial-but-parsed snapshot
        # cannot reap a live terminal on the first pass.
        for ws_id, session_name in self._workspace_to_session.items():
            live_labels = live_tabs_by_workspace.get(ws_id, set())
            db_terminals = list_terminals_by_session(session_name)
            for term in db_terminals:
                window = term.get("tmux_window", "")
                if window and window not in live_labels:
                    if not self._confirm_absent_or_defer(term["id"], now, absent_decided):
                        continue
                    logger.info(
                        f"Reconcile: deleting ghost terminal {term['id']} "
                        f"({session_name}:{window}) — tab absent past grace"
                    )
                    try:
                        delete_terminal(term["id"])
                    except Exception as e:
                        logger.warning(
                            f"Reconcile: failed to delete ghost terminal {term['id']}: {e}"
                        )

        # Find stale panes: stored pane_id no longer in herdr's live pane list.
        #
        # A stale pane_id does NOT mean the terminal is dead. herdr renumbers
        # compact pane_ids when a sibling tab in the workspace closes, so a
        # still-running terminal's stored pane_id can fall out of the live list
        # while its tab is very much alive. Identity must come from the durable
        # tab label (tmux_window), never the ephemeral pane_id.
        stale_pane_ids = set(self._pane_to_terminal.keys()) - live_pane_ids

        # Any terminal whose pane_id is still live is, by definition, live now —
        # clear any confirmed-absent run it had accumulated so a recovered flap
        # never carries stale absence toward a later teardown.
        for pane_id in set(self._pane_to_terminal.keys()) & live_pane_ids:
            tid = self._pane_to_terminal.get(pane_id)
            if tid:
                self._clear_absent(tid)

        if not stale_pane_ids:
            logger.debug("Reconcile: all panes live, nothing to prune")
            return

        # Live workspace labels, used to gate workspace teardown below: never
        # kill a workspace whose label is still present in herdr.
        live_workspace_labels = set(self._workspace_to_session.values())

        # Sessions that genuinely lost a terminal (deleted, not re-mapped).
        affected_sessions: Set[str] = set()
        remapped = 0
        deleted = 0

        for pane_id in stale_pane_ids:
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                self._pane_to_terminal.pop(pane_id, None)
                continue

            # Session/window identity before any mutation.
            meta = get_terminal_metadata(terminal_id)
            term_session: Optional[str] = meta["tmux_session"] if meta else None
            term_window: Optional[str] = meta["tmux_window"] if meta else None

            # R1 — tab label present in the (successful) snapshot ⇒ the tab is
            # AUTHORITATIVELY LIVE; the pane_id was merely renumbered. Re-map it.
            # A failure to re-resolve the new pane_id is a transient READ error
            # (UNKNOWN), NOT death — the tab is confirmed live. Leave the maps
            # untouched and retry next reconcile; NEVER delete here. This exact
            # line ("tab live but pane re-resolve failed … deleting") destroyed a
            # live fleet on 2026-08-14; a read failure must never mean dead.
            if term_window and term_window in all_live_tab_labels:
                self._clear_absent(terminal_id)
                try:
                    # Invalidate pane cache so get_pane_id does a fresh label-based
                    # lookup instead of returning the stale pane_id we just proved
                    # is no longer live. See PR #309 review comment.
                    backend = get_backend()
                    if hasattr(backend, "_pane_cache"):
                        backend._pane_cache.pop(terminal_id, None)
                    new_pane_id = backend.get_pane_id(terminal_id, term_session or "", term_window)
                except Exception as e:
                    logger.debug(
                        "Reconcile: tab %s live but pane re-resolve read failed for %s (%s); "
                        "UNKNOWN, not dead — leaving intact, will retry next reconcile",
                        term_window,
                        terminal_id,
                        e,
                    )
                    continue
                self._pane_to_terminal.pop(pane_id, None)
                self._pane_to_terminal[new_pane_id] = terminal_id
                self._terminal_to_pane[terminal_id] = new_pane_id
                logger.info(
                    "Reconcile: re-mapped %s %s -> %s (pane renumbered, tab still live)",
                    terminal_id,
                    pane_id,
                    new_pane_id,
                )
                remapped += 1
                continue

            # R2 — tab label ABSENT from the successful snapshot ⇒ authoritatively
            # gone THIS pass. Route through the grace gate: reap only after
            # THRESHOLD consecutive confirmed-absent observations AND
            # GRACE_SECONDS. Defer otherwise (leave maps + DB row for retry).
            if not self._confirm_absent_or_defer(terminal_id, now, absent_decided):
                continue

            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            try:
                delete_terminal(terminal_id)
                deleted += 1
            except Exception as e:
                logger.warning(f"Reconcile: failed to delete terminal {terminal_id}: {e}")

            if term_session:
                affected_sessions.add(term_session)

        # Kill a workspace only when its label is gone from herdr AND no managed
        # terminal remains for the session. A live label means the workspace is
        # alive and its panes were merely renumbered — killing it would tear down
        # working agents.
        if affected_sessions:
            remaining_by_session: Dict[str, int] = {s: 0 for s in affected_sessions}
            for tid in self._terminal_to_pane:
                meta = get_terminal_metadata(tid)
                if meta and meta["tmux_session"] in remaining_by_session:
                    remaining_by_session[meta["tmux_session"]] += 1

            for session_name, remaining in remaining_by_session.items():
                if remaining == 0 and session_name not in live_workspace_labels:
                    try:
                        get_backend().kill_session(session_name)
                        logger.info(f"Reconcile: killed empty workspace {session_name}")
                    except Exception as e:
                        logger.warning(f"Reconcile: failed to kill workspace {session_name}: {e}")

        logger.info(
            "Reconcile: %d stale pane(s) — %d re-mapped, %d deleted",
            len(stale_pane_ids),
            remapped,
            deleted,
        )

    async def _connect(self) -> None:
        """Connect to the herdr socket."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        logger.info(f"Connected to herdr socket: {self._socket_path}")

    async def _subscribe_all_events(self) -> None:
        """Subscribe to all events in a SINGLE events.subscribe call.

        herdr (0.7.5) resets the entire connection when it receives a second
        events.subscribe on a connection that already has an active
        subscription, so this must remain exactly one events.subscribe per
        connection.

        The subscription is a broadcast pane.updated (sent with NO pane_id):
        herdr streams it for every pane and its payload carries agent_status,
        so a single broadcast subscription replaces the former per-pane
        pane.agent_status_changed subscriptions. This is independent of
        _pane_to_terminal — no per-pane enumeration is needed. The pane.closed
        and workspace.closed lifecycle events are sent in the same call.
        """
        subscriptions = [
            {"type": "pane.updated"},
            {"type": "pane.closed"},
            {"type": "workspace.closed"},
        ]
        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        logger.info(
            "Subscribed to broadcast pane.updated + lifecycle events "
            "in one events.subscribe call"
        )

    async def _event_loop(self) -> None:
        """Listen for events and dispatch delivery."""
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("Socket closed")

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            # herdr identifies the event in the "event" key. Lifecycle events use
            # underscore names (pane_closed / workspace_closed); the agent-status
            # event uses the dotted name (pane.agent_status_changed). Normalize the
            # name so routing does not depend on the separator herdr happens to use.
            # (Older code read "type" and matched dotted lifecycle names, which never
            # matched herdr's real wire format — lifecycle cleanup silently never ran.)
            raw_event = event.get("event", "") or event.get("type", "")
            event_name = raw_event.replace("_", ".")

            # Handle lifecycle events
            if event_name in ("pane.closed", "workspace.closed"):
                self._handle_lifecycle_event(event_name, event.get("data", {}))
                continue

            data = event.get("data", {})
            # Broadcast pane.updated nests the pane under data.pane; retired
            # agent-status events used top-level data. Fall back to data, and
            # guard against a null/non-dict pane so one malformed event cannot
            # escape the loop and kill delivery.
            pane_obj = data.get("pane") or data
            if not isinstance(pane_obj, dict):
                pane_obj = {}
            pane_id = pane_obj.get("pane_id", "")
            status = pane_obj.get("agent_status", "")

            # Only process events for managed panes
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                continue

            if status in ("idle", "done"):
                # Clear working timestamp
                self._working_since.pop(terminal_id, None)
                # Trigger delivery
                self._deliver(terminal_id)

            elif status == "working":
                # Track working start for kiro supplement check
                if terminal_id in self._kiro_terminals:
                    if terminal_id not in self._working_since:
                        self._working_since[terminal_id] = time.time()

    def _label_still_live(self, window_name: str) -> Optional[bool]:
        """Tri-state tab-label liveness in herdr.

        Used to disambiguate herdr's reused compact pane_ids on replayed
        pane_closed events. The tab label is unique per incarnation, so a live
        label means the close event refers to an older incarnation and is stale.

        Returns:
            True  — the label is present in a SUCCESSFUL `tab list` (live).
            False — the query SUCCEEDED and the label is genuinely absent (gone).
            None  — the query FAILED (non-zero exit / timeout / parse error): the
                    answer is UNKNOWN. A read failure is never proof of death, so
                    the caller must NOT destroy on None; defer to the reconcile
                    grace backstop instead. (Previously this fell toward False,
                    turning "could not look" into "delete it" — the read-failure
                    fleet-death bug, on the lifecycle path.)
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "tab", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_label_still_live: herdr tab list failed (rc=%s): %s — UNKNOWN",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            tab_data = json.loads(result.stdout)
            tabs = tab_data.get("result", {}).get("tabs", [])
            live_labels = {tab.get("label", "") for tab in tabs}
            return window_name in live_labels
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_label_still_live: could not query herdr (%s) — UNKNOWN", e)
            return None

    def _resolve_session_from_herdr(self, workspace_id: str) -> Optional[str]:
        """Resolve a workspace_id to its session name from live herdr state.

        Used by workspace.closed handling when the in-memory _workspace_to_session
        map (populated only by _reconcile) does not contain the closed
        workspace_id. Queries herdr workspace list, refreshes the whole map from
        the result, and returns the label for workspace_id if found.

        Returns None when herdr cannot be queried or the workspace_id is not in
        the live list, so the caller can treat the event as unresolvable and take
        no destructive action.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "workspace", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_resolve_session_from_herdr: herdr workspace list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            ws_data = json.loads(result.stdout)
            workspaces = ws_data.get("result", {}).get("workspaces", [])
            self._workspace_to_session = {ws["workspace_id"]: ws["label"] for ws in workspaces}
            return self._workspace_to_session.get(workspace_id)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_resolve_session_from_herdr: could not query herdr (%s)", e)
            return None

    def _handle_lifecycle_event(self, event_type: str, data: dict) -> None:
        """Handle pane.closed and workspace.closed events."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            delete_terminals_by_session,
            get_terminal_metadata,
        )

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                return

            # Get session before cleanup
            meta = get_terminal_metadata(terminal_id)
            session_name = meta["tmux_session"] if meta else None

            # Guard against herdr's compact pane_id reuse + event replay.
            #
            # herdr (0.6.8) reuses compact pane_ids when a tab is killed and a
            # new tab takes the same index, AND replays the ENTIRE pane_closed
            # history on every fresh events.subscribe (e.g. after a reconnect on
            # socket disconnect). So a replayed close for an OLD incarnation of
            # this pane_id arrives mapped to the terminal that now occupies the
            # reused index — deleting a live terminal.
            #
            # The tab label (tmux_window) is unique per incarnation, so confirm
            # the label is genuinely gone from herdr before deleting.
            #   True  (still live)   → this close is a stale replay → ignore.
            #   None  (query FAILED) → UNKNOWN. A read failure is never proof of
            #       death: do NOT delete on the event alone. Defer to the
            #       reconcile grace backstop, which reaps a genuinely-closed pane
            #       once its absence is authoritatively confirmed past the grace.
            #       (Previously this fell toward delete, so a herdr query failing
            #       under load turned a possibly-stale replay into a live-terminal
            #       kill — the lifecycle half of the read-failure fleet-death bug.)
            #   False (query OK, gone) → corroborated close → delete (fast path).
            window_name = meta["tmux_window"] if meta else None
            if window_name:
                live = self._label_still_live(window_name)
                if live is True:
                    logger.info(
                        "pane.closed: ignoring stale close for %s (pane=%s) — "
                        "label %s still live in herdr (compact pane_id reused)",
                        terminal_id,
                        pane_id,
                        window_name,
                    )
                    return
                if live is None:
                    logger.warning(
                        "pane.closed: label %s liveness UNKNOWN for %s (herdr query "
                        "failed) — not deleting on a read failure; deferring to "
                        "reconcile grace",
                        window_name,
                        terminal_id,
                    )
                    return

            # Remove from maps
            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            # Delete DB record
            try:
                delete_terminal(terminal_id)
            except Exception as e:
                logger.warning(f"pane.closed: failed to delete terminal {terminal_id}: {e}")

            logger.info(f"pane.closed: cleaned up terminal {terminal_id} (pane={pane_id})")

            # If session has no more terminals in our map, kill workspace
            remaining_in_session = [
                t
                for t in self._pane_to_terminal.values()
                if (m := get_terminal_metadata(t)) and m.get("tmux_session") == session_name
            ]
            if session_name and not remaining_in_session:
                try:
                    get_backend().kill_session(session_name)
                    logger.info(f"pane.closed: killed empty workspace {session_name}")
                except Exception as e:
                    logger.warning(f"pane.closed: failed to kill workspace {session_name}: {e}")

        elif event_type == "workspace.closed":
            workspace_id = data.get("workspace_id", "")
            session_name = self._workspace_to_session.get(workspace_id)
            if not session_name:
                # The in-memory map is populated only by _reconcile(); a workspace
                # that closed before any reconcile cached it would otherwise be a
                # silent no-op, leaking the session's terminals as orphan rows.
                # Resolve the session identity from live herdr state instead of
                # trusting the map. Only treat the event as unresolvable after the
                # live query also fails to identify a session.
                session_name = self._resolve_session_from_herdr(workspace_id)
                if not session_name:
                    return

            # Delete all DB terminals for this session
            try:
                delete_terminals_by_session(session_name)
            except Exception as e:
                logger.warning(
                    f"workspace.closed: failed to delete terminals for {session_name}: {e}"
                )

            # Prune maps for terminals belonging to this session. Match on each
            # terminal's DB session rather than a pane_id/workspace_id string
            # prefix: herdr renumbers compact pane_ids and does not guarantee
            # they begin with the workspace_id, so a prefix test is unreliable.
            # This mirrors the session match used in the pane.closed handler.
            to_remove = [
                (pid, tid)
                for pid, tid in self._pane_to_terminal.items()
                if (m := get_terminal_metadata(tid)) and m.get("tmux_session") == session_name
            ]
            for pid, tid in to_remove:
                self._pane_to_terminal.pop(pid, None)
                self._terminal_to_pane.pop(tid, None)
                self._kiro_terminals.discard(tid)
                self._working_since.pop(tid, None)

            self._workspace_to_session.pop(workspace_id, None)
            logger.info(
                f"workspace.closed: cleaned up session {session_name} ({len(to_remove)} terminals)"
            )

    # TODO: _deliver() calls callback synchronously — if callback is async,
    # this will need a threadsafe bridge (out of scope for this change).
    def _deliver(self, terminal_id: str) -> None:
        """Check and deliver pending messages for a terminal."""
        if self._delivery_callback:
            try:
                self._delivery_callback(terminal_id)
            except Exception as e:
                logger.error(f"Delivery failed for terminal {terminal_id}: {e}")

    async def check_kiro_supplements(self) -> None:
        """Periodic check for kiro-cli terminals stuck in 'working' state.

        For terminals in 'working' for >30s, read pane content and check
        for permission prompt patterns.
        """
        import subprocess

        now = time.time()
        for terminal_id in list(self._working_since.keys()):
            if terminal_id not in self._kiro_terminals:
                continue

            working_duration = now - self._working_since[terminal_id]
            if working_duration < _KIRO_WORKING_THRESHOLD:
                continue

            # Read pane and check for permission prompt
            pane_id = self._terminal_to_pane.get(terminal_id)
            if not pane_id:
                continue

            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "pane", "read", pane_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue

            # Check for kiro permission prompt pattern
            # (WAITING_USER_ANSWER indicator)
            from cli_agent_orchestrator.providers.kiro_cli import TUI_PERMISSION_PATTERN

            if re.search(TUI_PERMISSION_PATTERN, result.stdout):
                logger.info(
                    f"Kiro permission prompt detected for {terminal_id} "
                    f"(working for {working_duration:.0f}s)"
                )
                self._deliver(terminal_id)
                # Reset the timer so we don't spam
                self._working_since[terminal_id] = now

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the herdr socket."""
        assert self._writer is not None
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()
