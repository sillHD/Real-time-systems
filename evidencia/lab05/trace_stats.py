"""Week-5 numbers from a CTF trace: context switch, tick to sampling, CPU per thread.

Reuses the decoder of Zephyr's scripts/tracing/trace_viewer.py (ZEPHYR_BASE must be
set). Every time printed here is a trace timestamp, so it includes the tracer's own
cost: each event is formatted with interrupts locked. Quote the analyzer for the
numbers and use these to explain them.

Usage: python trace_stats.py <trace_dir> [--log events.txt] [--show N] [--sem N]
  --log FILE  write every event, one per line, times in us from the first event
  --show N    print the events of the N-th tick, from the ISR to sampling pending
  --sem N     same, for the N-th tick in which sampling gives control_sem
  --calib N   the N-th tick that preempts the console while `calib` spins
              (add --sem-tick for one in which sampling also gives control_sem)
"""
import argparse
import os
import statistics as st
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.environ["ZEPHYR_BASE"], "scripts", "tracing"))
import trace_viewer as tv  # noqa: E402

GAP_NS = 1_500_000  # a 1 kHz tick leaves no 1.5 ms without events unless they were dropped
MAX_STEP_NS = 50_000_000  # consecutive events closer than this, or the header is misaligned
MAX_JUMP_NS = 10_000_000_000  # a longer forward jump is accepted if the records after it agree
RESYNC_CHAIN = 4  # records that must decode in a row before trusting a new alignment


class ResyncReader(tv.TraceReader):
    """trace_viewer's reader, but it survives bytes lost on the link.

    The CTF stream has no framing: one lost byte and every record after it is
    misaligned, which trace_viewer reports as a desync and stops. Through
    usbipd at 4 Mbaud the host drops bytes now and then. On a misaligned header
    (unknown id, or a timestamp that goes backwards or jumps) this scans forward
    to the next offset where RESYNC_CHAIN records decode with increasing
    timestamps, and carries on. Events in the skipped bytes are lost.

    A long forward jump is not a misalignment by itself: while `calib` spins, the
    tracer can't drain and the node drops whole events, so the next one can be
    hundreds of ms later. That jump is accepted when the records after it agree.
    """

    def __init__(self, defs):
        super().__init__(defs)
        self.resyncs = []  # (timestamp before the loss, bytes skipped)

    def _valid_at(self, data, off, prev):
        hsz = self.hdr.size
        first_max = MAX_JUMP_NS
        for _ in range(RESYNC_CHAIN):
            if off + hsz > len(data):
                return False
            ts, eid = self.hdr.unpack_from(data, off)
            edef = self.defs.get(eid)
            if edef is None or not (prev <= ts <= prev + first_max):
                return False
            prev, off = ts, off + hsz + edef.size
            first_max = MAX_STEP_NS
        return True

    def decode_all(self, data):
        hsz = self.hdr.size
        off, prev = 0, None
        while off + hsz <= len(data):
            ts, eid = self.hdr.unpack_from(data, off)
            edef = self.defs.get(eid)
            ok = edef is not None and (prev is None or prev <= ts <= prev + MAX_STEP_NS)
            if not ok and edef is not None and prev is not None:
                ok = self._valid_at(data, off, prev)
            if not ok:
                start = off
                off += 1
                while off + hsz <= len(data) and not self._valid_at(data, off, prev):
                    off += 1
                self.resyncs.append((prev, off - start))
                continue
            rec = hsz + edef.size
            if off + rec > len(data):
                break
            fields, _ = edef.decode(data, off + hsz)
            self._consume(ts, eid, edef.name, fields)
            prev, off = ts, off + rec
        return self.tr


def describe(vals_ns):
    us = sorted(v / 1000 for v in vals_ns)
    p99 = us[min(len(us) - 1, int(0.99 * len(us)))]
    return (f"n={len(us):5d}  min={us[0]:7.2f}  median={st.median(us):7.2f}  "
            f"p99={p99:7.2f}  max={us[-1]:7.2f} us")


def tid_of(tr, prefix):
    for tid, info in tr.threads.items():
        if info["name"].startswith(prefix):
            return tid
    sys.exit(f"no thread named {prefix}* in the trace")


def find_ticks(tr, samp):
    """Tick ISRs: the ISR spans in which sampling becomes ready (tick_isr's k_msgq_put).

    For each one: [isr_enter index, ready, isr_exit, sampling switched in,
    sampling pends, others readied in the same ISR, whether sampling gave a semaphore].
    """
    ev = tr.events
    ticks = []
    i = 0
    while i < len(ev):
        if ev[i].name != "isr_enter":
            i += 1
            continue
        enter, ready, isr_exit, others = i, None, None, []
        j = i + 1
        while j < len(ev) and ev[j].name != "isr_exit":
            if ev[j].name == "thread_sched_ready":
                if ev[j].fields.get("thread_id") == samp:
                    ready = j
                else:
                    others.append(tr.threads[ev[j].fields["thread_id"]]["name"])
            j += 1
        isr_exit = j if j < len(ev) else None
        i = j + 1
        if ready is None or isr_exit is None:
            continue
        k = isr_exit
        sw_in = pend = None
        gave = False
        while k < len(ev) and k - isr_exit < 400:
            e = ev[k]
            if sw_in is None and e.name == "thread_switched_in" and e.fields.get("thread_id") == samp:
                sw_in = k
            elif sw_in is not None and e.name == "semaphore_give_enter":
                gave = True
            elif sw_in is not None and e.name == "thread_sched_pend" and e.fields.get("thread_id") == samp:
                pend = k
                break
            k += 1
        if sw_in is None:
            continue
        # A tick whose events straddle a lost stretch would pair an enter with a
        # much later exit: no step of a real tick takes a millisecond.
        last = pend if pend is not None else sw_in
        if any(ev[b].ts - ev[a].ts > 1_000_000 for a, b in zip(range(enter, last), range(enter + 1, last + 1))):
            continue
        before = ev[sw_in - 1]
        preempted = (tr.threads[before.fields["thread_id"]]["name"]
                     if before.name == "thread_switched_out" else None)
        ticks.append(dict(enter=enter, ready=ready, exit=isr_exit, sw_in=sw_in, pend=pend,
                          others=others, gave=gave, preempted=preempted))
    return ticks


def show(tr, t, t0, after=6):
    """The tick's chain, from its ISR to the switch that follows sampling pending."""
    ev = tr.events
    end = (t["pend"] if t["pend"] is not None else t["sw_in"]) + after
    base = ev[t["enter"]].ts
    for e in ev[t["enter"]:min(end, len(ev))]:
        f = dict(e.fields)
        who = tr.threads.get(f.pop("thread_id", None), {}).get("name", "")
        f.pop("name", None)
        rest = tv._fmt_fields(tv.Event(0, 0, "", f)) if f else ""
        print(f"  +{(e.ts - base) / 1000:8.2f} us  (t={(e.ts - t0) / 1e6:9.4f} ms)  "
              f"{e.name:30s} {who:15s} {rest}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace_dir")
    ap.add_argument("--log")
    ap.add_argument("--show", type=int)
    ap.add_argument("--sem", type=int)
    ap.add_argument("--calib", type=int, help="print the N-th tick that preempts the spinning console")
    ap.add_argument("--sem-tick", action="store_true", help="with --calib, pick a tick that gives control_sem")
    a = ap.parse_args()

    defs, used = tv.build_event_defs(os.path.join(a.trace_dir, "metadata"))
    if used is None:
        sys.exit(f"no metadata in {a.trace_dir}: copy it and append ctf_sleep_events.tsdl (SOP-05 §4)")
    reader = ResyncReader(defs)
    with open(os.path.join(a.trace_dir, "channel0_0"), "rb") as fh:
        tr = reader.decode_all(fh.read())
    ev = tr.events
    t0, t1 = ev[0].ts, ev[-1].ts
    name = {tid: info["name"] for tid, info in tr.threads.items()}
    samp = tid_of(tr, "sampling")

    print(f"{a.trace_dir}: {len(ev)} events over {(t1 - t0) / 1e6:.3f} ms")
    skipped = sum(n for _, n in reader.resyncs)
    print(f"link losses: {len(reader.resyncs)} resyncs, {skipped} bytes skipped")

    # Dropped events show up as gaps: the node never goes 1.5 ms without one.
    gaps = [(ev[i - 1].ts, ev[i].ts) for i in range(1, len(ev)) if ev[i].ts - ev[i - 1].ts > GAP_NS]
    lost = sum(b - g for g, b in gaps)
    print(f"gaps > {GAP_NS / 1e6:.1f} ms: {len(gaps)}, {lost / 1e6:.1f} ms in total "
          f"({100 * lost / (t1 - t0):.1f} % of the span)")
    for g, b in gaps[:10]:
        print(f"  at t={(g - t0) / 1e6:9.3f} ms: {(b - g) / 1e6:8.3f} ms without events")
    cnt = Counter(e.name for e in ev)
    print(f"switched_out {cnt['thread_switched_out']}  switched_in {cnt['thread_switched_in']}")

    # 1. Context switch: switched_out to the next switched_in. The tracer drops its
    # own events (is_tracing_thread() in tracing_format_async.c), so a pair with the
    # tracer in between looks like one long switch; anything over SWITCH_MAX_NS is
    # that, not a switch.
    SWITCH_MAX_NS = 30_000
    sw = defaultdict(list)
    tracer_between = 0
    for i, e in enumerate(ev[:-1]):
        n = ev[i + 1]
        if e.name == "thread_switched_out" and n.name == "thread_switched_in":
            if n.ts - e.ts > SWITCH_MAX_NS:
                tracer_between += 1
                continue
            sw["all"].append(n.ts - e.ts)
            sw[f"{name.get(e.fields['thread_id'], '?')} -> {name.get(n.fields['thread_id'], '?')}"].append(n.ts - e.ts)
    print(f"\nContext switch (switched_out -> switched_in, <= {SWITCH_MAX_NS / 1000:.0f} us; "
          f"{tracer_between} longer pairs had the tracer in between)")
    print(f"  {'all':34s} {describe(sw.pop('all'))}")
    for k in sorted(sw, key=lambda k: -len(sw[k]))[:8]:
        print(f"  {k:34s} {describe(sw[k])}")

    # 2. Tick ISR to sampling.
    ticks = find_ticks(tr, samp)
    print(f"\nTick ISR -> sampling ({len(ticks)} ticks)")
    print(f"  isr_enter -> sampling ready      {describe([ev[t['ready']].ts - ev[t['enter']].ts for t in ticks])}")
    print(f"  isr_enter -> isr_exit            {describe([ev[t['exit']].ts - ev[t['enter']].ts for t in ticks])}")
    print(f"  isr_enter -> sampling switched in {describe([ev[t['sw_in']].ts - ev[t['enter']].ts for t in ticks])}")
    ran = [ev[t["pend"]].ts - ev[t["sw_in"]].ts for t in ticks if t["pend"] is not None]
    print(f"  sampling switched in -> pend     {describe(ran)}")
    with_sem = [t for t in ticks if t["gave"]]
    print(f"  ticks in which sampling gives control_sem: {len(with_sem)}")
    shared = Counter(o for t in ticks for o in t["others"])
    print(f"  threads readied in the same tick ISR: {dict(shared) or 'none'}")

    # 3. CPU per thread: running segments over the span. ISR time is inside them.
    busy = defaultdict(int)
    for s, e, tid in tr.segments:
        busy[tid] += min(e, t1) - max(s, t0)
    isr = sum(e - s for s, e in tr.isr_spans)
    span = t1 - t0
    print(f"\nCPU per thread over {span / 1e6:.1f} ms (gaps included)")
    for tid, b in sorted(busy.items(), key=lambda kv: -kv[1]):
        print(f"  {name.get(tid, hex(tid)):16s} {100 * b / span:6.2f} %")
    unseen = span - sum(busy.values())
    print(f"  {'(no lane)':16s} {100 * unseen / span:6.2f} %   tracing_thread, whose events are dropped, plus link losses")
    print(f"  ISRs, inside the rows above: {100 * isr / span:.2f} %")

    # 4. calib: the console spins in k_busy_wait() above the tracer, so the buffer
    # fills and the node drops events until it ends. Its visible part runs from the
    # console's last wake-up before the longest gap to the start of that gap.
    if gaps:
        g, b = max(gaps, key=lambda gb: gb[1] - gb[0])
        con = tid_of(tr, "console")
        # The k_sleep events (ctf_sleep_events.tsdl) carry no thread id: they
        # belong to whichever thread is running when they are emitted.
        woke, cur = [], None
        for e in ev:
            if e.ts > g:
                break
            if e.name == "thread_switched_in":
                cur = e.fields.get("thread_id")
            elif e.name.startswith("thread_sleep") and e.name.endswith("_exit") and cur == con:
                woke.append(e.ts)
        if woke:
            print(f"\nLongest gap: {(b - g) / 1e6:.1f} ms. Console busy since its last wake-up "
                  f"at t={(woke[-1] - t0) / 1e6:.3f} ms: {(g - woke[-1]) / 1e6:.1f} ms of it visible")
        in_calib = [t for t in ticks if woke and woke[-1] <= ev[t["enter"]].ts < g
                    and t["preempted"] == "console_tid"]
        print(f"  ticks in that window that preempt the console: {len(in_calib)}")
        if a.calib is not None and in_calib:
            pick = [t for t in in_calib if t["gave"] == a.sem_tick] or in_calib
            t = pick[min(a.calib, len(pick) - 1)]
            print(f"\nTick inside calib{' with k_sem_give' if a.sem_tick else ''}:")
            show(tr, t, t0)

    if a.show is not None:
        print(f"\nTick {a.show}:")
        show(tr, ticks[a.show], t0)
    if a.sem is not None:
        print(f"\nTick {a.sem} with k_sem_give:")
        show(tr, with_sem[a.sem], t0)

    if a.log:
        with open(a.log, "w") as fh:
            for e in ev:
                f = dict(e.fields)
                who = name.get(f.pop("thread_id", None), "")
                f.pop("name", None)
                rest = tv._fmt_fields(tv.Event(0, 0, "", f)) if f else ""
                fh.write(f"{(e.ts - t0) / 1000:12.2f}  {e.name:32s} {who:15s} {rest}\n")


if __name__ == "__main__":
    main()
