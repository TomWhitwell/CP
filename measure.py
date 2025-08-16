#!/usr/bin/env python3
"""
SPI speed benchmark harness for flashrom + Raspberry Pi + demuxed slots.

- NO buttons, NO per-slot LEDs (just a background 'LED noise' thread that hammers the 74HC595s)
- Measures read timings/reliability on slot 0
- Measures write+verify timings/reliability to slots 1..15
- Sweeps SPI speed from 2000 kHz to 48000 kHz in 2000 kHz steps
- Logs compact summaries to test.txt, with progress printed to console

Hardware assumptions:
- Demux (CD74HC154) select pins on GPIO22..25 (A0..A3), A0 = LSB.
- 74HC595 'LED' shift-register on GPIO2 (CLK), GPIO3 (LATCH), GPIO4 (DATA) for background activity.
- SPI device is /dev/spidev0.0 and used by flashrom as linux_spi.

This script intentionally keeps shift-register activity running in the background
to emulate system noise while benchmarking flash operations.
"""
import os
import sys
import time
import signal
import random
import subprocess
from statistics import mean
import gpiod

# --------------------------- Config ---------------------------

# SPI programmer base (flashrom)
SPI_DEV = "/dev/spidev0.0"
PROGRAMMER_BASE = f"linux_spi:dev={SPI_DEV}"

SPEED_START_KHZ = 10000
SPEED_END_KHZ   = 20000
SPEED_STEP_KHZ  = 1000

READ_REPEATS  = 10               # read slot 0 this many times per speed
WRITE_ROUNDS  = 10               # write all 15 targets this many rounds per speed
WRITE_SLOTS   = list(range(1, 16))  # 1..15

# Timeouts (be generous; flash operations can be slow)
READ_TIMEOUT_S  = 180
WRITE_TIMEOUT_S = 300

# Files
READ_OUTFILE = "file.bin"
RESULTS_FILE = "test.txt"

# 74HC595 pins (BCM) for LED noise generator
CLK_PIN   = 2   # physical 3
LATCH_PIN = 3   # physical 5
DATA_PIN  = 4   # physical 7
ACTIVE_LOW_LEDS = False          # polarity (not important for random noise)

# CD74HC154 demux address lines (BCM). A0 = LSB.
CS_A0, CS_A1, CS_A2, CS_A3 = 22, 23, 24, 25

# LED noise settings
LED_NOISE_TICK_S = 0.010         # every 10 ms push a random 16-bit pattern

# --------------------------- GPIO helpers ---------------------------

class Shift595:
    """Minimal 74HC595 driver using gpiod, for background 'LED noise' only."""
    def __init__(self, chip_name="gpiochip0", clk=CLK_PIN, latch=LATCH_PIN, data=DATA_PIN, active_low=False):
        self.active_low = active_low
        chip = gpiod.Chip(chip_name)
        self.clk   = chip.get_line(clk)
        self.latch = chip.get_line(latch)
        self.data  = chip.get_line(data)
        for ln in (self.clk, self.latch, self.data):
            ln.request(consumer="led_noise", type=gpiod.LINE_REQ_DIR_OUT)

    def shift_out(self, bits16: int):
        """Shift 16 bits MSB-first then latch to outputs."""
        if self.active_low:
            bits16 ^= 0xFFFF
        for bit in range(15, -1, -1):
            self.data.set_value((bits16 >> bit) & 1)
            self.clk.set_value(1)
            self.clk.set_value(0)
        self.latch.set_value(1)
        self.latch.set_value(0)

class LedNoiseThread:
    """Background thread that continuously bangs random data into the 74HC595."""
    def __init__(self, shift: Shift595, tick_s=LED_NOISE_TICK_S):
        self.shift = shift
        self.tick_s = tick_s
        self._stop = False

    def start(self):
        import threading
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop = True
        try:
            self._t.join(timeout=1.0)
        except Exception:
            pass
        # Optionally blank outputs at end:
        try:
            self.shift.shift_out(0x0000)
        except Exception:
            pass

    def _run(self):
        rnd = random.Random()
        next_deadline = time.monotonic()
        while not self._stop:
            # Push a new random pattern every tick
            try:
                self.shift.shift_out(rnd.getrandbits(16))
            except Exception:
                # Keep going even if the register momentarily errors
                pass
            next_deadline += self.tick_s
            sleep = next_deadline - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_deadline = time.monotonic()

class ChipSelector:
    """Drives CD74HC154 address lines A0..A3 (active-low outputs; we only drive addresses)."""
    def __init__(self, chip_name="gpiochip0"):
        chip = gpiod.Chip(chip_name)
        self.lines = [
            chip.get_line(CS_A0),  # A0 LSB
            chip.get_line(CS_A1),
            chip.get_line(CS_A2),
            chip.get_line(CS_A3),  # A3 MSB
        ]
        for ln in self.lines:
            ln.request(consumer="cs_decode", type=gpiod.LINE_REQ_DIR_OUT)

    def set(self, index: int):
        """Select a slot 0..15 by writing A0..A3 = index bits (LSB..MSB)."""
        idx = index & 0xF
        for bit, ln in enumerate(self.lines):
            ln.set_value((idx >> bit) & 1)
        # small settle
        time.sleep(0.001)

# --------------------------- Flashrom helpers ---------------------------

def run_flashrom(cmd, timeout_s):
    """
    Run a flashrom command with timeout, capture output, return (rc, output_str).
    """
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        stdout = (out.stdout or "") + (out.stderr or "")
        return out.returncode, stdout
    except subprocess.TimeoutExpired as e:
        try:
            # Kill process if still alive (subprocess.run already does this, but be explicit)
            if e.stdout or e.stderr:
                combined = (e.stdout or "") + (e.stderr or "")
            else:
                combined = ""
        except Exception:
            combined = ""
        return 124, combined + "\n[TIMEOUT] Command exceeded {}s".format(timeout_s)
    except Exception as e:
        return 1, f"[ERROR] {e}"

def prog_string(spispeed_khz: int) -> str:
    return f"{PROGRAMMER_BASE},spispeed={spispeed_khz}"

# --------------------------- Benchmark core ---------------------------

def ensure_clean_file(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"[warn] could not remove {path}: {e}", flush=True)

def filesize_or_zero(path) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

def bench_reads(selector: ChipSelector, spispeed_khz: int) -> dict:
    """
    Read slot 0 READ_REPEATS times into READ_OUTFILE.
    Returns dict with min/max/avg durations and success count.
    """
    durations = []
    successes = 0
    selector.set(0)
    for i in range(1, READ_REPEATS + 1):
        ensure_clean_file(READ_OUTFILE)
        cmd = ["flashrom", "-p", prog_string(spispeed_khz), "-r", READ_OUTFILE]
        t0 = time.monotonic()
        rc, out = run_flashrom(cmd, READ_TIMEOUT_S)
        dt = time.monotonic() - t0
        ok = (rc == 0 and filesize_or_zero(READ_OUTFILE) > 0)
        durations.append(dt)
        successes += 1 if ok else 0
        print(f"[read] {spispeed_khz:5d} kHz  try {i:2d}/{READ_REPEATS}  "
              f"{'OK' if ok else 'FAIL'}  ({dt:.2f}s)", flush=True)
        if not ok:
            # Optional: show last line of flashrom out for debugging
            last_line = out.strip().splitlines()[-1] if out else ""
            if last_line:
                print(f"       └─ {last_line}", flush=True)

    res = {
        "durations": durations,
        "successes": successes,
        "min": min(durations) if durations else None,
        "max": max(durations) if durations else None,
        "avg": mean(durations) if durations else None,
    }
    return res

def bench_writes(selector: ChipSelector, spispeed_khz: int) -> dict:
    """
    Write THEN verify READ_OUTFILE to slots 1..15, repeated WRITE_ROUNDS times.
    Success for a slot requires both write and verify to return rc==0.
    Per-round time includes both write+verify for all 15 slots.
    """
    if filesize_or_zero(READ_OUTFILE) == 0:
        print("[write] Skipping writes: file.bin not present or empty.", flush=True)
        return {"round_totals": [], "successes": 0, "total": 0, "max_round": None}

    round_totals = []
    successes = 0
    total = WRITE_ROUNDS * len(WRITE_SLOTS)

    for r in range(1, WRITE_ROUNDS + 1):
        r_start = time.monotonic()
        print(f"[write] {spispeed_khz:5d} kHz  round {r}/{WRITE_ROUNDS}", flush=True)

        for idx, slot in enumerate(WRITE_SLOTS, start=1):
            selector.set(slot)

            # WRITE
            cmd_write = ["flashrom", "-p", prog_string(spispeed_khz), "-w", READ_OUTFILE]
            t0 = time.monotonic()
            rc_w, out_w = run_flashrom(cmd_write, WRITE_TIMEOUT_S)
            dt_w = time.monotonic() - t0
            ok_w = (rc_w == 0)

            # VERIFY (only if write succeeded)
            ok_v = False
            dt_v = 0.0
            if ok_w:
                cmd_verify = ["flashrom", "-p", prog_string(spispeed_khz), "--verify", READ_OUTFILE]
                t1 = time.monotonic()
                rc_v, out_v = run_flashrom(cmd_verify, WRITE_TIMEOUT_S)
                dt_v = time.monotonic() - t1
                ok_v = (rc_v == 0)

            ok = ok_w and ok_v
            successes += 1 if ok else 0

            # Console progress
            if ok:
                print(f"    slot {slot:2d}  {idx:2d}/{len(WRITE_SLOTS)}  OK      "
                      f"(write {dt_w:.2f}s, verify {dt_v:.2f}s)", flush=True)
            else:
                print(f"    slot {slot:2d}  {idx:2d}/{len(WRITE_SLOTS)}  FAIL    "
                      f"(write {dt_w:.2f}s{', verify '+format(dt_v, '.2f')+'s' if ok_w else ''})", flush=True)
                # Show the most relevant last line
                last_line = ""
                if not ok_w and out_w:
                    last_line = out_w.strip().splitlines()[-1] if out_w.strip().splitlines() else ""
                elif ok_w and not ok_v and out_v:
                    last_line = out_v.strip().splitlines()[-1] if out_v.strip().splitlines() else ""
                if last_line:
                    print(f"       └─ {last_line}", flush=True)

        r_total = time.monotonic() - r_start
        round_totals.append(r_total)
        print(f"    round total: {r_total:.2f}s", flush=True)

    return {
        "round_totals": round_totals,
        "successes": successes,
        "total": total,
        "max_round": max(round_totals) if round_totals else None,
    }

def append_results(speed_khz: int, read_stats: dict, write_stats: dict):
    """
    Append compact results to RESULTS_FILE.
    """
    r_min = read_stats.get("min")
    r_max = read_stats.get("max")
    r_avg = read_stats.get("avg")
    r_ok  = read_stats.get("successes", 0)
    r_pct = 100.0 * r_ok / READ_REPEATS if READ_REPEATS else 0.0

    w_max = write_stats.get("max_round")
    w_ok  = write_stats.get("successes", 0)
    w_tot = write_stats.get("total", 0)
    w_pct = (100.0 * w_ok / w_tot) if w_tot else 0.0

    # Console summary
    print("----------------------------------------------------------------", flush=True)
    print(f"[summary] SPI={speed_khz} kHz", flush=True)
    if r_min is not None:
        print(f"  READ:  min={r_min:.2f}s  max={r_max:.2f}s  avg={r_avg:.2f}s  success={r_pct:.1f}% ({r_ok}/{READ_REPEATS})", flush=True)
    else:
        print("  READ:  no data", flush=True)
    if w_max is not None:
        print(f"  WRITE(15x{WRITE_ROUNDS}): max_round={w_max:.2f}s  success={w_pct:.1f}% ({w_ok}/{w_tot})", flush=True)
    else:
        print(f"  WRITE(15x{WRITE_ROUNDS}): skipped", flush=True)

    # Text log (compact)
    try:
        with open(RESULTS_FILE, "a") as f:
            f.write(f"SPI={speed_khz} kHz\n")
            if r_min is not None:
                f.write(f"READ: min={r_min:.2f}s max={r_max:.2f}s avg={r_avg:.2f}s success={r_pct:.1f}% ({r_ok}/{READ_REPEATS})\n")
            else:
                f.write("READ: no data\n")
            if w_max is not None:
                f.write(f"WRITE(15x{WRITE_ROUNDS}): max_round={w_max:.2f}s success={w_pct:.1f}% ({w_ok}/{w_tot})\n")
            else:
                f.write(f"WRITE(15x{WRITE_ROUNDS}): skipped\n")
            f.write("---\n")
    except Exception as e:
        print(f"[warn] could not write to {RESULTS_FILE}: {e}", flush=True)

# --------------------------- Main ---------------------------

_stop = False

def handle_signal(signum, frame):
    global _stop
    _stop = True
    print("\n[main] Signal received, stopping...", flush=True)

def main():
    # Hook signals
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Init demux & 74HC595
    selector = ChipSelector()
    shifter  = Shift595(active_low=ACTIVE_LOW_LEDS)
    noise    = LedNoiseThread(shifter, tick_s=LED_NOISE_TICK_S)
    noise.start()

    # Clear previous results file header
    try:
        if not os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE, "w") as f:
                f.write("# flashrom speed benchmark results\n")
                f.write("# format: one block per SPI speed\n")
                f.write("# ---\n")
    except Exception as e:
        print(f"[warn] could not initialize {RESULTS_FILE}: {e}", flush=True)

    # Sweep speeds
    for spispeed_khz in range(SPEED_START_KHZ, SPEED_END_KHZ + 1, SPEED_STEP_KHZ):
        if _stop:
            break

        print("\n================================================================", flush=True)
        print(f"[main] Testing SPI speed: {spispeed_khz} kHz", flush=True)
        # READ phase
        selector.set(0)
        read_stats = bench_reads(selector, spispeed_khz)

        # WRITE phase (only if we have a valid file.bin from this speed)
        if filesize_or_zero(READ_OUTFILE) == 0:
            print("[main] No valid file.bin after reads; skipping writes at this speed.", flush=True)
            write_stats = {"round_totals": [], "successes": 0, "total": 0, "max_round": None}
        else:
            write_stats = bench_writes(selector, spispeed_khz)

        # Summarize & log
        append_results(spispeed_khz, read_stats, write_stats)
 
    # Cleanup
    noise.stop()
    print("[main] Done.", flush=True)

if __name__ == "__main__":
    main()
