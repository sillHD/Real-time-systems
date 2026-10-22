#!/usr/bin/env python3
"""Week 6: the node's measured C_i, from any firmware/kernel capture.

    python3 vcd_taskset.py <capture.vcd> > <stats.txt>

For every instrumentation channel (names as set in PulseView): pulse count,
width min / mean / max (the task's C_i on the analyzer) and mean period. The
max is what analysis/taskset.csv takes as C_i.
"""
import io
import statistics as st
import sys

VCD = sys.argv[1]
CHANNELS = [("samp", "sampling"), ("ctrl", "control"), ("disp", "display"),
            ("tele", "telemetry"), ("cons", "console (per key)"), ("lock", "console lock")]

sym, ed, ts, now = {}, {}, None, 0
with io.open(VCD, encoding="utf-8", errors="replace") as fh:
    in_defs = True
    for line in fh:
        line = line.strip()
        if not line:
            continue
        if in_defs:
            if line.startswith("$timescale"):
                p = line.split()
                ts = {"ns": 1e-3, "us": 1.0}[p[2]] * float(p[1])
            elif line.startswith("$var"):
                p = line.split()
                sym[p[3]] = p[4]
                ed[p[4]] = []
            elif line.startswith("$enddefinitions"):
                in_defs = False
            continue
        toks = line.split()
        if toks[0].startswith("#"):
            now = int(toks[0][1:])
            toks = toks[1:]
        for t in toks:
            if t[0] in "01" and len(t) >= 2 and t[1:] in sym:
                ed[sym[t[1:]]].append((now * ts, int(t[0])))


def pulses(ch):
    out, rise, prev = [], None, None
    for t, v in ed.get(ch, []):
        if v == prev:
            continue
        if v == 1:
            rise = t if prev is not None else None
        elif rise is not None:
            out.append((rise, t))
            rise = None
        prev = v
    return out


print(f"C_i on the analyzer: {VCD}, {now * ts / 1e6:.3f} s at 4 MHz (250 ns resolution)")
print(f"{'task':20} {'pulses':>7} {'C min':>10} {'C mean':>10} {'C max':>10} {'period':>12}")
for ch, name in CHANNELS:
    p = pulses(ch)
    if not p:
        print(f"{name:20} {0:7d}")
        continue
    w = [f - r for r, f in p]
    per = [b[0] - a[0] for a, b in zip(p, p[1:])]
    print(f"{name:20} {len(p):7d} {min(w):8.2f}us {st.mean(w):8.2f}us {max(w):8.2f}us "
          f"{(st.mean(per) / 1000 if per else 0):9.3f} ms")
