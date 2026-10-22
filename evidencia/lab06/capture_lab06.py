"""Drives the node's console during a week-6 analyzer capture and logs it.

Same protocol as evidencia/lab04/capture_console.py (banner first, commands at
fixed offsets after Enter), minus the flow-jumper check: in week 6 the flow
input stays unplugged.

Usage (inside the Zephyr venv, which brings pyserial):
    python capture_lab06.py <log.txt> <mode>

nolock  Task B, no contention: 5 s with nothing typed, then `status`
sem     Task B, the inversion: `lock sem`, `invert`, `status`
mutex   Task B, the PI mutex: `lock mutex`, `invert`, `status`
estop   Task C: 5 trips (`set 3300` until estop=1, then `set 1500`, `clear`)
calib   Task C: `calib`, then `status`
"""
import os
import re
import sys
import threading
import time

import serial

PORT = os.environ.get("CONSOLE_PORT", "/dev/ttyACM0")
BAUD = 115200
CAPTURE_S = {"nolock": 7, "sem": 5, "mutex": 5, "estop": 30, "calib": 5}
TRIPS = 5

log_path, mode = sys.argv[1], sys.argv[2]
assert mode in CAPTURE_S, mode

ser = serial.Serial()
ser.port, ser.baudrate, ser.timeout = PORT, BAUD, 0.1
ser.dtr = ser.rts = False   # keep the auto-reset circuit idle on open
ser.open()

log = open(log_path, "w", encoding="utf-8")
lines, stop = [], threading.Event()


def reader():
    buf = b""
    while not stop.is_set():
        buf += ser.read(256)
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            text = raw.decode("utf-8", "replace").rstrip("\r")
            lines.append(text)
            log.write(text + "\n")
            log.flush()
            print("   | " + text, flush=True)


def send(cmd):
    print(">>> " + cmd, flush=True)
    ser.write(cmd.encode() + b"\r")


def wait_for(pattern, timeout):
    t0, start = time.time(), len(lines)
    while time.time() - t0 < timeout:
        if any(re.search(pattern, l) for l in lines[start:]):
            return True
        time.sleep(0.05)
    return False


threading.Thread(target=reader, daemon=True).start()

print("\n=== Press RST on the board now (waiting up to 30 s for the banner) ===")
if not wait_for(r"build \(", 30):
    sys.exit("no boot banner seen: check the port and press RST again")

# PulseView takes a moment to start: a run clicked after Enter missed a whole
# `invert` once. Armed first, it waits for its trigger instead.
TRIGGER = {"nolock": "none", "sem": "`lock` rising", "mutex": "`lock` rising",
           "estop": "`cons` rising", "calib": "`cons` rising"}
input(f"\n=== Trigger on {TRIGGER[mode]}, click Run in PulseView, THEN press Enter ===")
t0 = time.time()
at = lambda s: time.sleep(max(0, t0 + s - time.time()))

if mode == "nolock":
    at(5)
    send("status")
elif mode in ("sem", "mutex"):
    # `lock` goes after Enter, right before `invert`: sent earlier, a second
    # RST put the default (mutex) back and a "sem" capture came out as mutex.
    # It does not fire the trigger: only `invert` drives the lock pin.
    at(0.5)
    send("lock " + mode)
    at(1.5)
    send("invert")
    if not wait_for(r"^invert \w+:", 10):
        print("=== WARNING: invert did not report within 10 s")
    elif not any(l.startswith("invert " + mode + ":") for l in lines):
        print("=== WARNING: invert ran with the wrong lock: repeat this capture")
    send("status")
elif mode == "estop":
    at(1)
    for trip in range(1, TRIPS + 1):
        send("set 3300")
        if not wait_for(r"estop=1", 6):
            print("=== WARNING: trip %d did not latch within 6 s" % trip)
        time.sleep(0.5)
        send("set 1500")
        time.sleep(0.3)
        send("clear")
        time.sleep(1.5)       # pressure back under 3000 mV before the next trip
elif mode == "calib":
    at(1)
    send("calib")
    if not wait_for(r"calibration done", 5):
        print("=== WARNING: no 'calibration done' within 5 s")
    send("status")
at(CAPTURE_S[mode])
print("\n=== Capture window over: export the VCD in PulseView now ===")

stop.set()
time.sleep(0.3)
ser.close()
log.close()
print("=== Log saved to " + log_path)
