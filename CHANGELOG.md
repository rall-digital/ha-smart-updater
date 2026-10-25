# Changelog

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
