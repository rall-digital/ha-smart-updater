# ha-smart-updater

Automatic Home Assistant Core updates that undo themselves when they break something. One Python script (standard
library only) and one Home Assistant package. No custom integration, no add-on.

**Status: new. Selftested (including simulated update/rollback flows) and dry-run on a live box; not yet exercised by a real Core release — the first real install/rollback will be the first end-to-end run.**

- **Before an update**, an AI reads the release notes and the GitHub issues that mention the new version, compares
  them with the integrations you actually use, and decides: install tonight, or hold and tell you why.
- **After every start**, the script compares your integrations with a snapshot taken just before the update. If an
  integration that worked before the update is now failing, and still failing ten minutes later, it **rolls Core back
  to the previous version and skips the bad release.** That rule is fixed code; the AI has no vote on it.
- **Then the AI diagnoses it**: it reads the failing integration's log lines, searches GitHub for the error, and
  sends you a plain-English note: the cause, the known bug if there is one, the version that fixes it if known, and
  what to do (usually nothing).
- **When a newer release comes out**, the preflight checks whether it fixes the problem you rolled back from. If a
  closed GitHub issue says so, it installs right away; if not, it waits for the nightly window. If it breaks again,
  it rolls back again.
- **Every 30 minutes**, any integration that has been failing for 30 minutes or more, for any reason, gets the same
  diagnosis once.

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

This project is that afternoon's work, automated: notice, roll back, skip, find out why, retry when it is fixed.

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
   fight, and the old one will reinstall a release this one skipped.
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
| `window` | `[3, 5]` | install Core updates from 03:00 to before 05:00, Home Assistant's local time |
| `stuck_min` | `30` | the sweep diagnoses an integration once it has failed this many minutes |
| `ha_url` | `http://localhost:8123` | Home Assistant's own API as seen from inside the Core container. Change it only for HTTPS (see Limits). |

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
| A Core update is pending (checked when it appears, then hourly) | The AI assesses it once per version per day: `RISK: high` or `low`, and, if you have an open problem, `FIXED: yes`, `no` or `unknown`. |
| The release fixes the problem you rolled back from | Install now. `FIXED: yes` counts only if the AI cites a GitHub issue the script itself found, closed after your problem started. |
| `RISK: high` | Hold, and tell you why (once per version). |
| Anything else, AI failure included | Install inside the window. The health check is the safety net either way. |
| The install is refused twice, or Core comes back on the old version | Skip the release and tell you. It is not retried. |
| After a start, an integration fails that did not fail before the update | Wait ten minutes and look again. Still failing: diagnose, tell you, roll back to the version from before the update, then skip the bad release after the restart. |
| The rollback fails | Retry once; then tell you the exact command to run (`ha core update --version <old>`). |
| After a start, everything that worked still works | Record the version as good and tell you. |
| Any integration stays failing `stuck_min` minutes, update or not | Diagnose it once and tell you. A recovery resets it, so a relapse is diagnosed again. |

The AI reads untrusted text (release notes, issue bodies). It can only phrase advice and answer RISK and FIXED; it
never chooses whether to roll back, and the script never runs anything the AI writes.

`selftest` checks all of this without a network: `python3 -I smart_updater/smart_updater.py selftest`. It runs the
pure logic plus the update flows against a fake Home Assistant.

## What you get told

- `HA <version> held`: the AI found credible risk for an integration you use. The reason follows.
- `HA updated to <version>`: installed, and every integration that worked still works.
- `HA <version> broke <integrations>`: rolling back now, with the diagnosis.
- `HA rolled back to <version>`: the rollback finished and the bad release is skipped.
- `HA <version> did not install`: refused, or the Supervisor reverted a failed start. Skipped.
- `HA rollback FAILED` / `HA still on broken version`: you need to act; the message has the command.
- `HA: <integrations> broken 30+ min`: the diagnosis of something that broke on its own.
- `HA smart updater error`: the script itself failed; the error follows.

Everything is also written to `/config/smart_updater/log.txt`. State lives in `state.json` beside it.

## Limits

- **Core only.** Home Assistant OS, the Supervisor, add-ons and HACS integrations are not touched. Update them
  however you do today (the example automation keeps auto-installing them).
- **It sees config entries only.** An integration set up in YAML, or one that loads fine but talks to a dead device,
  is invisible to the health check.
- **The rollback is a version pin without a new backup.** The install itself asks the Supervisor for a backup first;
  that backup is your fallback if a rollback ever leaves things worse.
- **A planted GitHub issue can rush an install.** Someone could open an issue claiming a release fixes your problem
  and close it themselves; the AI could then say `FIXED: yes` and the release installs outside the window. The health
  check still runs after it, so the worst case is one more rollback.
- A rollback that takes longer than 30 minutes can produce a false "rollback FAILED" message.
- **HTTPS on port 8123.** If `configuration.yaml` sets `http: ssl_certificate`, plain `http://localhost:8123` stops
  answering. Set `ha_url` to an `https://` address whose name matches the certificate and that resolves from inside
  Home Assistant (for example your DuckDNS name with `:8123`). `https://localhost:8123` only works if the certificate
  covers `localhost`. The script verifies certificates and has no option to skip that, so a self-signed certificate or
  a name mismatch makes every call to Home Assistant fail: nothing is checked, rolled back or notified, and `log.txt`
  shows certificate errors. `probe` shows it as `probe ha /config: FAIL ... CERTIFICATE_VERIFY_FAILED`.
- GitHub's search API is used without a token (a handful of requests per run, well inside its limits).

## Uninstall

1. Delete `/config/packages/smart_updater.yaml` and restart Home Assistant.
2. Delete the `/config/smart_updater/` folder.
3. Delete the long-lived token in your profile.
4. If you added the `rejectattr` to another auto-updater, decide whether Core should go back to it.

## License

MIT, see [LICENSE](LICENSE). © 2026 RALL DIGITAL LLC.
