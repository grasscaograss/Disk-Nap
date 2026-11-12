import subprocess
import time

from .macos import USB_NAME, SleepError, apfs_store, children, devices, disk_info, registry, registry_nodes

COUNTER_KEYS = ("Operations (Read)", "Operations (Write)", "Bytes (Read)", "Bytes (Write)")


def resolve_volume(mount_point):
    info = disk_info(mount_point)
    if info.get("MountPoint") != str(mount_point):
        raise SleepError("The path is not the root of a mounted volume.")
    filesystem = info.get("FilesystemType")
    volume = info.get("DeviceIdentifier")
    if filesystem == "apfs":
        partition = apfs_store(info)
    elif filesystem == "exfat":
        partition = volume
    else:
        raise SleepError("The idle monitor supports APFS and exFAT volumes.")

    matches = [usb for usb in devices() if partition in usb.partitions]
    if len(matches) != 1 or not volume:
        raise SleepError("Expected one directly connected SPIRIT PREMIUM.")
    return matches[0], volume, filesystem


def storage_sample(usb, roots):
    drivers = [
        node
        for root in roots if root.get("IORegistryEntryID") == usb.registry_id
        for node in registry_nodes(root)
        if node.get("IOObjectClass") == "IOBlockStorageDriver"
        and any(child.get("BSD Name") == usb.whole_disk and child.get("Whole") is True
                for child in children(node))
    ]
    if len(drivers) != 1:
        raise SleepError("The original physical disk is no longer visible.")
    driver = drivers[0]
    driver_id = driver.get("IORegistryEntryID")
    stats = driver.get("Statistics", {})
    counters = tuple(stats.get(key) for key in COUNTER_KEYS)
    if not isinstance(driver_id, int) or any(type(value) is not int or value < 0 for value in counters):
        raise SleepError("The physical disk's read/write counters are unavailable.")
    return driver_id, counters


class IdleTimer:
    def __init__(self, max_gap):
        self.max_gap = max_gap
        self.previous = None
        self.quiet_since = None

    def observe(self, now, driver_id, counters):
        reason = "baseline"
        if self.previous is not None:
            previous_time, previous_driver, previous_counters = self.previous
            if driver_id != previous_driver or any(
                current < last for current, last in zip(counters, previous_counters)
            ):
                reason = "counter-reset"
            elif now - previous_time > self.max_gap:
                reason = "sample-gap"
            elif counters != previous_counters:
                reason = "io"
            else:
                reason = "quiet"
        if reason != "quiet":
            self.quiet_since = now
        self.previous = now, driver_id, counters
        return now - self.quiet_since, reason


def watch(mount_point, idle_seconds, interval, seconds):
    usb, volume, filesystem = resolve_volume(mount_point)
    print(f"Watching {mount_point} ({filesystem}, {volume} on {usb.whole_disk}). "
          f"Idle threshold: {idle_seconds} s. Observation only.", flush=True)
    mount_prefix = f"/dev/{volume} on {mount_point} ({filesystem},"
    timer = IdleTimer(max_gap=2 * interval)
    started = time.monotonic()
    deadline = started + seconds if seconds is not None else None
    while True:
        mounts = subprocess.check_output(["/sbin/mount"], text=True, timeout=5)
        if not any(line.startswith(mount_prefix) for line in mounts.splitlines()):
            raise SleepError("The original volume is no longer mounted at the specified path.")
        driver_id, counters = storage_sample(usb, registry("-r", "-n", USB_NAME))
        now = time.monotonic()
        idle, reason = timer.observe(now, driver_id, counters)
        state = "idle-threshold" if idle >= idle_seconds else "monitoring"
        reads, writes, read_bytes, write_bytes = counters
        print(f"elapsed={now - started:.1f}s idle={idle:.1f}s state={state} sample={reason} "
              f"read_ops={reads} write_ops={writes} read_bytes={read_bytes} write_bytes={write_bytes}",
              flush=True)
        if deadline is not None and now >= deadline:
            return 0
        delay = interval if deadline is None else min(interval, deadline - now)
        time.sleep(delay)
