import fcntl
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

DISKUTIL = "/usr/sbin/diskutil"
IOREG = "/usr/sbin/ioreg"
USB_CONTROL = Path(__file__).resolve().parents[1] / "build" / "usb-control"
USB_VENDOR = 0x21C4
USB_PRODUCT = 0x00C0
USB_LINK_SPEED = 10_000_000_000
USB_NAME = "SPIRIT PREMIUM"
WHOLE_DISK = re.compile(r"disk[0-9]+")
DISK_PARTITION = re.compile(r"disk[0-9]+s[0-9]+")


class SleepError(Exception):
    """The device or volume could not safely complete an operation."""


@dataclass(frozen=True)
class USBDevice:
    registry_id: int
    location: int
    serial: str = field(repr=False)
    whole_disk: str
    partitions: tuple[str, ...]


@dataclass(frozen=True)
class MountedVolume:
    mount_point: Path
    device: str
    uuid: UUID = field(repr=False)
    filesystem: str = "exfat"


@dataclass(frozen=True)
class Target:
    usb: USBDevice
    volume: MountedVolume


def children(node):
    return node.get("IORegistryEntryChildren", ())


def registry_nodes(node):
    yield node
    for child in children(node):
        yield from registry_nodes(child)


def registry(*args):
    output = subprocess.check_output([IOREG, "-a", "-l", *args], timeout=5)
    roots = plistlib.loads(output)
    return roots if isinstance(roots, list) else [roots]


def direct_usb_ids(roots):
    # The USB plane describes the physical path. A hub is a USB device
    # on that path, even when the I/O Service plane presents separate ports.
    def walk(node, below_device=False):
        usb_device = node.get("IOObjectClass") == "IOUSBHostDevice" or "idVendor" in node
        if usb_device and not below_device:
            yield node.get("IORegistryEntryID")
        for child in children(node):
            yield from walk(child, below_device or usb_device)

    return {entry for root in roots for entry in walk(root) if isinstance(entry, int)}


def whole_media(node):
    name = node.get("BSD Name", "")
    if node.get("Whole") is True and WHOLE_DISK.fullmatch(name):
        # An APFS container presents its own whole media below the physical disk.
        yield name
        return
    for child in children(node):
        yield from whole_media(child)


def find_supported_devices(usb_roots, service_roots):
    direct = direct_usb_ids(usb_roots)
    for node in service_roots:
        if (node.get("idVendor"), node.get("idProduct")) != (USB_VENDOR, USB_PRODUCT):
            continue
        if (
            node.get("IORegistryEntryID") not in direct
            or node.get("IORegistryEntryName") != USB_NAME
            or node.get("UsbLinkSpeed") != USB_LINK_SPEED
            or not isinstance(node.get("locationID"), int)
            or not isinstance(node.get("USB Serial Number"), str)
            or not node["USB Serial Number"]
        ):
            continue

        media = list(whole_media(node))
        if len(media) > 1:
            continue
        whole = media[0] if media else ""
        partitions = tuple(
            entry["BSD Name"]
            for entry in registry_nodes(node)
            if whole and entry.get("BSD Name", "").startswith(whole + "s")
            and DISK_PARTITION.fullmatch(entry["BSD Name"])
        )
        yield USBDevice(
            node["IORegistryEntryID"], node["locationID"], node["USB Serial Number"],
            whole, partitions,
        )


def devices():
    return list(find_supported_devices(
        registry("-p", "IOUSB"),
        registry("-p", "IOService", "-r", "-c", "IOUSBHostDevice"),
    ))


def disk_info(device_or_mount):
    result = subprocess.run(
        [DISKUTIL, "info", "-plist", str(device_or_mount)], capture_output=True, timeout=5,
    )
    return plistlib.loads(result.stdout) if result.returncode == 0 else {}


def matches_partition(info, usb, device):
    return (
        device in usb.partitions
        and info.get("DeviceIdentifier") == device
        and info.get("ParentWholeDisk") == usb.whole_disk
        and info.get("Internal") is False
        and info.get("Ejectable") is True
        and info.get("BusProtocol") == "USB"
        and info.get("FilesystemType") == "exfat"
        and bool(info.get("VolumeUUID"))
    )


def apfs_store(info):
    stores = info.get("APFSPhysicalStores", ())
    if len(stores) != 1 or not stores[0].get("APFSPhysicalStore"):
        raise SleepError("Expected an APFS volume backed by one physical store.")
    return stores[0]["APFSPhysicalStore"]


def apfs_volume_devices(usb):
    if not usb.partitions:
        return []
    listing = subprocess.check_output([DISKUTIL, "apfs", "list", "-plist"], timeout=5)
    volumes = []
    for container in plistlib.loads(listing)["Containers"]:
        stores = [store["DeviceIdentifier"] for store in container["PhysicalStores"]]
        if not set(usb.partitions).intersection(stores):
            continue
        if len(stores) != 1:
            raise SleepError("An APFS container spans multiple physical stores.")
        volumes.extend(container["Volumes"])
    return volumes


def probe(mount_point):
    if not mount_point.is_absolute() or mount_point == Path("/"):
        raise SleepError("Specify the absolute mount point of the external volume.")
    info = disk_info(mount_point)
    if info.get("MountPoint") != str(mount_point):
        raise SleepError("The path is not the root of a mounted volume.")
    volume = info.get("DeviceIdentifier")
    filesystem = info.get("FilesystemType")
    if filesystem == "apfs":
        if (info.get("Internal") is not False or info.get("BusProtocol") != "USB"
                or info.get("APFSSnapshot") is not False or not info.get("VolumeUUID")):
            raise SleepError("Expected an external USB APFS volume.")
        partition = apfs_store(info)
        candidates = [usb for usb in devices() if partition in usb.partitions]
    else:
        if mount_point.parent != Path("/Volumes"):
            raise SleepError("Specify the mounted exFAT volume directly under /Volumes.")
        partition = volume
        candidates = [usb for usb in devices() if matches_partition(info, usb, partition)]
    if len(candidates) != 1:
        raise SleepError("Expected one directly connected SPIRIT PREMIUM with an APFS or exFAT volume.")
    usb = candidates[0]
    if any(disk_info("/dev/" + name).get("MountPoint") for name in usb.partitions if name != partition):
        raise SleepError("Another volume on this physical disk is mounted.")
    volume_uuid = UUID(info["VolumeUUID"])
    if filesystem == "apfs":
        volumes = apfs_volume_devices(usb)
        if len(volumes) != 1 or volumes[0].get("DeviceIdentifier") != volume or (
            UUID(volumes[0]["APFSVolumeUUID"]) != volume_uuid
        ):
            raise SleepError("The APFS cycle requires exactly one APFS volume on the physical disk.")
    return Target(usb, MountedVolume(mount_point, volume, volume_uuid, filesystem))


def same_usb(expected):
    matches = [
        usb for usb in devices()
        if (usb.location, usb.serial) == (expected.location, expected.serial)
    ]
    if len(matches) > 1:
        raise SleepError("The USB device identity is ambiguous.")
    return matches[0] if matches else None


@contextmanager
def exclusive_cycle(location):
    lock = Path(tempfile.gettempdir()) / f"mac-ssd-sleep-{os.getuid()}-{location:x}.lock"
    descriptor = os.open(lock, os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR, 0o600)
    try:
        attributes = os.fstat(descriptor)
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != os.getuid():
            raise SleepError("The cycle lock is not owned by the current user.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SleepError("Another cycle is running for this USB port.") from None
        yield
    finally:
        os.close(descriptor)


def usb_arguments(operation, usb, target):
    return [
        str(USB_CONTROL), operation, str(usb.registry_id), str(usb.location), usb.serial,
        "/dev/" + target.usb.whole_disk, str(target.volume.mount_point),
    ]


def disk_command(operation, device, *, mount_point=None):
    arguments = [DISKUTIL, operation]
    if mount_point is not None:
        arguments.extend(["-mountPoint", str(mount_point)])
    result = subprocess.run(
        [*arguments, "/dev/" + device], capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise SleepError(message or f"diskutil {operation} failed.")


def restored_volume(usb, expected_uuid, filesystem):
    if filesystem == "apfs":
        volumes = [volume["DeviceIdentifier"] for volume in apfs_volume_devices(usb)
                   if UUID(volume["APFSVolumeUUID"]) == expected_uuid]
        if len(volumes) > 1:
            raise SleepError("Multiple APFS volumes have the expected volume UUID.")
        if volumes:
            device = volumes[0]
            info = disk_info("/dev/" + device)
            if info.get("VolumeUUID") and UUID(info["VolumeUUID"]) == expected_uuid:
                return device, Path(info["MountPoint"]) if info.get("MountPoint") else None
        return None, None
    matches = []
    for partition in usb.partitions:
        info = disk_info("/dev/" + partition)
        if matches_partition(info, usb, partition) and UUID(info["VolumeUUID"]) == expected_uuid:
            mount_point = Path(info["MountPoint"]) if info.get("MountPoint") else None
            matches.append((partition, mount_point))
    if len(matches) > 1:
        raise SleepError("Multiple partitions on the device have the expected volume UUID.")
    return matches[0] if matches else (None, None)


def remount_apfs(device, current_mount, volume):
    mount_point = volume.mount_point
    if mount_point.resolve() != mount_point:
        raise SleepError("The original mount path now resolves to a different directory.")
    mount_point.mkdir(mode=0o700, exist_ok=True)
    if any(mount_point.iterdir()):
        raise SleepError("The original mount point is not empty. Check the disk in Disk Utility.")
    if current_mount is not None:
        disk_command("unmount", device)
    try:
        disk_command("mount", device, mount_point=mount_point)
    except (SleepError, subprocess.TimeoutExpired):
        info = disk_info("/dev/" + device)
        if info.get("VolumeUUID") and UUID(info["VolumeUUID"]) == volume.uuid and not info.get("MountPoint"):
            disk_command("mount", device)
        raise


def restore(target):
    deadline = time.monotonic() + 25
    reenumerated = False
    mount_requested = set()
    while time.monotonic() < deadline:
        usb = same_usb(target.usb)
        if usb is not None:
            volume = target.volume
            device, mount_point = restored_volume(usb, volume.uuid, volume.filesystem)
            if mount_point and (volume.filesystem == "exfat" or mount_point == volume.mount_point):
                return MountedVolume(mount_point, device, volume.uuid, volume.filesystem)
            if device and (device, mount_point) not in mount_requested:
                if volume.filesystem == "apfs":
                    remount_apfs(device, mount_point, volume)
                else:
                    disk_command("mount", device)
                mount_requested.add((device, mount_point))
            if not usb.whole_disk and not reenumerated:
                # The original configuration may return before macOS recreates IOMedia.
                subprocess.run(usb_arguments("reenumerate", usb, target), check=True, timeout=8)
                reenumerated = True
        time.sleep(0.35)
    raise SleepError("The original volume did not return. Reconnect the SSD and check Disk Utility.")
