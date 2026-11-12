"""A scratch APFS lab for automatic sleep and wake."""

import argparse
import json
import os
import plistlib
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .macos import USB_CONTROL, SleepError, apfs_store, disk_info, probe
from .policy import AccessGate
from .usb_backing import USBBacking

HDIUTIL = "/usr/bin/hdiutil"
POWER_EVENTS = USB_CONTROL.with_name("power-events")
BACKING_ERRORS = (OSError, SleepError, subprocess.SubprocessError)


def log(event, **details):
    print(json.dumps({"event": event, "time": time.time(), **details}), flush=True)


class DiskImage:
    def __init__(self, root):
        self.image = root / "test.sparseimage"
        self.mount = root / "backing"
        self.mount.mkdir()
        self.metadata = root / "metadata"
        self.metadata.mkdir()
        self.device = None
        self.uuid = None
        subprocess.run(
            [HDIUTIL, "create", "-size", "128m", "-fs", "APFS", "-volname",
             "SSD Wake Lab", "-type", "SPARSE", str(self.image)],
            check=True, capture_output=True, timeout=15,
        )

    def wake(self):
        if self.device is not None:
            raise SleepError("The lab image is already attached.")
        self.mount.mkdir(exist_ok=True)
        output = subprocess.check_output(
            [HDIUTIL, "attach", "-nobrowse", "-mountpoint", str(self.mount),
             "-plist", str(self.image)], timeout=12,
        )
        entities = plistlib.loads(output)["system-entities"]
        self.device, = [entry["dev-entry"] for entry in entities
                        if entry.get("content-hint") == "GUID_partition_scheme"]
        try:
            info = disk_info(self.mount)
            stores = {entry["dev-entry"].removeprefix("/dev/") for entry in entities}
            if (info.get("MountPoint") != str(self.mount) or info.get("FilesystemType") != "apfs"
                    or apfs_store(info) not in stores or not info.get("VolumeUUID")
                    or self.uuid not in (None, info["VolumeUUID"])):
                raise SleepError("The mounted lab image did not match its recorded identity.")
        except (SleepError, subprocess.SubprocessError):
            self.detach()
            raise
        self.uuid = info["VolumeUUID"]
        self.refresh_metadata()
        log("backing-mounted", volume=info["DeviceIdentifier"])

    def refresh_metadata(self):
        attributes = self.mount.stat()
        os.utime(self.metadata, ns=(attributes.st_atime_ns, attributes.st_mtime_ns))

    def detach(self):
        if self.device is not None:
            subprocess.run([HDIUTIL, "detach", self.device], check=True, capture_output=True, timeout=12)
            self.device = None
            self.mount.mkdir(exist_ok=True)

    def sleep(self, reason):
        self.refresh_metadata()
        self.detach()
        log("backing-unmounted", reason=reason)


def gated_webdav(provider, metadata, gate):
    def serve(environ, start_response):
        method, path, depth = environ["REQUEST_METHOD"], environ["PATH_INFO"], environ.get("HTTP_DEPTH")
        # Root-only volume checks can use the local metadata directory.
        if path == "/" and (method == "OPTIONS" or (method == "PROPFIND" and depth == "0")):
            yield from metadata(environ, start_response)
            return
        try:
            with gate.access() as woke:
                if woke:
                    log("request-woke-backing", method=method, path=path, depth=depth)
                response = provider(environ, start_response)
                try:
                    yield from response
                finally:
                    if hasattr(response, "close"):
                        response.close()
        except BACKING_ERRORS as error:
            log("access-deferred", error=str(error))
            start_response("503 Service Unavailable", [("Content-Length", "0")], sys.exc_info())
    return serve


def handle_power_events(process, gate):
    for line in process.stdout:
        event = line.strip()
        if event == "will-sleep":
            log("mac-will-sleep")
            try:
                suspended = gate.will_sleep()
                log("system-sleep-handled", suspended=suspended, backing_offline=gate.asleep)
            except BACKING_ERRORS as error:
                log("system-sleep-deferred", error=str(error))
            finally:
                try:
                    process.stdin.write("1")
                    process.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
        elif event == "did-wake":
            gate.did_wake()
            log("mac-did-wake")


def monitor_idle(stop, gate, interval):
    while not stop.wait(interval):
        try:
            gate.idle_check()
        except BACKING_ERRORS as error:
            log("idle-sleep-deferred", error=str(error))


def run(root, idle_seconds, usb_volume=None):
    from cheroot.wsgi import Server
    from wsgidav.wsgidav_app import WsgiDAVApp

    if usb_volume is not None:
        target = probe(usb_volume)
        if target.volume.filesystem != "apfs":
            raise SleepError("Use an APFS volume for the USB scratch test.")
        backing = USBBacking(root, target, log)
        gate = AccessGate(
            backing, idle_seconds, sample_activity=backing.sample_activity, check_access=backing.check_access,
        )
    else:
        backing = DiskImage(root)
        gate = AccessGate(backing, idle_seconds)
    mount = root / "files"
    mount.mkdir()
    stop = threading.Event()
    server = power = None
    mounted = False
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        backing.wake()
        (backing.mount / "idle-read.txt").write_text("idle wake fixture\n")
        (backing.mount / "lid-read.txt").write_text("system sleep wake fixture\n")
        options = {
            "simple_dc": {"user_mapping": {"*": True}},
            "dir_browser": {"enable": False},
            "verbose": 0,
        }
        provider = WsgiDAVApp({**options, "provider_mapping": {"/": str(backing.mount)}})
        metadata = WsgiDAVApp({**options, "provider_mapping": {"/": str(backing.metadata)}})
        server = Server(("127.0.0.1", 0), gated_webdav(provider, metadata, gate))
        server.prepare()
        threading.Thread(target=server.serve, daemon=True).start()
        port = server.socket.getsockname()[1]
        subprocess.run(
            ["/sbin/mount_webdav", "-S", "-o", "nobrowse",
             f"http://127.0.0.1:{port}/", str(mount)], check=True, timeout=15,
        )
        mounted = True
        power = subprocess.Popen([str(POWER_EVENTS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        if not select.select([power.stdout], [], [], 5)[0] or power.stdout.readline().strip() != "ready":
            raise SleepError("Could not start the macOS sleep and wake listener.")
        threading.Thread(target=handle_power_events, args=(power, gate), daemon=True).start()
        idle = threading.Thread(target=monitor_idle, args=(stop, gate, 3 if usb_volume else 0.5), daemon=True)
        idle.start()
        log("lab-ready", path=str(mount), backing_path=str(backing.mount), idle_seconds=idle_seconds,
            backing="usb" if usb_volume else "image")
        stop.wait()
        idle.join()
    finally:
        try:
            if power is not None:
                power.stdin.close()
                power.wait(timeout=20)
        finally:
            try:
                if mounted and os.path.ismount(mount):
                    subprocess.run(["/sbin/umount", str(mount)], check=True, timeout=10)
            finally:
                if server is not None:
                    server.stop()
                backing.detach()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="empty private directory on the Mac's internal drive")
    parser.add_argument("--idle-seconds", type=int, default=120)
    parser.add_argument("--usb-volume", type=Path, help="eject and resume this APFS volume for a USB scratch test")
    args = parser.parse_args()
    root = args.directory.resolve()
    if (not root.is_dir() or any(root.iterdir()) or root.stat().st_mode & 0o077
            or root.stat().st_dev != Path.home().stat().st_dev):
        parser.error("Use an empty, owner-only directory on the internal drive (for example, mktemp -d).")
    if args.idle_seconds < 1 or not POWER_EVENTS.is_file():
        parser.error("Use a positive idle threshold and run make to build the power listener.")
    try:
        run(root, args.idle_seconds, args.usb_volume)
    except (ImportError, *BACKING_ERRORS) as error:
        print(f"SSD wake lab: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
