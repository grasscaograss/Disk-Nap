import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ssd_sleep import idle, macos

MOUNT = Path("/Users/test/External Documents")
USB = macos.USBDevice(101, 42, "fixture-serial", "disk4", ("disk4s2",))
COUNTERS = (10, 20, 40960, 81920)


class IdleMonitorTests(unittest.TestCase):
    def test_apfs_mount_resolves_to_physical_usb_partition(self):
        info = {
            "MountPoint": str(MOUNT), "FilesystemType": "apfs", "DeviceIdentifier": "disk5s1",
            "APFSPhysicalStores": [{"APFSPhysicalStore": "disk4s2"}],
        }
        with patch.object(idle, "disk_info", return_value=info), patch.object(
            idle, "devices", return_value=[USB],
        ):
            self.assertEqual(idle.resolve_volume(MOUNT), (USB, "disk5s1", "apfs"))
            info["APFSPhysicalStores"].append({"APFSPhysicalStore": "disk9s1"})
            with self.assertRaisesRegex(macos.SleepError, "one physical store"):
                idle.resolve_volume(MOUNT)

    def test_registry_uses_physical_media_and_ignores_apfs_container(self):
        virtual_media = {"BSD Name": "disk5", "Whole": True}
        virtual_driver = {
            "IORegistryEntryID": 203, "IOObjectClass": "IOBlockStorageDriver",
            "IORegistryEntryChildren": [virtual_media],
            "Statistics": dict.fromkeys(idle.COUNTER_KEYS, 0),
        }
        physical_media = {
            "BSD Name": "disk4", "Whole": True,
            "IORegistryEntryChildren": [{"BSD Name": "disk4s2", "IORegistryEntryChildren": [virtual_driver]}],
        }
        physical_driver = {
            "IORegistryEntryID": 202, "IOObjectClass": "IOBlockStorageDriver",
            "IORegistryEntryChildren": [physical_media], "Statistics": dict(zip(idle.COUNTER_KEYS, COUNTERS)),
        }
        device = {
            "IOObjectClass": "IOUSBHostDevice", "IORegistryEntryID": USB.registry_id,
            "IORegistryEntryName": macos.USB_NAME, "idVendor": macos.USB_VENDOR, "idProduct": macos.USB_PRODUCT,
            "UsbLinkSpeed": macos.USB_LINK_SPEED, "locationID": USB.location, "USB Serial Number": USB.serial,
            "IORegistryEntryChildren": [physical_driver],
        }
        self.assertEqual(list(macos.find_supported_devices([device], [device])), [
            replace(USB, display_name=macos.USB_NAME),
        ])
        device.update({"USB Vendor Name": "Test Vendor", "USB Product Name": "Test Model"})
        self.assertEqual(list(macos.find_supported_devices([device], [device])), [
            replace(USB, display_name="Test Vendor Test Model"),
        ])
        self.assertEqual(idle.storage_sample(USB, [device]), (202, COUNTERS))
        device["IORegistryEntryID"] += 1
        with self.assertRaisesRegex(macos.SleepError, "original physical disk"):
            idle.storage_sample(USB, [device])

    def test_any_io_resets_quiet_time(self):
        for index in range(len(COUNTERS)):
            with self.subTest(counter=idle.COUNTER_KEYS[index]):
                timer = idle.IdleTimer(max_gap=10)
                self.assertEqual(timer.observe(0, 202, COUNTERS), (0, "baseline"))
                self.assertEqual(timer.observe(5, 202, COUNTERS), (5, "quiet"))
                changed = tuple(value + (i == index) for i, value in enumerate(COUNTERS))
                self.assertEqual(timer.observe(10, 202, changed), (0, "io"))
                self.assertEqual(timer.observe(15, 202, changed), (5, "quiet"))

    def test_counter_reset_or_missing_samples_cannot_count_as_idle(self):
        timer = idle.IdleTimer(max_gap=10)
        timer.observe(0, 202, COUNTERS)
        self.assertEqual(timer.observe(20, 202, COUNTERS), (0, "sample-gap"))
        self.assertEqual(timer.observe(25, 203, COUNTERS), (0, "counter-reset"))
        self.assertEqual(timer.observe(30, 203, (0, 0, 0, 0)), (0, "counter-reset"))
        self.assertEqual(timer.observe(35, 203, (0, 0, 0, 0)), (5, "quiet"))
