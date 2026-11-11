import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from ssd_sleep import cli, macos

UUID_VALUE = UUID("2de8d17e-4214-4704-98cb-e0f72a272bb4")
MOUNT = Path("/Volumes/Test SSD")
USB = macos.USBDevice(101, 42, "fixture-serial", "disk4", ("disk4s2",))
TARGET = macos.Target(USB, macos.MountedVolume(MOUNT, "disk4s2", UUID_VALUE))


def volume_info(partition, uuid=UUID_VALUE, mount_point=MOUNT):
    return {
        "DeviceIdentifier": partition,
        "ParentWholeDisk": partition.rsplit("s", 1)[0],
        "Internal": False,
        "Ejectable": True,
        "BusProtocol": "USB",
        "FilesystemType": "exfat",
        "VolumeUUID": str(uuid),
        "MountPoint": str(mount_point) if mount_point else "",
    }


class DeviceSafetyTests(unittest.TestCase):
    def test_hub_path_refuses_matching_device(self):
        device = {
            "IOObjectClass": "IOUSBHostDevice",
            "IORegistryEntryID": 101,
            "IORegistryEntryName": "SPIRIT PREMIUM",
            "idVendor": 0x21C4,
            "idProduct": 0x00C0,
            "UsbLinkSpeed": 10_000_000_000,
            "locationID": 42,
            "USB Serial Number": "fixture-serial",
        }
        hub = {"IOObjectClass": "IOUSBHostDevice", "IORegistryEntryChildren": [device]}
        self.assertEqual(list(macos.find_supported_devices([hub], [device])), [])
        self.assertEqual(len(list(macos.find_supported_devices([device], [device]))), 1)

    def test_additional_mounted_volume_refuses_probe(self):
        usb = replace(USB, partitions=("disk4s2", "disk4s3"))
        with patch.object(macos, "devices", return_value=[usb]), patch.object(
            macos, "disk_info", side_effect=[volume_info("disk4s2"), volume_info("disk4s3")],
        ):
            with self.assertRaisesRegex(macos.SleepError, "Another volume"):
                macos.probe(MOUNT)

    def test_recovery_mounts_matching_uuid_after_disk_renumbering(self):
        usb = replace(USB, registry_id=102, whole_disk="disk9", partitions=("disk9s2", "disk9s3"))
        unrelated = replace(usb, serial="different-serial", whole_disk="disk8", partitions=("disk8s2",))
        renamed_mount = Path("/Volumes/Test SSD 1")
        with patch.object(macos, "devices", return_value=[unrelated, usb]), patch.object(
            macos, "disk_info", side_effect=[
                volume_info("disk9s2", uuid=UUID(int=1), mount_point=None),
                volume_info("disk9s3", mount_point=None),
                volume_info("disk9s2", uuid=UUID(int=1), mount_point=None),
                volume_info("disk9s3", mount_point=renamed_mount),
            ],
        ), patch.object(macos, "disk_command") as disk:
            restored = macos.restore(TARGET)
        disk.assert_called_once_with("mount", "disk9s3")
        self.assertEqual(restored, macos.MountedVolume(renamed_mount, "disk9s3", UUID_VALUE))

    def test_eject_refusal_prevents_usb_control(self):
        cycle = cli.Cycle(TARGET, 10, verify_io=False)
        with patch.object(cli, "probe", return_value=TARGET), patch.object(
            cli, "disk_command", side_effect=macos.SleepError("Disk is busy"),
        ), patch.object(cli, "restore", return_value=TARGET.volume), patch.object(
            cycle, "suspend",
        ) as suspend, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(macos.SleepError, "Disk is busy"):
                cycle.execute()
        suspend.assert_not_called()

    def test_parallel_cycle_cannot_acquire_same_port(self):
        with macos.exclusive_cycle(0xFFF123):
            with self.assertRaisesRegex(macos.SleepError, "Another cycle"):
                with macos.exclusive_cycle(0xFFF123):
                    self.fail("Acquired an already locked port")
