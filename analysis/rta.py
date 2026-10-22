#!/usr/bin/env python3
"""Utilization tests and response-time analysis of the SoilSense Control node.

    python3 analysis/rta.py                              # analysis/taskset.csv as measured
    python3 analysis/rta.py --set control.T_us=5000      # change one parameter live
    python3 analysis/rta.py --add sensor_a,4,800,29,5000 # add a task: name,prio,C,ovh,T[,D,S,cs]
    python3 analysis/rta.py --taskset other.csv

Fixed priorities as in firmware/kernel (lower number = higher priority). Each
row of the task set carries its measured C_i and where it was measured.

    C_i  = C_us + ovh_us     body on the analyzer + kernel cost charged per job
    R_i  = S_i + C_i + B_i + sum over higher h of ceil(R_i / T_h) * C_h

iterated from R_i = S_i + C_i + B_i until it stops moving, or stopped as soon as
it passes D_i (Buttazzo, Fig. 4.17, with blocking). S_i is self-suspension
(time asleep inside the job, e.g. calib's settles), counted whole: pessimistic,
and simple.

B_i, with one PI lock (priority inheritance, Zephyr's k_mutex): a job can wait
once for the longest critical section (cs_us) of a lower-priority task that
uses the lock, if the lock's ceiling (its highest-priority user) is at or above
the job's priority. Below the ceiling and above the holder that is
push-through blocking: the holder inherits the ceiling and runs ahead of them.

The utilization tests (Liu & Layland, hyperbolic) are for RM with D = T and are
only sufficient; RTA decides.
"""
import argparse
import csv
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ["name", "prio", "C_us", "ovh_us", "T_us", "D_us", "S_us", "cs_us"]

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--taskset", default=os.path.join(HERE, "taskset.csv"))
ap.add_argument("--set", action="append", default=[], metavar="TASK.FIELD=VALUE")
ap.add_argument("--add", action="append", default=[], metavar="name,prio,C,ovh,T[,D,S,cs]")
ap.add_argument("--no-blocking", action="store_true", help="B_i = 0 for every task")
args = ap.parse_args()

with open(args.taskset, newline="", encoding="utf-8") as fh:
    tasks = [{k: (r[k] if k == "name" else float(r[k])) for k in FIELDS} for r in csv.DictReader(fh)]

for spec in args.add:
    v = spec.split(",")
    t = dict(name=v[0], prio=float(v[1]), C_us=float(v[2]), ovh_us=float(v[3]), T_us=float(v[4]))
    t["D_us"] = float(v[5]) if len(v) > 5 else t["T_us"]
    t["S_us"] = float(v[6]) if len(v) > 6 else 0.0
    t["cs_us"] = float(v[7]) if len(v) > 7 else 0.0
    tasks.append(t)
for spec in args.set:
    key, val = spec.split("=")
    name, field = key.split(".")
    match = [t for t in tasks if t["name"] == name]
    if not match or field not in FIELDS[1:]:
        raise SystemExit(f"--set {spec}: no task '{name}' or no field '{field}'")
    match[0][field] = float(val)

tasks.sort(key=lambda t: t["prio"])
for t in tasks:
    t["C"] = t["C_us"] + t["ovh_us"]
    t["U"] = t["C"] / t["T_us"]

# ---- blocking under priority inheritance, one lock --------------------------
users = [t for t in tasks if t["cs_us"] > 0]
ceiling = min((t["prio"] for t in users), default=math.inf)
for t in tasks:
    lower = [u["cs_us"] for u in users if u["prio"] > t["prio"]]
    t["B"] = max(lower) if (lower and t["prio"] >= ceiling and not args.no_blocking) else 0.0

# ---- utilization tests ------------------------------------------------------
n = len(tasks)
U = sum(t["U"] for t in tasks)
ll = n * (2 ** (1 / n) - 1)
hyp = math.prod(t["U"] + 1 for t in tasks)
print(f"Task set: {os.path.relpath(args.taskset)}"
      + (f"  (changed: {', '.join(args.set + ['+' + a for a in args.add])})" if args.set or args.add else ""))
print()
print(f"{'task':12} {'prio':>4} {'C_i us':>10} {'T_i us':>10} {'D_i us':>10} {'U_i':>9}")
for t in tasks:
    print(f"{t['name']:12} {t['prio']:4.0f} {t['C']:10.2f} {t['T_us']:10.0f} {t['D_us']:10.0f} {t['U']:9.5f}")
print()
print(f"U = {U:.5f}  ({U * 100:.3f} % of the CPU)")
print(f"Liu & Layland, n = {n}: U <= {ll:.4f}  -> {'pass' if U <= ll else 'inconclusive (sufficient test only)'}")
print(f"Hyperbolic: prod(U_i + 1) = {hyp:.5f} <= 2  -> {'pass' if hyp <= 2 else 'inconclusive (sufficient test only)'}")
print(f"EDF (D >= T): U <= 1  -> {'pass' if U <= 1 else 'FAIL'}")

# ---- response-time analysis -------------------------------------------------
print()
print(f"RTA, blocking {'off' if args.no_blocking else 'on'} "
      f"(PI lock users: {', '.join(t['name'] for t in users) or 'none'}; ceiling prio {ceiling:.0f})")
print(f"{'task':12} {'B_i us':>9} {'S_i us':>10} {'R_i us':>12} {'D_i us':>10}  verdict   iterations")
worst = True
for i, t in enumerate(tasks):
    hp = tasks[:i]
    base = t["S_us"] + t["C"] + t["B"]
    R, seen = base, [base]
    while True:
        nxt = base + sum(math.ceil(R / h["T_us"]) * h["C"] for h in hp)
        if nxt == R or nxt > t["D_us"]:
            R = nxt
            break
        R = nxt
        seen.append(R)
    ok = R <= t["D_us"]
    worst &= ok
    trail = " -> ".join(f"{x:.2f}" for x in seen[:4]) + (" ..." if len(seen) > 4 else "")
    print(f"{t['name']:12} {t['B']:9.2f} {t['S_us']:10.0f} {R:12.2f} {t['D_us']:10.0f}  "
          f"{'R <= D' if ok else 'MISSES':8}  {trail}")
print()
print("Every task meets its deadline." if worst else "At least one task can miss its deadline.")
