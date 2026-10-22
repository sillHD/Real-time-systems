#!/usr/bin/env python3
"""Week 6 Task A: reads a firmware/fig413 capture and draws one hyperperiod.

    python3 vcd_fig413.py <capture.vcd> <figure.svg> RM|EDF > <stats.txt>

Channels (names as set in PulseView): tau1, tau2, miss1, miss2. A run pin is
high from the job's start to its end, preemption included; a miss pin rises at
the missed deadline (the k_timer ISR, a few us after the tick) and drops when
the late job ends.

The tau2 release grid is taken from the miss2 rising edges (the deadline of one
job is the release of the next, D = T). Without misses (EDF) it is taken from
the synchronous instants instead: the tau2 starts that coincide with a tau1
start, every 35 ms.
"""
import io
import statistics as st
import sys

VCD, OUT, POLICY = sys.argv[1], sys.argv[2], sys.argv[3]
T1, T2, HYPER = 5000.0, 7000.0, 35000.0  # us

# ---- VCD -> edges per channel, in us ----------------------------------------
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
end_us = now * ts


def pulses(ch):
    """Complete high pulses (rise, fall); one cut by either edge of the
    capture is dropped."""
    out, rise, prev = [], None, None
    for t, v in ed[ch]:
        if v == prev:
            continue
        if v == 1:
            rise = t if prev is not None else None
        elif rise is not None:
            out.append((rise, t))
            rise = None
        prev = v
    return out


tau1, tau2, miss1, miss2 = (pulses(c) for c in ("tau1", "tau2", "miss1", "miss2"))
span = (tau1[-1][1] - tau1[0][0]) if tau1 else 0.0

# ---- tau2 release grid ------------------------------------------------------
# The board's clock and the analyzer's differ by ~140 ppm (week 4): ~350 us over
# a 2.5 s capture, so the grid's period is fitted, not assumed.


def fit(points, period):
    """Least squares t = phase + k * period' over points that sit one grid
    step apart, k counted from the first point."""
    ks = [round((p - points[0]) / period) for p in points]
    mk, mp = st.mean(ks), st.mean(points)
    sl = (sum((k - mk) * (p - mp) for k, p in zip(ks, points)) /
          sum((k - mk) ** 2 for k in ks))
    return mp - sl * mk, sl


starts1 = [r for r, _ in tau1]


def release_of(start):
    """Latest tau2 release at or before this start (a late job starts after
    its release, never before it)."""
    k = (start - phase + 50) // T2s
    return phase + k * T2s


if miss2:
    phase, T2s = fit([r for r, _ in miss2], T2)
else:
    # No misses to mark the grid. tau1's own grid comes from the tau1 jobs
    # that started on their release: with tau2 low and not just finished
    # (one that waited for tau2 starts right at tau2's end). Every release
    # sits on the board's 1 ms tick, so tau2's grid is tau1's shifted by a
    # whole number of ms: the shift that keeps every tau2 start at or after
    # its release, as early as possible.
    def tau2_busy(t):
        return any(r - 50 < t < f + 50 for r, f in tau2)

    on_time = [r for r in starts1 if not tau2_busy(r)]
    phase1, T1s_fit = fit(on_time, T1)
    T2s = T1s_fit / T1 * T2
    best = None
    for j in range(7):
        phase = phase1 + j * T1s_fit / 5
        waits = [r - release_of(r) for r, _ in tau2]
        if min(waits) > -50 and (best is None or st.mean(waits) < best[0]):
            best = (st.mean(waits), phase)
    phase = best[1]
scale = T2s / T2
T1s, HYPs = T1 * scale, HYPER * scale


# ---- statistics -------------------------------------------------------------
w1 = [f - r for r, f in tau1]
jobs2 = [(release_of(r), r, f) for r, f in tau2]
resp2 = [f - rel for rel, _, f in jobs2]
pre = sum(1 for r1, _ in tau1 if any(r2 < r1 < f2 for r2, f2 in tau2))

# Synchronous instants: the tau2 releases that are also tau1 releases. Each
# hyperperiod holds five tau2 jobs, at offsets 0, 7, 14, 21, 28 ms.
first_sync = next((rel for rel, _, _ in jobs2
                   if any(-300 < s - rel < 300 for s in starts1)), jobs2[0][0])
by_offset = {}
for rel, r, f in jobs2:
    off = round(((rel - first_sync) % HYPs) / scale / 1000.0) % 35
    late = f - (rel + T2s)
    by_offset.setdefault(off, []).append((f - rel, late))

print(f"Fig. 4.13 under {POLICY}: {VCD}")
print(f"capture {end_us / 1e6:.3f} s, {span / 1e6:.3f} s of complete tau1 jobs, "
      f"{span / HYPER:.1f} hyperperiods")
print(f"tau2 period fitted on the analyzer's clock: {T2s:.3f} us "
      f"({(scale - 1) * 1e6:+.0f} ppm against the board)")
print()
print(f"tau1: {len(tau1)} jobs, run pin {min(w1):.2f} / {st.mean(w1):.2f} / {max(w1):.2f} us "
      f"(min / mean / max), misses {len(miss1)}")
print(f"tau2: {len(tau2)} jobs, R {min(resp2):.2f} / {st.mean(resp2):.2f} / {max(resp2):.2f} us "
      f"(release -> end, release grid from {'miss2' if miss2 else 'sync starts'})")
if miss2:
    mw = [f - r for r, f in miss2]
    print(f"tau2 misses: {len(miss2)} of {len(tau2)} jobs ({len(miss2) / len(tau2):.1%}), "
          f"{len(miss2) / (span / HYPER):.2f} per 35 ms; lateness (miss pin) "
          f"{min(mw):.2f} / {st.mean(mw):.2f} / {max(mw):.2f} us")
else:
    print("tau2 misses: 0")
print(f"preemptions (tau1 starts while tau2 is running): {pre}, "
      f"{pre / (span / HYPER):.2f} per 35 ms")
print()
print("tau2 by release offset inside the hyperperiod:")
print("  offset   jobs  misses   R max (us)   worst finish - deadline (us)")
for off in sorted(by_offset):
    rows = by_offset[off]
    print(f"  {off:4d} ms {len(rows):5d} {sum(1 for _, l in rows if l > 0):6d}"
          f"   {max(r for r, _ in rows):10.2f}   {max(l for _, l in rows):+10.2f}")

# ---- figure: one hyperperiod from a synchronous instant ---------------------
W, X0, X1 = 1120, 170, 1090
t0 = first_sync
t1 = t0 + HYPER
sx = lambda t: X0 + (t - t0) / HYPER * (X1 - X0)
BLUE, GREEN, RED, LIGHT = "#1f5c8b", "#2e7d4f", "#b3261e", "#cfe3d6"
rows = [("tau1", "τ1  C=2 T=5", 120), ("tau2", "τ2  C=4 T=7", 190),
        ("miss2", "τ2 late", 260)]
H = 360
s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
     f'font-family="ui-monospace,Menlo,Consolas,monospace">',
     f'<rect width="{W}" height="{H}" fill="#fbfaf8"/>']
n_miss = sum(1 for r, _ in miss2 if t0 <= r < t1)
title = (f"Fig. 4.13 under {POLICY} on the C6: τ2 misses {n_miss} of 5 deadlines"
         if POLICY == "RM" else
         f"Fig. 4.13 under {POLICY} on the C6: every deadline met")
s.append(f'<text x="{X0}" y="32" font-size="17" font-weight="700" fill="#1a1a1a">{title}</text>')
s.append(f'<text x="{X0}" y="54" font-size="12.5" fill="#555">ESP32-C6, firmware/fig413 · '
         f'rendered from {VCD.split("/")[-1]} · one hyperperiod (35 ms) from a synchronous release</text>')
s.append(f'<line x1="{X0}" y1="88" x2="{X1}" y2="88" stroke="#bbb"/>')
for ms in range(0, 36, 5):
    x = sx(t0 + ms * 1000)
    s.append(f'<line x1="{x:.1f}" y1="83" x2="{x:.1f}" y2="88" stroke="#bbb"/>')
    s.append(f'<text x="{x:.1f}" y="77" font-size="11" fill="#666" text-anchor="middle">{ms} ms</text>')
# deadlines: tau1 every 5 ms (thin), tau2 every 7 ms (dashed)
for k in range(0, 8):
    x = sx(t0 + k * T1)
    s.append(f'<line x1="{x:.1f}" y1="100" x2="{x:.1f}" y2="150" stroke="{BLUE}" stroke-opacity="0.25"/>')
for k in range(0, 6):
    x = sx(t0 + k * T2)
    s.append(f'<line x1="{x:.1f}" y1="100" x2="{x:.1f}" y2="290" stroke="{RED}" '
             f'stroke-opacity="0.45" stroke-dasharray="4 3"/>')
for ch, label, y in rows:
    s.append(f'<text x="{X0 - 12}" y="{y + 20}" font-size="12.5" fill="#333" text-anchor="end">{label}</text>')
    s.append(f'<line x1="{X0}" y1="{y + 30}" x2="{X1}" y2="{y + 30}" stroke="#ddd"/>')
for r, f in tau1:
    if f > t0 and r < t1:
        a, b = sx(max(r, t0)), sx(min(f, t1))
        s.append(f'<rect x="{a:.1f}" y="120" width="{b - a:.1f}" height="30" fill="{BLUE}"/>')
for r, f in tau2:
    if f > t0 and r < t1:
        # executing where tau1 is low, waiting (preempted) where tau1 is high
        a, b = max(r, t0), min(f, t1)
        s.append(f'<rect x="{sx(a):.1f}" y="190" width="{sx(b) - sx(a):.1f}" height="30" fill="{LIGHT}"/>')
        cut = [(max(a, r1), min(b, f1)) for r1, f1 in tau1 if f1 > a and r1 < b]
        cur = a
        for ca, cb in cut + [(b, b)]:
            if ca > cur:
                s.append(f'<rect x="{sx(cur):.1f}" y="190" width="{sx(ca) - sx(cur):.1f}" '
                         f'height="30" fill="{GREEN}"/>')
            cur = max(cur, cb)
for r, f in miss2:
    if f > t0 and r < t1:
        a, b = sx(max(r, t0)), sx(min(f, t1))
        s.append(f'<rect x="{a:.1f}" y="260" width="{b - a:.1f}" height="30" fill="{RED}"/>')
        s.append(f'<text x="{(a + b) / 2:.1f}" y="306" font-size="11" fill="{RED}" '
                 f'text-anchor="middle">+{(f - r) / 1000:.2f} ms</text>')
s.append(f'<rect x="{X0}" y="328" width="12" height="12" fill="{GREEN}"/>')
s.append(f'<text x="{X0 + 18}" y="339" font-size="11.5" fill="#444">τ2 running</text>')
s.append(f'<rect x="{X0 + 120}" y="328" width="12" height="12" fill="{LIGHT}"/>')
s.append(f'<text x="{X0 + 138}" y="339" font-size="11.5" fill="#444">τ2 preempted by τ1</text>')
s.append(f'<line x1="{X0 + 310}" y1="334" x2="{X0 + 330}" y2="334" stroke="{RED}" stroke-dasharray="4 3"/>')
s.append(f'<text x="{X0 + 336}" y="339" font-size="11.5" fill="#444">τ2 release = previous deadline</text>')
s.append("</svg>")
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(s) + "\n")
