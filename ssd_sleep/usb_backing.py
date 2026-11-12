"""Disposable filesystem backing on a directly attached USB SSD."""

import os
import plistlib
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID

from .cli import Cycle
from .idle import storage_sample
from .macos import USB_NAME, SleepError, probe, registry, restore, usb_arguments


class HoldCycle(Cycle):
    """Resume and restore the volume when the controller's pipe closes."""

    def suspend(self, usb):
        self.helper = subprocess.Popen(
            usb_arguments("suspend", usb, self.target) + ["0"], stdin=sys.stdin.buffer,
        )
        status = self.helper.wait()
        if status and not self.cancelled:
            raise SleepError(f"The USB helper exited with status {status}.")

    def wake(self, *_):
        self.cancelled = True
        if self.helper is not None and self.helper.poll() is None:
            self.helper.terminate()


def hold(mount, expected_uuid, expected_registry_id):
    target = probe(mount)
    if target.volume.uuid != expected_uuid or target.usb.registry_id != expected_registry_id:
        raise SleepError("The selected USB volume changed before the hold.")
    if select.select([sys.stdin], [], [], 0)[0]:
        return 0
    return HoldCycle(target, 0, False).execute()


class USBBacking:
    def __init__(self, root, target, log):
        self.target = target
        self.log = log
        self.metadata = root / "metadata"
        self.metadata.mkdir()
        self.trace = root / "usb-control.log"
        self.worker = None
        self.previous_sample = None
        self.mount = Path(tempfile.mkdtemp(prefix=".mac-ssd-sleep-lab-", dir=target.volume.mount_point))
        self.check_access()
        self.refresh_metadata()

    def refresh_metadata(self):
        attributes = self.mount.stat()
        os.utime(self.metadata, ns=(attributes.st_atime_ns, attributes.st_mtime_ns))

    def check_access(self):
        if self.worker is not None:
            self.wake()
        node = Path("/dev") / self.target.volume.device
        if self.mount.is_symlink() or self.mount.stat().st_dev != node.stat().st_rdev:
            raise SleepError("The scratch directory is not on the original USB volume.")

    def sample_activity(self):
        if self.worker is not None:
            self.wake()
        try:
            sample = storage_sample(self.target.usb, registry("-r", "-n", USB_NAME))
        except plistlib.InvalidFileException as error:
            raise SleepError("The physical USB drive is no longer visible.") from error
        changed = sample != self.previous_sample
        if changed:
            self.log("physical-activity", counters=sample[1])
        self.previous_sample = sample
        return changed

    def sleep(self, reason):
        self.check_access()
        self.refresh_metadata()
        args = [sys.executable, "-u", "-m", "ssd_sleep.usb_backing",
                str(self.target.volume.mount_point), str(self.target.volume.uuid), str(self.target.usb.registry_id)]
        with self.trace.open("a") as output:
            offset = output.tell()
            self.worker = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
        deadline = time.monotonic() + 13
        with self.trace.open() as output:
            output.seek(offset)
            received = ""
            while time.monotonic() < deadline and self.worker.poll() is None:
                received += output.read()
                if "suspend_usb=0x00000000" in received:
                    self.log("backing-suspended", reason=reason)
                    return
                time.sleep(0.1)
        self.wake()
        raise SleepError("The USB controller did not confirm suspend. Check the USB trace.")

    def wake(self):
        if self.worker is None:
            return
        start = time.monotonic()
        self.worker.stdin.close()
        status = self.worker.wait(timeout=40)
        if status:
            self.log("restore-retry", worker_status=status)
            restore(self.target)
        target = probe(self.target.volume.mount_point)
        if target.volume.uuid != self.target.volume.uuid or (
            target.usb.location, target.usb.serial
        ) != (self.target.usb.location, self.target.usb.serial):
            raise SleepError("The original USB volume did not return.")
        self.target = target
        self.worker = None
        self.previous_sample = None
        self.log("backing-mounted", volume=target.volume.device, recovery_seconds=round(time.monotonic() - start, 3))

    def detach(self):
        self.check_access()
        shutil.rmtree(self.mount)


if __name__ == "__main__":
    try:
        raise SystemExit(hold(Path(sys.argv[1]), UUID(sys.argv[2]), int(sys.argv[3])))
    except SleepError as error:
        print(f"USB hold failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"USB hold failed: {type(error).__name__}.", file=sys.stderr)
        raise SystemExit(1)
