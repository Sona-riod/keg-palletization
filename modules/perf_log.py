# modules/perf_log.py
"""
[PERF] log: measured numbers for camera efficiency and heat, written to the
app log every PERF_LOG_INTERVAL_S seconds (the first block after
PERF_LOG_FIRST_S, so a short test run still gets one).

    [PERF] camera: requested 30 fps | driver reports 30.0 fps | delivered 12.4 fps | format YUYV 3840x2160 | per frame: wait 78.1 ms + convert 9.2 ms
    [PERF] frames: 744 captured, 462 used (62%), 282 unused (38%) | preview 15.0/s, resize 4.1 ms
    [PERF] detection: 462 rounds = 7.7/s (active 400, idle 62) | 132 ms/round
    [PERF] system: CPU 43% (load 2.10) | CPU clock 1.73/2.20 GHz (79%) | GPU 61% | temp cpu 58.2 C, gpu 56.1 C, tj 59.0 C | fan 48% | app memory 1.9 GB

Everything is measured, nothing assumed:
  - "requested" is CAMERA_CONFIG['fps'], "driver reports" is what OpenCV
    says the device accepted, "delivered" is frames actually counted.
  - "wait" is time blocked until the camera hands over a frame (cap.grab);
    "convert" is the time to turn it into a 4K picture (cap.retrieve). The
    convert time is the CPU cost of every frame, used or not.
  - "used" counts distinct frames that detection (or a capture) took;
    "unused" were converted and replaced before anyone looked at them.
  - CPU clock below its maximum while hot means the Jetson is throttling.
Values the hardware does not expose (e.g. no fan, no GPU load file on a dev
PC) are left out of the line rather than guessed.

Summarise a log with: python tools/perf_summary.py logs/app.log

Producers (camera.py, workers.DetectWorker) call add()/inc()/set_info(); they
cost a lock and an addition. The reporter is one daemon thread.
"""

import glob
import logging
import os
import threading
import time

log = logging.getLogger("perf")

_lock = threading.Lock()
_sums = {}        # name -> [total, count, max]
_counts = {}      # name -> int
_info = {}        # name -> latest value (static facts: format, requested fps...)
_first = {}       # name -> time of the first count this interval (rates use it)
_started = False


# Producers
def add(name, value):
    """Record one measurement (e.g. a time in ms); reported as avg / max."""
    with _lock:
        s = _sums.get(name)
        if s is None:
            _sums[name] = [value, 1, value]
        else:
            s[0] += value
            s[1] += 1
            if value > s[2]:
                s[2] = value


def inc(name, n=1):
    """Count an event; reported per interval."""
    with _lock:
        if name not in _counts:
            _first[name] = time.time()
        _counts[name] = _counts.get(name, 0) + n


def set_info(name, value):
    """Remember a fact (latest value wins)."""
    with _lock:
        _info[name] = value


def _take():
    """Snapshot and reset the interval counters."""
    global _sums, _counts, _first
    with _lock:
        sums, counts, info, first = _sums, _counts, dict(_info), _first
        _sums, _counts, _first = {}, {}, {}
    return sums, counts, info, first


def _avg(sums, name):
    s = sums.get(name)
    return (s[0] / s[1]) if s and s[1] else None


# System readers (Jetson sysfs; each returns None when not available)
def _read(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _thermal():
    """{zone name: C} for the CPU / GPU / SoC zones."""
    out = {}
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        name = (_read(zone + "/type") or "").lower().replace("-thermal", "")
        temp = _read(zone + "/temp")
        if not name or temp is None:
            continue
        if any(k in name for k in ("cpu", "gpu", "tj", "soc")):
            try:
                out[name] = int(temp) / 1000.0
            except ValueError:
                pass
    return out


def _cpu_clock():
    """(average current GHz, average max GHz) over online cores."""
    cur, mx = [], []
    for cpu in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
        c, m = _read(cpu + "/scaling_cur_freq"), _read(cpu + "/cpuinfo_max_freq")
        if c and m:
            cur.append(int(c))
            mx.append(int(m))
    if not cur:
        return None
    return sum(cur) / len(cur) / 1e6, sum(mx) / len(mx) / 1e6


_GPU_LOAD = None
_FAN_PWM = None


def _find_once():
    global _GPU_LOAD, _FAN_PWM
    if _GPU_LOAD is None:
        cands = (["/sys/devices/gpu.0/load", "/sys/devices/platform/gpu.0/load"]
                 + glob.glob("/sys/devices/platform/*ga10b*/load")
                 + glob.glob("/sys/devices/platform/*/*ga10b*/load")
                 + glob.glob("/sys/devices/platform/*.gpu/load"))
        _GPU_LOAD = next((p for p in cands if os.path.exists(p)), "")
    if _FAN_PWM is None:
        cands = (glob.glob("/sys/devices/platform/pwm-fan/hwmon/hwmon*/pwm1")
                 + glob.glob("/sys/class/hwmon/hwmon*/pwm1"))
        _FAN_PWM = next((p for p in cands if os.path.exists(p)), "")


def _gpu_load():
    v = _read(_GPU_LOAD) if _GPU_LOAD else None
    try:
        return int(v) / 10.0 if v is not None else None   # tegra reports 0..1000
    except ValueError:
        return None


def _fan():
    v = _read(_FAN_PWM) if _FAN_PWM else None
    try:
        return int(v) * 100.0 / 255.0 if v is not None else None
    except ValueError:
        return None


_last_cpu = None


def _cpu_percent():
    """Whole-machine CPU busy % since the previous call (from /proc/stat)."""
    global _last_cpu
    line = _read("/proc/stat")
    if not line:
        return None
    parts = [int(x) for x in line.splitlines()[0].split()[1:]]
    idle, total = parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)
    prev, _last_cpu = _last_cpu, (idle, total)
    if prev is None or total == prev[1]:
        return None
    return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))


def _app_memory_gb():
    for line in (_read("/proc/self/status") or "").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024 / 1024
    return None


# Reporter
def _report(start, now):
    sums, counts, info, first = _take()

    def span(name):
        """Seconds this counter was actually running in the interval: from its
        first event (e.g. the camera starting mid-interval), not the interval
        start, so a rate is never diluted by time before it began."""
        return max(now - max(start, first.get(name, start)), 1e-6)

    def rate(name):
        return counts.get(name, 0) / span(name)

    # camera
    captured = counts.get("camera.frames", 0)
    if not captured and not counts.get("detect.rounds"):
        log.info("[PERF] camera: not running yet")
        return
    parts = [f"requested {info.get('camera.requested_fps', '?')} fps"]
    if info.get("camera.driver_fps") is not None:
        parts.append(f"driver reports {info['camera.driver_fps']:.1f} fps")
    active = span("camera.frames")
    parts.append(f"delivered {rate('camera.frames'):.1f} fps"
                 + (f" (camera ran {active:.0f} of {now - start:.0f} s)"
                    if active < 0.9 * (now - start) else ""))
    if info.get("camera.format"):
        parts.append(f"format {info['camera.format']}")
    wait, conv = _avg(sums, "camera.wait_ms"), _avg(sums, "camera.convert_ms")
    copy = _avg(sums, "camera.copy_ms")
    if wait is not None and copy is not None:
        # Raw mode: every frame is only copied; conversions happen on use.
        n_conv = sums.get("camera.convert_ms", [0, 0])[1]
        parts.append(f"per frame: wait {wait:.1f} ms + copy {copy:.1f} ms | convert "
                     f"{conv or 0:.1f} ms on {n_conv / active:.1f}/s used frames")
    elif wait is not None and conv is not None:
        parts.append(f"per frame: wait {wait:.1f} ms + convert {conv:.1f} ms")
    log.info("[PERF] camera: %s", " | ".join(parts))

    # frames
    used = counts.get("camera.frames_used", 0)
    unused = max(captured - used, 0)
    pct = (lambda n: 100.0 * n / captured) if captured else (lambda n: 0.0)
    line = (f"{captured} captured, {used} used ({pct(used):.0f}%), "
            f"{unused} unused ({pct(unused):.0f}%)")
    prev_n = counts.get("camera.previews", 0)
    if prev_n:
        line += (f" | preview {rate('camera.previews'):.1f}/s, resize "
                 f"{_avg(sums, 'camera.preview_ms') or 0:.1f} ms")
    small = _avg(sums, "camera.detect_small_ms")
    if small is not None:
        line += f" | detection input from raw {small:.1f} ms"
    log.info("[PERF] frames: %s", line)

    # detection
    rounds = counts.get("detect.rounds", 0)
    if rounds:
        log.info("[PERF] detection: %d rounds = %.1f/s (active %d, idle %d) | %.0f ms/round",
                 rounds, rate("detect.rounds"), counts.get("detect.rounds_active", 0),
                 counts.get("detect.rounds_idle", 0), _avg(sums, "detect.round_ms") or 0)

    # system
    parts = []
    cpu = _cpu_percent()
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    if cpu is not None:
        parts.append(f"CPU {cpu:.0f}%" + (f" (load {load:.2f})" if load is not None else ""))
    clk = _cpu_clock()
    if clk:
        parts.append(f"CPU clock {clk[0]:.2f}/{clk[1]:.2f} GHz ({100 * clk[0] / clk[1]:.0f}%)")
    gpu = _gpu_load()
    if gpu is not None:
        parts.append(f"GPU {gpu:.0f}%")
    temps = _thermal()
    if temps:
        parts.append("temp " + ", ".join(f"{k} {v:.1f} C" for k, v in temps.items()))
    fan = _fan()
    if fan is not None:
        parts.append(f"fan {fan:.0f}%")
    mem = _app_memory_gb()
    if mem is not None:
        parts.append(f"app memory {mem:.2f} GB")
    if parts:
        log.info("[PERF] system: %s", " | ".join(parts))


def _loop(first, interval):
    _find_once()
    _cpu_percent()                      # prime the CPU % baseline
    _take()                             # drop anything from before the start
    last = time.time()
    time.sleep(first)
    while True:
        now = time.time()
        try:
            _report(last, now)
        except Exception:
            log.debug("[PERF] report failed", exc_info=True)
        last = now
        time.sleep(interval)


def start():
    """Start the reporter thread once (no-op if PERF_LOG_ENABLED is False)."""
    global _started
    try:
        import config
        if not getattr(config, "PERF_LOG_ENABLED", True):
            return
        interval = float(getattr(config, "PERF_LOG_INTERVAL_S", 60))
        first = float(getattr(config, "PERF_LOG_FIRST_S", 15))
    except Exception:
        interval, first = 60.0, 15.0
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(first, interval), name="PerfLog",
                     daemon=True).start()
    log.info("[PERF] performance log every %.0f s (first after %.0f s)", interval, first)
