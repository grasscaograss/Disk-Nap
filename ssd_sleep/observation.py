"""Read-only USB observation for the menu bar prototype."""

import argparse
import json
import os
import plistlib
import re
import selectors
import signal
import statistics
import subprocess
import time
from bisect import bisect_right
from pathlib import Path

from .idle import IdleTimer, storage_sample
from .macos import (
    USB_CONTROL, USB_NAME, SleepError, apfs_volume_devices, children, devices,
    disk_info, probe, registry, same_usb,
)

DAY = 86400
EJECT_ALLOWANCE = 5
SAMPLE_SECONDS = 5
PRESETS = (2, 5, 10, 20, 30)
OBSERVATION_ERRORS = (OSError, SleepError, ValueError, subprocess.SubprocessError)


def overlap(window, start):
    return max(0, window["end"] - max(start, window["start"]))


def replay(windows, minutes=range(1, 121)):
    eligible = sorted((w["end"] - w["start"], w["ioEnded"]) for w in windows
                      if w["end"] - w["start"] > 60 + EJECT_ALLOWANCE)
    lengths = [length for length, _ in eligible]
    seconds = [0] * (len(eligible) + 1)
    wakes = [0] * (len(eligible) + 1)
    for index in range(len(eligible) - 1, -1, -1):
        seconds[index] = seconds[index + 1] + lengths[index]
        wakes[index] = wakes[index + 1] + eligible[index][1]
    forecasts = []
    for minute in minutes:
        minimum = 60 * minute + EJECT_ALLOWANCE
        first = bisect_right(lengths, minimum)
        available = seconds[first] - minimum * (len(eligible) - first)
        forecasts.append({"minutes": minute, "seconds": round(available), "wakes": wakes[first]})
    return forecasts


def recommend(windows, forecasts):
    completed = [w for w in windows if w["ioEnded"] and w["end"] - w["start"] > 120 + EJECT_ALLOWANCE]
    if len(completed) < 2:
        return None
    candidates = [f for f in forecasts if f["minutes"] in PRESETS]
    most_sleep = max(f["seconds"] for f in candidates)
    near_best = [f for f in candidates if f["seconds"] >= most_sleep * 0.9]
    return min(near_best, key=lambda f: (f["wakes"], -f["seconds"], f["minutes"]))["minutes"]


def battery_readout(battery):
    charge, capacity = battery.get("CurrentCapacity"), battery.get("MaxCapacity")
    percent = round(100 * charge / capacity) if (
        type(charge) is int and type(capacity) is int and 0 <= charge <= capacity and capacity > 0
    ) else None
    return percent, battery.get("ExternalConnected") is False


def battery_full_wh(model, battery):
    # Rated energy for Mac16,12 (https://support.apple.com/122209), adjusted for current full-charge capacity.
    if model != "Mac16,12":
        return None
    data = battery.get("BatteryData")
    if not isinstance(data, dict):
        return None
    full, design = data.get("AppleRawMaxCapacity"), data.get("DesignCapacity")
    if type(full) is not int or type(design) is not int or full <= 0 or design <= 0:
        return None
    return 53.8 * full / design


def port_watts(model, port, battery):
    # This pairing is verified on the pilot MacBook Air. Other ports still report I/O and U3.
    if model != "Mac16,12" or port.get("IORegistryEntryName") != "usb-drd0-port-ss" or (
        port.get("locationID") != 0x200000
    ):
        return None
    readings = [item for item in battery.get("PowerOutDetails", ()) if item.get("PortIndex") == 1]
    if len(readings) != 1:
        return None
    mv, ma = readings[0].get("AdapterVoltage"), readings[0].get("Current")
    if type(mv) is not int or type(ma) is not int or mv <= 0 or ma <= 0:
        return None
    return mv * ma / 1_000_000


def system_sleep_times(output):
    values = re.findall(r"sec\s*=\s*(\d+)\s*,\s*usec\s*=\s*(\d+)", output)
    if len(values) != 2:
        return None
    return tuple(int(sec) + int(usec) / 1_000_000 for sec, usec in values)


def lid_comparison(records):
    rates = {"observe": [], "managed": []}
    lengths = []
    for record in records:
        mode = record.get("mode")
        before, after = record["before"], record["after"]
        hours = (record["end"] - record["start"]) / 3600
        if mode in rates and record["onBattery"] and record.get("sameDevice") and hours >= 1 and before is not None and (
            after is not None and after <= before
        ):
            rates[mode].append((before - after) / hours)
            lengths.append(hours)
    if min(len(group) for group in rates.values()) < 2:
        return None
    reference = statistics.median(lengths)
    difference = statistics.median(rates["observe"]) - statistics.median(rates["managed"])
    return {"percentPoints": difference * reference, "hours": reference,
            "observeCount": len(rates["observe"]), "managedCount": len(rates["managed"])}


class History:
    def __init__(self, path):
        self.path = path
        data = json.loads(path.read_text()) if path.is_file() else {}
        self.windows = data.get("windows", [])
        self.u3 = data.get("u3", [])
        self.power = data.get("power", [])
        self.energy = data.get("energy", [])
        self.lids = data.get("lids", [])

    def add_energy(self, start, end, watts):
        while start < end:
            minute = int(start // 60) * 60
            stop = min(end, minute + 60)
            duration = stop - start
            if not self.energy or self.energy[-1]["minute"] != minute:
                self.energy.append({"minute": minute, "start": start, "end": start, "seconds": 0, "wh": 0})
            bucket = self.energy[-1]
            bucket["start"] = min(bucket["start"], start)
            bucket["end"] = max(bucket["end"], stop)
            bucket["seconds"] += duration
            bucket["wh"] += watts * duration / 3600
            start = stop

    def energy_since(self, start):
        wh = seconds = 0
        for bucket in self.energy:
            fraction = overlap(bucket, start) / (bucket["end"] - bucket["start"])
            wh += fraction * bucket["wh"]
            seconds += fraction * bucket["seconds"]
        return (wh if seconds else None), round(seconds)

    def save(self, now):
        start = now - 7 * DAY
        self.windows = [item for item in self.windows if item["end"] >= start and (
            item["end"] >= now - DAY or item["end"] - item["start"] > 60 + EJECT_ALLOWANCE
        )]
        self.u3 = [item for item in self.u3 if item["end"] >= start]
        self.power = [item for item in self.power if item["time"] >= start]
        self.energy = [item for item in self.energy if item["end"] >= now - DAY]
        self.lids = [item for item in self.lids if item["end"] >= start]
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({
            "windows": self.windows, "u3": self.u3, "power": self.power, "energy": self.energy, "lids": self.lids,
        }, separators=(",", ":")))
        temp.replace(self.path)


def attached_port(usb):
    for port in registry("-r", "-c", "AppleUSBHostPort", "-d", "2"):
        if any(child.get("IORegistryEntryID") == usb.registry_id for child in children(port)):
            return port
    return None


def power_snapshot():
    return next(iter(registry("-r", "-c", "AppleSmartBattery", "-d", "1")), {})


def mean_watts(samples, phase, since):
    values = [s["watts"] for s in samples if s["phase"] == phase and s["time"] >= since]
    return statistics.mean(values) if values else None


def is_u3(port):
    status = (port or {}).get("port-status")
    return bool(status & 0x2000) if type(status) is int else None


def find_target(mount):
    if mount is not None:
        return probe(mount)
    matches = []
    for usb in devices():
        for volume in apfs_volume_devices(usb):
            path = disk_info("/dev/" + volume["DeviceIdentifier"]).get("MountPoint")
            if path:
                matches.append(Path(path))
    if len(matches) != 1:
        raise SleepError("Connect one supported, directly attached SSD with a mounted APFS volume.")
    return probe(matches[0])


class Observation:
    def __init__(self, target, history, model, interval):
        self.target, self.history, self.model = target, history, model
        self.timer = IdleTimer(max_gap=2 * interval)
        self.u3_start = self.last_sample = self.last_power = None
        self.last_watts = None
        self.power_phase = None
        self.pending_sleep = None
        self.connected = True
        self.quiet = None

    def close_quiet(self, io_ended=False):
        if self.timer.previous:
            end = self.timer.previous[0]
            if end > self.timer.quiet_since:
                self.history.windows.append({
                    "start": self.timer.quiet_since, "end": end, "ioEnded": io_ended,
                })
        self.timer = IdleTimer(max_gap=self.timer.max_gap)
        self.quiet = None

    def close_u3(self):
        if self.u3_start is not None and self.last_sample is not None and self.last_sample > self.u3_start:
            self.history.u3.append({"start": self.u3_start, "end": self.last_sample})
        self.u3_start = None

    def stop_samples(self):
        self.close_quiet()
        self.close_u3()
        self.last_sample = None
        self.last_watts = None

    def sample(self, now):
        port = attached_port(self.target.usb)
        if port is None:
            self.connected = False
            self.stop_samples()
            return self.snapshot(now, None, None)
        self.connected = True
        suspended = is_u3(port)
        if self.last_sample is not None and (
            now <= self.last_sample or now - self.last_sample > self.timer.max_gap
        ):
            self.stop_samples()
        if suspended:
            self.close_quiet()
            if self.u3_start is None:
                self.u3_start = now
        else:
            self.close_u3()
            try:
                driver, counters = storage_sample(self.target.usb, registry("-r", "-n", USB_NAME))
            except OBSERVATION_ERRORS:
                self.close_quiet()
            else:
                previous, quiet_since = self.timer.previous, self.timer.quiet_since
                self.quiet, reason = self.timer.observe(now, driver, counters)
                if reason != "quiet" and previous and previous[0] > quiet_since:
                    self.history.windows.append({
                        "start": quiet_since, "end": previous[0], "ioEnded": reason == "io",
                    })
        battery = power_snapshot()
        watts = port_watts(self.model, port, battery)
        if self.last_sample is not None and self.last_watts is not None and watts is not None:
            self.history.add_energy(self.last_sample, now, (self.last_watts + watts) / 2)
        self.last_watts = watts
        phase = "u3" if suspended else "idle" if self.quiet and self.quiet >= SAMPLE_SECONDS else None
        if phase and watts is not None and (
            self.last_power is None or phase != self.power_phase or now - self.last_power >= 30
        ):
            self.history.power.append({"time": now, "phase": phase, "watts": watts})
            self.last_power, self.power_phase = now, phase
        self.last_sample = now
        return self.snapshot(now, watts, suspended, battery_full_wh(self.model, battery))

    def will_sleep(self, now):
        self.stop_samples()
        self.pending_sleep = None
        percent, on_battery = battery_readout(power_snapshot())
        self.pending_sleep = (now, percent, on_battery, is_u3(attached_port(self.target.usb)))

    def did_wake(self, now):
        pending, self.pending_sleep = self.pending_sleep, None
        if pending is None:
            return
        before, start_charge, start_on_battery, u3 = pending
        root = next(iter(registry("-r", "-c", "IOPMrootDomain", "-d", "1")), {})
        times = system_sleep_times(subprocess.check_output(
            ["/usr/sbin/sysctl", "kern.sleeptime", "kern.waketime"], text=True, timeout=2,
        ))
        if root.get("Last Sleep Reason") != "Clamshell Sleep" or not times or not (
            before - 1 <= times[0] < times[1] <= now + 1
        ):
            return
        end_charge, end_on_battery = battery_readout(power_snapshot())
        original_attached = same_usb(self.target.usb) is not None
        self.history.lids.append({
            "start": times[0], "end": times[1], "before": start_charge, "after": end_charge,
            "onBattery": start_on_battery and end_on_battery, "u3BeforeSleep": u3, "mode": "observe",
            "sameDevice": original_attached,
        })

    def u3_duration(self, now, since):
        duration = sum(overlap(window, since) for window in self.history.u3)
        if self.u3_start is not None:
            duration += overlap({"start": self.u3_start, "end": now}, since)
        return round(duration)

    def snapshot(self, now, watts, suspended, full_charge_wh=None):
        since = now - 7 * DAY
        windows = [{**w, "start": max(w["start"], since)} for w in self.history.windows if w["end"] >= since]
        if self.quiet:
            windows.append({"start": max(self.timer.quiet_since, since), "end": now, "ioEnded": False})
        forecasts = replay(windows)
        power = self.history.power
        idle_7d = mean_watts(power, "idle", since)
        u3_7d = mean_watts(power, "u3", since)
        savings_watts = max(0, idle_7d - u3_7d) if idle_7d is not None and u3_7d is not None else None
        lids = [r for r in self.history.lids if r["end"] >= now - 7 * DAY]
        energy, coverage = self.history.energy_since(now - DAY)
        return {
            "state": "observing" if self.connected else "waiting",
            "device": self.target.usb.display_name,
            "mount": str(self.target.volume.mount_point),
            "sampleTime": now, "quietSeconds": self.quiet, "inU3": suspended, "watts": watts,
            "idleWatts": mean_watts(power, "idle", now - DAY),
            "u3Watts": mean_watts(power, "u3", now - DAY),
            "sleepSavingsWatts7d": savings_watts, "batteryFullWh": full_charge_wh,
            "energyWh24h": energy, "powerCoverageSeconds": coverage,
            "quiet24hSeconds": round(sum(overlap(w, now - DAY) for w in windows)),
            "u3Seconds": self.u3_duration(now, now - DAY), "u3Seconds7d": self.u3_duration(now, since),
            "windowCount": len(windows),
            "forecasts": forecasts, "recommendation": recommend(windows, forecasts),
            "lids": lids, "lidComparison": lid_comparison(lids),
        }


def emit(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def run(mount, directory, interval, seconds):
    directory.mkdir(parents=True, exist_ok=True)
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True, timeout=2).strip()
    listener = subprocess.Popen(
        [str(USB_CONTROL.with_name("power-events"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    stop = False

    def request_stop(*_):
        nonlocal stop
        stop = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)
    observer = None
    read_buffer = b""
    sleeping = False
    last_save = 0
    next_sample = 0
    deadline = time.monotonic() + seconds if seconds else None
    try:
        with selectors.DefaultSelector() as events:
            events.register(listener.stdout, selectors.EVENT_READ)
            while not stop and (deadline is None or time.monotonic() < deadline):
                if events.select(min(interval, max(0, next_sample - time.monotonic())) if not sleeping else interval):
                    data = os.read(listener.stdout.fileno(), 4096)
                    if not data:
                        raise SleepError("The macOS sleep event listener stopped.")
                    read_buffer += data
                    while b"\n" in read_buffer:
                        event, read_buffer = read_buffer.split(b"\n", 1)
                        now = time.time()
                        if event == b"will-sleep":
                            sleeping = True
                            try:
                                if observer and observer.connected:
                                    observer.will_sleep(now)
                            except OBSERVATION_ERRORS:
                                observer.pending_sleep = None
                            finally:
                                listener.stdin.write(b"1")
                                listener.stdin.flush()
                        elif event == b"did-wake":
                            sleeping = False
                            if observer:
                                try:
                                    observer.did_wake(now)
                                except OBSERVATION_ERRORS:
                                    observer.pending_sleep = None
                                observer.history.save(now)
                            next_sample = 0
                if sleeping or time.monotonic() < next_sample:
                    continue
                now = time.time()
                next_sample = time.monotonic() + interval
                try:
                    if observer is None or not observer.connected:
                        target = find_target(mount)
                        path = directory / f"{target.volume.uuid}-{target.usb.location:x}.json"
                        observer = Observation(target, History(path), model, interval)
                    snapshot = observer.sample(now)
                    emit(snapshot)
                    if not observer.connected or now - last_save >= 60:
                        observer.history.save(now)
                        last_save = now
                except OBSERVATION_ERRORS as error:
                    if observer:
                        observer.stop_samples()
                    emit({"state": "waiting", "message": str(error)})
    finally:
        listener.stdin.close()
        try:
            listener.wait(timeout=3)
        except subprocess.TimeoutExpired:
            listener.terminate()
            listener.wait(timeout=3)
        if observer:
            observer.stop_samples()
            observer.history.save(time.time())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", type=Path)
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=SAMPLE_SECONDS)
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    if args.interval < 1 or (args.seconds is not None and args.seconds < 1):
        parser.error("Observation durations must be positive.")
    try:
        run(args.mount, args.data_directory, args.interval, args.seconds)
    except OBSERVATION_ERRORS as error:
        emit({"state": "error", "message": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
