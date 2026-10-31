# Changelog

## 2.5.1

- **Fix:** the silent test counted stateless entities as failures. Buttons, scenes and input buttons are `unknown`
  until pressed, so every job's "down" count was inflated by things that cannot be up. They are now excluded: on
  this house the Network job went from 20 down to 4, and 4 is the truth (sensors the hardware does not report).
- The `Network` job now covers both Omada integrations (`tplink_omada` and `omada_open_api`), so the Open API side is
  watched the same way as the rest.

## 2.5.0

- **Deep network read.** The `network` command now asks the controller itself, through the library the Omada
  integration already depends on, for what never reaches Home Assistant as entities: connected clients with their
  access point, network and signal; per-AP and per-SSID counts; the weakest links; guest and blocked counts; and each
  device's model and firmware. Read-only, and it falls back to the entity read if the call fails.
- The report now names the hardware and the client picture, so "healthy" is a claim about the whole network rather
  than about five sensors.
- Gotcha recorded in the README: the controller's API answers on **port 443**; the same calls on 8043 return HTTP 500.

## 2.4.1

- **Fix:** the status file carried the silent test's per-entity liveness (18 KB), pushing `sensor.smart_updater`'s
  attributes past Home Assistant's 16 KB limit — the recorder warned every minute and dropped them. The status now
  keeps the silent-test counters and per-job totals; the full per-entity list stays in `state.json`. File size
  28 KB → 7 KB.

## 2.4.0

- **Deeper network read.** The `network` command now builds one record per device — status, CPU, memory, firmware,
  every port's link state and PoE draw — instead of flat lists, and keeps a 14-day history so the read can say what
  changed since the last check. Warnings (a device off, CPU or memory at 80 %, a firmware update) are computed
  locally and handed to the AI as facts, not left for it to spot.
- **Fix:** `network` raised `NameError: name 'rows' is not defined` after saving, so every run logged a failure even
  though the report was written. The error streak now clears.

## 2.3.0

- **Network read.** A new `network` command summarises the TP-Link Omada network (devices, CPU/memory, WAN and LAN
  links, firmware, PoE, tracked clients) and asks the AI for a short plain-English read. Read-only: no reconnect,
  block or port control. Daily at 07:00; exposed as the `network` sensor attribute and a dashboard card.
- Adds a `Network` job when `tplink_omada` is in `jobs`.

## 2.2.0

- **Two-stage preflight.** The first AI pass now names suspected breaking changes (`CONCERN: <token> | <why>`).
  Subutai verifies each token against the house itself — `configuration.yaml`, `automations.yaml`, `scripts.yaml`,
  `scenes.yaml`, `packages/`, `custom_templates/`, the entity registry and the installed integrations — and a second
  AI pass judges with that evidence. A concern the house does not use is not risk. Only counts and file names are
  sent to the AI; file contents stay on the box.
- The dashboard shows what was checked against the house ("lutron_caseta — used here").
- Tests for concern parsing and local verification (including a temp config tree).

## 2.1.0

- **Add-on supervision.** Every add-on that was auto-updating is now driven only by the nightly window; the
  Supervisor's own `auto_update` toggle is off. A new sweep pass reports an add-on that errors, or that stops after
  running, once per add-on (24 h dedupe). Failed add-on updates no longer pass unnoticed.
- **The silent test.** With `jobs` configured, a per-job device check runs every 30 minutes: entities from the
  registry, mapped to the integration that provides them. Regressions go to `state.json` as `shadow.would_speak`
  and are never sent. The dashboard shows the count.
- README: the update classes table and the one-door setup step.

## 2.0.0

- **7-day soak.** `soak_hours` (default 168) holds every Core release for a week after it is published. The AI
  reassesses it once a day through the soak, so the release is checked against the issues as they accumulate, not
  only on the day it appears. `0` disables the soak.
- **Quiet by default.** Notifications are plain text and limited to things a person must act on: rollback,
  rollback failed, install failed, a newly broken integration, a stale backup, a freeze/recovery, and a command that
  failed three times in a row. Holds, successful updates and baseline failures are dashboard-only. Every push is
  deduplicated by key for 24 hours; urgent ones bypass the limit.
- **Sweep that does not cry wolf.** A failing integration is eligible only if it had been healthy for 6 hours or more
  before it broke. Anything already failing at first run is the baseline and stays quiet. `ignore` silences named
  domains outright. One alert per down-streak.
- **Heartbeat and stall detection.** `heartbeat` runs every minute, writes `.heartbeat`, and reports a gap longer
  than `stall_min` (15 minutes) once, on recovery, with the duration. A snapshot of Supervisor's view is written to
  `/config/claude/incidents/<timestamp>/` so a freeze leaves evidence. Planned restarts are suppressed.
- **Backup health.** `backup_health` checks the newest Supervisor backup is under 36 hours old and contains
  `homeassistant`. It alerts only on failure (once per day) and exposes the result to the dashboard.
- **Status file for a dashboard.** `status` (and every other command) writes
  `/config/www/smart_updater_status.json` with state, installed/pending versions, soak countdown, AI risk/verdict,
  blocked list, failing integrations, last stall and last backup. A ready-made Lovelace dashboard is in
  `dashboard/system_dashboard.yaml`; the `command_line` sensor it needs is documented in the README.
- New `config.json` keys: `soak_hours`, `stall_min`, `ignore`, `watch`, `www_dir`.
- **No fast path.** A release confirmed to fix the problem you rolled back from no longer installs immediately:
  it gets the same soak, risk check and window as everything else. `FIXED: yes` is informational.
- **Watched integrations.** `watch` lists domains that are always shown on the dashboard with their
  config-entry state, healthy or not — the dashboard previously listed failures only.
- **The house's jobs.** `jobs` groups integrations into what the house does (`Lights`, `Heating and cooling`, ...).
  A job is working only if every integration behind it is; the dashboard reports jobs, not plumbing.
- **30-day strip.** A daily counter (updates, rollbacks, freezes, bad backups) is kept in `state.json` and the
  dashboard shows one square per day: quiet, changed, or a problem.
- **Night timeline.** One card showing the update window, the conditional restart and reboot, and the backup.
- **Review fixes:** the 1.1 migration no longer seeds `blocked` from Home Assistant's `skipped_version` (that
  turned the updater's own badge-hide into a permanent block); each block records a reason; the 30-day strip
  starts at the first run instead of inventing quiet days, and a new integration failure counts as a problem,
  not an update; a corrupt `state.json` is moved aside instead of silencing every command; the notify dedupe
  key is recorded only after a successful send; jobs ignore `ignore`d domains and report domains that are not
  set up; the dashboard guards on both status freshness and heartbeat liveness; and without the dashboard
  sensor a hold is pushed once per version.
- Rollback now happens before the AI diagnosis (the diagnosis runs afterwards and is retried by the heartbeat if the
  restart interrupts it), so the reversal is never delayed by an AI call.
- The package adds two automations: heartbeat every minute and backup health daily at 09:00 and on start.

### Upgrading from 1.1.0

- Replace `smart_updater/smart_updater.py` and `packages/smart_updater.yaml`, then restart Home Assistant (the
  package adds two automations, and a restart makes sure they load). The new
  `config.json` keys are optional; defaults are `soak_hours: 168`, `stall_min: 15`, `ignore: []`, `www_dir: /config/www`.
- State carries over: the first sweep after the upgrade records what is failing now as the baseline, and nothing is
  pushed for it. A 1.1.0 preflight cache has no publish date, so the first v2 preflight applies the full soak and
  may hold up to a day longer than necessary. It corrects itself on the next daily reassessment.
- The dashboard is optional. To use it, add the `command_line` sensor from the README and the YAML dashboard.

## 1.1.0

- **Hides the update badge.** When the preflight decides to wait for the window or to hold a release, it skips the
  release in Home Assistant so the badge goes away, and still installs it later: the install goes straight to the
  Supervisor. The preflight now compares `installed_version` with `latest_version` instead of reading the entity's
  state, so a skipped release still counts as pending.
- **Its own block list.** Failed installs and rolled-back releases go into `blocked` in `state.json`, and the new
  `blocked_versions` key in `config.json` lets you block a release by hand. A blocked release is never installed.
  Home Assistant's own skip no longer stops the updater (1.0.0 relied on it); skipping a Core release in the UI now only
  hides the badge.
- The package's preflight automation no longer requires the update entity to be `on`. It runs hourly and whenever
  `latest_version` changes (see Upgrading below).
- Preflight prompt: closed issues are resolved and never count as risk; a fix listed in the release notes counts
  against risk; only open issues reported against the new version for an integration in use count. Release-note lines
  that mention the open problem's integration (from the full notes, not the truncated copy) are quoted as evidence
  for `FIXED: yes`. The closed-issue citation gate is unchanged. (A live run on 2026.10.4 had said `RISK: high` by
  citing the closed issue its own release notes fixed.)
- `dryrun` reports `blocked` for a blocked version.
- Installs are pinned: the Supervisor is asked for exactly the version the preflight assessed, so a newer release
  that appears in between is never installed unassessed.
- `selftest`: new flows for a skipped release installing in the window, blocked versions, a failed install that is
  never retried, a hold that notifies once, and a rolled-back version being blocked.

### Upgrading from 1.0.0

- Copy the new `smart_updater/smart_updater.py` and `packages/smart_updater.yaml`, then reload automations.
- On its first run, 1.1 seeds its `blocked` list once from what 1.0 left behind: the version of an open problem
  (a failed install or rollback), a recorded bad version, and the release currently skipped in Home Assistant. A
  skip is treated as a block because a person may have made it. The log shows `MIGRATED blocked=[...]` and, if
  anything was kept, you get one notification. To allow such a release, remove it from `blocked` in `state.json`.

## 1.0.0 (2026-10-25)

Status: new. Selftested (including simulated update/rollback flows) and dry-run on a live box; not yet exercised by a real Core release — the first real install/rollback will be the first end-to-end run.

- First public release. One stdlib Python script (`smart_updater/smart_updater.py`) and one Home Assistant package
  (`packages/smart_updater.yaml`): AI preflight before a Core update, install inside a time window, a health check
  after every start, a deterministic rollback and skip when an update breaks an integration that worked before it,
  and an AI diagnosis of any integration that stays broken.
- Settings live in an optional `config.json` next to the script (`notify`, `ai_entity`, `core_update`, `window`,
  `stuck_min`, `ha_url`). Without `notify`, every run logs a `CONFIG ERROR` and the updater still checks and rolls back; it
  just cannot tell you. A missing or malformed file falls back to the defaults.
- `selftest` covers the decision, log-parsing and config logic and runs the update flows (failed install,
  Supervisor revert, transient failure, real breakage, failed rollback) against a fake Home Assistant.
