# BEST PRACTICES + DESIGN — read failure = UNKNOWN, teardown only on corroborated absence + grace

## 1. Is the operator's proposal right? (best-practices evaluation)

**Operator's proposal:** make it config — a *retry count* and/or a *~60s timeout* before a session
is declared dead.

**Verdict: yes, the direction is correct and matches mature prior art — with one essential
refinement the raw proposal doesn't state.** Grounded in the manager's prior-art survey
(`knowledge/landscape/20260814_agent-session-supervision-prior-art.md`) and its sources:

- **Two independent timers, threshold + grace (Kubernetes node lifecycle).** Mature reconcilers
  separate a *detection threshold* from a *destructive-action grace window*. Kubernetes requires
  ≥3 missed heartbeats before a node is declared unhealthy (`--node-monitor-grace-period` ≥ 3×
  `nodeStatusUpdateFrequency`), and then applies a *separate, longer* grace (`tolerationSeconds`,
  default 300s) before the destructive action (pod eviction) fires — two timers, not one
  [prior-art Finding 3]. The operator's "retry count AND/OR ~60s timeout" is exactly this shape;
  the correct build is **AND** (both a corroboration count and a wall-clock grace), not OR, because
  we are defending an *irreversible* action (kill) against a *transient* signal.

- **A read error must be UNKNOWN, never a "miss" (the essential refinement).** Perfect liveness
  detection is proven impossible in an asynchronous system; different monitors observing the same
  node from partial information reach contradictory conclusions [prior-art Finding 7, Aeron]. So the
  count must be a count of **authoritative confirmed-absent observations** — the tab genuinely gone
  from a *successfully-read* full snapshot — **not** a count of failed reads. If a degraded read
  (timeout / `TmuxLookupError` / `TerminalNotFoundError` / `herdr tab list` non-zero exit) were
  allowed to tick the counter toward eviction, then load — which makes reads fail *and* is exactly
  when we must not evict — would still drive the fleet to death, just three ticks slower. The raw
  "retry N times then declare dead" framing has this trap; naming read-failure as UNKNOWN (skip,
  don't count) closes it. This is the core invariant.

- **Level-triggered, not edge-triggered** [prior-art Finding 1]. The teardown decision must be a
  pure function of *current observed state* (is this tab present in the snapshot right now), never
  of "an event said it closed." The existing reconcile is already level-triggered off `api
  snapshot`; the fix keeps it that way and removes the edge-y `pane.closed`-replay fragility by
  routing lifecycle-driven deletes through the same corroborated grace.

- **Backoff + jitter** [prior-art Finding 2] — relevant to the herdr-socket reconnect loop
  (currently fixed exponential backoff, `_BACKOFF_*`, no jitter). Adding jitter is a real but
  *secondary* improvement and is **out of scope for this fix** (flagged, not done here) to keep the
  change focused on the confirmed killer.

**Alternatives weighed:**

| Option | Verdict |
|---|---|
| Minimal: just `except TmuxLookupError: continue` in `_reconcile` | **Insufficient.** The two real failure modes don't surface as `TmuxLookupError`: `get_pane_id` raises `TerminalNotFoundError`, and `_label_still_live` returns `False` (no exception) on a failed `herdr tab list`. And it adds no grace for the authoritative-absent case, so a *partial-but-parsed* snapshot could still false-delete on the first pass. |
| Operator's config retry/timeout, refined (UNKNOWN-never-counts + two-timer grace) | **Adopted.** See design below. |
| Phi-Accrual adaptive statistical detector [Finding 7] | Over-engineered for now; flagged as a future option if flapping recurs under a different load profile. |

## 2. Design

### Invariant (the one rule)
> A tmux/herdr **read error** — `Window not found`, timeout, `TmuxLookupError`,
> `TerminalNotFoundError`, `herdr tab list` non-zero exit / parse failure — is **UNKNOWN**, never
> "dead." `delete_terminal` / `kill_session` may fire **only** on an **authoritative** confirmation
> (the tab label genuinely absent from a *successfully-read* full `api snapshot`) **and** only after
> a configurable grace: **N consecutive authoritative confirmed-absent observations AND a
> dead-for-≥T-seconds window.**

### Config knobs (`constants.py`, env, safe defaults)
- `CAO_RECONCILE_ABSENT_THRESHOLD` (int, default **3**) — consecutive authoritative confirmed-absent
  observations required before teardown (≈ k8s `--node-monitor-grace-period`'s ≥3-miss rule).
- `CAO_RECONCILE_ABSENT_GRACE_SECONDS` (float, default **60.0**) — wall-clock a terminal must remain
  continuously confirmed-absent before teardown (≈ k8s `tolerationSeconds`). Both required (AND).

Setting threshold=1 and grace=0 restores the old immediate-delete behavior (used by a test to prove
the knobs actually gate behavior).

### State (per `HerdrInboxService` instance)
- `self._absent_since: Dict[str, float]` — terminal_id → `time.monotonic()` of the *first* of the
  current unbroken run of confirmed-absent observations.
- `self._absent_count: Dict[str, int]` — length of that run.
Both are **cleared** the instant a terminal is observed live again, or a read comes back UNKNOWN
(the run is only "confirmed-absent"; an UNKNOWN breaks neither toward death nor necessarily resets
— see rule R3).

### Decision procedure (a single helper `_confirm_absent_or_defer(terminal_id) -> bool`)
Called only when the snapshot read **succeeded** (so absence is authoritative). Returns True =
"tear down now"; False = "not yet, leave it."
- Increment `_absent_count`; set `_absent_since` if unset (`time.monotonic()`).
- Return True iff `_absent_count >= THRESHOLD` **and** `monotonic() - _absent_since >= GRACE_SECONDS`.
- On True, clear both maps for that id (fresh start if it somehow reappears).

### Reconcile rewrite (`_reconcile`, the stale-pane loop)
For each stale pane_id → terminal_id, with `live_tab_labels` = union of all tab labels in the
**successful** snapshot (authoritative; replaces the separate `_label_still_live` subprocess):

- **R1 — tab label present in snapshot (live):** the pane_id was merely renumbered. Try
  `get_pane_id` to re-map.
  - success → re-map, `_clear_absent(id)`, continue.
  - **raises (UNKNOWN read):** the tab is confirmed live, so the terminal is **not** dead. Log at
    debug, `_clear_absent(id)`, **leave the maps untouched, do NOT delete**, retry next reconcile.
    *(This is the exact line that killed the fleet — a re-resolve read failure on a confirmed-live
    tab now never deletes.)*
- **R2 — tab label absent from snapshot (authoritatively gone this pass):** route through
  `_confirm_absent_or_defer(id)`.
  - returns False → within grace; leave maps + DB row intact, continue (retry next pass).
  - returns True → past grace; prune maps and `delete_terminal(id)` (real reaping preserved).
- **R3 — whole snapshot failed** (`_fetch_snapshot()` is None): already returns early at the top of
  `_reconcile` (UNKNOWN → skip). Counters are left untouched (not reset, not incremented) — a run of
  confirmed-absent broken by an unreadable pass simply doesn't advance that pass.

The ghost-DB-cleanup loop (terminals whose tab isn't in the snapshot's tab set) and the
empty-workspace `kill_session` are routed through the **same** `_confirm_absent_or_defer` gate, so
no authoritative-absent path can destroy without the grace.

### Lifecycle `pane.closed` (`_handle_lifecycle_event`)
`_label_still_live` becomes tri-state (`True` live / `False` confirmed-gone / `None` could-not-query).
- `True` → stale replay, ignore (unchanged).
- `None` (UNKNOWN) → **do not delete**; defer to the reconcile grace backstop (previously "fall
  toward delete" — the bug).
- `False` (authoritatively gone) → this is a real herdr close event corroborated by a successful
  "tab not in list" query; delete (fast-path reaping preserved).

### Why this preserves real cleanup (no orphan leak)
A genuinely-closed pane's tab label is genuinely absent from the (successful) snapshot on every
subsequent pass, so `_confirm_absent_or_defer` advances to True after THRESHOLD passes AND
GRACE_SECONDS, and it is reaped. The only behavior removed is reaping on a *read failure* and
reaping on the *first* confirmed-absent pass — both of which are the defect. Worst-case added
latency to reap a truly-dead terminal ≈ GRACE_SECONDS (60s default), which is exactly the intended,
safe trade.

### Test plan (RED→GREEN)
- **Test A (the bug):** a stale-pane reconcile where the tab label IS live in the snapshot but
  `get_pane_id` raises (`TerminalNotFoundError`) → `delete_terminal` is **not** called; the map is
  left intact for retry. (Also: `_fetch_snapshot` → None → no delete.)
- **Test B (no regression / real reaping):** a tab genuinely absent from the snapshot, observed
  absent across `THRESHOLD` passes past `GRACE_SECONDS` → `delete_terminal` **is** called.
- **Test C (knobs gate behavior):** with `THRESHOLD`/`GRACE` at defaults, one confirmed-absent pass
  does **not** delete; with `THRESHOLD=1, GRACE=0`, the same single pass **does** delete.

### Out of scope (flagged, not done here)
Socket-reconnect jitter (Finding 2); Phi-Accrual detector (Finding 7); resource isolation / getting
e2e off the prod box (an ops-layer fix, not a CAO code fix). This PR fixes the confirmed
code-level killer: read failure → irreversible teardown.
