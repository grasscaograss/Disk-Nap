# Contributing

Run `make test` before opening a pull request. Changes to the USB cycle should include the device model, firmware, macOS version, filesystem, and whether the drive was connected directly.

For a hardware issue, include the `probe` result and the last successful operation in the `cycle` output. Remove private volume names, serial numbers, and UUIDs from logs. If possible, reproduce using a scratch volume and report whether it returned after the test.
