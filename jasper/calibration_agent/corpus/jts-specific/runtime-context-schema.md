# Runtime Context Schema

> **Status: proposed schema.** This file defines the public shape for
> private per-install tuning context. Actual values belong under
> `/var/lib/jasper/...`, not in repo docs.

## Purpose

Future tuning sessions should remember useful context with explicit
user consent: room notes, gear, preferred sound, and prior feedback.
That memory should be inspectable and editable, but not committed to
the repository.

## Proposed Path

`/var/lib/jasper/correction/runtime_context.json`

## Proposed Shape

```json
{
  "schema_version": 1,
  "updated_at": 1779667200,
  "rooms": [
    {
      "id": "living-room",
      "display_name": "Living room",
      "dimensions_m": {
        "length": null,
        "width": null,
        "height": null
      },
      "speaker_placement": {
        "notes": "",
        "distance_from_front_wall_m": null,
        "distance_from_side_wall_m": null
      },
      "listening_positions": [
        {
          "id": "couch",
          "display_name": "Couch",
          "notes": ""
        }
      ]
    }
  ],
  "gear": {
    "speakers": "",
    "amplifier": "",
    "measurement_mics": []
  },
  "preferences": {
    "default_target": "neutral",
    "notes": "",
    "approved_descriptors": []
  },
  "agent_notes": [
    {
      "created_at": 1779667200,
      "source_session_id": "",
      "note": "",
      "confirmed_by_user": true
    }
  ]
}
```

## Rules

- Agent-authored notes require confirmation before persistence.
- The UI must show this context before using it in advice.
- Session bundles may reference runtime context by hash/version, but
  should not silently export private room notes unless the user asks
  for a debug bundle that includes them.
- Raw microphone serial numbers should not be stored here; calibration
  records use serial hashes.

Last verified: 2026-05-25
