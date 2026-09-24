# ADR-0357: The decay view reads reverberation from the kept impulse

- **Date:** 2026-09-24
- **Status:** Accepted
- **Context:** The toolbox had no decay reading (issue #5659): ADR-0355 left
  RT60, waterfall and spectrogram unbuilt. The only decay-like figure is the
  seat's 90–250 Hz early/late energy ratio (ADR-0325). Every take now keeps
  its impulse through 0.5 s past its sweep's scheduled start, with the
  pre-guard before it (ADR-0354).
- **Decision:**
  1. `decay` reads a take's kept impulse from its onset, octave by octave
     over the take's swept band: the Schroeder backward integral, and EDT
     (0 to −10 dB), T20 (−5 to −25 dB) and T30 (−5 to −35 dB), each scaled to
     60 dB (ISO 3382-1).
  2. Each band is filtered time-reversed, so the filter's own ringing falls
     before the sound instead of into its decay.
  3. A band's noise floor is its mean energy over the last tenth of the kept
     impulse. The integral stops where the band's decay line meets that
     floor, and the energy the decay would have carried past it is added back
     (Lundeby), so the floor neither lengthens nor cuts a decay time.
  4. A figure is reported only when the band's range above its noise reaches
     the figure's lower level plus 10 dB (EDT 20 dB, T20 35 dB, T30 45 dB),
     and its curve reaches that level before the crossing; otherwise it is
     `null`.
  5. The answer carries each band's figures, its range and where its decay
     met the noise; the artifact adds each band's Schroeder curve.
- **Consequences:** An agent can read how long each octave rings at a seat,
  and compare two takes, such as the rear stage on and off, without a new
  capture. Half a second of kept decay bounds what reads: a band whose decay
  is longer than about 0.6 s has no T30, and says so with `null`. Low octaves
  scatter more from take to take (half a second of a 63 Hz octave holds few
  independent samples), so a decay time there is read across repeats, not
  from one take. Not built: waterfall and spectrogram images; the per-octave
  decay carries the number a tuning decision needs. Rejected: keeping a
  longer impulse (more bytes on every take for decays a living room rarely
  holds past half a second), and noise subtraction without truncation (it
  leaves negative energy in the tail of the integral).
