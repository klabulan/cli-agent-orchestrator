# ANALYSIS — a failed tmux READ must never mean "terminal dead"

**Incident (2026-08-14 ~11:34Z):** under host load (mass process spawn drove loadavg to ~13 on a
4-core box), CAO's periodic reconcile misread a transient tmux/herdr read failure as proof a live
terminal had died, reaped the running sprint fleet in one dense burst, then re-created and
re-killed terminals in a sustained loop under the continued load.

**Reproducible severity numbers** (all re-derived from the cited log below via
`reproduce_incident_numbers.py` in this folder — the earlier headline "268 kill ops in 16s" did
**NOT** reproduce against this log under any 16-second slicing and has been corrected here):

- **Peak burst:** the densest 16-second window (`11:34:39–11:34:55`) holds **11 `Killed tmux window`
  + 21 `Deleted terminal`** events — the tightest reaping slice anywhere in the file.
- **Fleet-wide:** **22 distinct terminals deleted inside the 11:34 minute** (`11:34:00–11:35:00`) —
  i.e. the whole running fleet at once, not a single session.
- **Sustained loop:** reaping did not stop after the burst — **642 `Killed tmux window` / 659
  `Deleted terminal`** events total, spread across `10:59:29–12:59:16` (~2h), as CAO re-created and
  re-killed terminals under continued load. (These file totals grow while the log is still being
  appended; the densest-window and per-minute figures above are anchored to fixed timestamps and are
  stable across re-runs.)

Definitive timeline from CAO's own file log
(`~/.aws/cli-agent-orchestrator/logs/cao_2026-08-14_10-41-12.log`) — these exact lines reproduce
verbatim (`grep -nE '11:34:3[0-9]'`):

- `11:34:37 ERROR Failed to get history from cao-hc858…: Window '…1cf0' not found in session`
  + `WARNING Failed to snapshot terminal b4c1f349: Window not found`.
- `11:34:38 → Stopped FIFO reader → Cleaned up provider → Deleted terminal: b4c1f349`.
- `11:34:39–55 → Killed tmux window:` for the fleet (the peak-burst window above).

Deployed CAO at the time = `b067e9e` (klabulan `hc840-cao-consolidated-6f13d9f-plus7` tip). This
analysis was produced against that exact commit (the base of this fix branch).

## The destruction chain (file:line, on the deployed base `b067e9e`)

### 1. The trigger — `services/herdr_inbox_service.py::_reconcile()` (lines 286–455)

`_reconcile` runs once per herdr-socket (re)connect in `_socket_loop` (line 222). Under load the
herdr socket disconnects and reconnects repeatedly, so reconcile fires repeatedly (the "runs
continuously" the livecheck observed). Its job: prune the `pane_id → terminal_id` map against live
herdr state, delete DB rows for terminals whose tmux window is gone, and kill emptied workspaces.

The single snapshot read is already level-triggered *correctly* at the top:

```python
snapshot = self._fetch_snapshot()
if snapshot is None:
    logger.warning("Reconcile: no snapshot, skipping")   # whole-read-failed → UNKNOWN → skip. GOOD.
    return
```

The defect is **per-terminal**, below. For each stale pane_id (its pane_id fell out of the live
list — which herdr does routinely on pane renumbering, *not* only on close), the code tries to
re-map a still-live terminal, and **treats a failed re-resolve READ as authoritative death**:

```python
if term_window and self._label_still_live(term_window):        # line 385
    try:
        ...
        new_pane_id = backend.get_pane_id(terminal_id, term_session or "", term_window)  # 393
    except Exception as e:                                       # 394 — catches EVERYTHING
        logger.warning(
            "Reconcile: tab %s live but pane re-resolve failed for %s (%s); deleting", ...)  # 396
    else:
        ... re-map, continue
# falls through to:
self._pane_to_terminal.pop(pane_id, None)                       # 417
self._terminal_to_pane.pop(terminal_id, None)
try:
    delete_terminal(terminal_id)                                # 423  → kill_window + DB delete
```

**Two independent read-failure-→-death paths here:**

- **(a) `get_pane_id` raises under load.** `get_pane_id` (`backends/herdr_backend.py:696`) does a
  live `api snapshot` refresh + label→pane resolution and raises `TerminalNotFoundError` when a
  transient read fails to resolve. The bare `except Exception` (line 394) catches it and **falls
  through to `delete_terminal`** — even though the tab label was *just confirmed live* on line 385.
  A read error is being treated as proof of death.

- **(b) `_label_still_live` fails toward death.** `_label_still_live` (lines 548–578) shells out to
  `herdr … tab list` (its own subprocess, separate failure surface). Its docstring says it "Fails
  toward False (not live) when herdr can't be queried, so the caller proceeds with cleanup." Under
  load that query times out / errors → returns `False` → the re-map guard on line 385 is skipped
  entirely → straight to the delete path. "I could not look" is converted to "it is gone."

Then the emptied-workspace kill (lines 442–448) fires `get_backend().kill_session(session_name)`
once a session's last terminal is deleted — cascading a per-terminal misread into a whole-session
`Killed tmux window:` storm. Because load degrades *every* session's read at once, the whole fleet
is deleted in one pass.

### 2. Same defect class, lifecycle path — `_handle_lifecycle_event` (pane.closed) (lines 623–670)

A herdr `pane.closed` event is guarded by the same `_label_still_live` (line 648) to reject stale
*replayed* closes. Comment: "If herdr can't be queried, fall toward delete." Same conversion of an
UNKNOWN read into a destroy decision.

### 3. The teardown it triggers — `services/terminal_service.py::delete_terminal()` (1561–1696)

`delete_terminal` is irreversible: `unregister` → snapshot(best-effort) → `stop_pipe_pane` →
`stop_reader` → `clear_terminal` → **`kill_window`** → provider cleanup → `db_delete_terminal`. The
`WARNING Failed to snapshot terminal …: Window not found` in the incident log is the best-effort
scrollback capture *inside* `delete_terminal` (line ~1619) — a symptom logged during teardown, not
the trigger. The `Failed to get history …` line is the FIFO liveness probe / output read failing on
the same load; note the FIFO watchdog itself does **not** delete (it only re-arms the pipe), so it
is not the killer — the reconcile is.

### 4. The read layer already distinguishes UNKNOWN — the caller ignores it

`clients/tmux.py` was hardened in **PR #555** (`5cf846c`, "make libtmux listing parse failures
retryable, not fake 'not found'"): a listing that fails to parse twice raises **`TmuxLookupError`**
("the answer is UNKNOWN — not absent"), explicitly *not* a subclass of `ValueError`, so a caller
can tell "really gone" (`ValueError` / `None` / `[]`) from "could not look" (`TmuxLookupError`).

**But `herdr_inbox_service` never catches `TmuxLookupError`** — its `except Exception` (line 394)
swallows it identically to genuine absence. The read layer's UNKNOWN signal is discarded one frame
up. (And `get_pane_id`/`_label_still_live` fail with `TerminalNotFoundError` / `False`, which also
do not carry the UNKNOWN distinction to the decision site.)

## Original purpose — what this cleanup is legitimately for (do NOT reintroduce the leak)

`git blame`/`git log` on the reconcile delete path:

- **`7a7d1e1` (#271)** introduced the herdr event-driven inbox backend.
- **`1057d5d` (#309)** "auto-detect server backend + herdr reconcile fixes" — added the
  `_label_still_live` guard. Its purpose: herdr 0.6.8 **reuses compact pane_ids** when a tab is
  killed and a new tab takes the same index, and **replays its entire `pane_closed` history** on
  every fresh `events.subscribe` (which CAO triggers on every registration). So a replayed close
  for an *old* incarnation arrived mapped to the *live* terminal now occupying that reused index,
  deleting a live terminal. The `_label_still_live` guard (durable per-incarnation tab label) was
  added specifically to *prevent* false deletes.
- **`3daede2` (#502)** modernized to herdr 0.7.x (single `api snapshot`, broadcast events).

**So the delete path is legitimate and necessary:** it reaps genuinely-orphaned DB terminal rows
and genuinely-empty workspaces (panes that really closed while CAO was up, or ghost rows from a
prior server run). The fix must **preserve** that reaping — a genuinely-closed pane must still be
cleaned up — while refusing to reap on a *read failure*. The irony the incident exposed: `#309`'s
guard against false deletes was itself built out of reads (`_label_still_live`, `get_pane_id`) that
fail *open* (toward delete) under the exact load that makes them fail — so the guard inverts into a
false-delete amplifier precisely when it is needed most.

## Root cause (one sentence)

CAO's reconcile makes an **edge-triggered, irreversible teardown decision on an unreliable signal**:
it treats a transient tmux/herdr **read failure** (`TmuxLookupError` / `TerminalNotFoundError` /
`herdr tab list` timeout) as an **authoritative confirmation of death**, with **no grace window and
no corroboration**, so a single degraded read under load destroys a live terminal — and because
load degrades every session's read simultaneously, the whole fleet at once.

## What the fix must change (feeds `design.md`)

1. A read error / `Window not found` / timeout / `TmuxLookupError` / `TerminalNotFoundError` is
   **UNKNOWN, never dead.** Never `delete_terminal`/`kill_session` off a failed read.
2. Teardown may fire only on **authoritative absence** — the tab label genuinely absent from a
   *successfully-read* full snapshot — and only after a **configurable grace** (N consecutive
   confirmed-absent observations AND a dead-for-≥T-seconds window; safe defaults).
3. Preserve real reaping: a genuinely-closed pane, confirmed absent past the grace, is still reaped.
4. Make retries + grace-timeout **config** (env), with safe defaults.
