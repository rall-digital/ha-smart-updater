# ha-smart-updater

Automatic Home Assistant Core updates that undo themselves when they break something. One Python script (standard
library only) and one Home Assistant package. No custom integration, no add-on.

**Status: v2. Selftested (including simulated update/rollback flows, soak and notification policy) and dry-run on a live box; not yet exercised by a real Core release — the first real install/rollback will be the first end-to-end run.**

- **Before an update**, an AI reads the release notes and the GitHub issues that mention the new version, compares
  them with the integrations you actually use, and decides: safe, or hold. A release also has to finish its **soak**
  (7 days by default) before it may install, and it is reassessed every day through the soak — the AI keeps watching
  the issues while the release ages.
- **After every start**, the script compares your integrations with a snapshot taken just before the update. If an
  integration that worked before the update is now failing, and still failing ten minutes later, it **rolls Core back
  to the previous version and blocks the bad release.** That rule is fixed code; the AI has no vote on it.
- **Then the AI diagnoses it**: it reads the failing integration's log lines, searches GitHub for the error, and
  writes the cause and the one thing to do. That goes to the dashboard; the push just says what happened.
- **When a newer release comes out**, the preflight checks whether it fixes the problem you rolled back from and
  says so on the dashboard — but it does not jump the queue: every release gets the same soak, risk check and
  midnight window. If it breaks again, it rolls back again.
- **It stays quiet.** Only things a person must act on are pushed: a rollback, a failed rollback, a newly broken
  integration that had been healthy, a stale backup, or a freeze and recovery. Holds, successful updates and
  known-broken integrations go to the dashboard only — and without the dashboard sensor installed, a hold is
  pushed once per version instead. Messages are plain text — no Markdown, no AI scratchpad.
- **Every minute**, a heartbeat records liveness in `.heartbeat`. A gap longer than `stall_min` (15 minutes) is
  reported once, on recovery, and a snapshot of what Supervisor knows is saved to `/config/claude/incidents/`.
- **Every 30 minutes**, an integration that breaks after being healthy for 6 hours or more gets one plain-text alert.
  Integrations that were already broken at install time are the baseline and never alert; `ignore` silences any
  domain you never want to hear about.

## How it works, in plain English

**The cast.** Home Assistant is the house computer. Subutai is a small script that lives inside it: a set of fixed
rules plus a phone line to an AI. The AI is Home Assistant's own `ai_task` entity (default
`ai_task.openai_ai_task`), a separate model that this project calls; it is not Home Assistant itself. GitHub is the
town square where strangers report "this update broke my lights."

**What happens all day.** Every minute, Subutai writes a heartbeat. Every 30 minutes it checks every integration and
add-on, and runs the silent device test. Every hour it asks Home Assistant whether a newer Core version exists.

**The update ritual, and where the AI fits.**

1. **Is it new?** No newer version → nothing happens.
2. **The waiting room.** A new release must be at least `soak_hours` old (seven days by default) before Subutai will
   install it. No AI involved; a fixed rule. While it waits, the badge is hidden so nobody sees a pending update.
3. **The consultant.** Once a day during that week Subutai calls the AI with four things: the release notes, the
   recent GitHub complaints that mention this version, the names of the integrations in use (not devices, states or
   household data), and, if a previous release broke something, what broke. It asks two questions:
   - `RISK: high or low` — is there credible evidence this release breaks something we use?
   - `FIXED: yes / no / unknown` — does it fix the problem we rolled back from?
4. **The rules around the answer.** The AI answers in those two words, then at most five lines of explanation with
   links. A `FIXED: yes` counts only if it cites a closed GitHub issue that the script itself found; an invented
   citation is discarded. Closed issues never count as risk; only open issues about this version, for an integration
   in use, do.
5. **The second stage: check it against this house.** The first pass also names suspected breaking changes
   (`CONCERN: <integration, config key or entity prefix> | <why>`). Subutai then searches its own configuration files,
   the entity registry and the installed integrations for each one, and a second AI pass judges only with that
   evidence: a concern this house does not use is not risk. File contents never leave the house — the AI sees counts
   and file names, not your configuration.
5. **The decision.** Fixed code, not the AI: still soaking → wait; risk high → hold; otherwise install, but only
   between `window[0]` and `window[1]` (00:00–02:00 by default).
6. **The install.** It asks the Supervisor for exactly the version it assessed, with a backup first.
7. **The morning after.** Ten minutes after the restart it compares the integrations with a snapshot taken just
   before the update. Anything that worked before and is failing now gets a second look ten minutes later. Still
   failing → **roll back and block**. That decision is fixed code; **the AI has no vote on it.**

**What the AI is allowed to do.** Recommend caution (a hold) by saying "high risk"; and explain, in plain words, why
something broke. Nothing else. It cannot install, roll back, block, run a command or change a setting. Its output is
two words plus prose; the script parses the words and shows the prose.

**When the AI is down or wrong.** *Down:* the release is treated as normal — it still soaks, still installs in the
window, and the health check is the safety net. The AI failing never blocks an update or a rollback. *Wrong (says
low risk, but it breaks):* the health check catches it and rolls back. That is why the AI is advice, not authority.
*Wrong the other way (says high risk when it is fine):* Subutai holds and asks again the next day; nothing breaks.

**When else the AI is used.** Only when something breaks. It reads the error lines for that integration, searches
GitHub for the same error, and answers: `CAUSE / KNOWN BUG / FIXED IN / DO`. That explanation goes to the dashboard.
It never decides the rollback.

**What the AI is not used for.** The heartbeat, the silent device test, the backup check, add-on health, the soak,
the window, the block list and the rollback. All fixed rules. The AI is a consultant for two yes/no questions and a
translator of error messages — nothing more.

**The silent test.** With `jobs` configured, Subutai watches whether the devices behind each job go quiet, writes
down what it *would* have told you, and sends nothing, so the false-positive rate can be measured before device-level
alerts are trusted. It uses no AI.

## Why this exists

We ran hass-auto-update, our earlier public automation that installs every
pending update the moment it appears. On 2026-10-23 it installed Home Assistant Core 2026.10.3, which ships Python
3.14.8. The Lutron Caseta library in use at the time (pylutron-caseta 0.29.0) fails its TLS connection on that Python
(`check_hostname requires server_hostname`), so the `lutron_caseta` integration sat in `setup_retry` and the lights
stopped answering. It was a known upstream bug, reported by other users of that release too.

Home Assistant's own update rollback did not help: the Supervisor only reverts an update when Core fails to start, and
Core started fine with one dead integration. It stayed broken until a person rolled it back by hand, two days later. Rolling back
(`ha core update --version 2026.10.2`) fixed it, and skipping 2026.10.3 kept the auto-updater from reinstalling it
within the hour.

This project is that afternoon's work, automated: notice, roll back, block, find out why, retry when it is fixed.

## Install with a coding agent

If you use [Claude Code](https://claude.com/claude-code) or another coding agent that can SSH into your Home
Assistant, paste this:

```
Clone https://github.com/rall-digital/ha-smart-updater and follow AGENTS.md to install it on my Home Assistant.
```

The agent copies the files, asks you to create the token yourself (it never sees it), adds the package, checks the
configuration, and runs a dry run against the latest release. It asks before every change. [AGENTS.md](AGENTS.md) is
the full procedure.

## Requirements

- **Home Assistant OS or Supervised.** Installs and rollbacks go through the Supervisor API. Container and Core
  installs have no Supervisor, so this does not work there.
- **An AI Task entity** (`ai_task.*`), for example from the OpenAI or Google Generative AI integration. A model with
  web search gives better diagnoses. Without a working AI the updater still installs in the window, checks and rolls
  back; the notes just say "AI unavailable" and include the raw log lines.
- **A notify target**, for example `notify.mobile_app_<your phone>`.
- An admin user's **long-lived access token** (see "Why a token file" below).

## Install by hand

1. Copy `smart_updater/smart_updater.py` to `/config/smart_updater/smart_updater.py` on your Home Assistant.
2. Copy `smart_updater/config.example.json` to `/config/smart_updater/config.json` and set `notify` (see below).
3. Create a long-lived access token (your profile > Security > Long-lived access tokens; name it
   "smart updater") and save it, alone, in `/config/smart_updater/.token`. Make it readable only by root:
   `chmod 600 /config/smart_updater/.token`.
4. Copy `packages/smart_updater.yaml` to `/config/packages/smart_updater.yaml`. If your `configuration.yaml` has no
   packages line yet, add it:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
5. **Developer tools > YAML > Check configuration**, then restart Home Assistant. A restart is
   needed for the new `shell_command`; reloading automations is not enough.
6. Leave Core out of any other auto-updater. If you run hass-auto-update or similar, add the `rejectattr` line from
   [examples/auto_update_except_core.yaml](examples/auto_update_except_core.yaml) to it. Two installers for Core will
   fight: the old one installs releases the moment they appear, before the preflight and outside the window.
7. Check it: **Developer tools > Actions**, run `shell_command.smart_updater` with data `cmd: probe`, then read
   `/config/smart_updater/log.txt`. You want `probe supervisor /core/info: ok <version>` and `probe ha /config: ok
   <version>`. Then run it with `cmd: dryrun <latest version>` (for example `cmd: dryrun 2026.10.3`): a minute or two
   later the log shows what the preflight would decide for that release and why. A dry run installs nothing.

The first `sweep` (within 30 minutes) records whatever is failing right now as the baseline; those integrations are
not diagnosed until they recover and break again.

## Configuration

`/config/smart_updater/config.json`. Every key is optional; the file itself is optional.

| key | default | meaning |
|---|---|---|
| `notify` | none | the notify action to send to, `notify.mobile_app_my_phone` (or `notify/mobile_app_my_phone`) |
| `ai_entity` | `ai_task.openai_ai_task` | the AI Task entity used for the preflight and diagnoses |
| `core_update` | `update.home_assistant_core_update` | the Core update entity. If you change it, change it in the package's trigger too. |
| `window` | `[3, 5]` | install Core updates from 03:00 to before 05:00, Home Assistant's local time. Two ascending hours. |
| `soak_hours` | `168` | wait this long after a release is published before installing it. `0` disables the soak. |
| `stuck_min` | `30` | an integration failing this long is eligible to alert (it also must have been healthy first) |
| `stall_min` | `15` | a heartbeat gap this long is a freeze and is reported on recovery |
| `ignore` | `[]` | domains that must never alert, for example `["wyoming", "influxdb"]` |
| `watch` | `[]` | domains always shown on the dashboard with their state, healthy or not, for example `["lutron_caseta", "hue"]` |
| `jobs` | `{}` | the house's jobs, each a name and the integrations behind it: `{"Lights": ["lutron_caseta", "hue"], "Laundry": ["smartthinq_sensors"]}`. A job is "working" only if all of its integrations are. Takes precedence over `watch` on the dashboard. |
| `ha_url` | `http://localhost:8123` | Home Assistant's own API as seen from inside the Core container. Change it only for HTTPS (see Limits). |
| `www_dir` | `/config/www` | where `smart_updater_status.json` is written for the dashboard (read by the Core container). |
| `blocked_versions` | `[]` | versions never to install, for example `["2026.10.3"]`. This is how you block a release by hand. |

`notify` has no safe default. Without it, every run writes `CONFIG ERROR: config.json has no "notify" target` to
`log.txt`, and the updater still does everything else, rollbacks included; you just are not told. A file that is not
valid JSON, an unknown key or a bad `window` also logs a `CONFIG ERROR`, and the defaults are used.

### Why a token file

The script runs inside the Core container (through `shell_command`). Supervisor calls (install, rollback, logs) use
that container's `SUPERVISOR_TOKEN`. Home Assistant's own API is a different story: the same token is refused with
401 on the Supervisor's `/core/api` proxy, so the script calls Home Assistant directly (`ha_url`, by default
`http://localhost:8123`) with a long-lived token read from `.token`. Deleting that token in your profile silently stops the health check, so give it a name you will
recognise.

## How it decides

| situation | what happens |
|---|---|
| A Core release newer than the installed one exists (checked when it appears, then hourly), whether or not it is skipped in Home Assistant | The AI assesses it once per version per day: `RISK: high` or `low`, and, if you have an open problem, `FIXED: yes`, `no` or `unknown`. |
| The AI names a suspected breaking change | Subutai checks it against this house (config files, entities, installed integrations), then a second AI pass judges with that evidence. An unused concern is not risk. |
| The release is still inside its `soak_hours` | Wait, and keep the badge hidden. The AI reassesses it daily through the soak. |
| The release fixes the problem you rolled back from | Treated like any other release: soak, then risk, then the window. `FIXED: yes` is informational (shown on the dashboard) and counts only if the AI cites a GitHub issue the script itself found, closed after your problem started. |
| The release is blocked (by a failed install, a rollback, or `blocked_versions`) | Never installed. A newer release is assessed normally. |
| `RISK: high` | Hold and hide the badge. The reason is on the dashboard; nothing is pushed. Reassessed daily. |
| Anything else, AI failure included | Install inside the window; outside it, hide the badge and wait. The health check is the safety net either way. |
| The install is refused twice, or Core comes back on the old version | Block the release and tell you once. It is not retried. |
| After a start, an integration fails that did not fail before the update | Wait ten minutes and look again. Still failing: block the release, push "rolling back", roll back to the version from before the update, and diagnose afterwards (the heartbeat retries the diagnosis if the restart interrupts it). |
| The rollback fails | Retry once; then tell you the exact command to run (`ha core update --version <old>`). |
| After a start, everything that worked still works | Record the version as good. Dashboard only; no push. |
| An integration breaks after being healthy for 6 hours or more | One alert, once per down-streak, unless the domain is in `ignore`. A recovery resets it, so a relapse can alert again. |
| Anything was already failing when the updater first ran | That is the baseline. It is shown on the dashboard and never pushed. |

**Badges and skipping.** Home Assistant shows a badge for every pending update. The updater hides it by skipping the
release in Home Assistant whenever it decides to wait or hold, and installs that release later anyway: install goes
straight to the Supervisor, which does not care about Home Assistant's skip. So skipping a Core release in the Home
Assistant UI no longer stops it. To keep a release off your system, add it to `blocked_versions`. The updater's own
`blocked` list in `state.json` holds the releases it blocked itself.

**What counts as risk.** The preflight tells the AI that only an open issue reported against the new version, for
an integration you use, is risk; that closed issues are resolved; and that a fix listed in the release notes counts
against risk. If you have an open problem, the release-note lines that mention its integration are quoted to the AI
as evidence for `FIXED: yes`.

The AI reads untrusted text (release notes, issue bodies). It can only phrase advice and answer RISK and FIXED; it
never chooses whether to roll back, and the script never runs anything the AI writes.

`selftest` checks all of this without a network: `python3 -I smart_updater/smart_updater.py selftest`. It runs the
pure logic plus the update flows against a fake Home Assistant.

## What you get told

Only things that need a person, in plain text, at most once per 24 hours for the same key:

- `HA: <version> broke <integrations>` — rolling back now.
- `HA: rolled back to <version>` — the rollback finished and the bad release is blocked.
- `HA: rollback FAILED` / `HA: still on the broken version` — you need to act; the message has the command.
- `HA: <version> did not install` — refused, or the Supervisor reverted a failed start. Blocked.
- `HA: <integration> down` — a new failure after 6 or more healthy hours; cause and action in two lines.
- `HA: was unresponsive` — a freeze longer than `stall_min`, reported on recovery with the duration.
- `HA: backup needs attention` — the newest backup is over 36 hours old or does not contain the HA config.
- `HA: smart updater <command> failing` — the script itself failed three times in a row.

Everything else — holds, soak countdowns, successful updates, the known-broken baseline — is dashboard-only and in
`/config/smart_updater/log.txt`. State lives in `state.json` beside it.

## Dashboard

`status`, `heartbeat` and `backup_health` write `/config/www/smart_updater_status.json`. The dashboard is optional. A
ready-made Lovelace dashboard is in [dashboard/system_dashboard.yaml](dashboard/system_dashboard.yaml); it needs one
`command_line` sensor:

```yaml
command_line:
  - sensor:
      name: Smart Updater
      unique_id: smart_updater_status
      command: cat /config/www/smart_updater_status.json
      value_template: "{{ value_json.state }}"
      json_attributes:
        - installed
        - pending
        - soak_hours
        - soak_remaining_hours
        - ai_risk
        - ai_fixed
        - verdict
        - blocked
        - window
        - failing
        - ignored_failing
        - watched
        - jobs
        - strip
        - strip_summary
        - last_event
        - diagnosis
        - diagnosis_version
        - recent_log
        - last_stall_seconds
        - last_stall_at
        - backup_ok
        - backup_age_hours
        - backup_at
        - updated
      scan_interval: 60
```

If the sensor is not installed, holds are pushed once per version so nothing goes silent.

The `state` is `idle`, `soaking`, `holding`, `blocked` or `installing`. The dashboard also reads `jobs` (the
house's jobs, with `state` and the `broken` domains), `strip` (30 days, oldest first, `g` quiet / `a` update /
`r` rollback, freeze or bad backup) and `strip_summary`.

## What gets updated

Subutai's policy is one door: every piece of software that can change the house enters through the same nightly
window and the same decision point.

| Class | What happens | Undo |
|---|---|---|
| Core | Subutai: soak, risk read, window, health check, automatic rollback | version pin |
| Add-ons | The house's update automation installs them in the same 00:00–02:00 window, with the Supervisor's pre-update backup; Subutai reports an add-on that errors or stops | restore the backup (manual) |
| HACS integrations | Same window; supervised | manual reinstall |
| Cards / themes | Same window; cosmetic | irrelevant |
| OS / Supervisor | Staged in the window; the host reboots at 03:00 only when the Supervisor asks | previous boot slot |
| Device firmware | Manual | none |

To make that true, turn off each add-on's own **auto update** toggle once (Settings > Add-ons > the add-on). That
toggle is a Supervisor setting, separate from the update entities, and it is what lets an add-on update at 3 pm.

## The silent test

With `jobs` configured, every 30 minutes Subutai also checks the devices behind each job, using the entity registry
to map entities to the integration that provides them. A device that was working and has been unavailable for an
hour is recorded in `state.json` under `shadow.would_speak` — and **nothing is sent**. The dashboard shows the
count. Run it for a month before trusting device-level alerts; the false-positive rate is the number that matters.

## Limits

- **Core only.** Home Assistant OS, the Supervisor, add-ons and HACS integrations are not touched. Update them
  however you do today (the example automation keeps auto-installing them).
- **It sees config entries only.** An integration set up in YAML, or one that loads fine but talks to a dead device,
  is invisible to the health check.
- **The rollback is a version pin without a new backup.** The install itself asks the Supervisor for a backup first;
  that backup is your fallback if a rollback ever leaves things worse.
- **A planted GitHub issue cannot rush an install.** `FIXED: yes` is informational: it no longer skips the soak,
  the risk hold or the window. A planted issue can at most make the AI cautious, which can only cause a hold; the
  health check still runs after any install.
- A rollback that takes longer than 30 minutes can produce a false "rollback FAILED" message.
- **HTTPS on port 8123.** If `configuration.yaml` sets `http: ssl_certificate`, plain `http://localhost:8123` stops
  answering. Set `ha_url` to an `https://` address whose name matches the certificate and that resolves from inside
  Home Assistant (for example your DuckDNS name with `:8123`). `https://localhost:8123` only works if the certificate
  covers `localhost`. The script verifies certificates and has no option to skip that, so a self-signed certificate or
  a name mismatch makes every call to Home Assistant fail: nothing is checked, rolled back or notified, and `log.txt`
  shows certificate errors. `probe` shows it as `probe ha /config: FAIL ... CERTIFICATE_VERIFY_FAILED`.
- GitHub's search API is used without a token: 10 searches a minute. A busy sweep can approach that; when the
  limit is hit, the diagnosis falls back to the raw log lines and nothing else changes.
- The status file is written to `www_dir` (`/config/www` by default), which Home Assistant serves without
  authentication at `/local/smart_updater_status.json`. It contains versions, integration names and the last 12
  log lines — no secrets. Change `www_dir` if you do not want it served.

## Uninstall

1. Delete `/config/packages/smart_updater.yaml` and restart Home Assistant.
2. Delete the `/config/smart_updater/` folder.
3. Delete the long-lived token in your profile.
4. If you added the `rejectattr` to another auto-updater, decide whether Core should go back to it.

## License

MIT, see [LICENSE](LICENSE). © 2026 RALL DIGITAL LLC.
