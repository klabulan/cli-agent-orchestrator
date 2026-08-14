## A transient tmux/herdr READ failure must never mean "terminal dead"

### The incident (2026-08-14)
Under host load (a mass process spawn drove loadavg to ~13 on a 4-core box), CAO's periodic
`herdr_inbox_service._reconcile` misread a transient tmux/herdr **read failure** as proof a live
terminal had died, reaped the running session fleet in one dense burst, then re-created and
re-killed terminals in a sustained loop under the continued load. From CAO's own log
(`~/.aws/cli-agent-orchestrator/logs/cao_2026-08-14_10-41-12.log`):

```
11:34:37 ERROR Failed to get history from cao-hc858…: Window '…1cf0' not found in session
11:34:37 WARNING Failed to snapshot terminal b4c1f349: Window not found
11:34:38 → Deleted terminal: b4c1f349
11:34:39–55 → Killed tmux window: <the fleet>
```

**Severity (reproducible — re-derived via `Tasks/20260814_readfail_never_dead/reproduce_incident_numbers.py`).**
The peak burst — the densest 16-second window (`11:34:39–11:34:55`) — is **11 `Killed tmux window`
+ 21 `Deleted terminal`** events; **22 distinct terminals were deleted inside the 11:34 minute**
(the running fleet); and reaping continued in a loop for **642 kills / 659 deletes total** over
`10:59–12:59` (~2h). (An earlier draft's "268 kill ops in 16s" headline did **not** reproduce
against this log under any 16s slicing and has been corrected to these figures.)

### Root cause
`_reconcile` made an **edge-triggered, irreversible teardown decision on an unreliable signal**.
Two read-failure→death paths:

1. **Stale-pane re-map (the killer).** When a live terminal's compact `pane_id` is renumbered by
   herdr, `_reconcile` re-resolves it via `get_pane_id`. Even after `_label_still_live(term_window)`
   returned **True** (tab confirmed alive), a transient `get_pane_id` failure hit a bare
   `except Exception` and **fell through to `delete_terminal` → `kill_session`** (log: *"tab live
   but pane re-resolve failed … deleting"*).
2. **`_label_still_live` fails open.** Its own `herdr tab list` subprocess "failed toward False"
   (not-live) on any query error — so under load "I couldn't look" became "it's gone."

Because load degrades *every* session's read at once, one bad pass deleted the whole fleet. The read
layer already distinguishes UNKNOWN (`TmuxLookupError`, PR #555) from genuine absence — but the
reconcile discarded that distinction.

The delete path itself is legitimate (it reaps genuinely-orphaned DB rows / closed panes, added in
#309 to guard herdr's replayed `pane_closed` + reused compact pane_ids). This PR **preserves** that
reaping while refusing to reap on a *read failure* or on a *single* observation.

### The fix — invariant
> A tmux/herdr **read error** (`Window not found` / timeout / `TmuxLookupError` /
> `TerminalNotFoundError` / `herdr tab list` non-zero exit) is **UNKNOWN, never dead.** Teardown
> fires only on **authoritative** confirmed-absence (tab genuinely gone from a *successfully-read*
> `api snapshot`) and only after a configurable grace.

- Reconcile takes tab-liveness from the **single successful snapshot** (`all_live_tab_labels`), not
  a second `_label_still_live` subprocess that fails open. Tab present ⇒ live: a failed pane-id
  re-resolve is UNKNOWN → terminal left intact, retried next reconcile, **never deleted**.
- Teardown requires a **two-timer grace** (the Kubernetes `--node-monitor-grace-period` +
  `tolerationSeconds` shape): N consecutive confirmed-absent observations **AND** a wall-clock
  window. Applied to the stale-pane, ghost-DB-cleanup, empty-workspace-kill, **and the
  `pane.closed` confirmed-close fast-path** delete/kill sites.
- `_label_still_live` is now **tri-state** (True live / False confirmed-gone / **None UNKNOWN**);
  the `pane.closed` lifecycle handler **defers** on None (read failure), and — as of the follow-up
  commit for ROAST finding #2 — also routes the `False` (confirmed-gone) fast path through the
  **same** `_confirm_absent_or_defer` threshold+grace gate, because a *successful* `herdr tab list`
  can still return truncated/stale-but-parsed "label gone" under the same load that caused the
  incident. A single confirmed-gone observation now defers (maps + DB row left intact) and the
  reconcile grace backstop reaps it past the grace.
- A live terminal clears its absent counter, so a recovered flap never accumulates toward teardown.

### Config (safe defaults)
| Env var | Default | Meaning |
|---|---|---|
| `CAO_RECONCILE_ABSENT_THRESHOLD` | `3` | consecutive authoritative confirmed-absent observations before teardown |
| `CAO_RECONCILE_ABSENT_GRACE_SECONDS` | `60.0` | wall-clock a terminal must stay confirmed-absent before teardown |

`THRESHOLD=1, GRACE=0` restores the old immediate teardown.

### Why this doesn't reintroduce an orphan leak
A genuinely-closed pane's tab label is absent from every subsequent *successful* snapshot, so it is
still reaped after the grace (worst-case ≈ `GRACE_SECONDS`). Only two behaviors are removed: reaping
on a read failure, and reaping on the *first* confirmed-absent pass. A real `pane.closed` event
corroborated by a successful "label gone" query no longer reaps *instantly* — it now defers the
first confirmed-gone observation and is reaped once absence holds past the grace (via the
`pane.closed` replay stream and/or the reconcile backstop). Cleanup is preserved; only
single-observation reaping is removed. (`THRESHOLD=1, GRACE=0` restores the old instant fast path.)

### Coverage & scoped-out residual risk (ROAST finding #2)
The "read error = UNKNOWN, never dead + grace before teardown" invariant now covers **6 of 7**
delete/kill call sites: the three reconcile paths (stale-pane, ghost-DB, empty-workspace-kill), the
`pane.closed` UNKNOWN-defer, and — added for this finding — the `pane.closed` confirmed-close
delete. Two paths remain **out of scope for this fix and are accepted residual risk**, disclosed
here rather than implied away:

1. **`_startup_db_cleanup` — single boot snapshot, zero grace (unchanged from base).** At server
   start it deletes DB terminal rows whose tmux window is absent from **one** snapshot read. This is
   a boot-time reconciliation of rows left by a *previous* server process (no live terminals this
   process manages yet), so a grace loop has nothing to corroborate against and would only delay
   legitimate ghost-row cleanup. Risk: if that single boot snapshot is itself degraded, a row that
   is actually live could be deleted — but at boot the steady-state reconcile grace re-establishes
   correct state on its next pass, and there is no running fleet to cascade-kill. Left as-is;
   revisiting would mean giving startup its own multi-read confirmation, out of scope here.
2. **`pane.closed`'s cascading `kill_session` lacks reconcile's `live_workspace_labels` cross-check.**
   After the (now grace-gated) delete, if the session has no more managed terminals in our map,
   `pane.closed` calls `kill_session(session_name)` **without** re-checking the label against a fresh
   snapshot the way reconcile's equivalent does (`session_name not in live_workspace_labels`). It now
   only fires *after* the grace-gated delete has already corroborated the pane's absence, which
   narrows the exposure considerably, but it is not identical to reconcile's belt-and-suspenders
   workspace-label check. Accepted as scoped-out: adding a snapshot read inside the event handler is
   a larger change than this fix warrants, and the reconcile empty-workspace path (which *does* have
   the cross-check) is the durable backstop for workspace teardown.

### Tests (RED→GREEN)
`test/backends/test_herdr_inbox_service.py` — `TestHerdrInboxServiceReadFailureGrace` +
`TestHerdrInboxServiceLabelLiveness`:
- **A** — live tab + `get_pane_id` raises ⇒ **not** deleted, left intact (the fleet-death line);
  whole-snapshot `None` ⇒ nothing deleted.
- **B** — genuinely-absent tab ⇒ deferred then reaped past the grace (cleanup preserved).
- **C** — knobs gate behavior: default defers a single pass; `(1, 0)` deletes it.
- **D** — wall-clock timer isolated via `_confirm_absent_or_defer`.
- **E** — reappearing pane clears the counter.
- tri-state `_label_still_live`; `pane.closed` defers on UNKNOWN.
- **finding #2 (pane.closed fast-path):** confirmed-gone `pane.closed` under the default grace
  **defers** on a single observation (maps intact) and is **reaped after the threshold** (grace=0);
  the three pre-existing single-observation-reap tests now opt into `(threshold=1, grace=0)` to prove
  genuine-close reaping still works, exactly gated.

Pre-existing tests that encoded the **old buggy** behavior are flipped to assert the fix (direct
RED→GREEN evidence). **72/72 herdr tests green** (70 base + 2 net-new for finding #2). (Unrelated
pre-existing `test/services/agui` failures reproduce on the untouched base and are not touched here.)

Analysis + design rationale (Kubernetes node-lifecycle grace, level-triggered reconciliation, Aeron
"perfect liveness detection is impossible → corroborated detection"): see
`Tasks/20260814_readfail_never_dead/{analysis,design}.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
