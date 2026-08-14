# Bundled `usb_turntable` package

This directory carries the reusable controller from
[`jaspercurry/USB-Turntable`](https://github.com/jaspercurry/USB-Turntable) so
the manual JTS3 experiment works offline. JTS does not maintain a separate copy
of the device protocol.

[`UPSTREAM.json`](UPSTREAM.json) records the reviewed upstream commit, package
version, source aggregate, and license. The JTS test suite recomputes that
aggregate as a development-time provenance check. The manual wrapper simply
loads this bundled package; it does not authenticate files at runtime or claim
to defend against a hostile local checkout. Review normal Git changes when
updating the snapshot.

The package requires Python 3.10 or newer and has no third-party runtime
dependencies. It owns discovery, serial transport, protocol parsing, heartbeat
recovery, operation completion, and timeout defaults.

The controller is licensed under Apache-2.0. Its exact [`LICENSE`](LICENSE) and
[`NOTICE.md`](NOTICE.md) are included here. The notice explains that upstream
vendor reference materials fall outside that grant; those materials are not
included in JTS.
