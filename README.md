# Mac SSD Sleep

Mac SSD Sleep is an experimental macOS tool for suspending a directly connected USB SSD while the Mac is awake. The current prototype runs an explicit eject, suspend, resume, and remount cycle.

## Quick start

Install the Xcode Command Line Tools and use Python 3.9 or newer.

```sh
git clone https://github.com/grasscaograss/mac-ssd-sleep.git
cd mac-ssd-sleep
make
./mac-ssd-sleep probe "/Volumes/Your SSD"
./mac-ssd-sleep cycle "/Volumes/Your SSD" --seconds 25 --verify-io
```

`probe` is a read-only check of the mounted volume. `cycle` safely ejects the entire device and waits for the original volume to return after USB resume. Close files on the SSD before running it. Press Ctrl-C during the hold to resume early.

The optional `--verify-io` flag creates a scratch directory on the SSD, verifies an uncached read and a synced write after remount, and removes the directory. If recovery fails, reconnect the SSD and check Disk Utility.

## Supported setup

The prototype checks for a ZhiTai SPIRIT PREMIUM connected directly at 10 Gb/s, with one mounted exFAT volume on the physical disk. It was tested with the 1 TB model, firmware ZTB50002, and Apple silicon on macOS 27.2. Other firmware versions and configurations need separate hardware validation.

The controller uses a normal Disk Arbitration eject via `diskutil`, then clears the USB configuration and requests suspend through IOKit. On resume it restores the prior configuration and re-enumerates the device if needed. It finds the volume by USB identity and volume UUID, so it can recover if the BSD disk number changes.

## Power measurements

| Run | Mounted idle mean | USB U3 mean | Nonzero U3 samples below 0.1 W |
| --- | ---: | ---: | ---: |
| 25-second CLI cycle | 1.015 W | 0.085 W | 44 of 48 |
| Five-minute reference experiment | 1.084 W | 0.078 W | 490 of 525 |

The power estimates were logged separately from the CLI using the Mac's USB port sensors. The CLI test confirmed USB port suspension and verified the original volume and scratch file after resume.

## Next milestone

Prototype an FSKit folder backed by a scratch directory on the SSD. Serve directory listings and attributes from the Mac, then resume the SSD for uncached reads and writes. Test Finder browsing, application saves, and idle power before using the folder for everyday files.

## Development

`make test` builds the USB helper and runs tests without attached hardware. See [CONTRIBUTING.md](CONTRIBUTING.md) for hardware reports.

Released under the [MIT License](LICENSE).
