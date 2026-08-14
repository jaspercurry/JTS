# Vendored `usb_turntable` snapshot

This directory is an exact source snapshot of the reusable `usb_turntable`
package from the
[`jaspercurry/USB-Turntable`](https://github.com/jaspercurry/USB-Turntable)
repository. JTS vendors it because JTS3 cannot install from a private Git URL
without credentials. JTS must not maintain an independent copy of the device
protocol.

[`UPSTREAM.json`](UPSTREAM.json) records the upstream merged commit, package
version, exact file set, every file's SHA-256, and the aggregate digest. The JTS
adapter hard-codes the canonical manifest's raw SHA-256 before trusting any of
those fields, then rejects every unlisted package artifact before import. A
source-plus-manifest rewrite therefore fails closed unless the adapter's trust
root is deliberately updated. Update this tree only by replacing it from the
upstream package, regenerating the manifest, and updating that trust root.

The package uses only the Python standard library and requires Python 3.10 or
newer.

## License and redistribution boundary

The upstream Python controller is licensed under Apache-2.0. Its exact
[`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md) are included here. The notice
also records that the upstream repository's third-party vendor reference
materials are outside that grant; those materials are not included in JTS.
