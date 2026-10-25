# Changelog

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
  `latest_version` changes. **Upgrading: copy the new `packages/smart_updater.yaml` too**, then reload automations.
- Preflight prompt: closed issues are resolved and never count as risk; a fix listed in the release notes counts
  against risk; only open issues reported against the new version for an integration in use count. Release-note lines
  that mention the open problem's integration (from the full notes, not the truncated copy) are quoted as evidence
  for `FIXED: yes`. The closed-issue citation gate is unchanged. (A live run on 2026.10.4 had said `RISK: high` by
  citing the closed issue its own release notes fixed.)
- `dryrun` reports `blocked` for a blocked version.
- `selftest`: new flows for a skipped release installing in the window, blocked versions, a failed install that is
  never retried, a hold that notifies once, and a rolled-back version being blocked.

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
