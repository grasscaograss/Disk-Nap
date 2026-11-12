import argparse
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from .fixture import IOFixture
from .idle import watch
from .macos import (
    USB_CONTROL,
    SleepError,
    disk_command,
    exclusive_cycle,
    probe,
    restore,
    same_usb,
    usb_arguments,
)


class Cycle:
    def __init__(self, target, seconds, verify_io):
        self.target = target
        self.seconds = seconds
        self.fixture = IOFixture() if verify_io else None
        self.helper = None
        self.cancelled = False
        self.recovered = None

    def wake(self, *_):
        self.cancelled = True
        if self.helper is not None:
            self.helper.stdin.close()

    def suspend(self, usb):
        arguments = usb_arguments("suspend", usb, self.target) + [str(self.seconds)]
        self.helper = subprocess.Popen(arguments, stdin=subprocess.PIPE, start_new_session=True)
        try:
            if self.cancelled:
                self.helper.stdin.close()
            try:
                status = self.helper.wait(timeout=self.seconds + 12)
            except subprocess.TimeoutExpired:
                self.helper.stdin.close()
                self.helper.wait(timeout=6)
                raise SleepError("The USB helper exceeded its suspend deadline.")
            if status and not self.cancelled:
                raise SleepError(f"The USB helper failed with exit code {status}.")
        finally:
            self.helper.stdin.close()

    def run(self):
        if not USB_CONTROL.is_file() or not os.access(USB_CONTROL, os.X_OK):
            raise SleepError("Run make to build the USB helper.")
        if probe(self.target.volume.mount_point) != self.target:
            raise SleepError("The target disk changed before the cycle.")
        if self.cancelled:
            return self.target.volume
        if self.fixture:
            self.fixture.create(self.target.volume)
            if probe(self.target.volume.mount_point) != self.target:
                raise SleepError("The target disk changed before eject.")
        if self.cancelled:
            return self.target.volume

        try:
            print("Ejecting the physical disk through Disk Arbitration.", flush=True)
            disk_command("eject", self.target.usb.whole_disk)
            if not self.cancelled:
                usb = same_usb(self.target.usb)
                if usb is None:
                    raise SleepError("The USB device disappeared after eject.")
                self.suspend(usb)
        finally:
            if self.helper is not None and self.helper.poll() is None:
                raise SleepError("The USB helper is still running. Wait for it to exit before reconnecting the SSD.")
            start = time.monotonic()
            self.recovered = restore(self.target)
            print(f"Verified the original volume at {self.recovered.mount_point} "
                  f"in {time.monotonic() - start:.2f} s after USB control.", flush=True)
        return self.recovered

    def clean_fixture(self):
        if not self.fixture or not self.fixture.created:
            return
        volume = self.recovered or self.target.volume
        try:
            current = probe(volume.mount_point)
        except SleepError:
            current = None
        if current and current.volume.uuid == self.target.volume.uuid and (
            current.usb.serial, current.usb.location
        ) == (self.target.usb.serial, self.target.usb.location):
            self.fixture.clean(current.volume)
        else:
            print(f"Remove the scratch directory {self.fixture.name} after reconnecting "
                  "the original SSD.", file=sys.stderr)

    def execute(self):
        handlers = {
            number: signal.signal(number, self.wake) for number in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            with exclusive_cycle(self.target.usb.location):
                try:
                    volume = self.run()
                    if self.fixture and not self.cancelled:
                        self.fixture.verify(volume)
                        print("Verified uncached reads and a synced write after remount.", flush=True)
                finally:
                    self.clean_fixture()
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
        return 130 if self.cancelled else 0


def main():
    parser = argparse.ArgumentParser(description="Experimental USB SSD sleep and wake on macOS.")
    commands = parser.add_subparsers(dest="command", required=True)
    probe_command = commands.add_parser("probe", help="check whether a mounted volume is supported")
    cycle_command = commands.add_parser("cycle", help="eject, suspend, resume, and remount the volume")
    watch_command = commands.add_parser("watch", help="observe physical SSD I/O without ejecting it")
    for command in (probe_command, cycle_command):
        command.add_argument("volume", type=Path, help="absolute path to the volume mount point")
    watch_command.add_argument("volume", type=Path, help="absolute path to the mounted APFS or exFAT volume")
    watch_command.add_argument("--idle-seconds", type=int, default=600, help="quiet time threshold (default: 600)")
    watch_command.add_argument("--interval", type=int, default=5, help="sampling interval in seconds (default: 5)")
    watch_command.add_argument("--seconds", type=int, help="observation duration, otherwise runs until Ctrl-C")
    cycle_command.add_argument("--seconds", type=int, default=10, help="suspend duration (1–600)")
    cycle_command.add_argument("--verify-io", action="store_true",
                               help="verify read/write using a disposable directory on the target")
    args = parser.parse_args()

    if sys.platform != "darwin":
        parser.exit(1, "disk-nap requires macOS.\n")
    if args.command == "cycle" and not 1 <= args.seconds <= 600:
        parser.error("--seconds must be between 1 and 600")
    try:
        if args.command == "watch":
            if min(args.idle_seconds, args.interval) <= 0 or (args.seconds is not None and args.seconds <= 0):
                parser.error("watch durations must be positive")
            try:
                return watch(args.volume, args.idle_seconds, args.interval, args.seconds)
            except KeyboardInterrupt:
                return 130
        target = probe(args.volume)
        if args.command == "probe":
            print(json.dumps({
                "device": target.usb.display_name,
                "mount_point": str(target.volume.mount_point),
                "whole_disk": target.usb.whole_disk,
                "filesystem": target.volume.filesystem,
                "link_gbps": 10,
            }, indent=2))
            return 0
        return Cycle(target, args.seconds, args.verify_io).execute()
    except (SleepError, OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError) as error:
        # Subprocess exceptions may include the USB serial in their argument vector.
        if isinstance(error, subprocess.TimeoutExpired):
            error = SleepError("A macOS command or the USB helper timed out.")
        elif isinstance(error, subprocess.CalledProcessError):
            error = SleepError(f"A macOS command or the USB helper exited with status {error.returncode}.")
        print(f"disk-nap: {error}", file=sys.stderr)
        return 1
