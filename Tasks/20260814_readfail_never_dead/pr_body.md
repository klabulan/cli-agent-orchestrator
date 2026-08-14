## A transient tmux/herdr READ failure must never mean "terminal dead"

### The incident (2026-08-14)
Under host load (a mass process spawn drove loadavg to ~13 on a 4-core box), CAO's periodic
`herdr_inbox_service._reconcile` misread a transient tmux/herdr **read failure** as proof a live
terminal had died and destroyed the entire session fleet in **16 seconds**, then re-created and
re-killed in a loop — **268 kill ops**. From CAO's own log:

```
11:34:37 ERROR Failed to get history from cao-hc858…: Window '…1cf0' not found in session
11:34:37 WARNING Failed to snapshot terminal b4c1f349: Window not found
11:34:38 → Deleted terminal: b4c1f349
11:34:39–55 → Killed tmux window: <the entire fleet>
```

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
  window. Applied uniformly to the stale-pane, ghost-DB-cleanup, and empty-workspace-kill paths.
- `_label_still_live` is now **tri-state** (True live / False confirmed-gone / **None UNKNOWN**);
  the `pane.closed` lifecycle handler **defers** on None instead of deleting.
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
on a read failure, and reaping on the *first* confirmed-absent pass. The fast path (a real
`pane.closed` event corroborated by a successful "label gone" query) still reaps immediately.

### Tests (RED→GREEN)
`test/backends/test_herdr_inbox_service.py` — `TestHerdrInboxServiceReadFailureGrace` +
`TestHerdrInboxServiceLabelLiveness`:
- **A** — live tab + `get_pane_id` raises ⇒ **not** deleted, left intact (the fleet-death line);
  whole-snapshot `None` ⇒ nothing deleted.
- **B** — genuinely-absent tab ⇒ deferred then reaped past the grace (cleanup preserved).
- **C** — knobs gate behavior: default defers a single pass; `(1, 0)` deletes it.
- **D** — wall-clock timer isolated via `_confirm_absent_or_defer`.
- **E** — reappearing pane clears the counter.
- tri-state `_label_still_live`; `pane.closed` defers on UNKNOWN, deletes on confirmed-gone.

Two pre-existing tests that encoded the **old buggy** behavior are flipped to assert the fix (direct
RED→GREEN evidence). **70/70 herdr tests green.** (Unrelated pre-existing `test/services/agui`
failures reproduce on the untouched base and are not touched here.)

Analysis + design rationale (Kubernetes node-lifecycle grace, level-triggered reconciliation, Aeron
"perfect liveness detection is impossible → corroborated detection"): see
`Tasks/20260814_readfail_never_dead/{analysis,design}.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
