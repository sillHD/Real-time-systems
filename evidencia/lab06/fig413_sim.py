#!/usr/bin/env python3
"""Week 6 Task A: Buttazzo Fig. 4.13 simulated, to read the board against.

tau1 (C = 2 ms, T = 5 ms), tau2 (C = 4 ms, T = 7 ms), D = T, synchronous
release at t = 0. A late job keeps running and releases stay on the absolute
grid, as in firmware/fig413.

    python3 fig413_sim.py                 # book values and the C6's measured tau1
    python3 fig413_sim.py --c1 2027 --c2 4000 --policy RM
"""
import argparse

T = (5000, 7000)  # us
HYPER = 35000


def simulate(c, policy, horizon):
    """Event-driven, preemptive, in microseconds. Returns finished jobs as
    (task, release, finish, finish - deadline)."""
    jobs = ([], [])
    nxt = [0, 0]
    t = 0
    done = []
    cur = None
    while t < horizon:
        for i in (0, 1):
            while nxt[i] <= t:
                jobs[i].append([nxt[i], nxt[i] + T[i], c[i]])
                nxt[i] += T[i]
        ready = [i for i in (0, 1) if jobs[i]]
        if not ready:
            t = min(nxt)
            continue
        if policy == "RM":
            i = min(ready)
        else:
            # Zephyr only preempts for a strictly earlier deadline: on a tie
            # the running job keeps the CPU (t = 30 ms, both due at 35).
            first = min(jobs[k][0][1] for k in ready)
            tied = [k for k in ready if jobs[k][0][1] == first]
            i = cur if cur in tied else min(tied)
        cur = i
        job = jobs[i][0]
        run = min(job[2], min(nxt) - t)
        t += run
        job[2] -= run
        if job[2] == 0:
            done.append((i, job[0], t, t - job[1]))
            jobs[i].pop(0)
            cur = None
    return done


def report(c, policy):
    done = simulate(c, policy, 60_000_000)
    print(f"{policy}  C1 = {c[0] / 1000:.3f} ms  C2 = {c[1] / 1000:.3f} ms")
    for i, name in enumerate(("tau1", "tau2")):
        mine = [d for d in done if d[0] == i]
        misses = [d for d in mine if d[3] > 0]
        r_max = max(d[2] - d[1] for d in mine)
        first = f", first at deadline t = {misses[0][1] / 1000 + T[i] / 1000:.0f} ms" if misses else ""
        print(f"  {name}: {len(mine)} jobs in 60 s, {len(misses)} misses{first}, "
              f"R max = {r_max / 1000:.3f} ms")
    print("  tau2 in the first hyperperiod (release -> finish, slack):")
    for d in done:
        if d[0] == 1 and d[1] < HYPER:
            print(f"    {d[1] / 1000:5.1f} -> {d[2] / 1000:7.3f} ms   slack {-d[3] / 1000:+.3f} ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--c1", type=int, help="tau1 C in us")
    ap.add_argument("--c2", type=int, default=4000, help="tau2 C in us")
    ap.add_argument("--policy", choices=("RM", "EDF"))
    a = ap.parse_args()
    if a.c1:
        report((a.c1, a.c2), a.policy or "RM")
    else:
        # The book, then tau1 as the C6 runs it: 2.027 ms release -> end
        # (taskA-rm-console.txt), the 27 us being the kernel's wake-up path.
        for c in ((2000, 4000), (2027, 4000)):
            for p in ("RM", "EDF"):
                report(c, p)
