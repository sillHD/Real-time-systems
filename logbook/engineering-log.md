# Engineering Logbook

Running record of what we did and how we did it. One entry per lab session,
newest at the bottom.

This is **not** the RET. The RET holds timing requirements and measured
evidence, and it is what gets graded. This logbook holds the working detail:
setup steps, dead ends, error messages and the reasoning behind our choices,
so that any of us can reproduce a result months later without deriving it
again from scratch.

<details>
<summary><b>How to write an entry</b></summary>

Each entry keeps the same shape. Goal, Result and Open items stay visible so
the file can be scanned; the long sections are collapsed.

| Field | Meaning | Collapsed |
|---|---|---|
| Goal | What the session was supposed to achieve | no |
| Setup | Hardware and toolchain actually used | yes |
| What we did | The steps that worked, as commands | yes |
| Problems | What broke, the real cause, and the fix | yes, one block per problem |
| Result | What we can demonstrate at the end | no |
| Open items | What is still missing | no |

Collapsible blocks are plain HTML, which GitHub renders as an accordion:

```markdown
<details>
<summary>Title</summary>

Content.

</details>
```

The blank line after `</summary>` is mandatory. Without it the markdown inside
is rendered as literal text.

</details>

---

## Entry 01: Week 1 bring-up

**Date:** 2026-09-03 · **Lab:** `labs/lab01_bringup.md` · **Author:** Joshua Marín Castrillón
· **Board:** ESP32-C6-DevKitC

### Goal

Get a working build, flash and monitor cycle on a physical board and on
`native_sim`, and create the team RET from the course template. No timing
measurements this week: the deliverable is the environment itself.

<details>
<summary><b>Setup</b></summary>

| Item | Value |
|---|---|
| Host | Windows 11 + WSL2, Ubuntu-24.04 |
| RTOS | Zephyr 4.4.0, west 1.5.0, Zephyr SDK 1.0.1 |
| Board | ESP32-C6-DevKitC |
| Board target | `esp32c6_devkitc/esp32c6/hpcore` |
| Architecture | RISC-V, toolchain `riscv64-zephyr-elf` |
| Serial bridge | CH343 (`1a86:55d3`), enumerates as `/dev/ttyACM0` |
| Console | 115200 8N1 |

**Deviation from the lab specification.** The lab asks for the STM32C0116-DK,
a Cortex-M0+ that the course lends out for weeks 1 and 2 and that serves as a
clean cacheless baseline. We did not have it available, so this session ran on
the ESP32-C6 we already own. The build, flash and monitor workflow is identical
except for the board target; only the LED sample had to change, for the reason
described below. The C0116-DK run is still pending.

</details>

<details>
<summary><b>What we did</b></summary>

Every command runs from inside the west workspace, with the virtualenv active:

```bash
source ~/zephyrproject/.venv/bin/activate
cd ~/zephyrproject
```

**1. Expose the board to WSL.** WSL does not see USB devices by default. The
`usbipd-win` tool forwards them from Windows:

```powershell
usbipd bind --busid 2-1       # once, needs administrator
usbipd attach --wsl --busid 2-1
```

**2. Blink an LED.**

```bash
west build -p always -b esp32c6_devkitc/esp32c6/hpcore \
  zephyr/samples/drivers/led/led_strip \
  -- -DCONFIG_SAMPLE_LED_UPDATE_DELAY=500 -DCONFIG_SAMPLE_LED_BRIGHTNESS=64
west flash
```

**3. Serial console.**

```bash
west build -p always -b esp32c6_devkitc/esp32c6/hpcore zephyr/samples/hello_world
west flash
west espressif monitor -p /dev/ttyACM0     # exit with Ctrl+]
```

**4. Close the iteration cycle.** We copied `hello_world` out of the Zephyr
tree so the source stays ours, edited the `printf`, and rebuilt without
`-p always` so only the changed file recompiles.

**5. Native simulator.**

```bash
west build -p always -b native_sim ~/lab01/hello_lab01 --build-dir /tmp/nsim
timeout 5 /tmp/nsim/zephyr/zephyr.exe
```

</details>

<details>
<summary><b>Problems</b> (4)</summary>

<details>
<summary><code>samples/basic/blinky</code> does not build for this board</summary>

```
main.c:15:19: error: '__device_dts_ord_DT_N_ALIAS_led0...' undeclared
#define LED0_NODE DT_ALIAS(led0)
```

The cause is in the devicetree, not in the sample. `esp32c6_devkitc` declares
only two aliases, `sw0` and `watchdog0`. There is no `led0` because the board
has no plain GPIO LED: its only onboard LED is an addressable WS2812 on GPIO8.

Two ways out, both verified to compile:

1. Supply our own overlay defining `led0` over an external LED, which keeps the
   canonical blinky sample:

   ```dts
   #include <zephyr/dt-bindings/gpio/gpio.h>

   / {
       leds {
           compatible = "gpio-leds";
           led_ext: led_ext {
               gpios = <&gpio0 2 GPIO_ACTIVE_HIGH>;
           };
       };
       aliases { led0 = &led_ext; };
   };
   ```

2. Use `samples/drivers/led/led_strip`, which already ships an official
   `esp32c6_devkitc_hpcore.overlay`.

We took option 2 because it needs no extra hardware. Worth noting for later:
that overlay drives the WS2812 over **I2S with DMA**, not bit-banging. The
WS2812 protocol demands pulse widths accurate to hundreds of nanoseconds, which
no RTOS thread can guarantee, so the timing is delegated to a peripheral. This
is exactly the kind of decision the RET will later have to justify with numbers.

The sample defaults are unusable as evidence: with `chain-length = 1` and a
50 ms delay the LED cycles too fast to read, and brightness 16 of 255 barely
registers on camera. We raised them to 500 ms and 64.

</details>

<details>
<summary><code>west espressif monitor</code> fails with "could not find build configuration"</summary>

The command does not accept `--build-dir`. Its implementation calls
`find_build_dir(None, True)` with the argument hardcoded, so it only works when
west can guess the build directory on its own. We had set
`build.dir-fmt = ~/lab01/{app}`, and with three directories matching that
wildcard the guess became ambiguous and failed, even for `--help`.

Fix: drop the custom `build.dir-fmt` and keep builds in the default
`~/zephyrproject/build`. Then `flash` and `monitor` both work with no flags.

</details>

<details>
<summary><code>bind</code> persists, <code>attach</code> does not</summary>

`usbipd bind` writes a flag to the Windows registry and survives reboots.
`usbipd attach` opens a live TCP connection carrying the USB/IP protocol to the
`vhci-hcd` driver in the WSL kernel, so it dies when the board is unplugged,
when WSL shuts down, or on reboot. Running `usbipd attach --wsl --auto-attach`
in a window left open reconnects automatically.

Use the **CH343** UART bridge, not the ESP32 native USB (`303a:1001`). The
native interface disappears from the bus every time the chip resets, and the
chip resets in the middle of flashing, which drops the attach halfway through.

</details>

<details>
<summary>The <code>native_sim</code> binary never exits</summary>

It sits in the RTOS idle loop, so any script has to wrap it in `timeout`.

</details>

</details>

### Result

| Check | Status |
|---|---|
| LED blinking on the board | Done, `evidencia/lab01/led.mp4` |
| `hello_world` on hardware, serial console | Done, `evidencia/lab01/hello-world.jpeg` |
| Modified message, full rebuild cycle | Done, `evidencia/lab01/mensaje-propio.jpeg` |
| `hello_world` on `native_sim` | Done, `evidencia/lab01/native-sim.png` |
| Team RET under version control | Done, this repository |

### Open items

- Repeat the LED and console steps on the STM32C0116-DK once the course lends
  it out, and record that run here.
- Add the second team member to the RET header and as a repository
  collaborator.
- The ESP32-S3 is needed from week 3 on. The C6 is not a substitute: it changes
  architecture and toolchain, and it breaks the week 9 AMP lab.
- The 8-channel logic analyzer is needed by week 2. It is the instrument every
  later timing measurement depends on.

---

## Entry 02: Week 2 superloop, run and read

**Date:** 2026-09-10 · **Lab:** `labs/lab02_superloop.md` · **Author:** Joshua Marín Castrillón
· **Board:** NUCLEO-L476RG · **Team:** Joshua Marín Castrillón, Ismael Cortés Ramírez

### Goal

Complete the three marked pieces of the provided superloop, run it on hardware,
and fill every row of the week-2 evidence table with measured numbers: the flow
diagram, the EARS requirements in RET §1, and the latency and jitter table in
RET §3.

<details>
<summary><b>Setup</b></summary>

| Item | Value |
|---|---|
| Host | Windows 11 + WSL2, Ubuntu-24.04 |
| RTOS | Zephyr 4.4.0 (tree at v4.4.0-13771-gb9df9f46ae46), west 1.5.0, SDK 1.0.1 |
| Board | NUCLEO-L476RG (STM32L476RG, Cortex-M4F at 80 MHz) |
| Board target | `nucleo_l476rg` (`nucleo_l476rg/stm32l476xx`) |
| Architecture | ARM Cortex-M4F, toolchain `arm-zephyr-eabi` 14.3.0 |
| Debug probe | ST-LINK/V2-1, firmware V2J46M31, mass storage volume `NODE_L476RG` |
| Console | ST-LINK VCP (usart2), 115200 8N1, COM16 on this host |
| Footprint | FLASH 25 492 B / 1 MB (2.43 %), RAM 3 840 B / 96 KB (3.91 %) |
| Analyzer | Saleae Logic clone, `0925:3881`, Cypress FX2, 8 ch |
| Analyzer software | PulseView (sigrok), driver `fx2lafw` over WinUSB |
| Capture settings | 8 channels, 4 MHz (250 ns resolution), 200 M samples = 49.999 s |

Ismael Cortés Ramírez joined the team, which
closes the open item carried from entry 01.

</details>

<details>
<summary><b>What we did</b></summary>

**1. The three missing pieces**, all in `firmware/superloop/src/main.c`. The
diff is kept as `evidencia/lab02/lab02-tasks123.patch`.

- `instr_set()` (line 74): the GPIO toggle behind every instrumentation pin.
  Without it the analyzer sees six flat lines.
- `tick_isr()` (line 88): the 1 kHz timer ISR does not sample. It increments
  `ticks_pending` and keeps the worst pile-up in `backlog_peak`.
- `main()` (line 497): the `while (1)` itself, visiting the six tasks in a fixed
  order and draining exactly one pending tick per pass.

Before writing the loop, the build states its own job description: the six task
functions all warn as *defined but not used*, because the one thing whose job is
to call them is what is missing.

**2. Build and flash.**

```bash
source ~/zephyrproject/.venv/bin/activate
cd ~/zephyrproject
west build -p -b nucleo_l476rg ~/4101137-real-time-systems/firmware/superloop
```

```powershell
Copy-Item \\wsl.localhost\Ubuntu-24.04\home\joshua\zephyrproject\build\zephyr\zephyr.bin E:\
```

**3. Three captures, each isolating one variable.**

| Capture | File | What it adds |
|---|---|---|
| Baseline, node idle | `baseline-50s-4MHz.vcd` | rows 1 and 2, all `C_i` |
| Jumper D13→D9 plus `calib` | `calib-flow-50s-4MHz.vcd` | rows 3 and 5, the ISR cost |
| `nocache.conf`, jumper removed | `nocache-50s-4MHz.vcd` | row 4 |

Row 6 came from the console (`status` before and after `calib`).

**4. Reduction.** VCD was chosen over CSV because it records only transitions:
50 s at 4 MHz is 200 M samples but only ~110 k edges, so a 1.7 MB file holds
every edge at full resolution. The scripts in `evidencia/lab02/` recompute every
number in the RET from those files, so a claim can be re-derived rather than
trusted:

| Script | Produces |
|---|---|
| `vcd_stats.py` | three-regime split, `C_i`, utilisation |
| `vcd_calib.py` | flow latency, `calib` excursion |
| `vcd_isr_cost.py` | the controlled ISR comparison |
| `vcd_cache_compare.py` | cache on vs off |

**5. A stimulus for D9 without buying anything.** Row 3 needs flow pulses. A
jumper from **D13 (PA5, the valve output) to D9 (PC7, the flow input)** makes the
node's own valve drive its own flow sensor. The valve runs a software PWM at
1 kHz / 20 = 50 Hz, which gives 50 pulses per second and a flow batch every 2 s.
It is also physically coherent: flow exists because the valve is open. The
limitation is recorded in the RET, since a self-generated stimulus is
phase-locked to the loop and a real sensor would not be.

</details>

<details>
<summary><b>Channel map, verified by signature</b></summary>

Three numbering schemes for the same seven wires: the analyzer's pins are
`CH1..CH8`, PulseView names its inputs `D0..D7`, and the Nucleo's headers are
`D3..D9`. The map was confirmed from the signals rather than the labels, because
each task has an unmistakable signature.

| PulseView | Nucleo | Port | Signal | Signature |
|---|---|---|---|---|
| D0 | D3 | PB3 | `instr_samp` | dense 1 kHz train |
| D1 | D4 | PB5 | `instr_ctrl` | one pulse per ten of them |
| D2 | D5 | PB4 | `instr_cons` | flat, nothing typed |
| D3 | D6 | PB10 | `instr_tele` | one wide pulse per second |
| D4 | D7 | PA8 | `instr_flow` | flat until the jumper, then one per 2 s |
| D5 | D8 | PA9 | `instr_disp` | flat, HMI not compiled in |
| D6 | D9 | PC7 | `flow_pulse` | flat until the jumper, then 50 Hz |

PulseView `Dn` maps to Nucleo `D(n+3)`. Channels being flat is part of the
confirmation, not a fault.

</details>

<details>
<summary><b>Problems</b> (5)</summary>

<details>
<summary>We were working against the wrong board, and the ST-LINK told us</summary>

We had assumed the board on the desk was the STM32C0116-DK the course lends for
weeks 1 and 2, and the whole first pass of documents was written for it.

When it was plugged in, the ST-LINK's mass storage volume came up as:

```
DriveLetter  FileSystemLabel
E            NODE_L476RG
```

That label is written by the ST-LINK firmware and is board-specific. Confirmed
by `DETAILS.TXT` (ST-LINK V2J46M31) and by the single ST-LINK on the bus
(`0483:374b`). The board is a NUCLEO-L476RG.

The pin map, the flash budget and the meaning of one whole table row all change
with the board. Checking the probe's volume label takes two seconds and settles
it.

The correction was entirely in our favour. On the C0116-DK the display build
overflows (`region FLASH overflowed by 12800 bytes`, verified) and row 4
degenerates into a prefetch measurement, because `CONFIG_LAB_FLASH_CACHE_OFF`
declares `depends on SOC_SERIES_STM32L4X` and Kconfig drops it silently on a C0.
On the L476 the fragment does what the lab says. The six instrumentation pins
are also the unbroken Arduino run D3 to D8 here, instead of six scattered pins.

**This is worth passing to any pair measuring on a C0116-DK: their row 4 will be
a flash prefetch measurement, not a cache one, and nothing warns them.**

</details>

<details>
<summary>The SDK had no ARM toolchain, so no STM32 board could build at all</summary>

```
$ ls ~/zephyr-sdk-1.0.1/gnu
riscv64-zephyr-elf
```

Entry 01 ran on the ESP32-C6, which is RISC-V, so only that toolchain was ever
installed.

```bash
cd ~/zephyr-sdk-1.0.1 && ./setup.sh -t arm-zephyr-eabi
```

Before week 3: the ESP32-S3 is Xtensa, a third architecture, and needs
`xtensa-espressif_esp32s3_zephyr-elf` installed the same way. Doing it on lab day
costs the download.

</details>

<details>
<summary><code>west flash</code> has no runner on this host, and does not need one</summary>

`boards/st/nucleo_l476rg` offers the `openocd`, `stm32cubeprogrammer`, `jlink`
and `pyocd` runners. None of the underlying tools is installed here, and the
Zephyr SDK ships no OpenOCD in this install.

No tool is needed. The ST-LINK/V2-1 exposes a mass storage volume, so copying
`zephyr.bin` onto it programs the part. The file disappearing from the volume is
the success signal; a `FAIL.TXT` appearing is the error report. Neither `usbipd`
forwarding into WSL nor any driver install is involved, because the copy happens
from Windows.

</details>

<details>
<summary>One mini-USB cable, two devices that need one</summary>

The NUCLEO-L476RG and the logic analyzer both take mini-USB. Resolved by finding
a second cable, but it stops a session dead: the analyzer needs USB for power and
for streaming samples, and the board needs 5 V to run.

The way around, had no cable turned up: once programmed, the board does not need
the host, so any 5 V source on the CN6 pins frees the cable for the analyzer.
That covers a baseline capture but not Task C, where `calib` has to be typed on
the console while the capture runs.

</details>

<details>
<summary>The analyzer needs a driver swap before any software sees it</summary>

Plugged in, the clone enumerates as `0925:3881` and Windows reports it with
status `Error`: there is no in-box driver.

PulseView's installer bundles Zadig for this. With `Options → List All Devices`,
select the entry whose USB ID is exactly `0925 3881` and install **WinUSB**.
Afterwards:

```
Name: fx2lafw   Status: OK   Service: WinUSB
```

Care is required. With `List All Devices` ticked, Zadig lists every USB device on
the machine, including the keyboard, the mouse and the ST-LINK (`0483:374b`).
Replacing the driver of the wrong one breaks that device. The USB ID is the only
safe way to identify the target.

In PulseView, the driver to pick in step 1 of *Connect to Device* is
`fx2lafw (generic driver for FX2 based LAs)`, not the alphabetically first entry
the dialog opens on.

</details>

</details>

### Result

Every row of the week-2 table is measured. Full analysis in `ret.md` §3.

| # | Measurement | Value |
|---|---|---|
| 1 | Mean sampling period | 999.02 µs |
| 2 | Sampling jitter, max over 50 s | 6753.25 µs (10.75 µs in the clean regime) |
| 3 | Flow ISR to loop service | 9.67 µs (deterministic stimulus) |
| 4 | Sampling jitter, cache off | 119.25 µs clean regime, 11× row 2 |
| 5 | Sampling jitter during `calib` | 416.11 ms |
| 6 | `backlog_peak`, idle → `calib` | 6 → 416 ticks |

Measured `C_i`: sampling 4.95 µs, control 1.35 µs, telemetry 5980.36 µs, flow
batch 70.62 µs, flow ISR 11.50 µs. **Total utilisation 1.169 %.**

Four results worth carrying forward:

1. **The idle `backlog_peak` of 6 is the telemetry line.** 69 characters at
   115200 8N1 is 5.99 ms of polled-UART blocking; the analyzer measures the pulse
   at 5980.36 µs and counts exactly 6 catch-up periods after each one. The
   firmware's counter, the baud-rate arithmetic and the trace agree to better
   than 0.2 %. A soft task is eating a hard task's deadline at idle.
2. **`calib` is 416.11 ms of silence**, matching `backlog_peak = 416` and the
   hand calculation (1000 × 400 µs of `k_busy_wait` plus 1000 ADC reads at
   ~16 µs). The 416 queued samples are then taken inside 5.61 ms.
3. **One GPIO interrupt costs 11.50 µs and the control loop has 13.00 µs of
   margin.** Established by classifying control periods by whether a flow
   interrupt fired inside them, within the same capture: 0.0 % miss the deadline
   without one, 29.0 % with one. Connecting a sensor, changing no code, breaks
   REQ-CTRL-01.
4. **The ART accelerator was holding up REQ-CTRL-03 unrecorded.** Disabling it
   costs +47 % on `task_sampling` and +85.8 % on `task_control`, and pushes the
   clean-regime jitter from 10.75 µs to 119.25 µs, past the 100 µs budget. The
   telemetry task barely moves (+1.1 % once the data confound is removed),
   because its time is spent waiting on a UART, not executing.

| Check | Status |
|---|---|
| TASK 1, 2 and 3 completed | Done |
| Flow diagram, ten boxes, GPIOs marked | Done |
| RET §1: eight EARS requirements | Done, each with the origin of its number |
| RET §1: measured `C_i` for every task | Done |
| RET §3: rows 1 to 6 | **All measured** |
| Raw captures and reduction scripts committed | Done |

### Open items

- **Row 3 deserves a redo with an asynchronous source.** The jumper stimulus is
  phase-locked to the loop, so 9.67 µs is the deterministic case and not a worst
  case. A free-running square wave from a spare board would bound it properly.
- **`flow_isr` has no instrumentation GPIO**, so its 11.50 µs is inferred from
  the controlled comparison rather than measured directly. Adding a toggle there
  would confirm it.
- **`C_i` of `task_telemetry` is data-dependent** (5980 µs vs 6217 µs for the
  same code, because the printed values got longer). A WCET for it has to be
  bounded from the longest line the format string can produce, not from a
  typical run. Carry this into module 3.
- Optional extra, not part of the baseline: build with `display.conf` (47 372 B,
  fits here) and capture `instr_disp` next to `instr_samp`. It would add a third
  kind of blocker to the collection: UART-bound (telemetry), CPU-bound (`calib`)
  and I²C-bound (the 1 KB frame).
- REQ-CTRL-02 has no direct measurement: it is bounded by the control period
  (worst case 14.77 ms against a 5 ms deadline). Triggering a real overpressure
  and timing detection to actuation would measure it end to end.
- Add Ismael as a collaborator on this repository.
- Install the Xtensa toolchain before week 3, and buy the ESP32-S3.

---

## Entry 03: Week 3 the S3 port and the first thread

**Date:** 2026-09-23 · **Lab:** `labs/lab03_port.md` · **Author:** Joshua Marín Castrillón
· **Board:** none yet (ESP32-S3-DevKitC not delivered) · **Team:** Joshua Marín Castrillón, Ismael Cortés Ramírez

### Goal

Write the ESP32-S3 pin map as a devicetree overlay and move the week-2 superloop
onto the new silicon without touching a line of C (Task A), then lift sampling
out of the loop into a preemptive thread fed by a message queue (Task C). The
board has not arrived, so the target for this session was everything that can be
finished and verified without it: both firmwares written, building clean, and a
measurement protocol ready to run the day it lands.

<details>
<summary><b>Setup</b></summary>

- WSL2 `Ubuntu-24.04`, Zephyr v4.4.0 in `~/zephyrproject`, venv at
  `~/zephyrproject/.venv`.
- Course repo `~/4101137-real-time-systems` pulled to `2c69dd3` (which is what
  brought `firmware/sampling_thread/`, the week-3 reference).
- **New toolchain:** `xtensa-espressif_esp32s3_zephyr-elf`. The SDK only had
  `riscv64-zephyr-elf` (C6) and `arm-zephyr-eabi` (Nucleo). Installed with
  `cd ~/zephyr-sdk-1.0.1 && ./setup.sh -t xtensa-espressif_esp32s3_zephyr-elf`.
  This is the third architecture the course has needed.
- No board attached, so verification is: clean build, devicetree dump, and a
  `native_sim` run that proves the image boots and the thread does not fault.

</details>

<details>
<summary><b>What we did</b></summary>

Task A, the overlay, written from the pin table in
`firmware/superloop/README.md` rather than copied from the reference:

```bash
$EDITOR ~/4101137-real-time-systems/firmware/superloop/boards/esp32s3_devkitc_esp32s3_procpu.overlay
cd ~/zephyrproject && source .venv/bin/activate
west build -p -b esp32s3_devkitc/esp32s3/procpu ~/4101137-real-time-systems/firmware/superloop
west build -p -b esp32s3_devkitc/esp32s3/procpu ~/4101137-real-time-systems/firmware/superloop \
    -- -DEXTRA_CONF_FILE=display.conf          # the HMI path, to exercise i2c0
```

The check that the overlay is actually in the build, which matters because the
guide warns the build succeeds without it:

```bash
grep -A1 'instr_samp:\|valve_out:\|flow_pulse:' build/zephyr/zephyr.dts
```

All eight nodes resolve onto `&gpio0` at the intended pins, each line annotated
by dtc with the overlay path it came from.

Task C applied as four edits to `main.c` and `prj.conf` (the STEPs exactly as the
guide writes them), built, then extracted as a patch and reverted, so the tree
stays on the superloop that column 2 of the table needs first:

```bash
west build -p -b esp32s3_devkitc/esp32s3/procpu ~/4101137-real-time-systems/firmware/superloop
west build -p -b native_sim ~/4101137-real-time-systems/firmware/superloop
timeout 5 ./build/zephyr/zephyr.exe      # boots, banner prints, no fault
diff -ruN pa pb > ~/ret-equipo/evidencia/lab03/lab03-task-c.patch
git apply --check ~/ret-equipo/evidencia/lab03/lab03-task-c.patch
```

Evidence written to `evidencia/lab03/`: the overlay, `port-diffstat.txt`, the
Task C patch, and a README with the wiring and capture protocol.

</details>

<details>
<summary><b>Problem 1: the reference overlay puts i2c0 on pins Zephyr does not use</b></summary>

The firmware README and the professor's reference overlay both document the HMI
on **SDA GPIO8 / SCL GPIO9**, and the reference enables the bus with
`status = "okay"` and nothing else. Those are the ESP-IDF and Arduino defaults,
not Zephyr's. The board file
`zephyr/boards/espressif/esp32s3_devkitc/esp32s3_devkitc-pinctrl.dtsi` defines:

```dts
i2c0_default: i2c0_default {
        group1 {
                pinmux = <I2C0_SDA_GPIO1>,
                         <I2C0_SCL_GPIO2>;
```

So enabling `i2c0` as the reference does lands the display on GPIO1/GPIO2, and
**GPIO2 is the ADC1 channel `pot_esp32.overlay` claims for pressure**. Both
would be driven at once as soon as the HMI and the pot are used together.

Fix: declare our own pinctrl node in the overlay and point the bus at it.

```dts
&pinctrl {
        i2c0_hmi: i2c0_hmi {
                group1 {
                        pinmux = <I2C0_SDA_GPIO8>, <I2C0_SCL_GPIO9>;
                        bias-pull-up;
                        drive-open-drain;
                        output-high;
                };
        };
};

&i2c0 {
        status = "okay";
        pinctrl-0 = <&i2c0_hmi>;
        pinctrl-names = "default";
        clock-frequency = <I2C_BITRATE_FAST>;
```

Needs `#include <zephyr/dt-bindings/pinctrl/esp32s3-pinctrl.h>`, which the
reference does not include because it never names a pin. Worth confirming with
the analyzer on GPIO8/GPIO9 once the board is here.

</details>

<details>
<summary><b>Problem 2: the SDK had no Xtensa toolchain, and the failure would have looked like a board problem</b></summary>

`~/zephyr-sdk-1.0.1` only carried `riscv64-zephyr-elf` and `arm-zephyr-eabi`.
The S3 is Xtensa, a third architecture, and without it the build fails at
toolchain selection rather than at anything to do with the port. Installed in
one command and it took a few minutes:

```bash
cd ~/zephyr-sdk-1.0.1 && ./setup.sh -t xtensa-espressif_esp32s3_zephyr-elf
```

This was the last open item of entry 02 and is now closed.

</details>

<details>
<summary><b>Problem 3: `backlog_peak` is not the same quantity in the two firmwares</b></summary>

The table asks for `backlog_peak` under `calib` in both S3 columns, which invites
reading them as one series. They are not. In the superloop it is a free-running
counter of ticks that piled up, and week 2 measured 416 of them. In the thread
build it is `k_msgq_num_used_get()` on a queue declared 8 deep, posted to with
`K_NO_WAIT`, so it cannot exceed 8 and anything beyond that is a **dropped**
release, not a late one.

Two consequences we have to respect when the numbers come in: column 3
saturating at 8 is not an improvement over 416 by a factor of 52, and
`lat_peak_us` is the only unbounded lateness figure in that column. The queue
depth is also a design parameter we chose by pasting the guide's code, so it
belongs in an ADR if the drop ever happens.

</details>

### Result

| | |
|---|---|
| Task A overlay written and building | Done, 86 lines, **0 lines of C** |
| Overlay verified present in the devicetree | Done (`port-diffstat.txt`) |
| HMI path (`display.conf`) builds on the S3 | Done, with the i2c0 pinctrl fix |
| Task C thread implemented and building clean | Done, as a reverted patch |
| Boots without faulting | Done, on `native_sim` |
| RET §3 week-3 section | Written, S3 cells left as `____` |
| Measurement protocol and wiring | Written, `evidencia/lab03/README.md` |

The claim the session exists to support already holds on the evidence we have:
moving silicon cost one 86-line hardware description and no source change.

The one quantity that can be measured without the board is what the thread costs
in memory. Same overlay, same tree, only the patch differs
(`evidencia/lab03/footprint.txt`):

| Region | superloop | + sampling thread | delta |
|---|---|---|---|
| FLASH | 135 524 B | 135 604 B | +80 B |
| `iram0_0_seg` | 38 020 B | 38 720 B | +700 B |
| `dram0_0_seg` | 36 104 B | 38 520 B | **+2416 B** |

The 2416 B of RAM is the price of the first thread: 1536 B of stack, 32 B for the
8-deep queue of release timestamps, and the remainder in the `k_thread` control
block, the three atomics and alignment. Worth carrying into module 3, where the
argument for threads has to be paid for in something.

### Open items

- **Everything that needs the board**: columns 2 and 3 of the table, the L476 vs
  S3 reading, and `C_i` re-measured on the S3 for RET §1. The ESP32-S3-DevKitC
  has not been bought yet and week 3 is the session that needs it.
- The jumper for self-stimulus moves to **GPIO21 to GPIO16** on this board. Same
  caveat as week 2: phase-locked to the loop, so it measures the deterministic
  case.
- Confirm the HMI really talks on GPIO8/GPIO9 with the analyzer, since that fix
  is reasoned from the board files and not yet observed.
- Still pending from entry 02: add Ismael as a collaborator here.

---

## Entry 04: Week 4 the full migration and the A/B (and week 3 closed on the C6)

**Date:** 2026-10-07 · **Lab:** `labs/lab04_ipc.md`, plus the missing cells of `labs/lab03_port.md` · **Author:** Joshua Marín Castrillón
· **Board:** ESP32-C6-DevKitC (`esp32c6_devkitc/esp32c6/hpcore`) · **Team:** Joshua Marín Castrillón, Ismael Cortés Ramírez

### Goal

Move every task of the node into its own thread or deferred work (Task A),
repeat the week-2 protocol on it to get the superloop-versus-kernel A/B with
maxima, not averages (Task B), measure what a context switch costs (Task C), and
measure every thread's stack high-water mark under load (Task D), because, in
Samuel's words, threads bring context switches but they also bring stack
overflows. Since the S3 never arrived and Ismael had ported week 3 to the C6
(PR #1), the same session also had to close week 3 on the C6, whose `calib`
captures were missing and whose A side the A/B depends on.

<details>
<summary><b>Setup</b></summary>

- WSL2 `Ubuntu-24.04`, Zephyr v4.4.0, venv at `~/zephyrproject/.venv`. Course
  repo pulled to `38959a8`, which brought the professor's week-4 reference
  (`firmware/kernel/`), used only as a cross-check after writing ours.
- ESP32-C6-DevKitC on its CH343 UART port, attached to WSL with
  `conectar-esp32.cmd` (`/dev/ttyACM0`). Flashing with `west flash --build-dir`.
- Analyzer `0925:3881` in PulseView, `fx2lafw`, 4 MHz, 200 M samples (50 s),
  D0..D6 on GPIO3, 4, 5, 6, 7, 10, 11. Self-stimulus jumper GPIO2 to GPIO11 for
  the flow and `calib` captures.
- Three firmwares built in advance so the session was only flash and capture:
  `build-lab03-c6-superloop` (byte for byte the size Ismael measured),
  `build-lab03-c6-thread` and `build-lab04-c6-kernel`, each with its own boot
  banner so the console proves which one is on the board.

</details>

<details>
<summary><b>What we did</b></summary>

**1. Task A, without the board.** `firmware/kernel/` in this repo, built from
the week-3 sampling-thread version. Six threads in rate-monotonic order
(sampling 2, control 3, own workqueue for the flow batch at 5, display 7,
telemetry 8, console 9), `main` back to priority 0, every thread blocking. One
choice of ours: `control_sem` has a limit of 1 and the firmware counts
`ctrl_missed` when a release finds the previous one still pending. Checked with
a C6 build (0 warnings, devicetree dump) and a `native_sim` run
(`evidencia/lab04/build-c6.txt`, `native-sim-session.txt`).

**2. Five captures in one sitting**, each with a console log that starts with the
banner:

| # | Firmware | Jumper | Closes |
|---|---|---|---|
| 1 | superloop | on, `calib` | week 3, and side A of the A/B |
| 2 | sampling thread | off | week 3 (replaces the invalid capture) |
| 3 | sampling thread | on, `calib` | week 3 |
| 4 | kernel | off | week 4, side B at idle |
| 5 | kernel | on, `calib`, then `threads` | week 4, side B, Tasks C and D |

```bash
cd ~/zephyrproject && source .venv/bin/activate
west flash --build-dir build-lab04-c6-kernel
python ~/ret-equipo/evidencia/lab04/capture_console.py \
    ~/ret-equipo/evidencia/lab04/console-session.txt calib-threads
# press RST, wait for "Jumper OK", Enter, then Run in PulseView
```

**3. Reduction** with the week-2 scripts plus two new ones
(`vcd_ctx_gap.py` for Task C, `vcd_release_lateness.py` for the idle jitter),
and figures with `vcd_plot.py`. Every number in RET §3 weeks 3 and 4 has its
output file beside the VCD it came from.

</details>

<details>
<summary><b>Task D: stack monitoring, and what it can and cannot say about overflows</b></summary>

**What we measured.** `CONFIG_THREAD_ANALYZER=y` plus a `threads` console
command (the node has no shell, the console owns the UART) that calls
`thread_analyzer_print(0)`. It was issued right after capture 5, with the flow
jumper on and one `calib` done, so the flow batch, telemetry, console and both
hard threads had all run their deepest paths at least once:

| Thread | Size | Where the size comes from | Used | % | Margin |
|---|---:|---|---:|---:|---:|
| `idle` | 256 B | course `prj.conf` (`CONFIG_IDLE_STACK_SIZE`) | 228 B | **89 %** | **×1.1** |
| `sysworkq` | 512 B | course `prj.conf` (`CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE`) | 272 B | 53 % | ×1.9 |
| ISR stack | 512 B | course `prj.conf` (`CONFIG_ISR_STACK_SIZE`) | 244 B | 47 % | ×2.1 |
| `console_tid` | 2048 B | lab 4 guide, Task A table | 836 B | 40 % | ×2.4 |
| `flow_wq` | 1024 B | lab 4 guide, Task A table | 268 B | 26 % | ×3.8 |
| `telemetry_tid` | 2048 B | lab 4 guide, Task A table | 516 B | 25 % | ×4.0 |
| `control_tid` | 1024 B | lab 4 guide, Task A table | 236 B | 23 % | ×4.3 |
| `sampling_tid` | 1536 B | lab 4 guide, Task A table | 324 B | 21 % | ×4.7 |
| `display_tid` | 2048 B | lab 4 guide, Task A table | 220 B | 10 % | ×9.3 |

Margin is size divided by the high-water mark. In our code the thread sizes are
the `#define ..._STACK` values in `firmware/kernel/src/main.c`, copied from the
guide; the other three are in `firmware/kernel/prj.conf`, inherited unchanged
from the course superloop.

**Why nothing came close: the sizes were not ours, and they are generous.** A
stack size is not a limit that protects anything. It is a fixed block of RAM
reserved when the thread is created, and a thread that needs more simply keeps
writing past its end into whatever memory follows; without a guard nothing
stops it. So the question is not whether a limit held, but whether each
reservation covers what the thread actually uses. Ours do, by a factor of 2.4
to 9.3, because our tasks are shallow: one task function, one driver call, the
kernel below it, and almost no large locals. The two deepest are exactly the
two that format text: telemetry (`char line[96]` plus what `snprintf` uses
inside) and the console (`printk`, and the analyzer printing from it). The
guide's sizes are a deliberately generous starting point, and Task D exists to
replace them with measured ones.

The one reservation nobody sized for a kernel is `idle`. Its 256 B made sense in
the superloop, where `main` never sleeps and idle never runs. Here it is the
thread that runs most and that every interrupt arriving at idle lands on, and it
has 28 B to spare. That is the real risk in this build, and why the first fix is
512 B.

The table also says how much RAM could be returned (the display would fit in
512 B), but shrinking stacks is only safe with overflow detection on, so that a
stack cut too short faults immediately instead of corrupting memory. It goes
after the guard build below, not before.

**Did anything overflow? Not in this run, and we can say why.** The analyzer
works because `CONFIG_INIT_STACKS=y` fills every stack with `0xaa` at creation;
"unused" is how many bytes at the far end of the stack still hold that pattern.
Every thread reports unused > 0, so no stack reached its end during the run.
The closest is `idle`, with 28 B left. It is not ours: its 256 B come from
`CONFIG_IDLE_STACK_SIZE=256` in the course `prj.conf`, sized for a superloop
where idle never ran. In the kernel it runs 97 % of the time, and every
interrupt that lands while the CPU is idle saves its register frame on idle's
stack. Ours peak at 40 %, and that peak is the console's, partly caused by the
analyzer itself, which prints from the console thread.

**What it cannot say.** Three limits worth writing down before anyone quotes
"no overflows" as a guarantee:

1. **A high-water mark only covers the paths that ran.** The HMI is not wired on
   this board (no display), the e-stop and joystick paths never fired, and no
   console command was mistyped. Any of those can go deeper.
2. **Nothing in this build would have caught an overflow.** The `.config` has
   `CONFIG_HW_STACK_PROTECTION` and `CONFIG_STACK_SENTINEL` both off. An
   overflow would not stop the node: it would silently overwrite whatever sits
   next to the stack, and the analyzer would only show it afterwards as 0 bytes
   unused, if the node survived long enough to answer `threads`.
3. **One sample, one run.** The marks are from one 50 s capture plus boot.

**What the C6 offers.** It has a RISC-V PMP (`CONFIG_RISCV_PMP=y`,
`CONFIG_ARCH_HAS_STACK_PROTECTION=y`), so hardware stack guards are available.
A build with `-DCONFIG_HW_STACK_PROTECTION=y -DCONFIG_IDLE_STACK_SIZE=512`
compiles clean, enables `CONFIG_PMP_STACK_GUARD=y`, and costs 6,464 B of RAM
(74,016 B against 67,552 B), mostly guard regions. With it, an overflow becomes
an immediate fault naming the thread, instead of silent corruption. It is in
`~/zephyrproject/build-lab04-c6-guard`, built but not flashed: the week-4
numbers were measured without it, so turning it on belongs to the next session,
with a re-measure.

</details>

<details>
<summary><b>Problem 1: Ismael's sampling-thread baseline had been taken with the superloop on the board</b></summary>

`thread-baseline-50s-4MHz.vcd` (2026-09-29) matched the superloop capture to the
quarter microsecond: min 891.75, mean 1000.14, max 1112.75 µs over 49,992
periods, including 26 samples held back exactly until telemetry ended (110 µs).
A priority-2 thread cannot wait for priority-10 telemetry on the C6:
`uart_esp32_poll_out` takes no lock and `CONFIG_PRINTK_SYNC` is off on a single
core. His `footprint.txt` also said the thread build was never flashed.

Re-captured with the `sampling-thread build` banner confirmed in the console:
**3.75 µs** of jitter and zero samples held back. The old file stays in the
folder as a record. Rule we keep from this: every capture gets a console log
that starts with the boot banner, which is what `capture_console.py` enforces.

</details>

<details>
<summary><b>Problem 2: the first capture was taken at 20 kHz, with every probe one channel off</b></summary>

Capture 1 came back as a 133 KB VCD whose header read
`Acquisition with 8/8 channels at 20 kHz`. At 20 kHz the analyzer samples every
50 µs and misses most of the 2.5 µs sampling pulses. PulseView had gone back to
its default rate. A valid 50 s capture at 4 MHz is about 1.8 MB, so the file
size alone gives it away.

Identifying each channel by its signature (1 kHz train, 1 in 10, 1 Hz, 0.5 Hz,
50 Hz) showed every probe one channel up: sampling on D1, flow input on D7, D0
floating high. All reduction scripts read sampling from D0. Fix: move every
probe down one channel, then check on a 1 s capture that D0 is the dense 1 kHz
train before the real one.

</details>

<details>
<summary><b>Problem 3: typing commands by hand while telemetry scrolls</b></summary>

With miniterm open, one telemetry line per second interleaves with the echo of
what is typed. Three of the first commands arrived wrong (`? (try: help)`), and
the timing of `calib` inside the 50 s window depended on switching focus from
PulseView to the terminal at the right second.

Fix: `evidencia/lab04/capture_console.py`. It waits for the banner after RST,
checks the jumper with two `status` (batches must go up), and after one Enter
sends `status` at +5 s, `calib` at +28 s, `status` after `calibration done`,
and optionally `threads` at the end. The commands now land at the same offsets
in every capture. Tested first against a fake node on a pseudo-terminal,
because `native_sim` sends printk to stdout, not to its UART pty.

</details>

<details>
<summary><b>Problem 4: the C6 thread patch does not apply with <code>patch</code></b></summary>

`evidencia/lab03/c6/lab03-task-c.patch` has a hunk header that counts 41 added
lines where the hunk has 38 (it was edited by hand to add `ticks_dropped`). GNU
`patch` and an out-of-repo `git apply` reject it as corrupt. It applies with:

```bash
git apply --recount -p3 ~/ret-equipo/evidencia/lab03/c6/lab03-task-c.patch
```

</details>

<details>
<summary><b>Problem 5: <code>vcd_stats.py</code> crashed on every C6 capture</b></summary>

It divided by the number of "blocked by telemetry" periods, which was 50 on the
L476 and is 0 on the C6, whose UART FIFO removed the block. Guarded, and its
output for the L476 captures is unchanged (checked with `diff` against
`baseline-stats.txt`). `vcd_calib.py` now takes the baseline to compare against
as an argument, and `vcd_plot.py` takes a board label, skips normal 1 ms
periods, and has a zoom mode for one control release; `fig-calib-416ms.svg`
regenerates byte for byte.

</details>

### Result

| | Superloop (C6) | Kernel (C6) |
|---|---:|---:|
| Worst control period with `calib` | 411.26 ms | **10.011 ms** |
| Worst sampling period with `calib` | 403.26 ms | **1.006 ms** |
| `backlog_peak` / `ctrl_missed` with `calib` | 403 / n/a | **0 / 0** |
| Sampling jitter at idle | 221.00 µs | **17.25 µs** |
| Context-switch gap (Task C) | 0.50 µs | **14.50 µs** |
| Tightest stack (Task D) | n/a | **`idle`, 228 / 256 B** |

ADR-001 (multithreaded kernel) is accepted on these numbers. Week 3 on the C6 is
closed too: with sampling in a thread, `calib` no longer touches sampling
(jitter 4.25 µs) but control still stops for 403.49 ms, which is the case for
moving every task.

### Open items

- **Turn stack overflow detection on**: flash `build-lab04-c6-guard`
  (`CONFIG_HW_STACK_PROTECTION=y`, `CONFIG_IDLE_STACK_SIZE=512`), re-run
  `threads` under load, and re-measure the A/B row for idle jitter to see what
  the PMP guards cost in time, not only in RAM.
- **Week 5 trace**: the kernel has 506 sampling releases per 50 s more than
  4 µs late at idle, all of them odd-numbered. None of the designed wake-ups has
  a 2 ms period on its own.
- **REQ-CTRL-02** (e-stop within 5 ms) still fails: the valve waits for the next
  control run, up to 10.011 ms.
- Our S3 overlay (`evidencia/lab03/s3/`) moved i2c0 to GPIO8/9; on 2026-09-30 the
  professor fixed the same clash the other way (i2c0 stays on GPIO1/2, the pot
  moves to GPIO8/9). If anyone uses the S3, the overlay has to follow his.
- On the C6, the board's `i2c0` sits on GPIO6/7, the pins of `instr_tele` and
  `instr_flow`. The HMI cannot be added without moving one of the two.

---

## Entry 05: Week 5 tracing (turned into a demo halfway)

**Date:** 2026-10-21 · **Lab:** `labs/lab05_tracing.md`, SOP-05 · **Author:** Joshua Marín Castrillón
· **Board:** ESP32-C6-DevKitC (`esp32c6_devkitc/esp32c6/hpcore`) · **Team:** Joshua Marín Castrillón, Ismael Cortés Ramírez

### Goal

Stream a CTF trace off the node and use it to tell the story of one
preemption, then price the tracer against the GPIO jitter. Halfway through the
session the professor turned week 5 into a demo and moved the class to week 6,
so Tasks A and B were done and Task C was not.

<details>
<summary><b>What we did</b></summary>

- Course repo pulled to `7d30d89`: the course merged from 16 to 14 weeks
  (`0474a3b`) and the trace moved to the UART jack at 4 Mbaud (`248c42f`).
- Build `build-lab05-trace` from `firmware/kernel` with the course's
  `trace.conf` and `uart_trace.overlay`, unchanged on the C6. Trace on the CH343
  jack (`/dev/ttyACM0`), console on the USB jack (`/dev/ttyACM1`).
- Two traces, 3.1 s each: idle and during `calib` (`evidencia/lab05/trace-*`),
  reduced with our `trace_stats.py` and drawn with `trace_figure.py`.

</details>

<details>
<summary><b>Problem 1: WSL shuts itself down between commands</b></summary>

`usbipd attach` failed with "There is no WSL 2 distribution running". Keeping
`wsl -d Ubuntu-24.04 --exec sleep infinity` in the background holds it up. Two
`attach --auto-attach` launched at once fight over port 0 of `vhci_hcd`: one
after the other.

</details>

<details>
<summary><b>Problem 2: usbipd loses bytes at 4 Mbaud</b></summary>

About one loss every 3 s. CTF has no framing, so the course's viewer stops at
the first one. `trace_stats.py` resynchronises on four valid records in a row
with growing timestamps.

</details>

<details>
<summary><b>Problem 3: the C6's native USB resets the chip</b></summary>

Dropping DTR before RTS on the USB jack resets the C6
(`rst:0x15 USB_UART_HPSYS`). The console has to be opened without touching
DTR/RTS, or a capture ends in a reboot.

</details>

### Result

The trace confirms the analyzer's 14.5 µs switch and explains the week-4
odd-tick lateness: the console wakes every 6 ms (`k_msleep(5)` at a 1 kHz
tick) in the same tick ISR as sampling. Each trace event costs about 7 µs with
interrupts blocked on the C6, so latencies read from the trace are inflated;
numbers come from GPIO. RET §3 week 5.

### Open items

- Task C (jitter with tracing on and off) was not done; it only matters if the
  course grades it after the demo.

---

## Entry 06: Week 6 schedulability, priority inversion and ADR-005

**Date:** 2026-10-21 · **Lab:** `labs/lab06_schedulability.md` · **Author:** Joshua Marín Castrillón
· **Board:** ESP32-C6-DevKitC (`esp32c6_devkitc/esp32c6/hpcore`) · **Team:** Joshua Marín Castrillón, Ismael Cortés Ramírez

### Goal

Reproduce Buttazzo's Fig. 4.13 on the board (RM misses, EDF meets), reproduce a
priority inversion and close it with a priority-inheritance mutex, fix the
e-stop and `calib` at the source (ADR-005), and turn the RTA into a script fed
with measured `C_i` and B, ready for the week-7 workshop.

<details>
<summary><b>Setup</b></summary>

- One wiring for every capture: D0 to D5 on GPIO3, 4, 5, 6, 7, 10 as in week 4,
  D6 on GPIO18 (new `instr_lock`), D7 on GPIO2 (the valve). Flow jumper removed.
- PulseView, `fx2lafw`, 4 MHz, channels named, VCD export. Console driven by
  `evidencia/lab06/capture_lab06.py`.
- Firmwares: `build-lab06-rm` and `build-lab06-edf` (`firmware/fig413`),
  `build-lab06-kernel` (before), `build-lab06-adr005` (fix, 1 kHz tick),
  `build-lab06-adr005-10k` (final).

</details>

<details>
<summary><b>What we did</b></summary>

**Task A.** New app `firmware/fig413`: τ1 (2, 5) and τ2 (4, 7) on a calibrated
loop that counts work, run and miss pins per task, RM by priority and EDF with
`k_thread_absolute_deadline_set()` before each sleep. 60 s console runs, then
one 2 s capture each, reduced by `vcd_fig413.py` and checked job by job against
`fig413_sim.py`.

**Task B.** Control's PI state under one lock in `firmware/kernel`, its kind
picked at run time (`lock sem|mutex`), and `invert`: the console takes the
lock just before a control release, control asks for it, and the console wakes
telemetry with 20 ms of busy work. Three captures (no contention, `k_sem`,
`k_mutex`), reduced by `vcd_lock.py`.

**Task C.** `instr_flow` marks the e-stop detection; five trips per capture
(`set 3300`, wait for `estop=1`, `set 1500`, `clear`). `calib` timed on the
analyzer and its CPU read from the kernel's per-thread runtime counters. Before,
fix at 1 kHz, fix at 10 kHz.

**Analysis.** `analysis/rta.py` on `analysis/taskset.csv`: U, Liu & Layland,
hyperbolic, RTA with PI blocking computed from the lock's users.

</details>

<details>
<summary><b>Problem 1: under RM, τ2 missed every single job</b></summary>

717 of 717, against 1 in 5 expected. `k_sleep()` to an absolute time that has
already passed does not return at once: it waits for the next tick. A late job
that slept anyway left up to 1 ms idle and made the next job late too. Fix: a
late job does not sleep. Then the board missed 4 in 5, and the simulator with
τ1's measured 2.027 ms explained it (two jobs have zero slack in the book).

</details>

<details>
<summary><b>Problem 2: per-job response times of 15 ms that cannot exist</b></summary>

The analyzer's clock runs 140 ppm off the board's (as in week 4): 350 µs over
2.5 s. A fixed 7 ms release grid drifts and pins jobs to the wrong release. The
grid is now fitted on the data. Under EDF there are no miss edges to fit on, so
the grid comes from the τ1 jobs that started on their release.

</details>

<details>
<summary><b>Problem 3: two Task B captures that held the wrong thing</b></summary>

The first `sem` capture started after `invert` had already ended (PulseView
takes a moment to start, and `invert` went out 1 s after Enter). The second
one held a `mutex` run: the board rebooted after `lock sem` and came back with
the default. Kept as the mutex capture. Fix in the script: arm PulseView on a
trigger (`lock` or `cons` rising) before Enter, send `lock` after Enter, and
warn if `invert` reports the wrong kind.

</details>

<details>
<summary><b>Problem 4: the guide's fix for calib does not fit on the C6</b></summary>

The S3 ticks at 10 kHz, the C6 at 1 kHz. `k_usleep(400)` rounds up to whole
ticks plus one: 2 ms per settle, `calib` 2000 ms, still a miss of the 1 s
deadline. The final ADR-005 build sets a 10 kHz tick: 500 ms.

</details>

<details>
<summary><b>Problem 5: native_sim hung on <code>invert</code></b></summary>

Two causes. Its clock only moves inside `k_busy_wait()`, so the phase loop that
only read the clock froze it (fixed with a 10 µs busy wait per turn). And it
ticks at 100 Hz, so sampling ran every 10 ms and control every 100 ms, never
inside the section; with a 10 kHz tick it reproduces the board: mutex R 0.2 ms,
semaphore R 20.2 ms and 5 misses.

</details>

### Result

| | Value |
|---|---|
| Fig. 4.13, RM / EDF, 60 s | 6860 / **0** misses of τ2 (book: 1715 / 0) |
| Control's worst R: uncontended / `k_sem` / `k_mutex` | 20.0 µs / **20.43 ms** / **0.41 ms** |
| B (console's section) | 1.195 ms |
| E-stop, detection to valve closed, before / after | 10.005 ms / **1.00 µs** |
| `calib` CPU / duration, before / after | 396 / 403 ms → **10.6 / 500 ms** |
| RTA, control with B | 1.30 ms ≤ 10 ms |

ADR-002 (PI mutex) and ADR-005 (e-stop and `calib` at the source, with a 10 kHz
tick) accepted. RET §1, §2, §3 week 6 and §4.

### Open items

- **Daniela's two tasks**: parameters given in class; run `rta.py --add` and
  write the verdict in §4.
- **PWM gating on `estop`**: after ADR-005 control can still overwrite the 0
  duty in a few-µs window (ADR-005, cost).
- **Flow ISR on the C6**: never measured on this board; it belongs in the task
  set once the flow input is back.
- **RET v1**: tag `workshop` before the week-7 session.
