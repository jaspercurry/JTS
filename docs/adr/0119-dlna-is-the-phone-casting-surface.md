# ADR-0119: DLNA/UPnP is the phone-casting surface; Google Cast is closed

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

"Send audio from my Android phone to the speaker" is a real gap: iPhones have
AirPlay, Android does not. The obvious answer is Google Cast, and it is not
available to us. Cast uses hardware-fused device authentication — the receiver
must cryptographically prove it holds a private key burned into genuine
Chromecast silicon and signed by Google's root CA — and every commercial sender
app enforces it through the Cast SDK.

No open-source project has solved this for phone-app-initiated audio. The
partial workarounds cover something else (Chrome tab mirroring), require keys
extracted from a physically rooted Chromecast, or use a private protocol phones
cannot discover. Google's certification programme for the real thing is
commercial-partner-only under NDA; there is no hobbyist path. Matter Casting is
an open standard with no phone senders and no Pi-class reference receiver — a
2027-and-later thing to watch, not a plan.

## Decision

The phone-casting surface is **DLNA/UPnP**: JTS advertises itself as a UPnP
Media Renderer, and any controller app (BubbleUPnP on Android, Windows "Play
To", and so on) can discover it and stream to it. It is a **network-only music
source alongside AirPlay, Spotify Connect, Bluetooth A2DP, and USB sink**, and
it takes the same shape as all of them: its own private snd-aloop fan-in lane
summed by `jasper-fanin`, latest-source-wins arbitration through `jasper-mux`,
and camilla-as-master volume with the sender's slider as an upstream trim.

Cast compatibility is a permanent non-goal, not a deferred one.

## Consequences

- Android users install a controller app. This is a real UX cost and the
  honest one: transparent "Cast icon → JTS" bridging does not exist in any
  direction that helps us.
- No JTS-side wizard is needed beyond the `/sources/` on/off toggle — the
  controller lives on the phone.
- Reusing the existing per-source lane pattern means mux, the volume
  coordinator, `/state`, doctor, and the sources wizard all gain DLNA by
  declaration rather than by new machinery.
- Video rendering, acting as a DLNA *server*, UPnP volume push-mode, and
  Snapcast/multi-room participation are all out of scope.
- AirConnect (virtual AirPlay endpoints forwarding to UPnP renderers) remains
  available later if iOS-side DLNA demand ever appears.
