#!/usr/bin/env python3
import os
import time
import threading
import random
import signal
import sys
import subprocess
import gpiod
import hashlib
import shutil
import spidev
import re

# -------- Defaults --------
DEFAULT_SPISPEED_KHZ = 2000  # flashrom linux_spi default

# -------- Hardware (BCM) --------
CLK_PIN   = 2   # 74HC595 clock  (phys 3)
LATCH_PIN = 3   # 74HC595 latch  (phys 5)
DATA_PIN  = 4   # 74HC595 data   (phys 7)

# DIP switches (active-low with pull-ups) — read once at boot
DIP_PINS = {"DIP1": 16, "DIP2": 19, "DIP3": 20, "DIP4": 21}

# Buttons (active-low with pull-ups)
CHECK_BTN = 5   # Triggers scan
WRITE_BTN = 6   # Triggers read+clone

# LED polarity: set True if LEDs are active-low (sinking)
ACTIVE_LOW = False

# Modes
OFF, ON, BLINK_FAST, BLINK_SLOW, BLINK_DATA = 0, 1, 2, 3, 4

# -------- 74HC595 via gpiod --------
chip = gpiod.Chip("gpiochip0")
clk_line   = chip.get_line(CLK_PIN)
latch_line = chip.get_line(LATCH_PIN)
data_line  = chip.get_line(DATA_PIN)
for line in (clk_line, latch_line, data_line):
    line.request(consumer="led_ctrl", type=gpiod.LINE_REQ_DIR_OUT)

def shift_out(bits16: int):
    """Shift 16 bits MSB-first then latch."""
    if ACTIVE_LOW:
        bits16 ^= 0xFFFF
    for bit in range(15, -1, -1):
        data_line.set_value((bits16 >> bit) & 1)
        clk_line.set_value(1)
        clk_line.set_value(0)
    latch_line.set_value(1)
    latch_line.set_value(0)

# -------- Chip select via CD74HC154 (A0..A3 on GPIO22..25) --------
CS_A0, CS_A1, CS_A2, CS_A3 = 22, 23, 24, 25  # A0 = LSB
cs_chip = gpiod.Chip("gpiochip0")
cs_lines = [
    cs_chip.get_line(CS_A0),
    cs_chip.get_line(CS_A1),
    cs_chip.get_line(CS_A2),
    cs_chip.get_line(CS_A3),
]
for ln in cs_lines:
    ln.request(consumer="cs_decode", type=gpiod.LINE_REQ_DIR_OUT)

class ChipSelector:
    def __init__(self, lines):
        self.lines = lines  # [A0, A1, A2, A3]
    def set(self, index: int):
        index &= 0xF
        for bit, line in enumerate(self.lines):
            line.set_value((index >> bit) & 1)  # A0..A3 <= LSB..MSB
        time.sleep(0.001)  # small settle

selector = ChipSelector(cs_lines)

# -------- flashrom probe helpers --------
def parse_found_line(text: str):
    m = re.search(r'Found .* chip "?([\w.\-]+)"?\s+\((\d+)\s*(kB|KB|MiB|MB)\b', text)
    if not m:
        return None, None
    name = m.group(1)
    size = int(m.group(2))
    unit = m.group(3).lower()
    if unit in ("kb", "kb", "kb"):
        size_bytes = size * 1024
    elif unit in ("mb", "mib"):
        size_bytes = size * 1024 * 1024
    else:
        size_bytes = None
    return name, size_bytes

def probe_chip(programmer: str, spispeed_khz: int, chip_hint: str | None = None):
    prog_str = f"{programmer},spispeed={spispeed_khz}"
    cmd = ["flashrom", "-p", prog_str]
    if chip_hint:
        cmd += ["-c", chip_hint]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        return {"found": False, "name": None, "size_bytes": None, "raw": f"error: {e}"}
    stdout = (out.stdout or "") + (out.stderr or "")
    name, size_bytes = parse_found_line(stdout)
    return {
        "found": (name is not None and size_bytes is not None),
        "name": name,
        "size_bytes": size_bytes,
        "raw": stdout,
    }

def human_size(n):
    return f"{n//(1024*1024)} MiB" if n and n >= 1024*1024 else (f"{n//1024} KiB" if n else "n/a")

# Store the most recent scan so WRITE can use it
last_scan_results = None  # list[dict] of length 16

def scan_all_slots(programmer: str, spispeed_khz: int):
    global last_scan_results
    results = [None] * 16
    print("[scan] Probing source slot 0...", flush=True)
    selector.set(0)
    res0 = probe_chip(programmer, spispeed_khz)
    results[0] = res0
    ref_size = res0["size_bytes"] if (res0 and res0.get("found")) else None

    if not res0.get("found"):
        leds.set_status(0, BLINK_FAST)
        for i in range(1, 16):
            leds.set_status(i, OFF)
        print("[scan] Slot 00: source not found — BLINK_FAST, skipping all other checks.", flush=True)
        last_scan_results = results
        return results
    elif res0.get("found") and not ref_size:
        leds.set_status(0, BLINK_FAST)
        print("[scan] Slot 00: found but size unknown — BLINK_FAST", flush=True)
    else:
        leds.set_status(0, ON)
        print(f"[scan] Slot 00: {res0['name']} ({human_size(ref_size)})  [SOURCE]", flush=True)

    for i in range(1, 16):
        selector.set(i)
        res = probe_chip(programmer, spispeed_khz)
        results[i] = res
        found = res.get("found", False)
        size  = res.get("size_bytes")
        name  = res.get("name") or "unknown"

        if not found:
            leds.set_status(i, OFF)
            print(f"[scan] Slot {i:02d}: no chip found — LED OFF", flush=True)
        elif ref_size and size == ref_size:
            leds.set_status(i, BLINK_SLOW)
            print(f"[scan] Slot {i:02d}: {name} ({human_size(size)})  ✓ size matches source", flush=True)
        else:
            leds.set_status(i, BLINK_FAST)
            reason = []
            if not size:
                reason.append("size unknown")
            elif size != ref_size:
                reason.append(f"size mismatch {human_size(size)} != {human_size(ref_size)}")
            msg = "; ".join(reason) if reason else "unknown reason"
            print(f"[scan] Slot {i:02d}: {name} ({human_size(size)})  ✗ {msg}", flush=True)

    print("[scan] Done.", flush=True)
    last_scan_results = results
    return results

# -------- LED controller thread --------
class LedControl:
    TICK = 0.01  # 10 ms
    def __init__(self):
        self.modes = [OFF] * 16
        self._data_states = [False] * 16
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tick = 0
        self._rand = random.Random()
        self._thread = threading.Thread(target=self._run, daemon=True)
    def start(self): self._thread.start()
    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        shift_out(0x0000)
    def set_status(self, led: int, mode: int):
        if 0 <= led < 16:
            with self._lock:
                self.modes[led] = mode
    def get_status(self, led: int) -> int:
        with self._lock:
            return self.modes[led]
    def _frame_from_modes(self) -> int:
        tc = self._tick
        frame = 0
        with self._lock:
            if tc % 5 == 0:  # BLINK_DATA update every 50ms
                for i, m in enumerate(self.modes):
                    if m == BLINK_DATA:
                        self._data_states[i] = self._rand.choice([True, False])
            for i, m in enumerate(self.modes):
                if m == ON:
                    on = True
                elif m == OFF:
                    on = False
                elif m == BLINK_FAST:
                    on = ((tc // 5) % 2 == 0)      # 100 ms toggle
                elif m == BLINK_SLOW:
                    on = ((tc // 100) % 2 != 0)    # inverted phase, 1000 ms
                elif m == BLINK_DATA:
                    on = self._data_states[i]
                else:
                    on = False
                if on:
                    frame |= (1 << i)
        return frame
    def _run(self):
        next_deadline = time.monotonic()
        while not self._stop.is_set():
            shift_out(self._frame_from_modes())
            self._tick += 1
            next_deadline += self.TICK
            sleep = next_deadline - time.monotonic()
            if sleep > 0: time.sleep(sleep)
            else: next_deadline = time.monotonic()

# -------- Read UID directly via SPI (best effort) --------
def read_uid_spidev(spispeed_khz, bus=0, cs=0):
    spi = spidev.SpiDev()
    try:
        spi.open(bus, cs); spi.mode = 0; spi.bits_per_word = 8
        spi.max_speed_hz = spispeed_khz * 1000
        jedec = spi.xfer2([0x9F, 0, 0, 0])
        print(f"[spi] JEDEC ID: {jedec[1]:02X} {jedec[2]:02X} {jedec[3]:02X}", flush=True)
        rx = spi.xfer2([0x4B, 0, 0, 0, 0] + [0]*8)
        uid8 = bytes(rx[-8:])
        if uid8 not in (b"\x00"*8, b"\xFF"*8):
            print(f"[spi] UID(8): {uid8.hex()}", flush=True); return uid8
        rx = spi.xfer2([0x4B, 0, 0, 0, 0] + [0]*16)
        uid16 = bytes(rx[-16:])
        if uid16 not in (b"\x00"*16, b"\xFF"*16):
            print(f"[spi] UID(16): {uid16.hex()}", flush=True); return uid16
        print("[spi] UID not available (got all 00/FF)", flush=True); return None
    except Exception as e:
        print(f"[spi] UID read error: {e}", flush=True); return None
    finally:
        try: spi.close()
        except Exception: pass

# -------- gpiod input manager (pull-ups + debounce) --------
class GpioInputManager:
    def __init__(self, chip_name="gpiochip0"):
        self.chip = gpiod.Chip(chip_name)
        self.lines = {}
        for name, pin in DIP_PINS.items():
            self.lines[name] = self.chip.get_line(pin)
        self.lines["CHECK"] = self.chip.get_line(CHECK_BTN)
        self.lines["WRITE"] = self.chip.get_line(WRITE_BTN)
        self._request_input_with_pullups()
        self._stop = threading.Event()
        self._thread = None
        self._debounce_ms = 75
        self._poll_interval = 0.02
        self._state = {"CHECK": 1, "WRITE": 1}  # 1=released, 0=pressed
        self._pending = {"CHECK": None, "WRITE": None}
        self._last_change = {"CHECK": 0.0, "WRITE": 0.0}
        self.on_check = None; self.on_write = None
    def _request_input_with_pullups(self):
        flags = getattr(gpiod, "LINE_REQ_FLAG_BIAS_PULL_UP", 0)
        for key, line in self.lines.items():
            try: line.request(consumer="inputs", type=gpiod.LINE_REQ_DIR_IN, flags=flags)
            except OSError: line.request(consumer="inputs", type=gpiod.LINE_REQ_DIR_IN)
    def read_dips_once(self):
        return {name: (self.lines[name].get_value() == 0) for name in ["DIP1","DIP2","DIP3","DIP4"]}
    def start(self, on_check, on_write):
        self.on_check = on_check; self.on_write = on_write
        self._thread = threading.Thread(target=self._poll_loop, daemon=True); self._thread.start()
    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=1.0)
    def _poll_loop(self):
        debounce_s = self._debounce_ms / 1000.0
        while not self._stop.is_set():
            now = time.monotonic()
            for name in ("CHECK","WRITE"):
                raw = self.lines[name].get_value()
                stable = self._state[name]; pending = self._pending[name]
                if pending is None:
                    if raw != stable:
                        self._pending[name] = raw; self._last_change[name] = now
                else:
                    if raw != pending:
                        self._pending[name] = None
                    elif (now - self._last_change[name]) >= debounce_s:
                        self._state[name] = pending; self._pending[name] = None
                        if stable == 1 and pending == 0:
                            if name == "CHECK" and self.on_check: self.on_check()
                            elif name == "WRITE" and self.on_write: self.on_write()
            time.sleep(self._poll_interval)

# -------- Flashrom worker: read source & clone to matching slots --------
class FlashromWorker:
    """
    WRITE button runs: read source (slot 0) -> turn OFF ready targets -> clone to every present slot with matching size.
    """
    def __init__(self, leds, output_path="card.bin",
                 programmer="linux_spi:dev=/dev/spidev0.0", chip_name=None):
        self.leds = leds
        self.output_path = output_path
        self.programmer = programmer
        self.chip_name = chip_name
        self.spispeed = DEFAULT_SPISPEED_KHZ  # kHz; will be overridden from DIPs at boot
        self._busy = threading.Event()

    def is_busy(self) -> bool: return self._busy.is_set()

    def start_clone(self):
        if self._busy.is_set():
            print("[flashrom] Busy, ignoring new request.", flush=True); return
        threading.Thread(target=self._run_read_then_clone, daemon=True).start()

    def _run_read_then_clone(self):
        self._busy.set()
        try:
            ok = self._do_read_source()
            if not ok:
                print("[flashrom] Source read failed — aborting clone.", flush=True)
                return

            ref_size = os.path.getsize(self.output_path)
            print(f"[clone] Source size: {human_size(ref_size)}", flush=True)

            # Turn OFF all ready targets (present + size match) before writing
            turned_off = 0
            if last_scan_results and last_scan_results[0] and last_scan_results[0].get("size_bytes") in (ref_size, ref_size):
                for i in range(1, 16):
                    info = last_scan_results[i] if i < len(last_scan_results) else None
                    if info and info.get("found") and info.get("size_bytes") == ref_size:
                        self.leds.set_status(i, OFF)
                        turned_off += 1
                print(f"[clone] Set {turned_off} target LEDs to OFF before writing.", flush=True)
            else:
                # No recent scan; fall back to a quick probe-based OFF pass
                for i in range(1, 16):
                    selector.set(i)
                    res = probe_chip(self.programmer, self.spispeed)
                    if res.get("found") and res.get("size_bytes") == ref_size:
                        self.leds.set_status(i, OFF)
                        turned_off += 1
                print(f"[clone] (no scan) Set {turned_off} target LEDs to OFF before writing.", flush=True)

            # Iterate targets 1..15 and write+verify
            successes, failures, skipped = 0, 0, 0
            for i in range(1, 16):
                selector.set(i)
                # Prefer scan cache; probe if missing
                if last_scan_results and i < len(last_scan_results) and last_scan_results[i] is not None:
                    res = last_scan_results[i]
                else:
                    res = probe_chip(self.programmer, self.spispeed)

                found = res.get("found", False)
                size  = res.get("size_bytes")

                if not found:
                    self.leds.set_status(i, OFF)
                    print(f"[clone] Slot {i:02d}: no chip — skip", flush=True)
                    skipped += 1
                    continue
                if not size or size != ref_size:
                    self.leds.set_status(i, BLINK_FAST)
                    reason = "size unknown" if not size else f"size mismatch {human_size(size)}"
                    print(f"[clone] Slot {i:02d}: present but {reason} — skip", flush=True)
                    failures += 1
                    continue

                # Good target: write then verify
                self.leds.set_status(i, BLINK_DATA)
                prog_str = f"{self.programmer},spispeed={self.spispeed}"

                # WRITE
                cmd_write = ["flashrom", "-p", prog_str, "-w", self.output_path]
                if self.chip_name: cmd_write += ["-c", self.chip_name]
                print(f"[clone] Slot {i:02d}: writing...", flush=True)
                proc = subprocess.Popen(cmd_write, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    print(f"[flashrom S{i:02d}]", line.rstrip())
                write_code = proc.wait()
                if write_code != 0:
                    self.leds.set_status(i, BLINK_FAST)
                    print(f"[clone] Slot {i:02d}: WRITE failed (exit {write_code})", flush=True)
                    failures += 1
                    continue

                # VERIFY
                cmd_verify = ["flashrom", "-p", prog_str, "--verify", self.output_path]
                if self.chip_name: cmd_verify += ["-c", self.chip_name]
                print(f"[clone] Slot {i:02d}: verifying...", flush=True)
                proc = subprocess.Popen(cmd_verify, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    print(f"[flashrom S{i:02d}]", line.rstrip())
                verify_code = proc.wait()
                if verify_code == 0:
                    self.leds.set_status(i, ON)
                    print(f"[clone] Slot {i:02d}: OK", flush=True)
                    successes += 1
                else:
                    self.leds.set_status(i, BLINK_FAST)
                    print(f"[clone] Slot {i:02d}: VERIFY failed (exit {verify_code})", flush=True)
                    failures += 1

            print(f"[clone] Done. ok={successes}, failed={failures}, skipped={skipped}", flush=True)

        finally:
            self._busy.clear()

    def _do_read_source(self) -> bool:
        target_led = 0
        try:
            selector.set(0)  # ensure source selected

            uid = read_uid_spidev(self.spispeed)
            if uid and len(uid) == 8:
                as_int = int.from_bytes(uid, "big")
                print(f"[spi] UID(64-bit): 0x{as_int:016X}", flush=True)

            if os.path.exists(self.output_path):
                try:
                    os.remove(self.output_path)
                    print(f"[flashrom] Removed existing {self.output_path}", flush=True)
                except Exception as e:
                    print(f"[flashrom] Could not remove {self.output_path}: {e}", flush=True)

            self.leds.set_status(target_led, BLINK_DATA)

            prog_str = f"{self.programmer},spispeed={self.spispeed}"
            cmd = ["flashrom", "-p", prog_str, "-r", self.output_path]
            if self.chip_name: cmd += ["-c", self.chip_name]
            print("[flashrom] Reading source:", " ".join(cmd), flush=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                print("[flashrom]", line.rstrip())
            code = proc.wait()
            if code != 0 or not os.path.exists(self.output_path):
                print(f"[flashrom] Read failed (exit {code})", flush=True)
                self.leds.set_status(target_led, BLINK_FAST)
                return False

            print("[flashrom] Read OK → card.bin", flush=True)

            # Archive copy
            h = hashlib.sha1()
            with open(self.output_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digest12 = h.hexdigest()[:12]
            archive_dir = "CardArchive"
            os.makedirs(archive_dir, exist_ok=True)
            dest_path = os.path.join(archive_dir, f"{digest12}.bin")
            shutil.copyfile(self.output_path, dest_path)
            print(f"[flashrom] Archived as {dest_path}", flush=True)

            self.leds.set_status(target_led, ON)
            return True

        except Exception as e:
            print(f"[flashrom] Error during source read: {e}", flush=True)
            self.leds.set_status(target_led, BLINK_FAST)
            return False

# -------- Wire-up & UX control --------
leds = LedControl()
inputs = GpioInputManager()
flash = None  # will be created after DIP speed is computed
_scan_busy = threading.Event()

def startup_animation():
    for i in range(16):
        leds.set_status(i, ON); time.sleep(0.05)
    for i in range(16):
        leds.set_status(i, OFF); time.sleep(0.05)

def on_check_pressed():
    if flash.is_busy() or _scan_busy.is_set():
        print("[ui] Busy; CHECK ignored.", flush=True); return
    def _worker():
        _scan_busy.set()
        try:
            print("[ui] CHECK: scanning...", flush=True)
            for i in range(16): leds.set_status(i, OFF)
            selector.set(0)
            scan_all_slots(flash.programmer, flash.spispeed)
            print("[ui] Scan complete.", flush=True)
        finally:
            _scan_busy.clear()
    threading.Thread(target=_worker, daemon=True).start()

def on_write_pressed():
    if flash.is_busy() or _scan_busy.is_set():
        print("[ui] Busy; WRITE ignored.", flush=True); return
    print("[ui] WRITE: read source, then clone to matching slots.", flush=True)
    flash.start_clone()

def cleanup_and_exit(code=0):
    try: inputs.stop()
    except Exception: pass
    try: leds.stop()
    except Exception: pass
    sys.exit(code)

def handle_signal(signum, frame): cleanup_and_exit(0)

if __name__ == "__main__":
    leds.start()
    dips = inputs.read_dips_once()
    print("[boot] DIP state:", dips, flush=True)

    # Map DIP switches (active-low) to N=0..15, DIP1 = bit0 (LSB)
    N = (1 if dips["DIP1"] else 0) \
        | ((1 if dips["DIP2"] else 0) << 1) \
        | ((1 if dips["DIP3"] else 0) << 2) \
        | ((1 if dips["DIP4"] else 0) << 3)
#     N = (~N_raw) & 0xF  # invert 4 bits because active-LOW

    # 2 MHz base, 2 MHz step → 2000, 4000, 6000, ... up to 32000/48000 etc.
    spispeed_khz = 2000 + N * 2000
    # (Optionally clamp: spispeed_khz = min(spispeed_khz, 32000))
    print(f"[boot] SPI speed from DIP: {spispeed_khz} kHz", flush=True)

    # Create flash worker and APPLY the chosen speed
    flash = FlashromWorker(leds)
    flash.spispeed = spispeed_khz

    # Startup animation then idle
    startup_animation()
    selector.set(0)
    inputs.start(on_check_pressed, on_write_pressed)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        print("[boot] Ready. CHECK = scan, WRITE = read+clone.", flush=True)
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_and_exit(0)
