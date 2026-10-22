# firmware/fig413: week 6 Task A

Buttazzo's Fig. 4.13 on the C6: τ1 (C = 2 ms, T = 5 ms) and τ2 (C = 4 ms,
T = 7 ms), U = 0.971, synchronous release at t = 0. The computation is a loop
calibrated at boot (it counts work, so a preempted job resumes where it
stopped; `k_busy_wait()` would count wall time). A late job keeps running and
the releases stay on the absolute grid.

| Build | Priorities | Expected (book) |
|---|---|---|
| default | RM: τ1 = 1, τ2 = 2 | τ2 misses at t = 7 ms, once per 35 ms hyperperiod |
| `edf.conf` | both 1, `CONFIG_SCHED_DEADLINE=y`, absolute deadline set before each sleep | no misses, U ≤ 1 |

Analyzer: D0 = GPIO3 `tau1_run`, D1 = GPIO4 `tau2_run`, D2 = GPIO5 `tau1_miss`,
D3 = GPIO6 `tau2_miss`. A run pin stays high while its job is preempted; a miss
pin goes high at the missed deadline and drops when the late job ends. The
console (CH343 jack, 115200) prints jobs, misses, worst R and worst lateness
every 5 s. `i2c0` is disabled so GPIO6 is free.

```bash
cd ~/zephyrproject && source .venv/bin/activate
west build -p -b esp32c6_devkitc/esp32c6/hpcore --build-dir build-lab06-rm ~/ret-equipo/firmware/fig413
west build -p -b esp32c6_devkitc/esp32c6/hpcore --build-dir build-lab06-edf ~/ret-equipo/firmware/fig413 -- -DEXTRA_CONF_FILE=edf.conf
west flash --build-dir build-lab06-rm      # or build-lab06-edf
```

Measured on 2026-10-21 (`evidencia/lab06/taskA-*-console.txt`), against
`evidencia/lab06/fig413_sim.py`:

| 60 s | τ2 misses | τ1 R max | τ2 R max |
|---|---|---|---|
| RM, book (C1 = 2.000 ms) | 1715 | 2.000 ms | 8.000 ms |
| RM, simulated with the measured C1 = 2.027 ms | 6857 | 2.027 ms | 8.135 ms |
| RM, C6 | **6860** of 8575 | 2.027 ms | 8.167 ms |
| EDF, C6 | **0** of 8575 | 4.250 ms | 6.159 ms |

Under RM two of τ2's five jobs per hyperperiod finish with exactly zero slack
in the book (t = 14 and t = 28 ms). The 27 µs the kernel adds to every τ1 job
(wake-up, GPIO, timer) turns both into misses, and one more follows from the
backlog: 4 misses per 35 ms instead of 1.

Under EDF, at t = 30 ms both jobs are due at 35 and Zephyr does not preempt on
a tie: τ2 keeps the CPU and τ1 waits, which is where τ1's 4.25 ms comes from.
The simulator applies the same rule. The analyzer captures, per-job tables and
figures are in `evidencia/lab06/` (`vcd_fig413.py`) and the RET, §3 week 6.

Found on the way: `k_sleep(K_TIMEOUT_ABS_MS(t))` with `t` already past still
waits for the next tick. A late job that sleeps anyway leaves up to 1 ms idle
and makes the next job late too; before the check in `task_thread()`, τ2
missed 717 of 717.
