#!/usr/bin/env python3
"""Week 6 Task B: control's response under the three lock regimes.

    python3 vcd_lock.py <capture.vcd> <figure.svg> nolock|sem|mutex > <stats.txt>

Channels (names as set in PulseView): samp, ctrl, cons, tele, lock.
Control's release is the rising edge of the sampling pulse that released it
(every 10th): the tick ISR itself has no pin, so R here starts ~18 us late
(`lat_peak_us`). instr_ctrl rises before control asks for the PI lock, so a
wait for the lock shows inside its pulse. instr_lock is high while the console
holds the lock: its width is B.

nolock: R over every control job, figure zoomed on the worst one.
sem / mutex: one row per `invert` round (a lock pulse that control asked for),
figure on the worst round, 24 ms wide in both so they compare at one scale.
"""
import bisect
import io
import statistics as st
import sys

VCD, OUT, MODE = sys.argv[1], sys.argv[2], sys.argv[3]

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


def pulses(ch, cut=False):
    """Complete high pulses (rise, fall). One cut by either edge of the
    capture is dropped, unless cut=True (for drawing): then it is clipped."""
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
        out.append((rise, now * ts))
    return out


samp, ctrl, cons, tele, lock = (pulses(c) for c in ("samp", "ctrl", "cons", "tele", "lock"))
samp_r = [r for r, _ in samp]


def released(start):
    """Rising edge of the sampling job that released this control job."""
    return samp_r[bisect.bisect_right(samp_r, start) - 1]


jobs = [(released(r), r, f) for r, f in ctrl if r > samp_r[0]]
R = [f - rel for rel, _, f in jobs]
fmt = lambda xs: f"{min(xs):.2f} / {st.mean(xs):.2f} / {max(xs):.2f} us"

print(f"Task B, {MODE}: {VCD}")
print(f"capture {now * ts / 1e6:.3f} s, {len(jobs)} control jobs, {len(lock)} lock holds by the console")
print(f"control R (sampling rise -> ctrl fall), min / mean / max: {fmt(R)}")
print(f"control pulse (start -> end, lock wait included): {fmt([f - r for _, r, f in jobs])}")
print(f"control R > 10 ms (deadline): {sum(1 for x in R if x > 10000)}")
normal_tele = [f - r for r, f in tele if f - r < 10000]
if normal_tele:
    print(f"telemetry line (not the medium busy work): {fmt(normal_tele)}")

rounds = []
for lr, lf in lock:
    asked = [(r, f) for r, f in ctrl if lr < r < lf]
    if not asked:
        continue
    cr, cf = asked[0]
    med = next(((r, f) for r, f in tele if r > lr and f - r > 10000), None)
    rel = released(cr)
    rounds.append(dict(lr=lr, lf=lf, cr=cr, cf=cf, rel=rel, med=med))

if rounds:
    print()
    print("per round (us; t = 0 at the console taking the lock):")
    print("  control asks   medium runs (start..end)   lock released   control ends"
          "     B (lock width)   control R")
    for d in rounds:
        lr = d["lr"]
        m = (f"{d['med'][0] - lr:8.1f}..{d['med'][1] - lr:8.1f}" if d["med"] else "      none        ")
        print(f"  {d['cr'] - lr:11.1f}   {m}      {d['lf'] - lr:11.1f}   {d['cf'] - lr:11.1f}"
              f"   {d['lf'] - lr:14.1f}   {d['cf'] - d['rel']:9.1f}")
    B = [d["lf"] - d["lr"] for d in rounds]
    Rr = [d["cf"] - d["rel"] for d in rounds]
    print(f"B (lock width): {fmt(B)}")
    print(f"control R in these rounds: {fmt(Rr)}")
    inside = [d for d in rounds if d["med"] and d["med"][0] < d["lf"]]
    print(f"rounds where the medium ran while the console held the lock: {len(inside)} of {len(rounds)}")

# ---- figure -----------------------------------------------------------------
W, X0, X1 = 1120, 170, 1090
COL = {"samp": "#1f5c8b", "ctrl": "#2e7d4f", "cons": "#6b6b6b", "tele": "#b3541e",
       "lock": "#7a4fa3"}
LAB = {"samp": "sampling (prio 2)", "ctrl": "control (prio 3)", "tele": "telemetry (prio 8)",
       "cons": "console (prio 9)", "lock": "console holds lock"}
ORDER = ["samp", "ctrl", "tele", "cons", "lock"]

if MODE == "nolock" or not rounds:
    i = max(range(len(jobs)), key=lambda k: jobs[k][2] - jobs[k][0])
    rel, cr, cf = jobs[i]
    t0, t1 = rel - 20, max(cf + 30, rel + 100)
    marks = [(rel, cf, f"control R = {cf - rel:.1f} us (worst of {len(jobs)})")]
    title = f"No contention: control takes the PI lock uncontended, R ≤ {cf - rel:.0f} us"
    segs = []
else:
    d = max(rounds, key=lambda d: d["cf"] - d["rel"])
    t0 = d["lr"] - 1000
    t1 = t0 + 24000
    marks = [(d["rel"], d["cf"], f"control R = {(d['cf'] - d['rel']) / 1000:.2f} ms"),
             (d["lr"], d["lf"], f"B = {(d['lf'] - d['lr']) / 1000:.2f} ms")]
    lr, lf, cr, cf, med = d["lr"], d["lf"], d["cr"], d["cf"], d["med"]
    if MODE == "sem":
        # The pulse is not B here: telemetry ran inside it.
        held, own = (d["lf"] - d["lr"]) / 1000, (d["lf"] - d["lr"] - (med[1] - med[0])) / 1000
        marks[1] = (d["lr"], d["lf"], f"lock held {held:.2f} ms = {own:.2f} ms of section "
                                      f"+ {(med[1] - med[0]) / 1000:.2f} ms of telemetry")
        title = (f"Priority inversion with a k_sem: control waits "
                 f"{(cf - d['rel']) / 1000:.1f} ms for a {(lf - lr) / 1000 - (med[1] - med[0]) / 1000:.1f} ms section")
        segs = [(lr, cr, "console in section"), (cr, med[0], "control blocks"),
                (med[0], med[1], "telemetry (medium) runs: control waits for it"),
                (med[1], lf, "console ends"), (lf, cf, "control")]
    else:
        title = (f"PI k_mutex: the console runs at control's priority, R = "
                 f"{(cf - d['rel']) / 1000:.2f} ms")
        segs = [(lr, cr, "console in section"), (cr, lf, "console inherits prio 3"),
                (lf, cf, "control")]
        if med:
            segs.append((med[0], med[1], "telemetry (medium) runs after control"))
    title = title.replace("  ", " ")

span = t1 - t0
sx = lambda t: X0 + (t - t0) / span * (X1 - X0)
H = 120 + 70 * len(ORDER) + 50 + 18 * len(segs)
s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
     f'font-family="ui-monospace,Menlo,Consolas,monospace">',
     f'<rect width="{W}" height="{H}" fill="#fbfaf8"/>',
     f'<text x="{X0}" y="32" font-size="17" font-weight="700" fill="#1a1a1a">{title}</text>',
     f'<text x="{X0}" y="54" font-size="12.5" fill="#555">ESP32-C6, firmware/kernel · '
     f'rendered from {VCD.split("/")[-1]} · window {span / 1000:.2f} ms</text>',
     f'<line x1="{X0}" y1="88" x2="{X1}" y2="88" stroke="#bbb"/>']
step = 2000 if span > 5000 else (50 if span < 600 else 500)
for k in range(0, int(span // step) + 1):
    x = sx(t0 + k * step)
    lab = f"{k * step / 1000:.0f} ms" if step >= 1000 else f"{k * step:.0f} us"
    s.append(f'<line x1="{x:.1f}" y1="83" x2="{x:.1f}" y2="88" stroke="#bbb"/>')
    s.append(f'<text x="{x:.1f}" y="77" font-size="11" fill="#666" text-anchor="middle">{lab}</text>')
y = 120
ys = {}
for ch in ORDER:
    ys[ch] = y
    s.append(f'<text x="{X0 - 12}" y="{y + 20}" font-size="12.5" fill="#333" text-anchor="end">{LAB[ch]}</text>')
    s.append(f'<line x1="{X0}" y1="{y + 30}" x2="{X1}" y2="{y + 30}" stroke="#ddd"/>')
    for r, f in pulses(ch, cut=True):
        if f > t0 and r < t1:
            a, b = sx(max(r, t0)), sx(min(f, t1))
            s.append(f'<rect x="{a:.1f}" y="{y}" width="{max(b - a, 0.8):.1f}" height="30" fill="{COL[ch]}"/>')
    y += 70
# brackets: control R over the ctrl row, B over the lock row
for (a, b, text), ch in zip(marks, ("ctrl", "lock")):
    yy = ys[ch] - 8
    s.append(f'<line x1="{sx(a):.1f}" y1="{yy}" x2="{sx(b):.1f}" y2="{yy}" stroke="#1a1a1a"/>')
    for x in (sx(a), sx(b)):
        s.append(f'<line x1="{x:.1f}" y1="{yy - 4}" x2="{x:.1f}" y2="{yy + 4}" stroke="#1a1a1a"/>')
    if sx(b) + 6 + 7 * len(text) <= X1:
        s.append(f'<text x="{sx(b) + 6:.1f}" y="{yy + 4}" font-size="11.5" fill="#1a1a1a">{text}</text>')
    else:  # no room on the right: above the bracket
        s.append(f'<text x="{sx(a):.1f}" y="{yy - 7}" font-size="11.5" fill="#1a1a1a">{text}</text>')
# who runs, segment by segment: numbered on a band, spelled out below it
if segs:
    yb = y - 6
    s.append(f'<text x="{X0 - 12}" y="{yb + 24}" font-size="12.5" fill="#333" text-anchor="end">who runs</text>')
    last = -1e9
    for i, (a, b, text) in enumerate(segs, 1):
        shade = ("#c4c4c4", "#8a8a8a")[i % 2]
        s.append(f'<rect x="{sx(a):.1f}" y="{yb + 14}" width="{max(sx(b) - sx(a), 1.5):.1f}" '
                 f'height="12" fill="{shade}"/>')
        xn = max(sx(a), last + 16)  # short segments: numbers side by side
        s.append(f'<text x="{xn:.1f}" y="{yb + 9}" font-size="11" font-weight="700" fill="#1a1a1a">{i}</text>')
        last = xn
        s.append(f'<text x="{X0}" y="{yb + 48 + 18 * (i - 1)}" font-size="11.5" fill="#444">'
                 f'{i}  {text} ({(b - a) / 1000:.2f} ms)</text>')
s.append("</svg>")
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(s) + "\n")
