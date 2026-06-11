# Runbook: 2026-06-10 fix-batch hardware validation

> **Status: executed; archived 2026-06-11.** One-time validation
> runbook for the 2026-06-10 audited fix batch (PRs #558, #563, #567,
> #569, #571), executed 2026-06-11 on the jts3 lab Pi at main
> `d27fae67` — boot-loop guard tripped and re-armed across two
> reboots, watchdog conversion and new doctor checks verified green.
> Full evidence (journal lines, drop-in lists, `/state` snapshots,
> doctor output) lives in the
> [PR #573 execution comment](https://github.com/jaspercurry/JTS/pull/573#issuecomment-4683638459).
> Preserved as the record of what was validated and how; specific
> facts below will drift. Current operational truth lives in
> [HANDOFF-resilience.md](../HANDOFF-resilience.md) and AGENTS.md.

The batch is fully merged and CI-green. CI cannot exercise real
systemd, ALSA, or the watchdog — per AGENTS.md, "green CI means safe
to merge, not validated on hardware." Three changes are systemd-
behavior claims that need this pass: the boot-loop guard, the
jasper-control watchdog conversion, and jasper-voice `MemoryHigh`.

## 1. Deploy

```sh
git checkout main && git pull
bash scripts/deploy-to-pi.sh
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor'
```

Expect green on the new checks: boot-loop guard (armed), daily spend
cap (real value, or "disabled" if set to 0), DTLN engine state, web
design assets.

## 2. Verify the new resilience surfaces

```sh
curl -s http://jts.local:8780/state | jq .resilience.bootloop_guard
ssh pi@jts.local 'journalctl -u jasper-bootloop-guard | grep event=bootloop_guard'
# expect event=bootloop_guard.ok on this boot

# control-plane watchdog armed (Type=notify conversion):
ssh pi@jts.local 'systemctl status jasper-control | grep -i watchdog'
```

Functional pass (jasper-control changed service type): dashboard at
http://jts.local/system/ loads, mic-mute chip toggles, volume
responds.

## 3. Boot-loop guard experiment (no units harmed; one extra reboot)

Replace the guard history with two synthetic recent boot timestamps so
the next boot looks like the third inside the window. Do not append to
the live file: unrelated recent real boots would remain in the window
and can keep the guard tripped longer than this experiment intends.

```sh
ssh pi@jts.local 'set -e; \
  state=/var/lib/jasper/bootloop_guard_boots; \
  now=$(date +%s); \
  sudo install -d -m 755 /var/lib/jasper; \
  if sudo test -f "$state"; then \
    sudo cp -a "$state" "$state.runbook-backup.$now"; \
  fi; \
  printf "%s runbook-fake1\n%s runbook-fake2\n" "$((now-300))" "$((now-150))" \
  | sudo tee "$state" >/dev/null'
ssh pi@jts.local sudo reboot
```

After it returns:

```sh
ssh pi@jts.local 'find /run/systemd/system -path "*/90-jts-bootloop-guard.conf" -print'
ssh pi@jts.local 'journalctl -b -u jasper-bootloop-guard | grep event=bootloop_guard.tripped'
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor' | grep -i bootloop
# doctor should WARN, naming the disarmed units
```

Before the re-arm reboot, replace the synthetic history with this boot
only. That leaves one prior boot in the next window, so the next boot
should be below the threshold and write no runtime drop-ins.

```sh
ssh pi@jts.local 'set -e; \
  state=/var/lib/jasper/bootloop_guard_boots; \
  now=$(date +%s); \
  boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo runbook-clean); \
  printf "%s %s\n" "$now" "$boot_id" | sudo tee "$state" >/dev/null'
ssh pi@jts.local sudo reboot
ssh pi@jts.local 'find /run/systemd/system -path "*/90-jts-bootloop-guard.conf" -print'
# expect no output from find
curl -s http://jts.local:8780/state | jq .resilience.bootloop_guard
ssh pi@jts.local 'journalctl -b -u jasper-bootloop-guard | grep event=bootloop_guard.ok'
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor' | grep -i bootloop
# doctor should be green/armed for the boot-loop guard
```

## 4. Generate the Pi constraints file (closes the supply-chain gap)

Start from a clean tree. If `git status --short` shows unrelated
changes, stop and move them out of the constraints branch before
committing.

```sh
git status --short
bash scripts/generate-pi-constraints.sh
git add -N deploy/constraints-pi.txt 2>/dev/null || true
git diff -- deploy/constraints-pi.txt
git status --short -- deploy/constraints-pi.txt
if [[ -z "$(git status --short -- deploy/constraints-pi.txt)" ]]; then
  echo "No constraints change; nothing to commit."
else
  branch="deps/pi-constraints-$(date +%Y%m%d-%H%M%S)"
  git switch -c "$branch"
  git add deploy/constraints-pi.txt
  git commit -m "deps: pin Pi runtime via on-device constraints" -- \
    deploy/constraints-pi.txt
  git push -u origin "$branch"
  gh pr create --draft --fill --base main --head "$branch"
fi
```

After that follow-up branch merges, subsequent deploys pin every
transitive Python dep to the validated set; regenerate deliberately
after intentional upgrades. If `gh` is not authenticated, open the
same branch as a draft PR from the GitHub web UI.

## 5. Record evidence

Before marking the batch validated, paste or attach the evidence in
the PR/handoff thread:

- local `git rev-parse HEAD` and Pi `/var/lib/jasper/build.txt`
- `jasper-doctor` output before and after the boot-loop experiment
- `/state.resilience.bootloop_guard` before trip, while tripped, and
  after re-arm
- journal lines for `event=bootloop_guard.ok` and
  `event=bootloop_guard.tripped`
- guarded drop-in list while tripped and empty drop-in list after
  re-arm
- `deploy/constraints-pi.txt` diff or the follow-up constraints PR

## 6. If anything misbehaves

```sh
bash scripts/fetch-pi-logs.sh    # then read logs/*-latest.*
```

Roll back a bad deploy by checking out the prior main SHA and
re-running `scripts/deploy-to-pi.sh`.

Prepared: 2026-06-10 (written at batch merge time, pre-hardware-run)
