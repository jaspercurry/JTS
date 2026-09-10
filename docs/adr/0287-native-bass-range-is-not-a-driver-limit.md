# Native bass range is not a driver limit

Date: 2026-09-10

## Decision

The dynamic bass descriptor accepts the installed CamillaDSP Loudness filter's
documented boost range. `jasper/bass_extension/dynamic.py` owns this bound and
names it as a native software limit. The former 12 dB cap had no amplifier,
driver, enclosure or measured basis.

The empirical fitter remains bounded by its measured candidate. A larger
descriptor enables a trial; it does not prove clean output. The gain reserve,
final driver limiters, Main ceiling, declared driver caps and SPL stop retain
their existing owners and behavior. The shelf setting is not the gain at a
particular frequency; native replay measures the filter shape, volume taper
and net compressor effect before acoustic trials.

Source: [CamillaDSP v4.1.3 Loudness](https://github.com/HEnquist/camilladsp/blob/v4.1.3/README.md#loudness).
