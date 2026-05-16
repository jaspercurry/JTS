"""Shared test setup.

Intentionally empty. The previous Pi-side dep stub (`camilladsp`) is
no longer needed: `jasper.camilla`, `jasper.audio_io`, and `jasper.wake`
all lazy-import their Pi-side runtime deps inside the methods that
actually use them, so test modules can `import jasper.camilla` (etc.)
on a dev machine that doesn't have those packages in its venv.

Kept as a marker for future shared fixtures or pytest hooks.
"""
