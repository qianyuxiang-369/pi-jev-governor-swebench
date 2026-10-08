#!/usr/bin/env python3
"""Assertion helper for pi-jev decision logs (decisions-*.jsonl).

Usage:
  jevlog.py FILE --lines                     # pretty-print entries
  jevlog.py FILE --has REGEX [--has ...]     # every regex matches >= 1 entry
  jevlog.py FILE --absent REGEX              # no entry matches
  jevlog.py FILE --seq SUB [SUB ...]         # substrings appear, in order
  jevlog.py FILE --kind-count kind=n [...]   # exact decisionKind counts

Combos allowed (--has/--absent/--seq/--kind-count together = AND).
Matching runs on the RAW line (answers, effective, error included).
Exit 0 = all assertions pass, 1 = failure (reasons on stderr).
"""

import json
import re
import sys


def entries(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append((line.rstrip("\n"), json.loads(line)))
    return rows


def main(argv):
    path = argv[1]
    flags = argv[2:]
    rows = entries(path)
    failures = []

    if "--lines" in flags:
        for raw, row in rows:
            phase = row.get("correctionPhase", "")
            print(f"{row.get('decisionKind','?'):10} | {phase:9} | {row.get('effective','')[:80]} | {row.get('error','')}")

    def take_values(flag):
        out = []
        i = 0
        while i < len(flags):
            if flags[i] == flag and i + 1 < len(flags):
                out.append(flags[i + 1])
                i += 2
            else:
                i += 1
        return out

    for pattern in take_values("--has"):
        if not any(re.search(pattern, raw) for raw, _ in rows):
            failures.append(f"--has: no entry matches /{pattern}/")

    for pattern in take_values("--absent"):
        if any(re.search(pattern, raw) for raw, _ in rows):
            failures.append(f"--absent: entry matches /{pattern}/")

    seq = take_values("--seq")
    if seq:
        pos = 0
        for sub in seq:
            while pos < len(rows) and sub not in rows[pos][0]:
                pos += 1
            if pos >= len(rows):
                failures.append(f"--seq: substring not found after previous match: {sub!r}")
                break
            pos += 1

    for spec in take_values("--kind-count"):
        kind, _, n = spec.partition("=")
        actual = sum(1 for _, row in rows if row.get("decisionKind") == kind)
        if actual != int(n):
            failures.append(f"--kind-count: {kind}={n} expected, found {actual}")

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        sys.exit(1)
    print(f"ok ({len(rows)} entries)")


if __name__ == "__main__":
    main(sys.argv)
