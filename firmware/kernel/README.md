# firmware/kernel: the node after week 4

Our migration of the superloop to kernel threads (lab 4, Task A). It starts from
the week-3 sampling-thread build and keeps every task body and every
instrumentation GPIO unchanged, so the A/B against the superloop compares
architectures and nothing else. Plan, rationale and measurements are in
`ret.md` (§2 ADR-001, §3 week 4); the capture plan in
[`../../evidencia/lab04/README.md`](../../evidencia/lab04/README.md).

| Task | Runs in | Priority | Woken by |
|---|---|---|---|
| Tick ISR | `tick_isr` posts the release time to `tick_q` (`k_msgq`) | ISR | 1 kHz `k_timer` |
| Sampling | `sampling_thread` | 2 | `tick_q` |
| Control | `control_thread` | 3 | `control_sem` (`k_sem`, limit 1), given every 10th sample |
| Flow batch | `flow_work` on its own `flow_wq` | 5 | flow ISR, at 100 pulses |
| Display | `display_thread` | 7 | 50 ms sleep |
| Telemetry | `telemetry_thread` | 8 | absolute 1 s sleep |
| Console | `console_thread` | 9 | 5 ms sleep between polls |
| `main` | bring-up, starts the timer, returns | 0 | |

`status` adds `lat_peak_us` (release to sampling-thread start), `ticks_dropped`
(releases lost to a full `tick_q`) and `ctrl_missed` (control releases that found
the previous one still pending: a REQ-CTRL-01 deadline miss). `threads` prints
each thread's stack high-water mark.

The C6 pin map is `boards/esp32c6_devkitc_hpcore.overlay`, identical to week 3's
(`evidencia/lab03/c6/`). The board's own `i2c0` is enabled on GPIO6/GPIO7, the
same pins as `instr_tele` and `instr_flow`; the instrumentation works because
`init_hw()` reconfigures them as GPIOs after the I2C driver, but the HMI cannot
be added on the C6 without moving one of the two.

```bash
cd ~/zephyrproject && source .venv/bin/activate
west build -p -b esp32c6_devkitc/esp32c6/hpcore --build-dir build-lab04-c6-kernel ~/ret-equipo/firmware/kernel
west flash --build-dir build-lab04-c6-kernel

# without a board: every thread blocks, so simulated time advances
west build -p -b native_sim/native/64 --build-dir build-lab04-native ~/ret-equipo/firmware/kernel -- -DCONFIG_THREAD_ANALYZER=n
./build-lab04-native/zephyr/zephyr.exe -uart_stdinout
```

## Week 6

Same thread plan; three additions (RET §2 ADR-002 and ADR-005, §3 week 6):

- **The PI lock** (Task B). Control's PI state (`setpoint_mv`, `integral_mv`)
  is taken under one lock by control and by the console's `set` and `clear`.
  `lock sem|mutex` picks its kind at run time (default: the `k_mutex`).
  `invert` forces the inversion five times and reports control's worst R, the
  longest hold and the misses. `instr_lock` (GPIO18) is high while the console
  holds the lock. `status` adds `ctrl_r_peak_us` (release tick to control's end)
  and the lock kind.
- **The e-stop mark** (Task C). `instr_flow` follows `estop` from the sampling
  job that detects it, so the flow input has to stay unplugged.
- **ADR-005** (Task C). `CONFIG_LAB_ADR005` (`adr005.conf`): `calib` sleeps
  (`k_usleep(400)`) and the detecting sampling job sets the duty to 0. With the
  C6's 1 kHz tick each sleep lasts 2 ms and `calib` 2 s, so the final build also
  sets a 10 kHz tick. `calib` prints its duration and the console thread's CPU.

```bash
APP=~/ret-equipo/firmware/kernel; B=esp32c6_devkitc/esp32c6/hpcore
west build -p -b $B --build-dir build-lab06-kernel $APP                  # before
west build -p -b $B --build-dir build-lab06-adr005 $APP -- -DEXTRA_CONF_FILE=adr005.conf
west build -p -b $B --build-dir build-lab06-adr005-10k $APP -- \
    -DEXTRA_CONF_FILE=adr005.conf -DCONFIG_SYS_CLOCK_TICKS_PER_SEC=10000  # final

# native_sim ticks at 100 Hz: sampling would run every 10 ms and `invert` could
# never line up with control. Give it the 10 kHz tick as well.
west build -p -b native_sim/native/64 --build-dir build-lab06-native $APP -- \
    -DCONFIG_THREAD_ANALYZER=n -DCONFIG_SYS_CLOCK_TICKS_PER_SEC=10000
```
