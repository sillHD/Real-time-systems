#!/usr/bin/env python3
"""Week 6 Task C: the e-stop path and `calib`, before and after ADR-005.

    python3 vcd_taskc.py <capture.vcd> estop <figure.svg> <label> > <stats.txt>
    python3 vcd_taskc.py <capture.vcd> calib > <stats.txt>

Channels (names as set in PulseView): samp, ctrl, cons, estop, valve.

estop: `estop` rises in the sampling job that detects the overpressure and
falls at `clear`. The valve is closed for good at its last falling edge before
`clear` (0 if it never rises again after the detection). The path is
detection -> valve closed; the worst of the trips is drawn.

calib: the console pin stays high for the whole command (`calib` runs inside
the handler of the Enter key): its longest pulse is the duration. While it
runs, the sampling grid is checked (REQ-CTRL-08).
"""
import io
import statistics as st
import sys

VCD, MODE = sys.argv[1], sys.argv[2]

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
END = now * ts


def pulses(ch, cut=False):
    """High pulses (rise, fall). One cut by either edge of the capture is
    dropped, unless cut=True (for drawing): then it is clipped."""
    out, rise, prev = [], None, None
    for t, v in ed[ch]:
        if v == prev:
            continue
        if v == 1:
            rise = t if (prev is not None or cut) else None
        elif rise is not None:
            out.append((rise, t))
            rise = None
        prev = v
    if cut and rise is not None:
        out.append((rise, END))
    return out


def level_at(ch, t):
    v = 0
    for te, ve in ed[ch]:
        if te > t:
            break
        v = ve
    return v


fmt = lambda xs: f"{min(xs):.2f} / {st.mean(xs):.2f} / {max(xs):.2f}"
samp, ctrl = pulses("samp"), pulses("ctrl")

if MODE == "calib":
    cons = pulses("cons")
    r, f = max(cons, key=lambda p: p[1] - p[0])
    rises = [a for a, _ in samp if r <= a <= f]
    gaps = [b - a for a, b in zip(rises, rises[1:])]
    crise = [a for a, _ in ctrl if r <= a <= f]
    cgaps = [b - a for a, b in zip(crise, crise[1:])]
    print(f"calib: {VCD}")
    print(f"console pulse (the whole command): {(f - r) / 1000:.3f} ms")
    print(f"sampling during it: {len(rises)} jobs, period {fmt(gaps)} us (min / mean / max)")
    print(f"control during it: {len(crise)} jobs, period {fmt(cgaps)} us")
    sys.exit(0)

OUT, LABEL = sys.argv[3], sys.argv[4]
valve = ed["valve"]
trips = []
for er, ef in pulses("estop"):
    falls = [t for t, v in valve if er < t < ef and v == 0]
    rises = [t for t, v in valve if er < t < ef and v == 1]
    if rises and falls and max(rises) > max(falls):
        closed = None  # the valve was still open at `clear`: never closed
    elif falls:
        closed = max(falls)
    else:
        closed = er if level_at("valve", er) == 0 else None
    c_after = next(((a, b) for a, b in ctrl if a > er), None)
    s_after = next(((a, b) for a, b in samp if c_after and a > c_after[1]), None)
    trips.append(dict(er=er, ef=ef, closed=closed, ctrl=c_after, samp=s_after,
                      open_at=level_at("valve", er)))

print(f"e-stop, {LABEL}: {VCD}")
print(f"{len(trips)} trips (estop pulses complete in the capture)")
print("  detection -> control starts   control ends -> valve closed   detection -> valve closed"
      "   valve open at detection")
for d in trips:
    if d["closed"] is None:
        print("   valve never closed before clear")
        continue
    c = d["ctrl"]
    a = (c[0] - d["er"]) / 1000 if c else float("nan")
    b = (f"{(d['closed'] - c[1]) / 1000:26.3f} ms" if c and d["closed"] > c[1]
         else f"{'closed before control ran':>29}")
    print(f"  {a:25.3f} ms   {b}   {(d['closed'] - d['er']) / 1000:24.3f} ms"
          f"   {'yes' if d['open_at'] else 'no'}")
paths = [(d["closed"] - d["er"]) / 1000 for d in trips if d["closed"] is not None]
print(f"detection -> valve closed, min / mean / max over {len(paths)} trips: "
      f"{fmt([x * 1000 for x in paths])} us")

# ---- figure: the worst trip --------------------------------------------------
d = max((d for d in trips if d["closed"] is not None), key=lambda d: d["closed"] - d["er"])
if d["closed"] - d["er"] < 100:  # closed inside the detecting job: zoom on it
    t0, t1 = d["er"] - 15, d["er"] + 30
else:
    t0, t1 = d["er"] - 1500, max(d["closed"], d["er"]) + 2500
span = t1 - t0
W, X0, X1 = 1120, 170, 1090
sx = lambda t: X0 + (t - t0) / span * (X1 - X0)
COL = {"samp": "#1f5c8b", "ctrl": "#2e7d4f", "estop": "#b3261e", "valve": "#7a4fa3"}
LAB = {"samp": "sampling 1 kHz", "ctrl": "control 10 ms", "estop": "e-stop detected",
       "valve": "valve (GPIO2)"}
ORDER = ["samp", "ctrl", "estop", "valve"]
path = (d["closed"] - d["er"]) / 1000
H = 120 + 70 * len(ORDER) + 30
s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
     f'font-family="ui-monospace,Menlo,Consolas,monospace">',
     f'<rect width="{W}" height="{H}" fill="#fbfaf8"/>',
     f'<text x="{X0}" y="32" font-size="17" font-weight="700" fill="#1a1a1a">'
     f'E-stop {LABEL}: detection to valve closed in '
     f'{f"{path:.3f} ms" if path >= 0.1 else f"{path * 1000:.2f} us"} (worst of {len(paths)})</text>',
     f'<text x="{X0}" y="54" font-size="12.5" fill="#555">ESP32-C6, firmware/kernel · '
     f'rendered from {VCD.split("/")[-1]} · window '
     f'{f"{span / 1000:.1f} ms" if span >= 1000 else f"{span:.0f} us"} · REQ: 5 ms</text>',
     f'<line x1="{X0}" y1="88" x2="{X1}" y2="88" stroke="#bbb"/>']
step = 1000 if span > 4000 else (200 if span > 400 else 5)
for k in range(0, int(span // step) + 1):
    x = sx(t0 + k * step)
    lab = (f"{k * step / 1000:.0f} ms" if step >= 1000 else
           f"{k * step / 1000:.1f} ms" if step >= 100 else f"{k * step:.0f} us")
    s.append(f'<line x1="{x:.1f}" y1="83" x2="{x:.1f}" y2="88" stroke="#bbb"/>')
    s.append(f'<text x="{x:.1f}" y="77" font-size="11" fill="#666" text-anchor="middle">{lab}</text>')
y = 120
for ch in ORDER:
    s.append(f'<text x="{X0 - 12}" y="{y + 20}" font-size="12.5" fill="#333" text-anchor="end">{LAB[ch]}</text>')
    s.append(f'<line x1="{X0}" y1="{y + 30}" x2="{X1}" y2="{y + 30}" stroke="#ddd"/>')
    for r, f in pulses(ch, cut=True):
        if f > t0 and r < t1:
            a, b = sx(max(r, t0)), sx(min(f, t1))
            s.append(f'<rect x="{a:.1f}" y="{y}" width="{max(b - a, 0.8):.1f}" height="30" fill="{COL[ch]}"/>')
    y += 70
yy = 120 + 70 * 3 - 10
a, b = sx(d["er"]), sx(d["closed"])
s.append(f'<line x1="{a:.1f}" y1="{yy}" x2="{max(b, a + 1):.1f}" y2="{yy}" stroke="#1a1a1a"/>')
for x in (a, b):
    s.append(f'<line x1="{x:.1f}" y1="{yy - 4}" x2="{x:.1f}" y2="{yy + 4}" stroke="#1a1a1a"/>')
if path >= 0.1:
    text = f"detection -> valve closed = {path:.3f} ms"
else:
    text = f"detection -> valve closed = {path * 1000:.2f} us, same sampling job"
if max(b, a) + 6 + 7 * len(text) <= X1:
    s.append(f'<text x="{max(b, a) + 6:.1f}" y="{yy + 4}" font-size="11.5" fill="#1a1a1a">{text}</text>')
else:  # no room on the right: above the bracket
    s.append(f'<text x="{a:.1f}" y="{yy - 7}" font-size="11.5" fill="#1a1a1a">{text}</text>')
# REQ-CTRL-02: 5 ms from the detecting sample
xr = sx(d["er"] + 5000)
if xr < X1:
    s.append(f'<line x1="{xr:.1f}" y1="100" x2="{xr:.1f}" y2="{120 + 70 * len(ORDER) - 30}" '
             f'stroke="#b3261e" stroke-dasharray="4 3"/>')
    s.append(f'<text x="{xr + 4:.1f}" y="112" font-size="11" fill="#b3261e">deadline 5 ms</text>')
s.append("</svg>")
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(s) + "\n")
