"""Draw one or two ticks of a CTF trace as an annotated SVG (Task B evidence).

    python trace_figure.py <trace_dir> <out.svg> <t_ms> [<t_ms> ...]

Each t_ms is a tick's isr_enter time in ms from the first event, as printed by
`trace_stats.py --calib` / `--sem`. Each tick gets a panel with the ISR, the
three threads involved and the two instrumentation pins, and a numbered marker
on every event of the chain. Pin edges are drawn at the gpio call's exit event:
the register write happens after the enter event has been formatted.
"""
import html
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trace_stats as ts  # noqa: E402

WINDOW_US = 200.0
PIN_SAMP, PIN_CTRL = 1 << 3, 1 << 4  # GPIO3 instr_samp, GPIO4 instr_ctrl on the C6
LANES = [
    ("[ISR]", "tick ISR", "#b3541e"),
    ("sampling_tid", "prio 2", "#1f5c8b"),
    ("control_tid", "prio 3", "#2e7d4f"),
    ("console_tid", "prio 9", "#6b6b6b"),
    ("instr_samp", "GPIO3 pin", "#1f5c8b"),
    ("instr_ctrl", "GPIO4 pin", "#2e7d4f"),
]
W, LEFT, ROW = 1120, 170, 34
PW = W - LEFT - 30


def chain(tr, i0):
    """Events of one tick, from its isr_enter to the first thread switched in
    after sampling pends, with the label each marker gets."""
    ev, name = tr.events, {t: v["name"] for t, v in tr.threads.items()}
    out, pended, call_pins = [], False, 0
    for k in range(i0, len(ev)):
        e = ev[k]
        who = name.get(e.fields.get("thread_id"), "")
        # Only a gpio *_enter carries the pins; its *_exit inherits them.
        if e.name.startswith("gpio_port_") and e.name.endswith("_enter"):
            call_pins = e.fields.get("pins", 0)
        pins = call_pins if e.name.startswith("gpio_port_") else 0
        e.fields["pins"] = pins
        label = None
        if e.name == "isr_enter":
            label = "tick ISR enters"
        elif e.name == "thread_sched_ready" and who == "sampling_tid":
            label = "k_msgq_put: sampling ready"
        elif e.name == "isr_exit":
            label = "ISR exits"
        elif e.name == "thread_switched_out" and not pended:
            label = f"{who} preempted: ready, not blocked" if who != "idle" else "idle switched out"
        elif e.name == "thread_switched_in" and who == "sampling_tid":
            label = "sampling switched in"
        elif e.name == "gpio_port_set_bits_raw_exit" and pins == PIN_SAMP:
            label = "instr_samp rises"
        elif e.name == "semaphore_give_enter":
            label = "k_sem_give(control_sem)"
        elif e.name == "thread_sched_ready" and who == "control_tid":
            label = "control ready: prio 3, waits for sampling (prio 2)"
        elif e.name == "thread_sched_pend" and who == "sampling_tid":
            label, pended = "k_msgq_get: sampling pends", True
        elif e.name == "thread_switched_in" and pended:
            label = f"CPU to {who}"
        elif e.name == "gpio_port_set_bits_raw_exit" and pins == PIN_CTRL:
            label = "instr_ctrl rises"
        out.append((e, who, label))
        if e.name == "thread_switched_in" and pended and who != "control_tid":
            break
        if e.name == "gpio_port_set_bits_raw_exit" and pins == PIN_CTRL:
            break
    return out


def panel(add, tr, events, top, title):
    base_ts = events[0][0].ts
    x = lambda t: LEFT + (t - base_ts) / 1000 / WINDOW_US * PW  # noqa: E731
    lane_y = {n: top + 58 + i * ROW for i, (n, _d, _c) in enumerate(LANES)}
    add('<text x="%d" y="%d" font-size="14" font-weight="700" fill="#1a1a1a">%s</text>'
        % (LEFT, top + 14, title))
    # time axis
    ay = top + 34
    add('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#bbb"/>' % (LEFT, ay, LEFT + PW, ay))
    for i in range(9):
        px = LEFT + PW * i / 8
        add('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#bbb"/>' % (px, ay - 5, px, ay))
        add('<text x="%.1f" y="%d" font-size="11" fill="#666" text-anchor="middle">+%.0f us</text>'
            % (px, ay - 9, WINDOW_US * i / 8))
    for n, desc, colour in LANES:
        y = lane_y[n]
        add('<text x="12" y="%d" font-size="12.5" font-weight="700" fill="#1a1a1a">%s</text>' % (y + 14, n))
        add('<text x="12" y="%d" font-size="10.5" fill="#777">%s</text>' % (y + 26, desc))
        add('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#ddd"/>' % (LEFT, y + 22, LEFT + PW, y + 22))

    def bar(lane, t_a, t_b, ready=False):
        colour = dict((n, c) for n, _d, c in LANES)[lane]
        y = lane_y[lane]
        xa, xb = x(t_a), min(x(t_b), LEFT + PW)
        if ready:
            add('<rect x="%.2f" y="%d" width="%.2f" height="14" fill="%s" opacity="0.18" '
                'stroke="%s" stroke-dasharray="3 2"/>' % (xa, y + 8, max(1, xb - xa), colour, colour))
        else:
            add('<rect x="%.2f" y="%d" width="%.2f" height="18" fill="%s"/>'
                % (xa, y + 4, max(1, xb - xa), colour))

    end_ts = base_ts + WINDOW_US * 1000
    first = events[0][0]
    # ISR
    isr_in = first.ts
    for e, who, _l in events:
        if e.name == "isr_exit":
            bar("[ISR]", isr_in, e.ts)
            break
    # threads: running from switched_in to switched_out, ready in between
    running = {}
    ready_since = {}
    preempted = None
    for e, who, _l in events:
        if e.name == "thread_switched_out" and preempted is None and who in ("console_tid",):
            preempted = e.ts
            ready_since[who] = e.ts
        if e.name == "thread_sched_ready" and who in ("control_tid",):
            ready_since[who] = e.ts
        if e.name == "thread_switched_in" and who in lane_y:
            if who in ready_since:
                bar(who, ready_since.pop(who), e.ts, ready=True)
            running[who] = e.ts
        if e.name == "thread_switched_out" and who in running:
            bar(who, running.pop(who), e.ts)
    if preempted is None and "console_tid" not in running:
        pass
    for who, t_a in running.items():
        bar(who, t_a, end_ts)
    for who, t_a in ready_since.items():
        bar(who, t_a, end_ts, ready=True)
    # pins
    samp_up = None
    for e, who, _l in events:
        pins = e.fields.get("pins", 0)
        if e.name == "gpio_port_set_bits_raw_exit" and pins == PIN_SAMP:
            samp_up = e.ts
        if e.name == "gpio_port_clear_bits_raw_exit" and pins == PIN_SAMP and samp_up:
            bar("instr_samp", samp_up, e.ts)
            samp_up = None
        if e.name == "gpio_port_set_bits_raw_exit" and pins == PIN_CTRL:
            bar("instr_ctrl", e.ts, end_ts)
    # numbered markers
    marks = [(e, l) for e, _w, l in events if l]
    y_top, y_bot = top + 40, lane_y[LANES[-1][0]] + 24
    legend_y = y_bot + 22
    for n, (e, label) in enumerate(marks, 1):
        px = x(e.ts)
        add('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#d92b2b" stroke-width="1" '
            'stroke-dasharray="3 3" opacity="0.7"/>' % (px, y_top + 8, px, y_bot))
        add('<circle cx="%.1f" cy="%d" r="8" fill="#d92b2b"/>' % (px, y_top))
        add('<text x="%.1f" y="%d" font-size="10" font-weight="700" fill="#fff" '
            'text-anchor="middle">%d</text>' % (px, y_top + 3.5, n))
        col, row = (n - 1) // 6, (n - 1) % 6
        add('<text x="%d" y="%d" font-size="11.5" fill="#1a1a1a">'
            '<tspan font-weight="700" fill="#d92b2b">%d</tspan>  +%6.2f us  %s</text>'
            % (LEFT + col * 470, legend_y + row * 16, n, (e.ts - base_ts) / 1000, html.escape(label)))
    return legend_y + 6 * 16 + 10, marks, base_ts


def main():
    trace_dir, out = sys.argv[1], sys.argv[2]
    at_ms = [float(v) for v in sys.argv[3:]]
    defs, _used = ts.tv.build_event_defs(os.path.join(trace_dir, "metadata"))
    reader = ts.ResyncReader(defs)
    with open(os.path.join(trace_dir, "channel0_0"), "rb") as fh:
        tr = reader.decode_all(fh.read())
    t0 = tr.events[0].ts

    s = []
    add = s.append
    body_top = 70
    y = body_top
    notes = []
    for t_ms in at_ms:
        target = t0 + t_ms * 1e6
        i0 = min((i for i, e in enumerate(tr.events) if e.name == "isr_enter"),
                 key=lambda i: abs(tr.events[i].ts - target))
        events = chain(tr, i0)
        gave = any(e.name == "semaphore_give_enter" for e, _w, _l in events)
        title = (f"A 10th tick at t = {t_ms:.3f} ms: k_sem_give sends the CPU to control"
                 if gave else f"One tick inside calib at t = {t_ms:.3f} ms: the console is preempted")
        y, marks, base = panel(add, tr, events, y, title)
        lab = {lbl: e.ts for e, lbl in marks}
        sw = next(v for k, v in lab.items() if k == "sampling switched in")
        lines = [f"tick ISR to sampling switched in: {(sw - base) / 1000:.2f} us "
                 f"(lat_peak_us 68 us traced, 16 us untraced)"]
        if gave:
            fall = next(e.ts for e, _w, _l in events
                        if e.name == "gpio_port_clear_bits_raw_exit" and e.fields.get("pins") == PIN_SAMP)
            rise = lab.get("instr_ctrl rises")
            if rise:
                lines.append(f"instr_samp falls to instr_ctrl rises: {(rise - fall) / 1000:.2f} us "
                             f"(analyzer, untraced: 14.50 us)")
        for line in lines:
            add('<text x="%d" y="%d" font-size="12" font-weight="700" fill="#555">%s</text>' % (LEFT, y, line))
            y += 18
        y += 26
    H = y
    head = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
        'font-family="ui-monospace,Menlo,Consolas,monospace">' % (W, H, W),
        '<rect width="%d" height="%d" fill="#fbfaf8"/>' % (W, H),
        '<text x="%d" y="32" font-size="17" font-weight="700" fill="#1a1a1a">'
        'The story of one preemption, from the trace</text>' % LEFT,
        '<text x="%d" y="54" font-size="12.5" fill="#555">ESP32-C6 kernel node, CTF over the UART jack at 4 Mbaud · '
        'rendered from %s · solid = running, dashed = ready</text>' % (LEFT, trace_dir.rstrip("/") + "/channel0_0"),
    ]
    io.open(out, "w", encoding="utf-8").write("\n".join(head + s + ["</svg>"]))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
