# ADR-0278: AirPlay session release belongs to source takeover

- **Date:** 2026-09-09
- **Status:** Accepted

## Context

JTS3 stopped AirPlay, went idle, then selected USB without calling
`DropSession`: mux only preempted renderers reporting playback. Shairport can
keep its principal connection after playback stops. Resetting Shairport
restored connection, but the retained session's role in that failure was not
proved.

## Decision

After a successful automatic takeover by another source, mux calls the
existing receiver release even when AirPlay is idle or mux has no playback
history. This bounded operation holds the source transition lock, after the
audible gate moves. A later selection cannot overtake it. Idle, manual pins,
and failed handoffs do not release sessions. No background retry is added.

`airplay_session` owns the outcome. `/state.source_selection` and doctor
project it. A native acknowledgement means Shairport completed `stop_play`;
the MPRIS Stop fallback does not prove release. An absent receiver is normal
and must not be activated for cleanup. Calls remain bounded by the shared
D-Bus runner. Shairport's method has no session identifier: the mux lock
orders local selections, but cannot make a sender reconnect atomic with it.
