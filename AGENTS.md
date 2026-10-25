# AGENTS.md: installing ha-smart-updater on someone's Home Assistant

You are a coding agent. A person asked you to install ha-smart-updater on their Home Assistant. You have this
repository and an SSH session into their Home Assistant (the Terminal & SSH add-on). This file is the whole
procedure. Follow the steps in order.

The product is two files: `smart_updater/smart_updater.py` (goes to `/config/smart_updater/`) and
`packages/smart_updater.yaml` (goes to `/config/packages/`). There is no build step.

## Rules that hold for the whole install

1. **The token is a secret.** The human writes it to the token file in their own terminal (step 3). You never see
   it, never print the file, never type it into a command line, never echo it, never include it in a summary. The
   one allowed use is step 6: `$(cat /config/smart_updater/.token)` inside a curl header, which the shell expands
   without showing it. If the
   human pastes it into the chat anyway: say it is now stored in this conversation's transcript, do not repeat it,
   ask them to write it to the file themselves as in step 3, and tell them to replace that token with a new one
   when the install is done.
2. **No write without a yes.** Before each change on the Home Assistant machine (a copied file, an edit, a restart),
   show the human exactly what will change and wait for an explicit yes to that change.
3. **Back up before you edit an existing file**: `cp <file> <file>.bak-smart-updater-$(date +%Y%m%d%H%M%S)`. Keep
   the backups until the human says the install is fine.
4. **Do not restart Home Assistant without a yes**, and do not touch anything this file does not name.
5. **Read back after every write** (`ls -l`, `md5sum`, `grep`) and compare with what you intended.

## Step 1: check the machine

```sh
ha core info
ha supervisor info >/dev/null && echo SUPERVISOR_OK
ls -la /config/smart_updater /config/packages 2>&1
grep -n -A2 '^homeassistant:' /config/configuration.yaml; grep -n 'packages' /config/configuration.yaml
```

- If `ha` is not found or the Supervisor does not answer: this is not Home Assistant OS or Supervised. Stop and tell
  the human it cannot work here (README, Requirements).
- If `/config/smart_updater/` already exists: stop and ask. It holds a previous install's state and token.
- Note the version `ha core info` prints, and whether `configuration.yaml` already has a `packages:` line and which
  form (`!include_dir_named packages` or `!include_dir_merge_named packages`).

Before copying anything, run the selftest on your own machine. It needs only Python 3:

```sh
python3 -I smart_updater/smart_updater.py selftest
```

It must print `selftest ok`. If it does not, stop: the checkout is broken.

## Step 2: choose the settings with the human

Ask, or read from Home Assistant if the human prefers (Developer tools > Actions lists them):

- **notify**: the notify action to use, for example `notify.mobile_app_their_phone`. Required in practice: without
  it the updater still rolls back but tells nobody. Do not guess between several phones; ask.
- **ai_entity**: the `ai_task.*` entity. One exists: use it. Several: ask. None: tell the human the preflight and
  diagnoses will say "AI unavailable" until they add one (the rollback works regardless). Default
  `ai_task.openai_ai_task`.
- **window**: the hours to install in. Default 03:00 to 05:00. Keep it unless asked.

Write `config.json` locally from `smart_updater/config.example.json` with only the keys you are changing plus
`notify`. It holds no secret.

## Step 3: copy the files

Show the human the commands, get a yes, then copy (adjust the SSH target to theirs):

```sh
ssh <ha> 'mkdir -p /config/smart_updater /config/packages'
scp smart_updater/smart_updater.py <ha>:/config/smart_updater/smart_updater.py
scp config.json <ha>:/config/smart_updater/config.json
scp packages/smart_updater.yaml <ha>:/config/packages/smart_updater.yaml
ssh <ha> 'md5sum /config/smart_updater/smart_updater.py /config/packages/smart_updater.yaml'
```

Compare the md5 sums with `md5sum` (or `md5`) of your local files.

If `configuration.yaml` uses `!include_dir_merge_named packages`, the package file must be nested one level: put the
whole file's content under a top-level `smart_updater:` key before copying.

**The token.** Ask the human to create a long-lived access token while logged in as an **admin** user (profile >
Security > Long-lived access tokens > Create token, name it "smart updater"), and then to run this in **their own**
SSH session, not through you:

```sh
read -rs T && printf '%s' "$T" > /config/smart_updater/.token && unset T && chmod 600 /config/smart_updater/.token
```

They paste the token and press Enter; nothing is shown. Then you check only that the file exists and its mode:

```sh
ssh <ha> 'ls -l /config/smart_updater/.token'
```

It must be `-rw-------` and non-empty. Do not read it.

## Step 4: enable packages, check, restart

If `configuration.yaml` has no `packages:` line: back it up (rule 3), then add it under the existing `homeassistant:`
key, or add the key if there is none:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Show the human the exact edit first. Never add a second `homeassistant:` key; YAML would silently keep only one.

Then check the configuration:

```sh
ha core check
```

It must succeed. If it fails, show the human the error, fix only what you changed, or restore the backup.

A new `shell_command` needs a restart; reloading automations is not enough. Ask the human for a yes, then:

```sh
ha core restart
```

Wait until `ha core info` answers again.

## Step 5: keep other auto-updaters away from Core

Look for automations that install updates:

```sh
grep -n -B3 -A25 'update.install' /config/automations.yaml
```

If one installs Core (anything iterating over `states.update` without excluding Core), it must stop doing so, or it
will install Core releases the moment they appear, before the preflight and outside the window. Show the human the change from
`examples/auto_update_except_core.yaml`: a `rejectattr('entity_id', 'eq', 'update.home_assistant_core_update')` in
each template that lists update entities. Back up `automations.yaml`, edit, `ha core check`, then reload automations
(Developer tools > YAML > Automations, or a restart). If you cannot tell whether an automation touches Core, ask.

## Step 6: probe and dry run

Run the script inside the Core container through its `shell_command`, by calling Home Assistant directly with the
long-lived token from step 3. Use `http://homeassistant:8123`, not the Supervisor's `/core/api` proxy: from the SSH
add-on the proxy answers 401 to both the add-on's `$SUPERVISOR_TOKEN` and the long-lived token.
If `configuration.yaml` sets `http: ssl_certificate`, use `https://` and a name the certificate covers, here and in
`config.json` as `ha_url` (README, Limits); with a self-signed certificate the script cannot reach Home Assistant.

```sh
curl -sS -X POST -H "Authorization: Bearer $(cat /config/smart_updater/.token)" -H 'Content-Type: application/json' \
  -d '{"cmd":"probe"}' http://homeassistant:8123/api/services/shell_command/smart_updater; echo
sleep 5; tail -n 5 /config/smart_updater/log.txt
```

If that call is refused, ask the human to run it from Developer tools > Actions instead: action
`shell_command.smart_updater`, data `cmd: probe`. Either way, `log.txt` must show:

- `probe config: ... notify=notify/<their target> ...`
- `probe supervisor /core/info: ok <version>`
- `probe ha /config: ok <version>`

and no `CONFIG ERROR` line. A `FAIL` on `ha /config` with 401 means the token file is wrong (step 3); a `FAIL` on the
Supervisor line means this is not running in the Core container.

Then a dry run against the latest Core release (look it up on
https://github.com/home-assistant/core/releases, or `ha core info` shows `version_latest`):

```sh
curl -sS -X POST -H "Authorization: Bearer $(cat /config/smart_updater/.token)" -H 'Content-Type: application/json' \
  -d '{"cmd":"dryrun <latest>"}' http://homeassistant:8123/api/services/shell_command/smart_updater; echo
```

It runs in the background and can take up to three minutes (the AI call). Then:

```sh
grep -A8 DRYRUN /config/smart_updater/log.txt | tail -n 12
```

You want `DRYRUN <latest>: {'risk': ..., 'fixed': ...} -> install|wait|hold` and a few lines of explanation. `risk`
of `None` means the AI did not answer in the expected format or is unavailable (`ai failed:` in the log says which).
A dry run installs nothing and writes no state.

## Step 7: finish

Tell the human, in a short summary: the notify target, the AI entity, the install window, the dry-run verdict, which
auto-updater you changed (if any) and where every backup file is. Mention that the first sweep in the next 30 minutes
records what is failing now as the baseline, and that the token must stay: deleting it stops the health check.

## Uninstall

Each step is a write; get a yes. Delete `/config/packages/smart_updater.yaml`, `ha core check`, `ha core restart`,
then delete `/config/smart_updater/`. Ask the human to delete the "smart updater" token in their profile. Restore
any `rejectattr` change only if the human wants Core back on the old auto-updater.
