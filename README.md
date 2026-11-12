# Disk Nap

[简体中文](README.zh-CN.md)

Disk Nap is an experimental macOS menu bar app for tracking external SSD power and testing automatic sleep and wake. The menu bar prototype observes a compatible SSD while it stays mounted. Its automatic sleep switch controls a separate APFS test image.

## Quick start

The menu bar app requires macOS 14 or later and the Xcode Command Line Tools. The command-line tools and test lab require Python 3.9 or later.

```sh
git clone https://github.com/grasscaograss/Disk-Nap.git
cd Disk-Nap
make menubar
open "build/Disk Nap.app"
```

Click the SSD icon in the menu bar. Disk Nap follows the system language by default. Use **Language** to choose English or Simplified Chinese (磁盘小憩). To try the UI with labeled sample data, launch `open -n "build/Disk Nap.app" --args --preview`.

### What the app shows

- **Overview** shows physical SSD idle time, measured sleep, and power. Click the main power number to switch between watts, sampled energy use, and estimated sleep savings. The lid card records the Mac's battery change and SSD sleep state around lid closure.
- **Sleep Strategy** estimates how long the SSD could have slept over the last seven days at the selected idle threshold. With Auto Sleep on, the card shows confirmed physical sleep time and the associated estimated energy savings. Click the savings figure to switch between Wh and an equivalent share of the Mac's full battery. The app shows a threshold recommendation when it has enough idle records and the suggested setting differs from the current one.

The app keeps up to seven days of observation records on the Mac. Power readings and battery percentages depend on the hardware and available samples. The USB wattage mapping has been verified on one port of the 13-inch M4 MacBook Air. Energy use includes only intervals between valid samples while the Mac is awake. The lid savings comparison requires valid battery sessions in both modes.

### Try Auto Sleep on a test image

Create the Python environment shown in [Scratch wake lab](#scratch-wake-lab), then build with its Python executable:

```sh
make menubar LAB_PYTHON=/tmp/mac-ssd-sleep-lab-venv/bin/python
open "build/Disk Nap.app" --args --show
```

The app starts in Observation Mode. Turn on **Auto Sleep** to test idle- and system-sleep-triggered detachment. A file request through **Open Test Folder** wakes the image at the same path. **Only on battery power** pauses the strategy on external power and restarts the idle timer after unplugging. The connected SSD remains mounted for read-only observation throughout this test.

## Command-line tools

Use a supported test volume to check compatibility and run a manual USB suspend cycle.

```sh
make
./mac-ssd-sleep probe "/Volumes/Your SSD"
./mac-ssd-sleep cycle "/Volumes/Your SSD" --seconds 25 --verify-io
```

`probe` is a read-only check of the mounted volume. `cycle` safely ejects the entire device and waits for the original volume to return after USB resume. Close files on the SSD before running it. Press Ctrl-C during the hold to resume early.

The optional `--verify-io` flag creates a scratch directory on the SSD, verifies an uncached read and a synced write after remount, and removes the directory. If recovery fails, reconnect the SSD and check Disk Utility.

## Observe idle time while mounted

```sh
caffeinate -i ./mac-ssd-sleep watch "/Volumes/Your SSD" --seconds 610
```

`watch` accepts the actual mount path of an APFS or exFAT volume on the supported SSD, including a custom mount outside `/Volumes`. It runs without the USB helper. For APFS it follows the volume's physical store and samples the underlying disk's cumulative read and write counts and bytes every five seconds. Any change, counter reset, or sampling gap greater than twice the interval restarts the timer. Stop with Ctrl-C or pass a duration.

`state=idle-threshold` means the physical counters stayed unchanged for the configured time (600 seconds by default). The monitor leaves the device mounted. The counters do not describe open files, pending writes, USB link state, or power.

## Supported setup

The prototype checks for a ZhiTai SPIRIT PREMIUM connected directly at 10 Gb/s. The eject and resume cycle supports a single mounted exFAT volume or a single APFS volume backed by that disk alone. Both filesystems were tested with the 1 TB model, firmware ZTB50002, and Apple silicon on macOS 27.2.

The controller uses a normal Disk Arbitration eject via `diskutil`, checks that no media on the target USB device remain mounted or open, then clears the USB configuration and requests suspend through IOKit. On resume it restores the prior configuration and re-enumerates the device if needed. It finds the volume by USB identity and volume UUID, so it can recover if the BSD disk number changes. APFS recovery verifies that the same volume returned to its original mount path.

## Power measurements

| Run | Mounted idle mean | USB U3 mean | Nonzero U3 samples below 0.1 W |
| --- | ---: | ---: | ---: |
| 25-second CLI cycle | 1.015 W | 0.085 W | 44 of 48 |
| Five-minute reference experiment | 1.084 W | 0.078 W | 490 of 525 |
| 120-second APFS CLI cycle | 1.057 W | 0.085 W | 174 of 215 |

The power estimates were logged separately from the CLI using the Mac's USB port sensors. The APFS run excluded the first five seconds of U3 from the power comparison. The remaining readings ranged from 0.057 to 0.142 W. The original APFS volume returned to its custom mount point and passed the scratch file read and write checks.

## APFS recovery

A disposable APFS disk image returned to the same custom mount point after its BSD disk identifier changed. Recovery located the volume by UUID, mounted its current device identifier, and verified an uncached read and a synced write. In a separate test, a process with its working directory on the volume blocked a normal unmount even while the process was idle.

## Scratch wake lab

The lab keeps a stable macOS WebDAV mount backed by a disposable APFS disk image. It detaches the image after 120 seconds without requests to the backing store or when macOS prepares to sleep. A file request reattaches the image at its private mount point. Root-only volume checks use local metadata. The lab defers detaching while a request is in progress.

```sh
make
python3 -m venv /tmp/mac-ssd-sleep-lab-venv
/tmp/mac-ssd-sleep-lab-venv/bin/python -m pip install 'WsgiDAV==4.3.4' 'cheroot>=10,<12'
lab_dir=$(mktemp -d)
/tmp/mac-ssd-sleep-lab-venv/bin/python -m ssd_sleep.lab "$lab_dir"
```

Use the printed `files` path to open the fixtures or create scratch files. The events log shows backing attachment, detachment and macOS power notifications. Press Ctrl-C to clean up the mounts. The lab listens only on loopback and exports only its disposable image. It exercises path stability and access-triggered wake without sending USB commands.

To use a physical USB SSD, pass `--usb-volume "/Volumes/Test SSD"` with the actual mount path of a supported APFS volume. The lab shares only a new scratch directory. It counts physical read and write activity toward the idle window, ejects the whole disk through Disk Arbitration, and holds USB suspend until a request needs the disk. On access or a normal stop, it verifies the volume UUID and restores the original mount path. Keep the lab's working directory on the internal drive. A production filesystem proxy still needs application-specific save, locking and metadata tests.

In a hardware run with the 120-second idle threshold, the USB port entered U3 about 126 seconds after the last observed physical I/O, including eject time. A read through the unchanged app path completed in about 17 seconds, including USB resume and APFS remount. A synced write, rename and readback through the same path passed.

In a separate lid-close run, macOS reported 47 seconds of clamshell sleep. The USB port reported U3 just before system sleep and again after system wake, before the first file request. The first save took about 28 seconds, including a remount retry and rename, and passed readback.

## Development

`make test` builds the USB helper and runs tests without attached hardware. See [CONTRIBUTING.md](CONTRIBUTING.md) for hardware reports.

Released under the [MIT License](LICENSE).
