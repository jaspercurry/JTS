# ADR-0350: Agents change settings through the owner function and jasper-settings

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** LLM agents operate this repo, and they changed the voice
  provider, its model and the wake word with three laptop scripts
  (`scripts/switch-voice-provider.sh`, `switch-gemini-model.sh`,
  `switch-wake-word.sh`). The scripts wrote the wizard-owned files as a
  second writer, skipped `restart_voice_daemon`'s gates (no provider
  selected, bonded follower) and the wizards' `event=voice.save` and
  `event=wake.model` lines, while the voice wizard itself wrote without the
  lock the scripts took. The owner decided to delete the scripts and first
  build the path they stood in for: one clear, documented contract, not the
  wizard pages (SWITCH-SCRIPTS on #5643).
- **Decision:**
  1. Each settings file has one owner function, in the module that already
     owns the file: `jasper.voice.provider_state.select_voice` writes the
     provider and model in `voice_provider.env`, and
     `jasper.wake_models.select_wake_model` writes `JASPER_WAKE_MODEL` in
     `wake_model.env`. It validates, writes under the file's lock at the
     wizard's mode, names its module in the file header, and emits the
     setting's `event=` line with `via=wizard|cli`. `select_voice` accepts a
     model the wizard offers (the catalog's or one its Refresh discovered) or
     the one already in effect, and refuses a provider whose API key no env
     file sets, checking only that it is set.
  2. Two front ends call it: the web wizard for people, and
     `jasper-settings` for agents, one verb per wizard page (`show`,
     `voice [--provider ID] [--model ID]`, `wake [--model KEY]`); with no
     flags a verb only reads. There is no HTTP API, no registry and no new
     auth.
  3. The CLI's stdout is one JSON document and its exit codes are
     ADR-0237's (0 answered, 1 refused, 2 unreadable, 3 not saved); its
     `--help` is the contract (ADR-0204). It runs on the Pi as
     `sudo /opt/jasper/.venv/bin/jasper-settings` and from a laptop as
     `ssh $PI_USER@$PI_HOST sudo /opt/jasper/.venv/bin/jasper-settings voice --provider openai`.
  4. API keys and OAuth never pass through the CLI. No key goes on argv
     (argv shows in `ps`, shell history and agent transcripts), `show`
     prints each key as `set` or `unset` only, and reconciler-resolved files
     are out of its scope.
  5. The restart gates apply to agents too. After a save the CLI calls
     `restart_voice_daemon`: a skipped restart answers exit 0 with
     `restart: "skipped"` and its reason, and a refused one exits 1 with
     `detail.saved: true`.
- **Consequences:** An agent reads the same choices and gets the same
  refusals as the page, and every change emits the page's `event=` line;
  the CLI's prints on the caller's stderr, and the journal records sudo's
  command line. On a bonded follower an agent's change is saved but voice
  does not restart.
  Scope: `voice` and `wake` now; `voice --voice`, `voice --barge-in` and
  `wake --threshold` next, in settings PR 2, which also moves
  `restart_voice_daemon` out of `web/` and so deletes the one
  `jasper/cli` → `jasper.web._common` row in
  `tests/test_audio_measurement_boundary_ssot.py`; `sources`, `tools` and
  `chat` optionally later. Rejected: a JSON HTTP API. jasper-control is
  non-root, outside the secrets compartment and bound to 0.0.0.0, and
  jasper-web's guard is browser CSRF and costs a 60–90 MB wake per call; the
  CLI starts the same short Python the scripts started, which ADR-0226
  allows for an on-demand run.
