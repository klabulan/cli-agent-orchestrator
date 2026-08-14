#!/usr/bin/env python3
"""Re-derive the incident severity numbers cited in analysis.md / pr_body.md,
directly from CAO's own file log, so every load-bearing number is reproducible.

Usage:
    python3 reproduce_incident_numbers.py \
        ~/.aws/cli-agent-orchestrator/logs/cao_2026-08-14_10-41-12.log

Reports, for both "Killed tmux window" and "Deleted terminal" events:
  - file total + full time span
  - the densest 16s and 60s sliding windows (count + [start..end])
  - the count inside the 11:34 minute (the fleet-death burst the docs cite)

This script exists because the original "268 kill ops in 16s" headline did NOT
reproduce against this log (max any-16s-window = 11 kills / 21 deletes); it was
corrected to the numbers this script prints.
"""
import re
import sys
from datetime import datetime, timedelta

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
PATTERNS = {
    "Killed tmux window": re.compile(r"Killed tmux window"),
    "Deleted terminal": re.compile(r"Deleted terminal"),
}


def parse(log_path, pat):
    ts = []
    with open(log_path, errors="replace") as f:
        for line in f:
            if pat.search(line):
                m = TS_RE.match(line)
                if m:
                    ts.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f"))
    return sorted(ts)


def densest(ts, win_seconds):
    n = len(ts)
    if n == 0:
        return 0, None, None
    w = timedelta(seconds=win_seconds)
    best, bi, ei, j = 0, 0, 0, 0
    for i in range(n):
        while ts[i] - ts[j] > w:
            j += 1
        if i - j + 1 > best:
            best, bi, ei = i - j + 1, j, i
    return best, ts[bi], ts[ei]


def in_minute(ts, hh_mm):
    return sum(1 for t in ts if t.strftime("%H:%M") == hh_mm)


def main():
    log_path = sys.argv[1]
    for name, pat in PATTERNS.items():
        ts = parse(log_path, pat)
        print(f"== {name} ==")
        if not ts:
            print("  (no events)")
            continue
        print(f"  total: {len(ts)}   span: {ts[0]:%H:%M:%S} .. {ts[-1]:%H:%M:%S}")
        for win in (16, 60):
            c, s, e = densest(ts, win)
            print(f"  densest {win}s window: {c}   [{s:%H:%M:%S} .. {e:%H:%M:%S}]")
        print(f"  in the 11:34 minute: {in_minute(ts, '11:34')}")


if __name__ == "__main__":
    main()
