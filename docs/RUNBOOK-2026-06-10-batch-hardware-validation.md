# Runbook: 2026-06-10 fix-batch hardware validation

> **Status: session artifact.** One-time validation runbook for the
> 2026-06-10 audited fix batch (PRs #558, #563, #567, #569, #571).
> Run once from the laptop checkout on the home LAN; afterwards this
> doc is historical. Current operational truth lives in
> [HANDOFF-resilience.md](HANDOFF-resilience.md) and AGENTS.md.

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

Seed two fake recent boot timestamps so the next boot looks like the
third inside the window:

```sh
ssh pi@jts.local 'now=$(date +%s); \
  printf "%s fake1\n%s fake2\n" $((now-300)) $((now-150)) \
  | sudo tee -a /var/lib/jasper/bootloop_guard_boots'
ssh pi@jts.local sudo reboot
```

After it returns:

```sh
ssh pi@jts.local 'ls /run/systemd/system/*.d/90-jts-bootloop-guard.conf'
ssh pi@jts.local 'journalctl -b -u jasper-bootloop-guard | grep tripped'
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor' | grep -i bootloop
# doctor should WARN, naming the disarmed units
```

One more clean reboot re-arms automatically (/run drop-ins are wiped
at boot; the stale fake entries age out of the 3600 s window):

```sh
ssh pi@jts.local sudo reboot
# then: drop-ins gone, doctor green, event=bootloop_guard.ok
```

## 4. Generate the Pi constraints file (closes the supply-chain gap)

```sh
bash scripts/generate-pi-constraints.sh
git diff -- deploy/constraints-pi.txt
if git diff --quiet -- deploy/constraints-pi.txt; then
  echo "No constraints change; nothing to commit."
else
  git switch -c deps/pi-constraints-2026-06-10
  git add deploy/constraints-pi.txt
  git commit -m "deps: pin Pi runtime via on-device constraints"
  git push -u origin deps/pi-constraints-2026-06-10
fi
```

Subsequent deploys pin every transitive Python dep to the validated
set; regenerate deliberately after intentional upgrades.

## 5. If anything misbehaves

```sh
bash scripts/fetch-pi-logs.sh    # then read logs/*-latest.*
```

Roll back a bad deploy by checking out the prior main SHA and
re-running `scripts/deploy-to-pi.sh`.

Prepared: 2026-06-10 (written at batch merge time, pre-hardware-run)
