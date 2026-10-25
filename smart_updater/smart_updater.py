#!/usr/bin/env python3
"""Smart updater for Home Assistant Core. Runs inside HA core via shell_command.

  preflight  core update pending: AI reads release notes + GitHub issues; hold, install now, or wait for the window
  check      on HA start: diff failing integrations vs pre-update snapshot; new failure -> roll back + block + AI diagnosis
  sweep      every 30 min: any integration failing 30+ min that we haven't diagnosed -> AI diagnosis
  selftest   assert-based checks of the pure logic (no network)

Rollback is deterministic; the AI only advises/diagnoses. State: state.json next to this file.
Settings: config.json next to this file (see README). MIT License, (c) 2026 RALL DIGITAL LLC."""
import datetime, json, os, platform, re, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
LOG = os.path.join(DIR, "log.txt")

# --- settings: config.json next to this file; every key is optional, but without "notify" nobody hears anything ---
DEFAULTS = {"ai_entity": "ai_task.openai_ai_task",            # any ai_task entity
            "notify": None,                                    # e.g. "notify.mobile_app_my_phone"; no safe default
            "core_update": "update.home_assistant_core_update",
            "window": [3, 5],                                  # install core updates between 03:00 and 05:00 local
            "stuck_min": 30,                                   # sweep: diagnose an integration after it has failed this long
            "ha_url": "http://localhost:8123",                 # HA's own API, as seen from inside the core container
            "blocked_versions": []}                            # versions a human never wants installed, e.g. ["2026.10.3"]

def load_config(path):
    """(AI_ENTITY, NOTIFY, CORE_UPDATE, WINDOW, STUCK_MIN, HA_URL, BLOCKED_VERSIONS, problems). Never raises: a bad file falls back to the
    defaults, so check() can still roll back; the problems are logged on every run."""
    problems = []
    try:
        c = json.load(open(path))
        if not isinstance(c, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        c = {}
    except ValueError as e:
        c, problems = {}, [f"config.json is unreadable ({e}); using defaults"]
    problems += [f"config.json: unknown key {k!r}" for k in sorted(set(c) - set(DEFAULTS))]
    c = {**DEFAULTS, **{k: v for k, v in c.items() if k in DEFAULTS}}
    try:
        window, stuck = tuple(int(h) for h in c["window"]), int(c["stuck_min"])
        assert len(window) == 2 and 0 <= window[0] < window[1] <= 24 and stuck > 0
    except (TypeError, ValueError, AssertionError):
        problems.append(f"config.json: bad window {c['window']!r} or stuck_min {c['stuck_min']!r}; using defaults")
        window, stuck = tuple(DEFAULTS["window"]), DEFAULTS["stuck_min"]
    notify = c["notify"].replace(".", "/", 1) if isinstance(c["notify"], str) and c["notify"] else None
    if not notify:
        problems.append('config.json has no "notify" target: updates, rollbacks and diagnoses still run, '
                        'but you will not be told about them')
    ha_url = c["ha_url"].rstrip("/") if isinstance(c["ha_url"], str) else ""
    if not ha_url.startswith(("http://", "https://")):
        problems.append(f"config.json: bad ha_url {c['ha_url']!r}; using {DEFAULTS['ha_url']}")
        ha_url = DEFAULTS["ha_url"]
    blocked = c["blocked_versions"]
    if not (isinstance(blocked, list) and all(isinstance(v, str) for v in blocked)):
        problems.append(f"config.json: blocked_versions must be a list of version strings, got {blocked!r}; ignored")
        blocked = []
    return c["ai_entity"], notify, c["core_update"], window, stuck, ha_url, tuple(blocked), problems

AI_ENTITY, NOTIFY, CORE_UPDATE, WINDOW, STUCK_MIN, HA_URL, BLOCKED_VERSIONS, CONFIG_PROBLEMS = load_config(os.path.join(DIR, "config.json"))
FAILING = ("setup_error", "setup_retry", "migration_error")
STALE_MIN = 120          # a pre-update snapshot older than this means check() never ran for that install
SETTLE_MIN = 15          # pre younger than this at check() time: the install hasn't been applied yet

# ---------- pure logic (selftested) ----------
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^\S+ \S+ (WARNING|ERROR|CRITICAL) \([^)]*\) \[([^\]]+)\] (.*)$")

def failing(entries):
    return {e["entry_id"]: e["domain"] for e in entries if e["state"] in FAILING and not e.get("disabled_by")}

def new_failures(before, now):
    return {k: v for k, v in now.items() if k not in before}

def log_lines(text, domain, top=3):
    """Top repeating WARNING/ERROR messages whose logger mentions the domain (or its first word, e.g. pylutron_caseta)."""
    keys = {domain.lower(), domain.split("_")[0].lower()}
    msgs = Counter()
    for raw in text.splitlines():
        m = LINE.match(ANSI.sub("", raw).strip())
        if m and any(k in m.group(2).lower() for k in keys):
            msgs[f"[{m.group(2)}] {m.group(3)[:300]}"] += 1
    return [f"{n}x {msg}" for msg, n in msgs.most_common(top)]

def error_query(line):
    """Most searchable fragment of a log message: the text after the last ': ' if it is long enough."""
    msg = re.sub(r"^\d+x \[[^\]]+\] ", "", line)
    tail = msg.rsplit(": ", 1)[-1].strip()
    return (tail if len(tail) >= 15 else msg)[:120]

def parse_ai(text, closed_urls=()):
    """RISK/FIXED from the AI's reply; tolerates markdown. FIXED: yes only counts if it cites a closed issue we found."""
    plain = re.sub(r"[*_#`]", "", text or "")
    def field(name):
        m = re.search(rf"^\s*{name}\s*:\s*(\w+)", plain, re.I | re.M)
        return m.group(1).lower() if m else None
    r = {"risk": field("RISK"), "fixed": field("FIXED")}
    cited = set(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+", text or ""))
    if r["fixed"] == "yes" and not cited & set(closed_urls):
        r["fixed"] = "unknown"
    return r

def fix_lines(notes, domains, top=10):
    """Release-note lines that mention one of the domains (or its first word, or its pip-style name)."""
    keys = {k for d in domains for k in (d.lower(), d.lower().replace("_", "-"), d.split("_")[0].lower()) if len(k) >= 3}
    return [l.strip() for l in (notes or "").splitlines() if any(k in l.lower() for k in keys)][:top]

def decide(ai, open_issue, hour):
    if open_issue and ai["fixed"] == "yes":
        return "install"
    if ai["risk"] == "high":
        return "hold"
    return "install" if WINDOW[0] <= hour < WINDOW[1] else "wait"

def selftest():
    global WINDOW
    WINDOW = tuple(DEFAULTS["window"])                 # the asserts below assume the default window, whatever config.json says
    entries = [{"entry_id": "a", "domain": "lutron_caseta", "state": "setup_retry"},
               {"entry_id": "b", "domain": "wyoming", "state": "setup_retry"},
               {"entry_id": "c", "domain": "hue", "state": "loaded"},
               {"entry_id": "d", "domain": "tplink", "state": "setup_error", "disabled_by": "user"}]
    assert failing(entries) == {"a": "lutron_caseta", "b": "wyoming"}
    assert new_failures({"b": "wyoming"}, failing(entries)) == {"a": "lutron_caseta"}
    assert new_failures(failing(entries), failing(entries)) == {}
    logs = ("\x1b[33m2026-10-25 08:19:27.335 WARNING (MainThread) [pylutron_caseta.smartbridge] Reconnecting after error: check_hostname requires server_hostname\x1b[0m\n" * 3
            + "2026-10-25 08:19:28.000 ERROR (MainThread) [homeassistant.components.xbox.coordinator] Error fetching xbox data: timeout\n"
            + "2026-10-25 08:19:29.000 INFO (MainThread) [pylutron_caseta.smartbridge] fine\n")
    got = log_lines(logs, "lutron_caseta")
    assert got == ["3x [pylutron_caseta.smartbridge] Reconnecting after error: check_hostname requires server_hostname"], got
    assert error_query(got[0]) == "check_hostname requires server_hostname"
    assert error_query("1x [x.y] Error fetching xbox data: timeout") == "Error fetching xbox data: timeout"
    assert parse_ai("RISK: high\nFIXED: unknown\nblah") == {"risk": "high", "fixed": "unknown"}
    assert parse_ai("garbage") == {"risk": None, "fixed": None}
    assert parse_ai("**RISK:** high\n**FIXED:** no")["risk"] == "high"            # audit #2: markdown
    assert parse_ai("RISK: **high**")["risk"] == "high"
    assert parse_ai("### RISK: High")["risk"] == "high"
    u = "https://github.com/home-assistant/core/issues/1"
    assert parse_ai("RISK: low\nFIXED: yes")["fixed"] == "unknown"                 # audit #7: uncited yes
    assert parse_ai(f"RISK: low\nFIXED: yes per {u}", {u})["fixed"] == "yes"
    assert parse_ai(f"FIXED: yes per {u}64", {u})["fixed"] == "unknown"           # re-review #7a: no substring match
    assert decide({"risk": "high", "fixed": "yes"}, {"x": 1}, 14) == "install"   # fix for our open issue: go now
    assert decide({"risk": "high", "fixed": None}, None, 4) == "hold"
    assert decide({"risk": None, "fixed": None}, None, 4) == "install"            # AI failed -> window + canary
    assert decide({"risk": "low", "fixed": "no"}, {"x": 1}, 14) == "wait"
    notes = "## Fixes\n- Bump pylutron-caseta to 0.30.0 (#188388)\n- Fix hue scenes (#1)\n"
    assert fix_lines(notes, ["lutron_caseta"]) == ["- Bump pylutron-caseta to 0.30.0 (#188388)"]
    assert fix_lines(notes, []) == [] and fix_lines("", ["hue"]) == []
    selftest_config()
    selftest_flows()
    print("selftest ok")

def selftest_config():
    import tempfile
    d = tempfile.mkdtemp()
    def cfg(text):
        p = os.path.join(d, "config.json")
        with open(p, "w") as f:
            f.write(text)
        return load_config(p)
    *vals, probs = load_config(os.path.join(d, "missing.json"))
    assert vals == ["ai_task.openai_ai_task", None, "update.home_assistant_core_update", (3, 5), 30,
                    "http://localhost:8123", ()], vals
    assert len(probs) == 1 and "notify" in probs[0]                                  # missing NOTIFY is loud
    *vals, probs = cfg('{"notify": "notify.mobile_app_x", "ai_entity": "ai_task.y", "window": [1, 2], "stuck_min": 5}')
    assert vals == ["ai_task.y", "notify/mobile_app_x", "update.home_assistant_core_update", (1, 2), 5,
                    "http://localhost:8123", ()] and probs == []
    assert cfg('{"notify": "notify.a", "blocked_versions": ["2026.1.1"]}')[6] == ("2026.1.1",)
    *vals, probs = cfg('{"notify": "notify.a", "blocked_versions": "2026.1.1"}')
    assert vals[6] == () and any("blocked_versions" in p for p in probs)
    assert cfg('{"notify": "notify.a", "ha_url": "https://localhost:8123/"}')[5] == "https://localhost:8123"
    *vals, probs = cfg('{"notify": "notify.a", "ha_url": "localhost:8123"}')
    assert vals[5] == "http://localhost:8123" and any("bad ha_url" in p for p in probs)
    assert cfg('{"notify": "notify/a"}')[1] == "notify/a"                             # service-path form also works
    *vals, probs = cfg("{not json")
    assert vals[3] == (3, 5) and any("unreadable" in p for p in probs)                # bad file -> defaults, still runs
    *vals, probs = cfg('{"notify": "notify.a", "notfy": 1, "window": [5, 3]}')
    assert vals[3] == (3, 5) and any("'notfy'" in p for p in probs) and any("bad window" in p for p in probs)
    assert cfg("[]")[-1] and cfg('{"window": "3-5", "notify": "notify.a"}')[3] == (3, 5)

def selftest_flows():
    """check()/preflight()/sweep() against a fake HA, in a temp state dir."""
    import tempfile
    g = globals()
    saved = {k: g[k] for k in ("STATE", "LOG", "version", "entries", "ha", "service", "sup", "notify", "diagnose", "gh_issues", "ai", "http",
                               "BLOCKED_VERSIONS")}
    fake = {"v": "1.0", "entries": [], "calls": [], "latest": "1.1", "now": datetime.datetime(2026, 1, 1, 4, 0),
            "skipped": None, "ai": "RISK: low"}
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None): return fake["now"]
    real_dt, real_sleep = datetime.datetime, time.sleep
    d = tempfile.mkdtemp()
    def fsup(path, method="GET", body=None, **kw):
        fake["calls"].append((path, body))
    def fha(path, *a, **kw):                    # like HA: a skipped release turns the entity off but keeps latest_version
        on = fake["latest"] != fake["v"] and fake["skipped"] != fake["latest"]
        return {"state": "on" if on else "off", "attributes": {"latest_version": fake["latest"], "installed_version": fake["v"],
                                                               "skipped_version": fake["skipped"]}}
    def fservice(ds, data, **kw):
        fake["calls"].append((ds, data))
        if ds == "update/skip":
            fake["skipped"] = fake["latest"]
    g.update(STATE=os.path.join(d, "s.json"), LOG=os.path.join(d, "log"), version=lambda: fake["v"],
             entries=lambda: fake["entries"], ha=fha, sup=fsup, gh_issues=lambda *a, **k: [], ai=lambda *a: fake["ai"],
             service=fservice, BLOCKED_VERSIONS=(),
             notify=lambda t, m: fake["calls"].append(("notify", t)), diagnose=lambda ds: "diag",
             http=lambda *a, **k: {"body": ""})
    datetime.datetime, time.sleep = FakeDT, lambda s: None
    def installs(): return sum(1 for c in fake["calls"] if c[0] == "/core/update" and "version" not in (c[1] or {}))
    try:
        # audit #1: Supervisor reverts a failed start -> must skip + notify, never reinstall the same version
        preflight()                                            # 04:00, in window -> install
        assert installs() == 1
        fake["now"] += datetime.timedelta(minutes=25)          # HA back on 1.0, check fires
        check()
        assert ("update/skip", {"entity_id": CORE_UPDATE}) in fake["calls"] and "pre" not in load()
        fake["latest"] = "1.0"                                 # skip took effect: entity off
        fake["now"] += datetime.timedelta(hours=1)
        preflight()
        assert installs() == 1, "reinstalled after revert"
        # audit #3: stale snapshot with version unchanged is a failed install, not a later rollback trigger
        fake.update(calls=[], latest="1.2"); save({"last_good": "1.0", "pre": {"version": "1.0", "failing": {}, "at": (fake["now"] - datetime.timedelta(hours=3)).isoformat()}})
        sweep()
        assert "pre" not in load() and ("update/skip", {"entity_id": CORE_UPDATE}) in fake["calls"]
        # audit #4: a failure that clears within the re-check window does not roll back
        fake.update(calls=[], v="1.2", entries=[{"entry_id": "x", "domain": "hue", "state": "setup_retry"}])
        save({"last_good": "1.0", "pre": {"version": "1.0", "failing": {}, "at": (fake["now"] - datetime.timedelta(minutes=20)).isoformat()}})
        time.sleep = lambda s: fake.update(entries=[])         # recovers during the re-check wait
        check()
        assert not any(c[0] == "/core/update" for c in fake["calls"]) and load()["last_good"] == "1.2"
        # real breakage: rolls back to the pre-update version
        fake.update(calls=[], entries=[{"entry_id": "x", "domain": "hue", "state": "setup_retry"}])
        save({"last_good": "1.0", "pre": {"version": "1.0", "failing": {}, "at": (fake["now"] - datetime.timedelta(minutes=20)).isoformat()}})
        time.sleep = lambda s: None
        check()
        assert ("/core/update", {"version": "1.0", "backup": False}) in fake["calls"] and load()["bad_version"] == "1.2"
        # re-review #6: still on the bad version at the next start (rollback never happened) -> retry once
        fake["calls"] = []
        check()
        assert ("/core/update", {"version": "1.0", "backup": False}) in fake["calls"]
        # re-review NEW: rollback failed for good, then a newer release (1.3) installs and breaks -> must still roll back
        s = load(); s["pre"] = {"version": "1.2", "failing": {}, "at": (fake["now"] - datetime.timedelta(minutes=20)).isoformat()}; save(s)
        fake.update(calls=[], v="1.3", entries=[{"entry_id": "z", "domain": "zha", "state": "setup_error"}])
        check()
        assert ("/core/update", {"version": "1.2", "backup": False}) in fake["calls"], fake["calls"]
        assert ("notify", "HA rolled back to 1.3") not in fake["calls"]
        skip = ("update/skip", {"entity_id": CORE_UPDATE})
        def fresh(at, **kw):
            fake.update({"calls": [], "v": "1.0", "latest": "1.1", "skipped": None, "entries": [], "ai": "RISK: low", "now": at, **kw})
            save({})
        # v1.1 (a): waiting outside the window skips (badge gone), and still installs at 03:xx though HA shows it skipped
        fresh(datetime.datetime(2026, 2, 1, 14, 0))
        preflight()
        assert installs() == 0 and skip in fake["calls"] and fha("")["state"] == "off"
        fake["now"] = datetime.datetime(2026, 2, 2, 3, 30)
        preflight()
        assert installs() == 1, fake["calls"]
        # v1.1 (b): a blocked version is never installed, even in the window (state list and config key)
        fresh(datetime.datetime(2026, 2, 3, 4, 0))
        save({"blocked": ["1.1"]})
        preflight()
        assert installs() == 0 and skip in fake["calls"]
        fresh(datetime.datetime(2026, 2, 3, 4, 0))
        g["BLOCKED_VERSIONS"] = ("1.1",)
        preflight()
        assert installs() == 0
        g["BLOCKED_VERSIONS"] = ()
        # v1.1 (c): a failed install (Supervisor revert) is blocked and never reinstalled, though HA still offers it
        fresh(datetime.datetime(2026, 2, 4, 4, 0))
        preflight()
        fake["now"] += datetime.timedelta(minutes=25)
        check()                                                # back on 1.0
        assert installs() == 1 and "1.1" in load()["blocked"]
        fake.update(skipped=None)                              # even if a human un-skips it in HA
        fake["now"] += datetime.timedelta(minutes=30)
        preflight()
        assert installs() == 1, "reinstalled a failed version"
        # v1.1 (d): a hold skips and notifies once
        fresh(datetime.datetime(2026, 2, 5, 4, 0), ai="RISK: high")
        preflight()
        fake.update(skipped=None)
        fake["now"] += datetime.timedelta(hours=1)
        preflight()
        assert installs() == 0 and fake["calls"].count(skip) == 2
        assert fake["calls"].count(("notify", "HA 1.1 held")) == 1, fake["calls"]
        # v1.1: a rolled-back bad version is blocked
        fresh(datetime.datetime(2026, 2, 6, 4, 0), v="1.1", entries=[{"entry_id": "x", "domain": "hue", "state": "setup_retry"}])
        save({"last_good": "1.0", "pre": {"version": "1.0", "failing": {}, "at": (fake["now"] - datetime.timedelta(minutes=20)).isoformat()}})
        check()
        assert "1.1" in load()["blocked"]
    finally:
        datetime.datetime, time.sleep = real_dt, real_sleep
        g.update(saved)

# ---------- side effects ----------
def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")

def http(url, method="GET", body=None, headers=None, timeout=60, raw=False):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read().decode(errors="replace")
    return data if raw else (json.loads(data) if data else None)

def sup(path, method="GET", body=None, raw=False, timeout=60, headers=None):
    h = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}", **(headers or {})}
    return http(f"http://supervisor{path}", method, body, h, timeout, raw)

def ha(path, method="GET", body=None, timeout=60):
    # the core container's SUPERVISOR_TOKEN works for Supervisor endpoints but is refused by the
    # http://supervisor/core/api proxy (401), so HA's own API is called directly (HA_URL) with a long-lived token
    tok = open(os.path.join(DIR, ".token")).read().strip()
    return http(f"{HA_URL}/api{path}", method, body, {"Authorization": f"Bearer {tok}"}, timeout)

def service(domain_service, data, response=False, timeout=60):
    return ha(f"/services/{domain_service}" + ("?return_response" if response else ""), "POST", data, timeout)

def notify(title, message):
    log(f"NOTIFY {title}: {message[:500]}")
    if not NOTIFY:
        return log('notify skipped: no "notify" target in config.json')
    try:
        service(NOTIFY, {"title": title, "message": message[:1500]})
    except Exception as e:
        log(f"notify failed: {e}")

def ai(task, prompt):
    try:
        r = service("ai_task/generate_data", {"task_name": task, "entity_id": AI_ENTITY, "instructions": prompt},
                    response=True, timeout=180)
        return str(r["service_response"]["data"])
    except Exception as e:
        log(f"ai failed: {e}")
        return ""

def gh_issues(q, n=5, closed=None):
    """Issue lines for the AI prompt. Bodies are untrusted text: the AI can only phrase advice and set RISK/FIXED."""
    try:
        r = http("https://api.github.com/search/issues?" + urllib.parse.urlencode({"q": q, "sort": "updated", "per_page": n}),
                 headers={"Accept": "application/vnd.github+json", "User-Agent": "ha-smart-updater"})
        if closed is not None:
            closed.update(i["html_url"] for i in r["items"] if i["state"] == "closed")
        return [f"- {i['title']} [{i['state']}] {i['html_url']}\n  {(i.get('body') or '')[:400]!r}" for i in r["items"]]
    except Exception as e:
        log(f"github search failed: {e}")
        return []

def load():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {}

def save(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)

def version():
    return ha("/config")["version"]

def entries():
    return ha("/config/config_entries/entry")

def core_logs():
    return sup("/core/logs", raw=True, timeout=120, headers={"Accept": "text/plain", "Range": "entries=:-20000:"})

def diagnose(domains):
    logs = core_logs()
    lines = {d: log_lines(logs, d) for d in domains}
    issues = []
    for d, ls in lines.items():
        if ls:
            issues += gh_issues(f'"{error_query(ls[0])}" {d.split("_")[0]} is:issue')   # name keeps it out of unrelated repos
        issues += gh_issues(f"repo:home-assistant/core is:issue {d} {version()}", 3)
    prompt = f"""You are diagnosing a Home Assistant integration failure, like a senior engineer would.
Home Assistant Core {version()}, Python {platform.python_version()}.
Failing integrations: {', '.join(domains)}.
Top repeating log lines per integration:
{json.dumps(lines, indent=1)}
GitHub issues found by searching the error text:
{chr(10).join(issues) or '(none found)'}

Answer in plain English for a busy homeowner, max 8 short lines:
CAUSE: the root cause in one sentence (is it an upstream regression, a device/network problem, or a config problem?)
KNOWN BUG: the most relevant issue URL, or "none found"
FIXED IN: the version or PR that fixes it if the issues say so, else "unknown"
DO: the one thing to do (e.g. "nothing - rolled back, will retry when fixed", "check the device's power/network")
Only cite URLs from the list above. If the issues show many users hit the same error after an update, it is an
upstream regression, not this house's device or network - say so plainly and don't suggest checking the device."""
    return ai("smart_updater_diagnose", prompt) or ("AI unavailable. Log lines: " + json.dumps(lines)[:800])

def age_min(pre):
    return (datetime.datetime.now() - datetime.datetime.fromisoformat(pre["at"])).total_seconds() / 60

def failed_install(s, why):
    """The release never took (request refused, or the Supervisor reverted a failed start): block it so we don't loop."""
    latest = ha(f"/states/{CORE_UPDATE}")["attributes"].get("latest_version")
    s.pop("pre", None)
    s["open_issue"] = {"version": latest, "domains": [], "diagnosis": why, "at": datetime.datetime.now().isoformat()}
    block(s, latest)
    save(s)
    clear_badge()
    notify(f"HA {latest} did not install", f"{why} It is blocked; the next release will be tried automatically.")

def is_blocked(s, v):
    return v in s.get("blocked", []) or v in BLOCKED_VERSIONS

def block(s, v):
    """Our own never-install list. HA's skipped_version can't be it: we skip releases just to hide the badge."""
    if v and v not in s.setdefault("blocked", []):
        s["blocked"].append(v)

def clear_badge():
    try:
        service("update/skip", {"entity_id": CORE_UPDATE})
    except Exception as e:
        log(f"skip failed: {e!r}")

def expire_stale_pre(s, v):
    """check() never ran for an install. Same version -> it never took; new version -> unchecked, just forget it."""
    pre = s.get("pre")
    if not pre or age_min(pre) <= STALE_MIN:
        return
    log(f"stale pre-update snapshot from {pre['at']} (pre {pre['version']}, now {v})")
    if pre["version"] == v:
        return failed_install(s, f"An install started at {pre['at']} never took effect.")
    s.pop("pre")
    save(s)

def rollback(target):
    for attempt in (1, 2):
        try:
            return sup("/core/update", "POST", {"version": target, "backup": False}, timeout=1800)
        except (urllib.error.URLError, OSError) as e:   # HTTPError is a URLError; timeouts are OSError
            log(f"rollback attempt {attempt} failed: {e!r}")
            time.sleep(60)
    notify("HA rollback FAILED", f"Could not roll back to {target}. Do it by hand: ha core update --version {target}")

def install(s, v_now):
    s["pre"] = {"version": v_now, "failing": failing(entries()), "at": datetime.datetime.now().isoformat()}
    s.setdefault("last_good", v_now)
    save(s)
    log(f"installing core update from {v_now}")
    try:
        sup("/core/update", "POST", {"backup": True}, timeout=1800)
    except urllib.error.HTTPError as e:      # a real refusal; our own process dying in the restart never gets here
        latest = ha(f"/states/{CORE_UPDATE}")["attributes"].get("latest_version")
        n = s.get("refusals", {}).get(latest, 0) + 1
        s["refusals"] = {latest: n}
        s.pop("pre", None)
        save(s)
        log(f"install refused ({n}x): {e!r}")
        if n >= 2:                            # once may be transient (job running, low disk); twice: skip it
            s.pop("refusals")
            failed_install(s, f"The Supervisor refused the update twice ({e!r}).")

def preflight():
    s = load()
    st = ha(f"/states/{CORE_UPDATE}")
    latest, v_now = st["attributes"].get("latest_version"), version()
    if not latest or latest == (st["attributes"].get("installed_version") or v_now):   # skipped still counts as pending
        return log("preflight: no core update pending")
    badge = st["state"] == "on"
    if is_blocked(s, latest):
        if badge:
            clear_badge()
        return log(f"preflight {latest}: blocked, not installing")
    expire_stale_pre(s, v_now)
    if s.get("pre"):
        return log("preflight: install already in progress")
    cache = s.get("preflight", {})
    if cache.get("version") != latest or cache.get("date") != datetime.datetime.now().date().isoformat():
        s["preflight"] = cache = assess(s, v_now, latest, cache)
        save(s)
    d = decide(cache["ai"], s.get("open_issue"), datetime.datetime.now().hour)
    log(f"preflight {latest}: {cache['ai']} -> {d}")
    if d == "hold" and not cache["notified"]:
        cache["notified"] = True
        save(s)
        notify(f"HA {latest} held", f"Not installing yet - the AI found risk for this house:\n{cache['text'][:1200]}")
    if d in ("wait", "hold") and badge:
        clear_badge()                             # hides HA's badge only; preflight still installs it later
    if d == "install":
        install(s, v_now)

def dryrun(latest):
    """preflight's AI assessment for any version, without installing or touching state."""
    s = load()
    c = assess(s, version(), latest, {})
    d = "blocked" if is_blocked(s, latest) else decide(c["ai"], s.get("open_issue"), datetime.datetime.now().hour)
    log(f"DRYRUN {latest}: {c['ai']} -> {d}\n{c['text']}")

def assess(s, v_now, latest, cache):
    try:
        body = http(f"https://api.github.com/repos/home-assistant/core/releases/tags/{latest}",
                    headers={"User-Agent": "ha-smart-updater"}).get("body", "") or ""
        notes = body[:8000]
    except Exception:
        body, notes = "", "(release notes unavailable)"
    domains = sorted({e["domain"] for e in entries() if e["state"] == "loaded"})
    closed = set()
    issues = gh_issues(f'repo:home-assistant/core is:issue "{latest}"', 15)
    oi = s.get("open_issue")
    for d in (oi or {}).get("domains", []):
        since = (oi.get("at") or "")[:10]         # only issues closed after we hit the problem count as a fix
        issues += gh_issues(f"repo:home-assistant/core is:issue is:closed {d}" + (f" closed:>={since}" if since else ""), 5, closed)
    fixes = fix_lines(body, (oi or {}).get("domains", []))
    prompt = f"""Home Assistant Core update {v_now} -> {latest} is pending. Decide if it is safe for THIS house.
Integrations in use: {', '.join(domains)}
Release notes (truncated):
{notes}
Recent GitHub issues mentioning {latest} ([open] = unresolved, [closed] = resolved):
{chr(10).join(issues) or '(none)'}
{'Open problem we rolled back from: ' + json.dumps(oi) if oi else ''}
{'Release-note lines (full notes) that mention that problem' + chr(39) + 's integrations:' + chr(10) + chr(10).join(fixes) if fixes else ''}

Rules:
- Only an [open] issue reported against {latest} itself, for an integration in use, is evidence of risk.
- A [closed] issue is resolved. It is never risk for {latest}, even if it mentions {latest} or an integration in use.
- A fix listed in the release notes counts AGAINST risk, never for it.
- A release-note line that fixes the open problem's integration is evidence for FIXED: yes; also cite the [closed]
  issue URL above that it resolves.

Reply with exactly these first two lines, then up to 5 short lines of explanation citing issue URLs from above:
RISK: high or low   (high only if there is credible evidence {latest} breaks an integration in use)
FIXED: yes, no or unknown   (does {latest} fix the open problem above? "unknown" if there is none)"""
    text = ai("smart_updater_preflight", prompt)
    cache = {"version": latest, "date": datetime.datetime.now().date().isoformat(), "ai": parse_ai(text, closed), "text": text,
             "notified": cache.get("version") == latest and cache.get("notified", False)}   # one hold text per version
    return cache

def check():
    s, v = load(), version()
    bad = s.get("bad_version")
    target = (s.get("pre") or {}).get("version") or s.get("last_good")
    if bad and v == bad:                          # the rollback never happened (failed, or HA restarted mid-diagnose)
        if not s.get("rollback_retried"):
            s["rollback_retried"] = True
            save(s)
            return rollback(target)
        if not s.get("rollback_failed_notified"):
            s["rollback_failed_notified"] = True
            save(s)
            notify("HA still on broken version", f"Still on {v}. Roll back by hand: ha core update --version {target}")
        return
    if bad and v != target:                       # a newer release replaced the broken one: forget it, check normally
        for k in ("bad_version", "rollback_retried", "rollback_failed_notified"):
            s.pop(k, None)
        save(s)
        bad = None
    if bad:
        for k in ("bad_version", "pre", "rollback_retried", "rollback_failed_notified"):
            s.pop(k, None)
        save(s)
        block(s, bad)
        save(s)
        st = ha(f"/states/{CORE_UPDATE}")
        if st["attributes"].get("latest_version") == bad and st["state"] == "on":
            clear_badge()
        return notify(f"HA rolled back to {v}", f"{bad} broke something, so it was undone and blocked. "
                      f"The next release will be tried automatically.\n\n{s.get('open_issue', {}).get('diagnosis', '')}")
    expire_stale_pre(s, v)
    pre = s.get("pre")
    if not pre:
        s.setdefault("last_good", v)
        save(s)
        return log(f"check: no update in flight ({v}), nothing to do")
    if pre["version"] == v:                       # audit #1: same version after an install = it didn't take
        if age_min(pre) < SETTLE_MIN:
            return log("check: install requested but not applied yet")
        return failed_install(s, f"The update did not take - HA came back on {v} (the Supervisor likely reverted a failed start).")
    new = new_failures(pre["failing"], failing(entries()))
    if new:                                       # audit #4: one sample is not enough at 3-5am; look again
        log(f"check: new failures {sorted(set(new.values()))}; re-checking in 10 min")
        time.sleep(600)
        s = load()                                # sweep() may have written meanwhile
        new = new_failures(pre["failing"], failing(entries()))
    if new:
        domains = sorted(set(new.values()))
        s["bad_version"] = v
        block(s, v)
        s["open_issue"] = {"version": v, "domains": domains, "diagnosis": "(diagnosing)", "at": datetime.datetime.now().isoformat()}
        save(s)                                   # persist before anything slow or restart-y
        log(f"check: {v} broke {domains}; rolling back to {pre['version']}")
        s["open_issue"]["diagnosis"] = diagnose(domains)
        s.setdefault("diagnosed", {}).update({k: "update" for k in new})
        save(s)
        notify(f"HA {v} broke {', '.join(domains)}", f"Rolling back to {pre['version']} now.\n\n{s['open_issue']['diagnosis']}")
        rollback(pre["version"])
    else:
        s["last_good"] = v
        s.pop("pre", None)
        if s.get("open_issue"):
            s.pop("open_issue")
        save(s)
        notify(f"HA updated to {v}", "All integrations healthy after the update.")

def sweep():
    s = load()
    expire_stale_pre(s, version())                # audit #1/#3: a stuck snapshot must not silence the sweep forever
    now, ts = failing(entries()), datetime.datetime.now()
    if "first_seen" not in s:                     # first run: today's failures are the known baseline
        s["first_seen"] = {k: ts.isoformat() for k in now}
        s["diagnosed"] = {k: "baseline" for k in now}
        save(s)
        return log(f"sweep: baseline {sorted(set(now.values()))}")
    seen, diagnosed = s["first_seen"], s.setdefault("diagnosed", {})
    for k in list(seen):
        if k not in now:
            seen.pop(k); diagnosed.pop(k, None)   # recovered: forget it so a relapse is diagnosed again
    for k in now:
        seen.setdefault(k, ts.isoformat())
    due = {k: d for k, d in now.items() if k not in diagnosed
           and (ts - datetime.datetime.fromisoformat(seen[k])).total_seconds() >= STUCK_MIN * 60}
    if s.get("pre"):
        due = {}                                  # mid-update: check() owns it
    for k in due:
        diagnosed[k] = ts.isoformat()
    save(s)
    if due:
        domains = sorted(set(due.values()))
        notify(f"HA: {', '.join(domains)} broken {STUCK_MIN}+ min", diagnose(domains))
    log(f"sweep: failing={sorted(set(now.values()))} diagnosed_now={sorted(set(due.values()))}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "selftest":
        selftest(); sys.exit()
    if cmd != "selftest":
        for p in CONFIG_PROBLEMS:
            log(f"CONFIG ERROR: {p}")
    if cmd == "probe":
        log(f"probe config: ai_entity={AI_ENTITY} notify={NOTIFY} core_update={CORE_UPDATE} window={WINDOW} stuck_min={STUCK_MIN} ha_url={HA_URL}")
        for name, f in (("supervisor /core/info", lambda: sup("/core/info")["data"]["version"]), ("ha /config", version)):
            try: log(f"probe {name}: ok {f()}")
            except Exception as e: log(f"probe {name}: FAIL {e!r}")
        sys.exit()
    if cmd not in ("preflight", "check", "sweep", "diagnose", "dryrun"):
        sys.exit(f"usage: {sys.argv[0]} preflight|check|sweep|diagnose <domain..>|dryrun <version>|selftest")
    fg = "--fg" in sys.argv
    if not fg:                                    # detach: shell_command times out at 60s and waits for pipe EOF
        if os.fork():
            sys.exit()
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
    try:
        if cmd == "diagnose":
            out = diagnose([a for a in sys.argv[2:] if a != "--fg"])
            log(f"DIAGNOSE {out}")
            if fg:
                print(out)
        elif cmd == "dryrun":
            dryrun(sys.argv[2])
        else:
            globals()[cmd]()
    except Exception as e:
        log(f"{cmd} failed: {e!r}")
        if cmd != "sweep":
            notify("HA smart updater error", f"{cmd} failed: {e!r}")
