#!/usr/bin/env python3
"""Smart updater for Home Assistant Core. Runs inside HA core via shell_command.

  preflight  core update pending: AI reads release notes + GitHub issues; soak, hold, install now, or wait for the window
  check      on HA start: diff failing integrations vs pre-update snapshot; new failure -> roll back + block + AI diagnosis
  sweep      every 30 min: a NEW integration failure that had been healthy -> one plain-text alert
  heartbeat  every minute: liveness file; a gap over stall_min is reported once, with an incident snapshot
  backup_health  daily: newest Supervisor backup under 36h old and containing the HA config
  status     rewrite /config/www/smart_updater_status.json for the dashboard
  selftest   assert-based checks of the pure logic (no network)

Rollback is deterministic; the AI only advises/diagnoses. State: state.json next to this file.
Settings: config.json next to this file (see README). MIT License, (c) 2026 RALL DIGITAL LLC."""
import datetime, json, os, platform, re, shutil, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
LOG = os.path.join(DIR, "log.txt")

# --- settings: config.json next to this file; every key is optional, but without "notify" nobody hears anything ---
DEFAULTS = {"ai_entity": "ai_task.openai_ai_task",            # any ai_task entity
            "notify": None,                                    # e.g. "notify.mobile_app_my_phone"; no safe default
            "core_update": "update.home_assistant_core_update",
            "window": [3, 5],                                  # install core updates between 03:00 and 05:00 local
            "soak_hours": 168,                                 # wait this long after a release before installing it (0 = off)
            "stuck_min": 30,                                   # sweep: an integration failing this long is eligible to alert
            "stall_min": 15,                                   # heartbeat: a gap this long is a stall worth reporting
            "ignore": [],                                      # domains that are known-broken and must never alert
            "watch": [],                                       # domains always shown on the dashboard, healthy or not
            "jobs": {},                                        # the house's jobs: {"Lights": ["lutron_caseta", "hue"]}
            "ha_url": "http://localhost:8123",                 # HA's own API, as seen from inside the core container
            "www_dir": "/config/www",                          # where the dashboard's status JSON is written
            "blocked_versions": []}                            # versions a human never wants installed, e.g. ["2026.10.3"]

def load_config(path):
    """All settings as a tuple in module order, plus a list of problems. Never raises: a bad file falls back to
    the defaults, so check() can still roll back; the problems are logged on every run."""
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
    try:
        soak, stall = int(c["soak_hours"]), int(c["stall_min"])
        assert soak >= 0 and stall > 0
    except (TypeError, ValueError, AssertionError):
        problems.append(f"config.json: bad soak_hours {c['soak_hours']!r} or stall_min {c['stall_min']!r}; using defaults")
        soak, stall = DEFAULTS["soak_hours"], DEFAULTS["stall_min"]
    ignored = c["ignore"]
    if not (isinstance(ignored, list) and all(isinstance(v, str) for v in ignored)):
        problems.append(f"config.json: ignore must be a list of domain strings, got {ignored!r}; ignored")
        ignored = []
    watch = c["watch"]
    if not (isinstance(watch, list) and all(isinstance(v, str) for v in watch)):
        problems.append(f"config.json: watch must be a list of domain strings, got {watch!r}; ignored")
        watch = []
    jobs = c["jobs"]
    if not (isinstance(jobs, dict) and all(isinstance(k, str) and isinstance(v, list)
                                          and all(isinstance(d, str) for d in v) for k, v in jobs.items())):
        problems.append(f"config.json: jobs must map a name to a list of domains, got {jobs!r}; ignored")
        jobs = {}
    www = c["www_dir"]
    if not (isinstance(www, str) and www):
        problems.append(f"config.json: bad www_dir {c['www_dir']!r}; using {DEFAULTS['www_dir']}")
        www = DEFAULTS["www_dir"]
    return (c["ai_entity"], notify, c["core_update"], window, soak, stuck, stall, ha_url,
            tuple(blocked), tuple(ignored), tuple(watch), dict(jobs), www, problems)

(AI_ENTITY, NOTIFY, CORE_UPDATE, WINDOW, SOAK_HOURS, STUCK_MIN, STALL_MIN, HA_URL, BLOCKED_VERSIONS,
 IGNORE, WATCH, JOBS, WWW_DIR, CONFIG_PROBLEMS) = load_config(os.path.join(DIR, "config.json"))
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

def parse_concerns(text, top=5):
    """CONCERN: <token> | <why> lines from the first AI pass."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*CONCERN\s*:\s*([^|—–]+?)\s*\|\s*(.*)$", line, re.I)
        if not m:
            continue
        token = m.group(1).strip().strip("`'\"")
        if not token or len(token) > 60:
            continue
        out.append({"token": token, "why": plain(m.group(2), 160)})
    return out[:top]

def config_paths(base="/config"):
    paths = [os.path.join(base, f) for f in ("configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml")]
    for sub, ext in (("packages", ".yaml"), ("custom_templates", ".jinja")):
        try:
            paths += [os.path.join(base, sub, f) for f in sorted(os.listdir(os.path.join(base, sub))) if f.endswith(ext)]
        except OSError:
            pass
    return [p for p in paths if os.path.exists(p)]

def verify_concerns(concerns, domains=(), base="/config"):
    """Does this house use the thing each concern names? Counts only; file contents never leave the box."""
    paths = config_paths(base)
    by_plat = entities_by_platform() if base == "/config" else {}
    out = []
    for c in concerns:
        tok = c["token"]
        files = {}
        for p in paths:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    n = f.read().count(tok)
            except OSError:
                continue
            if n:
                files[os.path.basename(p)] = n
        ents_hit = len(by_plat.get(tok, []))          # entities provided by that integration
        configured = tok in set(domains)
        out.append({**c, "files": files, "entities": ents_hit, "configured": configured,
                    "used": bool(files) or ents_hit > 0 or configured})
    return out

def fix_lines(notes, domains, top=10):
    """Release-note lines that mention one of the domains (or its first word, or its pip-style name)."""
    keys = {k for d in domains for k in (d.lower(), d.lower().replace("_", "-"), d.split("_")[0].lower()) if len(k) >= 3}
    return [l.strip() for l in (notes or "").splitlines() if any(k in l.lower() for k in keys)][:top]

def decide(ai, hour, soak_left=0):
    """install | wait | hold. Soak first; a high risk always holds, even for a release that fixes our old problem;
    otherwise install only inside the window. FIXED is informational and never jumps either rule."""
    if soak_left > 0:
        return "wait"
    if ai["risk"] == "high":
        return "hold"
    return "install" if WINDOW[0] <= hour < WINDOW[1] else "wait"

def parse_dt(s):
    return datetime.datetime.fromisoformat((s or "").replace("Z", "+00:00"))

def soak_remaining(cache):
    """Hours left before this release may install. Unknown publish date holds the full soak (conservative)."""
    if SOAK_HOURS <= 0:
        return 0.0
    pub = (cache or {}).get("published_at")
    if not pub:
        return float(SOAK_HOURS)
    try:
        p = parse_dt(pub)
    except (ValueError, TypeError, AttributeError):
        return float(SOAK_HOURS)
    now = datetime.datetime.now(p.tzinfo) if p.tzinfo else datetime.datetime.now()
    return max(0.0, SOAK_HOURS - (now - p).total_seconds() / 3600)

def plain(text, limit=400):
    """AI markdown -> one plain line for an iPhone notification."""
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text or "")
    t = re.sub(r"(?m)^\s*#+\s*", "", t)                # heading markers only; keep issue numbers like #1
    t = re.sub(r"[*`]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]

def summarize(diag, limit=320):
    """Keep the two lines a person acts on, not the AI's whole scratchpad."""
    lines = [re.sub(r"[*_`#]+", "", l).strip() for l in (diag or "").splitlines() if l.strip()]
    keep = [l for l in lines if re.match(r"(CAUSE|DO)\s*:", l, re.I)][:2] or lines[:2]
    return plain(" ".join(keep), limit)

def selftest():
    global WINDOW, SOAK_HOURS
    WINDOW = tuple(DEFAULTS["window"])                 # the asserts below assume the default settings, whatever config.json says
    SOAK_HOURS = DEFAULTS["soak_hours"]
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
    assert decide({"risk": "high", "fixed": "yes"}, 4) == "hold"                 # a fix never outranks a new risk
    assert decide({"risk": "high", "fixed": None}, 4) == "hold"
    assert decide({"risk": None, "fixed": None}, 4) == "install"                  # AI failed -> window + canary
    assert decide({"risk": "low", "fixed": "no"}, 14) == "wait"
    assert decide({"risk": "low", "fixed": "yes"}, 14) == "wait"                  # a fix waits for the window
    assert decide({"risk": "low", "fixed": "yes"}, 4) == "install"
    notes = "## Fixes\n- Bump pylutron-caseta to 0.30.0 (#188388)\n- Fix hue scenes (#1)\n"
    assert fix_lines(notes, ["lutron_caseta"]) == ["- Bump pylutron-caseta to 0.30.0 (#188388)"]
    assert fix_lines(notes, []) == [] and fix_lines("", ["hue"]) == []
    # v2: soak gate, plain-text formatter, summarizer
    hours_ago = lambda h: (datetime.datetime.now() - datetime.timedelta(hours=h)).isoformat()
    assert soak_remaining({"published_at": hours_ago(1)}) > 160
    assert soak_remaining({"published_at": hours_ago(200)}) == 0
    assert soak_remaining({}) == float(SOAK_HOURS)                                # unknown publish date holds
    assert soak_remaining({"published_at": "garbage"}) == float(SOAK_HOURS)
    assert decide({"risk": "low", "fixed": None}, 4, soak_left=10) == "wait"
    assert decide({"risk": "low", "fixed": None}, 4, soak_left=0) == "install"
    # v2.2: concerns are parsed, then verified against this house's own config
    cs = parse_concerns("RISK: high\nCONCERN: lutron_caseta | TLS change\nCONCERN: `template` | new option\nnot a concern")
    assert cs == [{"token": "lutron_caseta", "why": "TLS change"}, {"token": "template", "why": "new option"}], cs
    import tempfile
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "packages"))
    open(os.path.join(d, "configuration.yaml"), "w").write("lutron_caseta:\n  host: x\n")
    open(os.path.join(d, "automations.yaml"), "w").write("- alias: a\n  service: light.turn_on\n")
    open(os.path.join(d, "packages", "p.yaml"), "w").write("# lutron_caseta again\n")
    v = verify_concerns([{"token": "lutron_caseta", "why": "x"}, {"token": "no_such_thing", "why": "y"}], ("hue",), d)
    assert v[0]["files"] == {"configuration.yaml": 1, "p.yaml": 1} and v[0]["used"] and not v[0]["configured"], v
    assert v[1]["used"] is False and v[1]["files"] == {}, v
    assert verify_concerns([{"token": "hue", "why": ""}], ("hue",), d)[0]["used"] is True
    assert plain("**CAUSE:** boom\n[issue #1](https://x/1)") == "CAUSE: boom issue #1 (https://x/1)"
    assert plain("a\n\nb") == "a b" and len(plain("x" * 900)) == 400
    assert summarize("X\nCAUSE: a\nKNOWN BUG: b\nDO: c") == "CAUSE: a DO: c"
    assert summarize("") == ""
    selftest_network()
    selftest_config()
    selftest_flows()
    selftest_v2()
    print("selftest ok")

def selftest_network():
    def st(name, v, eid):
        return {"entity_id": eid, "state": v, "attributes": {"friendly_name": name}}
    states = {
        "sensor.north_cpu": st("North CPU usage", "10", "sensor.north_cpu"),
        "sensor.north_mem": st("North Memory usage", "85", "sensor.north_mem"),
        "sensor.north_status": st("North Device status", "connected", "sensor.north_status"),
        "update.north_fw": st("North Firmware", "off", "update.north_fw"),
        "sensor.jet_poe1": st("Jetstream Port 1 PoE power", "5.1", "sensor.jet_poe1"),
        "switch.jet_poe1": st("Jetstream Port 1 PoE", "on", "switch.jet_poe1"),
        "binary_sensor.r_port4": st("Gigabit VPN Router Port 4 LAN status", "off", "binary_sensor.r_port4"),
        "device_tracker.phone": st("Phone", "home", "device_tracker.phone"),
    }
    p = parse_network(states, list(states))
    assert [d["name"] for d in p["devices"]] == ["Gigabit VPN Router", "Jetstream", "North"], p["devices"]
    north = p["devices"][2]
    assert (north["cpu"], north["mem"], north["status"], north["fw"]) == (10.0, 85.0, "connected", "current"), north
    assert p["devices"][1]["ports"]["Port 1"] == {"poe_w": 5.1, "poe": "on"}, p["devices"][1]
    assert p["devices"][0]["ports"]["Port 4"] == {"links": {"LAN status": "off"}}, p["devices"][0]
    assert p["clients"] == [{"name": "Phone", "state": "home"}], p["clients"]
    assert parse_network({}, ["a"]) == {"devices": [], "clients": []}
    assert network_warnings(p) == ["North memory at 85%"], network_warnings(p)
    h = [{"day": "2026-10-29", "d": {"North": [10.0, 70.0]}},
         {"day": "2026-10-30", "d": {"North": [10.0, 85.0]}}]
    assert network_change(h) == ["North memory 70% to 85%"], network_change(h)
    assert network_change(h[:1]) == [] and network_change(h, step=30) == []     # one sample, or below the step
    h2 = network_history([{"day": "2026-10-30", "d": {"North": [1.0, 2.0]}}], p, day="2026-10-30")
    assert len(h2) == 1 and h2[0]["d"]["North"] == [10.0, 85.0], h2               # same day replaces, never doubles
    assert len([h for h in network_history([], p)]) == 1

def selftest_config():
    import tempfile
    d = tempfile.mkdtemp()
    def cfg(text):
        p = os.path.join(d, "config.json")
        with open(p, "w") as f:
            f.write(text)
        return load_config(p)
    *vals, probs = load_config(os.path.join(d, "missing.json"))
    assert vals == ["ai_task.openai_ai_task", None, "update.home_assistant_core_update", (3, 5), 168, 30, 15,
                    "http://localhost:8123", (), (), (), {}, "/config/www"], vals
    assert len(probs) == 1 and "notify" in probs[0]                                  # missing NOTIFY is loud
    *vals, probs = cfg('{"notify": "notify.mobile_app_x", "ai_entity": "ai_task.y", "window": [1, 2], "stuck_min": 5}')
    assert vals == ["ai_task.y", "notify/mobile_app_x", "update.home_assistant_core_update", (1, 2), 168, 5, 15,
                    "http://localhost:8123", (), (), (), {}, "/config/www"] and probs == []
    assert cfg('{"notify": "notify.a", "blocked_versions": ["2026.1.1"]}')[8] == ("2026.1.1",)
    *vals, probs = cfg('{"notify": "notify.a", "blocked_versions": "2026.1.1"}')
    assert vals[8] == () and any("blocked_versions" in p for p in probs)
    assert cfg('{"notify": "notify.a", "ha_url": "https://localhost:8123/"}')[7] == "https://localhost:8123"
    *vals, probs = cfg('{"notify": "notify.a", "ha_url": "localhost:8123"}')
    assert vals[7] == "http://localhost:8123" and any("bad ha_url" in p for p in probs)
    assert cfg('{"notify": "notify/a"}')[1] == "notify/a"                             # service-path form also works
    *vals, probs = cfg("{not json")
    assert vals[3] == (3, 5) and any("unreadable" in p for p in probs)                # bad file -> defaults, still runs
    *vals, probs = cfg('{"notify": "notify.a", "notfy": 1, "window": [5, 3]}')
    assert vals[3] == (3, 5) and any("'notfy'" in p for p in probs) and any("bad window" in p for p in probs)
    assert cfg("[]")[-1] and cfg('{"window": "3-5", "notify": "notify.a"}')[3] == (3, 5)
    *vals, probs = cfg('{"notify": "notify.a", "soak_hours": -1, "stall_min": 0, "ignore": "wyoming"}')
    assert vals[4] == 168 and vals[6] == 15 and vals[9] == () and len(probs) >= 2
    assert cfg('{"notify": "notify.a", "soak_hours": 0, "ignore": ["wyoming"], "www_dir": "/tmp/w"}')[4] == 0
    assert cfg('{"notify": "notify.a", "ignore": ["wyoming"], "www_dir": "/tmp/w"}')[9] == ("wyoming",)
    assert cfg('{"notify": "notify.a", "watch": ["lutron_caseta"]}')[10] == ("lutron_caseta",)
    assert cfg('{"notify": "notify.a", "jobs": {"Lights": ["hue"]}}')[11] == {"Lights": ["hue"]}
    *vals, probs = cfg('{"notify": "notify.a", "jobs": {"Lights": "hue"}}')
    assert vals[11] == {} and any("jobs" in p for p in probs)
    *vals, probs = cfg('{"notify": "notify.a", "watch": "lutron_caseta"}')
    assert vals[10] == () and any("watch" in p for p in probs)

def selftest_flows():
    """check()/preflight()/sweep() against a fake HA, in a temp state dir."""
    import tempfile
    g = globals()
    saved = {k: g[k] for k in ("STATE", "LOG", "version", "entries", "ha", "service", "sup", "notify", "diagnose", "gh_issues", "ai", "http",
                               "BLOCKED_VERSIONS", "SOAK_HOURS", "IGNORE", "write_status")}
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
             service=fservice, BLOCKED_VERSIONS=(), SOAK_HOURS=0, IGNORE=(), write_status=lambda: None,
             notify=lambda kind, title, message, key=None, urgent=False: fake["calls"].append(("notify", kind, title)),
             diagnose=lambda ds: "diag", http=lambda *a, **k: {"body": ""})
    datetime.datetime, time.sleep = FakeDT, lambda s: None
    def installs(): return sum(1 for c in fake["calls"] if c[0] == "/core/update" and (c[1] or {}).get("backup"))
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
        assert not any(c[0] == "notify" and c[2] == "HA: rolled back to 1.3" for c in fake["calls"])
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
        assert ("/core/update", {"version": "1.1", "backup": True}) in fake["calls"]   # pinned to the assessed version
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
        # v2: a hold skips and stays quiet (holds are dashboard-only now)
        fresh(datetime.datetime(2026, 2, 5, 4, 0), ai="RISK: high")
        preflight()
        fake.update(skipped=None)
        fake["now"] += datetime.timedelta(hours=1)
        preflight()
        assert installs() == 0 and fake["calls"].count(skip) == 2
        assert not [c for c in fake["calls"] if c[0] == "notify"], fake["calls"]
        # v1.1: a rolled-back bad version is blocked
        fresh(datetime.datetime(2026, 2, 6, 4, 0), v="1.1", entries=[{"entry_id": "x", "domain": "hue", "state": "setup_retry"}])
        save({"last_good": "1.0", "pre": {"version": "1.0", "failing": {}, "at": (fake["now"] - datetime.timedelta(minutes=20)).isoformat()}})
        check()
        assert "1.1" in load()["blocked"]
        # v1.1 upgrade: a 1.0 state (no `blocked`) migrates only the bad version; HA's skip is NOT a block
        fresh(datetime.datetime(2026, 2, 7, 4, 0), skipped="1.1")
        save({"last_good": "1.0", "bad_version": "0.9"})
        preflight()
        assert load()["blocked"] == ["0.9"] and installs() == 1, fake["calls"]
        assert [c for c in fake["calls"] if c[0] == "notify"] == []       # migration is logged, never pushed
        # v2: a release inside its 7-day soak waits and hides the badge, even inside the window
        fresh(datetime.datetime(2026, 2, 8, 4, 0))
        g["SOAK_HOURS"] = 168
        save({"preflight": {"version": "1.1", "date": fake["now"].date().isoformat(),
                            "ai": {"risk": "low", "fixed": None},
                            "published_at": (fake["now"] - datetime.timedelta(hours=1)).isoformat()}})
        preflight()
        assert installs() == 0 and skip in fake["calls"], fake["calls"]
        g["SOAK_HOURS"] = 0
    finally:
        datetime.datetime, time.sleep = real_dt, real_sleep
        g.update(saved)

def selftest_v2():
    """notify render/dedupe, sweep health rules, status JSON - against a fake HA."""
    import tempfile
    g = globals()
    keys = ("STATE", "LOG", "NOTIFY", "service", "version", "entries", "ha", "sup", "diagnose",
            "write_status", "SOAK_HOURS", "IGNORE", "WATCH", "JOBS", "WWW_DIR", "STUCK_MIN")
    saved = {k: g[k] for k in keys}
    d = tempfile.mkdtemp()
    calls = []
    fake = {"v": "1.0", "entries": [], "now": datetime.datetime(2026, 3, 1, 12, 0)}
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None): return fake["now"]
    real_dt = datetime.datetime
    def fha(path, *a, **kw):
        if path.startswith("/states/"):
            return {"state": "off", "attributes": {"latest_version": "1.1", "installed_version": fake["v"]}}
        if path == "/config":
            return {"version": fake["v"]}
        return {}
    g.update(STATE=os.path.join(d, "s.json"), LOG=os.path.join(d, "log"), NOTIFY="notify/mobile_app_x",
             service=lambda ds, data, **kw: calls.append((ds, data)), version=lambda: fake["v"],
             entries=lambda: fake["entries"], ha=fha, sup=lambda *a, **kw: {},
             diagnose=lambda ds: "CAUSE: upstream\nDO: nothing", STUCK_MIN=30, SOAK_HOURS=0,
             IGNORE=("wyoming",), WWW_DIR=os.path.join(d, "www"))
    datetime.datetime = FakeDT
    try:
        notify("k", "t", "**hello** [i](https://x)", key="k")
        assert calls[-1][1]["message"] == "hello i (https://x)", calls[-1]
        assert calls[-1][1]["data"]["push"]["interruption-level"] == "passive"
        notify("k", "t", "again", key="k")
        assert len(calls) == 1, calls
        notify("k", "t", "urgent", key="k", urgent=True)
        assert len(calls) == 2 and calls[-1][1]["data"]["push"]["interruption-level"] == "time-sensitive"
        # a stale caller save must not wipe the dedupe log written by notify (review #3)
        save({})
        notify("race", "t", "one", key="race")
        stale = load()
        notify("race2", "t", "two", key="race2")
        save(stale)                                            # snapshot taken before race2 was logged
        assert "race2" in (load().get("notify_log") or {}), load().get("notify_log")
        calls.clear()
        # sweep: no healthy history = quiet
        save({"first_seen": {}, "diagnosed": {},
              "health": {"hue": {"down_since": fake["now"].isoformat()}}})
        fake["entries"] = [{"entry_id": "a", "domain": "hue", "state": "setup_retry"}]
        sweep()
        assert not [c for c in calls if c[0] == "notify/mobile_app_x"], calls[-3:]
        # an ignored domain never alerts, even with a clean history
        save({"first_seen": {}, "diagnosed": {},
              "health": {"wyoming": {"down_since": (fake["now"] - datetime.timedelta(hours=1)).isoformat(),
                                     "ok_since": (fake["now"] - datetime.timedelta(hours=9)).isoformat()}}})
        fake["entries"] = [{"entry_id": "b", "domain": "wyoming", "state": "setup_retry"}]
        sweep()
        assert not [c for c in calls if c[0] == "notify/mobile_app_x"], calls[-3:]
        # healthy 8 h then failing: one alert, one per down-streak
        save({"first_seen": {}, "diagnosed": {},
              "health": {"hue": {"down_since": (fake["now"] - datetime.timedelta(hours=1)).isoformat(),
                                 "ok_since": (fake["now"] - datetime.timedelta(hours=9)).isoformat()}}})
        fake["entries"] = [{"entry_id": "a", "domain": "hue", "state": "setup_retry"}]
        sweep()
        n = len([c for c in calls if c[0] == "notify/mobile_app_x"])
        assert n == 1, calls
        sweep()
        assert len([c for c in calls if c[0] == "notify/mobile_app_x"]) == n, calls
        # a healthy streak accumulates across sweeps (regression: ok_since must not reset every run)
        calls.clear()
        fake["now"] += datetime.timedelta(hours=1)
        fake["entries"] = [{"entry_id": "z", "domain": "zha", "state": "loaded"}]
        save({})
        sweep()                                            # baseline, healthy
        t0 = fake["now"]
        fake["now"] += datetime.timedelta(hours=3)
        sweep()                                            # still healthy
        assert load()["health"]["zha"]["ok_since"] == t0.isoformat(), load()["health"]["zha"]
        fake["now"] += datetime.timedelta(hours=4)         # 7 h since t0
        fake["entries"] = [{"entry_id": "z", "domain": "zha", "state": "setup_retry"}]
        sweep()                                            # first failing sample: too fresh to alert
        assert not [c for c in calls if c[0] == "notify/mobile_app_x"], calls
        fake["now"] += datetime.timedelta(minutes=31)      # past stuck_min
        sweep()
        assert [c for c in calls if c[0] == "notify/mobile_app_x"], calls
        # backup health: no backups at all is the worst case and must alert (review #5)
        calls.clear()
        g["sup"] = lambda *a, **kw: {"data": {"backups": []}}
        backup_health()
        assert [c for c in calls if c[0] == "notify/mobile_app_x"], calls
        assert load()["backup"]["ok"] is False
        # watched domains are recorded even when healthy (the dashboard always shows them)
        g["WATCH"] = ("hue", "not_a_thing")
        fake["entries"] = [{"entry_id": "a", "domain": "hue", "state": "loaded"}]
        save({"first_seen": {}, "diagnosed": {}, "health": {}})
        sweep()
        w = {i["domain"]: i["state"] for i in load()["last_watched"]}
        assert w == {"hue": "loaded", "not_a_thing": "not set up"}, w
        g["WATCH"] = ()
        # jobs aggregate their domains: one broken domain breaks the whole job
        g["JOBS"] = {"Lights": ("hue",), "Climate": ("intesisbox",)}
        fake["entries"] = [{"entry_id": "a", "domain": "hue", "state": "setup_retry"},
                           {"entry_id": "b", "domain": "intesisbox", "state": "loaded"}]
        save({"first_seen": {}, "diagnosed": {}, "health": {}})
        sweep()
        j = {x["name"]: x for x in load()["last_jobs"]}
        assert j["Lights"]["state"] == "not working" and j["Lights"]["broken"] == ["hue"], j
        assert j["Climate"]["state"] == "working", j
        g["JOBS"] = {}
        # 30-day strip: a problem outranks an update; quiet days are green
        today = datetime.date.today()
        save({"first_run": (today - datetime.timedelta(days=30)).isoformat(),
              "daily": {today.isoformat(): {"updates": 1},
                        (today - datetime.timedelta(days=1)).isoformat(): {"rollbacks": 1}}})
        strip, summ = build_strip(load())
        assert strip[-1] == "u" and strip[-2] == "r" and strip[0] == "g" and len(strip) == 30, strip
        assert summ == {"quiet": 28, "updates": 1, "problems": 1}, summ
        # a fresh install must not invent days before it existed
        save({"first_run": today.isoformat(), "daily": {}})
        strip2, summ2 = build_strip(load())
        assert len(strip2) == 1 and strip2 == ["g"] and summ2 == {"quiet": 1, "updates": 0, "problems": 0}
        # status JSON shape
        calls.clear()
        write_status()
        js = json.load(open(os.path.join(g["WWW_DIR"], "smart_updater_status.json")))
        for key in ("state", "installed", "pending", "soak_hours", "soak_remaining_hours", "verdict",
                    "blocked", "window", "failing", "ignored_failing", "watched", "jobs", "strip", "strip_summary",
                    "last_event", "recent_log",
                    "backup_ok", "updated"):
            assert key in js, (key, js)
    finally:
        datetime.datetime = real_dt
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

def notify(kind, title, message, key=None, urgent=False):
    """One plain-text push. Same key at most once per 24 h unless urgent. Every call is logged first."""
    msg = plain(message)
    key = key or kind
    log(f"NOTIFY {kind} [{title}] {msg[:300]}")
    if not NOTIFY:
        return log('notify skipped: no "notify" target in config.json')
    try:
        s = load()
        now = datetime.datetime.now()
        logbook = s.setdefault("notify_log", {})
        try:
            fresh = (now - parse_dt(logbook.get(key) or "")).total_seconds() < 24 * 3600
        except (ValueError, TypeError):
            fresh = False
        if fresh and not urgent:
            return log(f"notify skipped (dedupe) key={key}")
        data = {"push": {"interruption-level": "time-sensitive" if urgent else "passive"}}
        service(NOTIFY, {"title": title, "message": msg, "data": data})
        s = load()                                 # record only after a successful send: a failure retries
        logbook = s.setdefault("notify_log", {})
        logbook[key] = now.isoformat()
        pruned = {}
        for k, v in logbook.items():
            try:
                if (now - parse_dt(v)).total_seconds() < 7 * 86400:
                    pruned[k] = v
            except (ValueError, TypeError):
                pass
        s["notify_log"] = pruned
        save(s)
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
        s = json.load(open(STATE))
    except FileNotFoundError:
        s = {}
    except (OSError, ValueError) as e:
        bad = STATE + ".corrupt"
        try:
            os.replace(STATE, bad)
        except OSError:
            pass
        log(f"state.json unreadable ({e!r}); moved to {bad} and starting fresh")
        s = {}
    if "blocked" not in s:
        migrate_blocked(s)
    return s

def migrate_blocked(s):
    """Once, on the first run of 1.1+: seed `blocked` from the open problem and the recorded bad version.
    Home Assistant's skipped_version is deliberately NOT used: we skip releases ourselves to hide the badge,
    so seeding from it turns our own badge-hide into a permanent block (measured: 2026.10.4 on 2026-10-25)."""
    found = [(s.get("open_issue") or {}).get("version"), s.get("bad_version")]
    s["blocked"] = list(dict.fromkeys(v for v in found if v))
    save(s)
    log(f"MIGRATED blocked={s['blocked']}")

def bump(s, key, n=1):
    """One counter per day for the dashboard strip. Keeps 60 days."""
    today = datetime.date.today()
    daily = s.setdefault("daily", {})
    e = daily.setdefault(today.isoformat(), {})
    e[key] = int(e.get(key, 0)) + n
    cutoff = (today - datetime.timedelta(days=60)).isoformat()
    s["daily"] = {k: v for k, v in daily.items() if k >= cutoff}

def build_strip(s, days=30):
    """Oldest -> newest, only from the day the tool started. g quiet, u an update, r a rollback, freeze,
    bad backup or a new integration failure (all problems)."""
    daily = s.get("daily") or {}
    today = datetime.date.today()
    try:
        first = datetime.date.fromisoformat(s.get("first_run") or "")
    except ValueError:
        first = today
    start = max(today - datetime.timedelta(days=days - 1), first)
    out, summ = [], {"quiet": 0, "updates": 0, "problems": 0}
    for i in range((today - start).days, -1, -1):
        e = daily.get((today - datetime.timedelta(days=i)).isoformat()) or {}
        if e.get("rollbacks") or e.get("stalls") or e.get("backup_bad") or e.get("new_failures"):
            out.append("r")
            summ["problems"] += 1
        elif e.get("updates"):
            out.append("u")
            summ["updates"] += 1
        else:
            out.append("g")
            summ["quiet"] += 1
    return out, summ

def save(s):
    # notify() writes the dedupe log from a fresh read; a caller that loaded state before a notify would
    # otherwise clobber it on its next save. Merge the on-disk log so those entries always survive.
    try:
        disk = json.load(open(STATE))
        merged = dict(s.get("notify_log") or {})
        for k, v in (disk.get("notify_log") or {}).items():
            if k not in merged or (v or "") > (merged[k] or ""):
                merged[k] = v
        if merged:
            s["notify_log"] = merged
    except (OSError, ValueError):
        pass
    tmp = STATE + f".tmp.{os.getpid()}"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)

def version():
    return ha("/config")["version"]

def dashboard_present():
    """Is the documented status sensor installed? Without it a hold would be invisible."""
    try:
        ha("/states/sensor.smart_updater")
        return True
    except Exception:
        return False

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
    if not latest:
        return log(f"failed_install: no latest_version to block ({why})")
    s.pop("pre", None)
    s["open_issue"] = {"version": latest, "domains": [], "diagnosis": why, "at": datetime.datetime.now().isoformat()}
    block(s, latest, "the update did not install")
    save(s)
    clear_badge()
    notify("install_failed", f"HA: {latest} did not install",
           f"{why} It is blocked; the next release will be tried automatically.")

def is_blocked(s, v):
    return v in s.get("blocked", []) or v in BLOCKED_VERSIONS

def block(s, v, reason=""):
    """Our own never-install list. HA's skipped_version can't be it: we skip releases just to hide the badge."""
    if not v:
        return
    if v not in s.setdefault("blocked", []):
        s["blocked"].append(v)
    if reason:
        reasons = s.get("blocked_reasons")
        if not isinstance(reasons, dict):
            reasons = s["blocked_reasons"] = {}
        reasons[v] = reason

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
    notify("rollback_failed", "HA: rollback FAILED",
           f"Could not roll back to {target}. Do it by hand: ha core update --version {target}", urgent=True)

def install(s, v_now, latest):
    s["pre"] = {"version": v_now, "failing": failing(entries()), "at": datetime.datetime.now().isoformat()}
    s.setdefault("last_good", v_now)
    bump(s, "updates")
    save(s)
    log(f"installing core update from {v_now}")
    try:
        sup("/core/update", "POST", {"version": latest, "backup": True}, timeout=1800)   # pinned: never a newer, unassessed one
    except urllib.error.HTTPError as e:      # a real refusal; our own process dying in the restart never gets here
        n = s.get("refusals", {}).get(latest, 0) + 1
        s["refusals"] = {latest: n}
        s.pop("pre", None)
        save(s)
        log(f"install refused ({n}x): {e!r}")
        if n >= 2:                            # once may be transient (job running, low disk); twice: skip it
            s.pop("refusals")
            failed_install(s, f"The Supervisor refused the update twice ({e!r}).")
    write_status()

def preflight():
    s = load()
    st = ha(f"/states/{CORE_UPDATE}")
    latest, v_now = st["attributes"].get("latest_version"), version()
    s["last_version"] = v_now
    if not latest or latest == (st["attributes"].get("installed_version") or v_now):   # skipped still counts as pending
        save(s)
        write_status()
        return log("preflight: no core update pending")
    badge = st["state"] == "on"
    if is_blocked(s, latest):
        if badge:
            clear_badge()
        save(s)
        write_status()
        return log(f"preflight {latest}: blocked, not installing")
    expire_stale_pre(s, v_now)
    if is_blocked(s, latest):                     # expire_stale_pre may have blocked it just now
        if badge:
            clear_badge()
        save(s)
        write_status()
        return log(f"preflight {latest}: blocked after a failed install, not installing")
    if s.get("pre"):
        return log("preflight: install already in progress")
    cache = s.get("preflight", {})
    if cache.get("version") != latest or cache.get("date") != datetime.datetime.now().date().isoformat():
        cache = assess(s, v_now, latest, cache)                       # once a day; this call can take minutes
        s = load()                                                    # do not clobber a concurrent sweep or heartbeat
        s["preflight"] = cache
        s["last_version"] = v_now
        save(s)
    left = soak_remaining(cache)
    d = decide(cache["ai"], datetime.datetime.now().hour, left)
    log(f"preflight {latest}: risk={cache['ai']['risk']} soak={left:.0f}h -> {d}")
    if d == "hold" and not dashboard_present():    # no dashboard to read: say it once per version
        notify("held", f"HA: {latest} held",
               f"Not installing it yet. {summarize(cache.get('text', ''))}", key=f"held:{latest}")
    if d in ("wait", "hold") and badge:
        clear_badge()                             # hides HA's badge only; preflight still installs it later
    save(s)
    write_status()
    if d == "install":
        install(s, v_now, latest)

def dryrun(latest):
    """preflight's AI assessment for any version, without installing or touching state."""
    s = load()
    c = assess(s, version(), latest, {})
    left = soak_remaining(c)
    d = "blocked" if is_blocked(s, latest) else decide(c["ai"], datetime.datetime.now().hour, left)
    if c.get("concerns"):
        log("DRYRUN concerns: " + json.dumps(c["concerns"])[:900])
    log(f"DRYRUN {latest}: {c['ai']} soak={left:.0f}h -> {d}\n{c.get('verify_text', '')}\n{c['text']}")

def assess(s, v_now, latest, cache):
    published = None
    try:
        rel = http(f"https://api.github.com/repos/home-assistant/core/releases/tags/{latest}",
                   headers={"User-Agent": "ha-smart-updater"})
        body = rel.get("body", "") or ""
        published = rel.get("published_at")
        notes = body[:8000]
    except Exception:
        body, notes = "", "(release notes unavailable)"
    if published is None and (cache or {}).get("version") == latest:
        published = cache.get("published_at")
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
FIXED: yes, no or unknown   (does {latest} fix the open problem above? "unknown" if there is none)
Then up to 5 lines, one per suspected breaking change, naming something searchable in a config file or entity id:
CONCERN: <integration domain, config key, entity id prefix, or YAML key> | <one line why>"""
    text = ai("smart_updater_preflight", prompt)
    cache = {"version": latest, "date": datetime.datetime.now().date().isoformat(), "ai": parse_ai(text, closed), "text": text,
             "published_at": published}
    concerns = parse_concerns(text)
    if concerns:
        verified = verify_concerns(concerns, domains)
        cache["concerns"] = verified
        lines = []
        for i, c in enumerate(verified, 1):
            where = []
            if c["configured"]:
                where.append("integration configured")
            if c["files"]:
                where.append("config: " + ", ".join(f"{k} x{v}" for k, v in c["files"].items()))
            if c["entities"]:
                where.append(f"{c['entities']} entities")
            lines.append(f"{i}. {c['token']} — {c['why']}\n   used here: {'; '.join(where) if where else 'not used'}")
        verify_prompt = f"""A Home Assistant Core update {v_now} -> {latest} is pending. A first analysis listed suspected
breaking changes. A local scan then checked whether THIS house uses each one.
{chr(10).join(lines)}

Rules: a concern this house does not use is not risk. A concern it does use is risk only if the release notes or the
issues above show this release changes that thing. Answer with exactly:
RISK: high or low
Then one short line per concern saying whether it affects this house, with the reason."""
        text2 = ai("smart_updater_verify", verify_prompt)
        risk2 = parse_ai(text2)["risk"]
        if risk2:
            cache["ai"]["risk"] = risk2
        cache["verify_text"] = text2
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
            notify("rollback_failed", "HA: still on the broken version",
                   f"Still on {v}. Roll back by hand: ha core update --version {target}", urgent=True)
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
        block(s, bad, "rolled back: it broke something here")
        save(s)
        st = ha(f"/states/{CORE_UPDATE}")
        if st["attributes"].get("latest_version") == bad and st["state"] == "on":
            clear_badge()
        write_status()
        return notify("rollback", f"HA: rolled back to {v}",
                      f"{bad} broke something, so it was undone and blocked. The next release will be tried.",
                      key="rollback_done")
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
        block(s, v, f"rolled back: it broke {', '.join(domains)}")
        bump(s, "rollbacks")
        s["open_issue"] = {"version": v, "domains": domains, "diagnosis": "(diagnosing)", "at": datetime.datetime.now().isoformat()}
        s["pending_diagnosis"] = {"version": v, "domains": domains, "at": datetime.datetime.now().isoformat(), "attempts": 0}
        save(s)                                   # persist before anything slow or restart-y
        log(f"check: {v} broke {domains}; rolling back to {pre['version']}")
        notify("rollback", f"HA: {v} broke {', '.join(domains)}",
               f"Rolling back to {pre['version']} now.", key="rollback_started", urgent=True)
        rollback(pre["version"])
        try:                                      # best effort: the restart may cut this off; heartbeat retries it later
            s = load()
            s["open_issue"]["diagnosis"] = diagnose(domains)
            s.pop("pending_diagnosis", None)
            s.setdefault("diagnosed", {}).update({k: "update" for k in new})
            save(s)
        except Exception as e:
            log(f"diagnosis deferred to heartbeat: {e!r}")
    else:
        s["last_good"] = v
        s.pop("pre", None)
        for k in ("open_issue", "pending_diagnosis"):
            s.pop(k, None)
        save(s)
    s["last_version"] = v
    save(s)
    write_status()

def entities_by_platform():
    """entity_id -> integration platform, from the registry the Core container can read."""
    try:
        reg = json.load(open("/config/.storage/core.entity_registry"))
        ents = reg["data"]["entities"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {}
    for e in ents:
        if e.get("disabled_by") or not e.get("platform"):
            continue
        out.setdefault(e["platform"], []).append(e["entity_id"])
    return out

def network():
    network_read()

def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

PORT_NAME = re.compile(r"^(?P<dev>.+?) (?P<port>Port \d+) (?P<what>.+)$")

def parse_network(states, eids):
    """One record per Omada device, plus the tracked clients. Pure: no I/O, no clock."""
    devs, clients = {}, []
    def rec(name):
        return devs.setdefault(name, {"name": name, "ports": {}})
    for eid in sorted(eids):
        st = states.get(eid)
        if not st:
            continue
        name = (st.get("attributes") or {}).get("friendly_name") or eid
        v = st.get("state")
        if eid.startswith("device_tracker."):
            clients.append({"name": name, "state": v})
        elif name.endswith(" Device status"):
            rec(name[:-14])["status"] = v
        elif name.endswith(" Firmware"):
            rec(name[:-9])["fw"] = "update" if v == "on" else "current"
        elif name.endswith(" CPU usage"):
            rec(name[:-10])["cpu"] = to_num(v)
        elif name.endswith(" Memory usage"):
            rec(name[:-13])["mem"] = to_num(v)
        else:
            m = PORT_NAME.match(name)
            if not m:
                continue
            p = rec(m.group("dev"))["ports"].setdefault(m.group("port"), {})
            what = m.group("what")
            if what == "PoE power":
                p["poe_w"] = to_num(v)
            elif what == "PoE":
                p["poe"] = v
            else:
                p.setdefault("links", {})[what] = v
    return {"devices": [devs[k] for k in sorted(devs)], "clients": clients}

def network_warnings(parsed, warn=80.0):
    """The facts an owner would want named, not the AI's judgement of them."""
    out = []
    for d in parsed["devices"]:
        if d.get("status") and d["status"] != "connected":
            out.append(f"{d['name']} is {d['status']}")
        if d.get("cpu") is not None and d["cpu"] >= warn:
            out.append(f"{d['name']} CPU at {d['cpu']:.0f}%")
        if d.get("mem") is not None and d["mem"] >= warn:
            out.append(f"{d['name']} memory at {d['mem']:.0f}%")
        if d.get("fw") == "update":
            out.append(f"firmware update available for {d['name']}")
    return out

def network_history(prev, parsed, day=None, keep=14):
    """One compact sample a day: device -> [cpu, mem]. Yesterday is kept so change can be named."""
    day = day or datetime.datetime.now().date().isoformat()
    hist = [h for h in (prev or []) if h.get("day") != day]
    hist.append({"day": day,
                 "d": {d["name"]: [d.get("cpu"), d.get("mem")] for d in parsed["devices"]}})
    return hist[-keep:]

def network_change(hist, step=5.0):
    """What moved since the previous sample. Silent below `step` points."""
    if len(hist) < 2:
        return []
    prev, cur = hist[-2].get("d") or {}, hist[-1].get("d") or {}
    out = []
    for name, (cpu, mem) in cur.items():
        old = prev.get(name) or [None, None]
        for a, b, label in ((old[1], mem, "memory"), (old[0], cpu, "CPU")):
            if a is not None and b is not None and abs(b - a) >= step:
                out.append(f"{name} {label} {a:.0f}% to {b:.0f}%")
    return out

def network_read():
    """Summarise the Omada network for the AI and store a plain-English read. No control, read-only."""
    s = load()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    eids = entities_by_platform().get("tplink_omada", [])
    if not eids:
        s["network"] = {"at": now, "entities": 0,
                        "report": "The TP-Link Omada integration is not set up yet."}
        save(s)
        write_status()
        return log("network: no tplink_omada entities")
    try:
        states = {x["entity_id"]: x for x in ha("/states")}
    except Exception as e:
        return log(f"network: states unavailable ({e!r})")
    parsed = parse_network(states, eids)
    if not parsed["devices"]:
        return log("network: no device entities")
    hist = network_history((s.get("network") or {}).get("history"), parsed)
    change, warns = network_change(hist), network_warnings(parsed)
    lines = []
    for d in parsed["devices"]:
        bits = [d.get("status") or "status unknown"]
        if d.get("cpu") is not None:
            bits.append(f"CPU {d['cpu']:.0f}%")
        if d.get("mem") is not None:
            bits.append(f"memory {d['mem']:.0f}%")
        if d.get("fw"):
            bits.append(f"firmware {d['fw']}")
        for port, p in sorted(d["ports"].items()):
            if p.get("poe_w"):                          # 0.0 W on an idle port is not a fact worth a line
                bits.append(f"{port} PoE {p['poe_w']:.1f} W")
            for what, val in sorted((p.get("links") or {}).items()):
                bits.append(f"{port} {what} {val}")
        lines.append(f"- {d['name']}: " + "; ".join(bits))
    names = [c["name"] for c in parsed["clients"]]
    home = sum(1 for c in parsed["clients"] if c["state"] == "home")
    summary = ("Devices:\n" + "\n".join(lines) +
               f"\n\nClients seen on the network: {len(names)}, {home} home" +
               (": " + ", ".join(names) if names else "") +
               "\nWarnings: " + ("; ".join(warns) if warns else "none") +
               "\nSince the last check: " + ("; ".join(change) if change else "nothing notable"))
    prompt = f"""You are the network operator for a home. Below is the live state of its TP-Link Omada network
as reported by Home Assistant.

{summary}

Write a short plain-English read for the owner, exactly in this shape:
OVERALL: healthy / degraded / down
- up to five bullets covering: whether every device is connected; CPU and memory, naming anything at or above
  80 %; any port that is off and what that likely means; firmware; PoE. Then the one thing worth doing, if any.
Use only the numbers above. Mention a change only if the "since the last check" line lists one. No preamble,
no markdown headings."""
    text = ai("smart_updater_network", prompt) or "The AI did not answer."
    s["network"] = {"at": now, "entities": len(eids), "devices": parsed["devices"],
                    "clients": {"tracked": len(names), "home": home, "names": names},
                    "warnings": warns, "change": change, "history": hist, "report": text}
    save(s)
    log(f"network: report devices={len(parsed['devices'])} clients={len(names)} warnings={len(warns)}")
    write_status()

def addons_health(s):
    """Supervised add-ons: an add-on that errors, or stops after running, is reported once."""
    try:
        data = sup("/addons") or {}
        addons = (data.get("data") or {}).get("addons") or []
    except Exception as e:
        log(f"addons: unavailable ({e!r})")
        return
    prev = s.get("addons_state") or {}
    bad = []
    for a in addons:
        slug, st = a.get("slug"), a.get("state")
        if not slug:
            continue
        if st == "error" or (prev.get(slug) == "started" and st == "stopped"):
            bad.append((slug, st))
        prev[slug] = st
    s["addons_state"] = prev
    s["addons_bad"] = [{"slug": x, "state": y} for x, y in bad]
    if bad and not s.get("pre"):
        for slug, why in bad:
            notify("addon_down", f"HA: add-on {slug} is {why}",
                   f"An add-on is not running: {slug} ({why}). Check its log.", key=f"addon_down:{slug}")
    log(f"addons: bad={[':'.join(b) for b in bad]}")

def shadow_health(s):
    """The Truth Audit, in shadow: per-job device liveness. Writes would_speak to state; never notifies."""
    by_plat = entities_by_platform()
    try:
        states = {x["entity_id"]: x for x in ha("/states")}
    except Exception as e:
        log(f"shadow: states unavailable ({e!r})")
        return
    now = datetime.datetime.now()
    sh = s.setdefault("shadow", {})
    ents = sh.setdefault("entities", {})
    jobs = {}
    for job, domains in JOBS.items():
        total = down = 0
        for d in domains:
            if d in IGNORE:
                continue
            for eid in by_plat.get(d, []):
                st = states.get(eid)
                if not st:
                    continue
                total += 1
                ok = st["state"] not in ("unavailable", "unknown")
                rec = ents.setdefault(eid, {})
                if ok:
                    rec["ok_at"] = now.isoformat(timespec="seconds")
                    rec.pop("down_since", None)
                else:
                    down += 1
                    rec.setdefault("down_since", now.isoformat(timespec="seconds"))
                rec["ok"] = ok
        if total:
            jobs[job] = {"total": total, "down": down}
    would = []
    for eid, rec in ents.items():
        if rec.get("ok"):
            continue
        try:
            since, last_ok = parse_dt(rec.get("down_since")), parse_dt(rec.get("ok_at"))
        except (TypeError, ValueError):
            continue
        if (now - since).total_seconds() >= 3600 and (now - last_ok).total_seconds() >= 3600:
            would.append({"entity": eid, "down_since": rec["down_since"], "last_ok": rec["ok_at"]})
    sh["at"] = now.isoformat(timespec="seconds")
    sh.setdefault("first_at", sh["at"])
    sh["jobs"] = jobs
    sh["would_speak"] = would[-50:]
    sh["count"] = len(would)
    log(f"shadow: jobs={jobs} would_speak={len(would)}")

def sweep():
    s = load()
    ent = entries()
    v = version()
    s["last_version"] = v
    expire_stale_pre(s, v)                        # audit #1/#3: a stuck snapshot must not silence the sweep forever
    now, ts = failing(ent), datetime.datetime.now()
    failing_domains = sorted(set(now.values()))
    if not s.get("first_run"):
        try:
            with open(LOG) as f:
                s["first_run"] = f.readline()[:10] or datetime.date.today().isoformat()
        except OSError:
            s["first_run"] = datetime.date.today().isoformat()
    s["last_failing"] = failing_domains
    s["last_ignored"] = [d for d in failing_domains if d in IGNORE]
    watched = []
    for d in WATCH:
        states = sorted({e["state"] for e in ent if e["domain"] == d and not e.get("disabled_by")})
        watched.append({"domain": d, "state": ", ".join(states) if states else "not set up"})
    s["last_watched"] = watched
    jobs = []
    for name, domains in JOBS.items():
        domains = [d for d in domains if d not in IGNORE]      # known-broken domains are not this job's problem
        if not domains:
            jobs.append({"name": name, "state": "not set up", "broken": [], "missing": []})
            continue
        raw = {d: [e["state"] for e in ent if e["domain"] == d and not e.get("disabled_by")] for d in domains}
        broken = [d for d, v in raw.items() if any(st in FAILING for st in v)]
        missing = [d for d, v in raw.items() if not v]
        if broken:
            jobs.append({"name": name, "state": "not working", "broken": broken, "missing": missing})
        elif len(missing) == len(domains):
            jobs.append({"name": name, "state": "not set up", "broken": [], "missing": missing})
        else:
            jobs.append({"name": name, "state": "working", "broken": [], "missing": missing})
    s["last_jobs"] = jobs
    addons_health(s)
    if JOBS:
        shadow_health(s)
    health = s.setdefault("health", {})
    for d in {e["domain"] for e in ent if not e.get("disabled_by")}:
        h = health.setdefault(d, {})
        if d in failing_domains:
            h.setdefault("down_since", ts.isoformat())
            # keep ok_since: it is when the healthy streak began, and the 6 h rule needs it
        elif h.get("down_since"):                 # recovered: a new healthy streak starts now
            h["ok_since"] = ts.isoformat()
            h.pop("down_since", None)
            h.pop("notified", None)
        else:                                     # still healthy: keep the streak start, do not reset it
            h.setdefault("ok_since", ts.isoformat())
    if "first_seen" not in s:                     # first run: today's failures are the known baseline
        s["first_seen"] = {k: ts.isoformat() for k in now}
        s["diagnosed"] = {k: "baseline" for k in now}
        save(s)
        write_status()
        return log(f"sweep: baseline {failing_domains}")
    seen, diagnosed = s["first_seen"], s.setdefault("diagnosed", {})
    for k in list(seen):
        if k not in now:
            seen.pop(k); diagnosed.pop(k, None)   # recovered: forget it so a relapse is diagnosed again
    for k in now:
        seen.setdefault(k, ts.isoformat())
    due = {}
    for k, d in now.items():
        h = health.get(d) or {}
        down, ok = h.get("down_since"), h.get("ok_since")
        if s.get("pre") or d in IGNORE or not down or h.get("notified") == down:
            continue                              # mid-update check() owns it; known-broken; already told
        if not ok or (parse_dt(down) - parse_dt(ok)).total_seconds() < 6 * 3600:
            continue                              # no healthy history = baseline, stays quiet
        if (ts - parse_dt(down)).total_seconds() < STUCK_MIN * 60:
            continue
        due[k] = d
    for k in due:
        health[due[k]]["notified"] = health[due[k]]["down_since"]
    if due:
        bump(s, "new_failures")
    save(s)
    if due:
        domains = sorted(set(due.values()))
        key = "integration_down:" + ";".join(f"{d}@{health[d].get('down_since', '')}" for d in domains)
        notify("integration_down", f"HA: {', '.join(domains)} down", summarize(diagnose(domains)), key=key)
    log(f"sweep: failing={failing_domains} quiet={s['last_ignored']} alerted={sorted(set(due.values()))}")
    write_status()

# ---------- dashboard status, heartbeat, backup health ----------
def write_status():
    """state.json + log tail -> <www_dir>/smart_updater_status.json for the dashboard. Never raises."""
    try:
        s = load()
        cache = s.get("preflight") or {}
        installed = s.get("last_version")
        pending = None
        prev = {}
        try:
            with open(os.path.join(WWW_DIR, "smart_updater_status.json")) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            pass
        try:
            st = ha(f"/states/{CORE_UPDATE}")          # skipped versions still count as pending here
            latest = st["attributes"].get("latest_version")
            installed = st["attributes"].get("installed_version") or installed
            if latest and latest != installed:
                pending = latest
        except Exception:
            pending = prev.get("pending")
            installed = prev.get("installed") or installed
        if cache.get("version") != pending:        # a brand-new release has no assessment yet
            cache = {}
        left = soak_remaining(cache)
        blocked = list(s.get("blocked", []))
        ai = cache.get("ai") or {"risk": None, "fixed": None}
        if pending in blocked:
            verdict = "blocked"
        elif pending:
            verdict = decide(ai, datetime.datetime.now().hour, left)
        else:
            verdict = "none"
        if s.get("pre"):
            state = "installing"
        elif verdict == "blocked":
            state = "blocked"
        elif pending and left > 0:
            state = "soaking"
        elif pending and verdict == "hold":
            state = "holding"
        else:
            state = "idle"
        try:
            recent = "".join(open(LOG).readlines()[-12:]).strip()
        except OSError:
            recent = ""
        lines = recent.splitlines()
        last_event = next((l for l in reversed(lines) if l[:2] == "20"), lines[-1] if lines else "")
        stall = s.get("last_stall") or {}
        backup = s.get("backup") or {}
        oi = s.get("open_issue") or {}
        try:
            hb = float(open(os.path.join(DIR, ".heartbeat")).read().strip())
            hb_at, hb_age = datetime.datetime.fromtimestamp(hb).isoformat(timespec="seconds"), int(time.time() - hb)
        except (OSError, ValueError):
            hb_at, hb_age = None, None
        strip, strip_summary = build_strip(s)
        sh = s.get("shadow") or {}
        # per-entity liveness is ~18 KB and the dashboard reads none of it; keep the counters and the per-job totals
        shadow = {k: sh[k] for k in ("at", "first_at", "count", "jobs") if k in sh}
        out = {"state": state, "installed": installed, "pending": pending,
               "diagnosis": oi.get("diagnosis"), "diagnosis_version": oi.get("version"),
               "soak_hours": SOAK_HOURS, "soak_remaining_hours": round(left, 1),
               "ai_risk": ai.get("risk"), "ai_fixed": ai.get("fixed"), "verdict": verdict,
               "concerns": cache.get("concerns") or [],
               "blocked": blocked, "bad_version": s.get("bad_version"),
               "blocked_reasons": s.get("blocked_reasons") or {}, "blocked_config": list(BLOCKED_VERSIONS),
               "first_run": s.get("first_run"), "strip_days": len(strip),
               "window": list(WINDOW), "ignore": list(IGNORE),
               "failing": s.get("last_failing") or [], "ignored_failing": s.get("last_ignored") or [],
               "watched": s.get("last_watched") or [], "jobs": s.get("last_jobs") or [],
               "shadow": shadow, "network": s.get("network") or {},
               "last_event": last_event, "recent_log": recent,
               "strip": strip, "strip_summary": strip_summary,
               "heartbeat_at": hb_at, "heartbeat_age_seconds": hb_age,
               "last_stall_seconds": stall.get("seconds"), "last_stall_at": stall.get("at"),
               "backup_ok": backup.get("ok"), "backup_age_hours": backup.get("age_hours"),
               "backup_at": backup.get("at"),
               "updated": datetime.datetime.now().isoformat(timespec="seconds")}
        os.makedirs(WWW_DIR, exist_ok=True)
        tmp = os.path.join(WWW_DIR, ".smart_updater_status.tmp")
        with open(tmp, "w") as f:
            json.dump(out, f, indent=1)
        os.replace(tmp, os.path.join(WWW_DIR, "smart_updater_status.json"))
    except Exception as e:
        if datetime.datetime.now().minute % 15 == 0:   # a broken ha_url must not write 1,440 lines a day
            log(f"status write failed: {e!r}")

def status():
    write_status()

def incident(last_beat, gap):
    """Snapshot what Supervisor knows, before the old boot's logs rotate away."""
    d = os.path.join("/config/claude/incidents", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    meta = {"gap_seconds": round(gap), "last_beat": last_beat.isoformat(),
            "recovered": datetime.datetime.now().isoformat()}
    for name, path in (("host", "/host/info"), ("os", "/os/info"), ("core", "/core/info"),
                       ("resolution", "/resolution/info"), ("supervisor", "/supervisor/info"),
                       ("backups", "/backups/info")):
        try:
            meta[name] = sup(path)
        except Exception as e:
            meta[name] = {"error": repr(e)}
    with open(os.path.join(d, "supervisor.json"), "w") as f:
        json.dump(meta, f, indent=1)
    for name in ("home-assistant.log", "home-assistant.log.1", "home-assistant.log.2"):
        p = os.path.join("/config", name)
        try:
            if os.path.exists(p):
                with open(p, "rb") as src, open(os.path.join(d, name), "wb") as dst:
                    dst.write(src.read()[-2_000_000:])
        except OSError:
            pass
    try:
        root = "/config/claude/incidents"
        dirs = sorted(x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x)))
        for old in dirs[:-10]:
            shutil.rmtree(os.path.join(root, old), ignore_errors=True)
    except OSError:
        pass
    log(f"incident saved to {d}")

def heartbeat():
    """Every minute: a gap over stall_min is a freeze; report it once, on recovery. Retries a deferred diagnosis."""
    path = os.path.join(DIR, ".heartbeat")
    now = time.time()
    try:
        last = float(open(path).read().strip())
    except (OSError, ValueError):
        last = 0.0
    try:
        s = load()
        gap = now - last if last else 0
        if last and gap > STALL_MIN * 60 and not s.get("pre"):
            beat = datetime.datetime.fromtimestamp(last)
            s["last_stall"] = {"seconds": round(gap), "at": datetime.datetime.now().isoformat(timespec="seconds")}
            bump(s, "stalls")
            save(s)
            log(f"STALL detected: {gap:.0f}s since {beat.isoformat(timespec='seconds')}")
            notify("stall_recovered", "HA: was unresponsive",
                   f"Unresponsive for about {int(gap // 3600)}h {int((gap % 3600) // 60)}m, recovered at "
                   f"{datetime.datetime.now():%H:%M}.", key=f"stall_recovered:{s['last_stall']['at']}")
            try:
                incident(beat, gap)
            except Exception as e:
                log(f"incident capture failed: {e!r}")
        s = load()                                # notify() saved a fresh state; do not overwrite its dedupe record
        pd = s.get("pending_diagnosis")
        if pd and not s.get("pre") and int(pd.get("attempts", 0)) < 3:
            pd["attempts"] = int(pd.get("attempts", 0)) + 1
            s["pending_diagnosis"] = pd
            save(s)
            s["open_issue"] = s.get("open_issue") or {"version": pd.get("version"), "domains": pd.get("domains"),
                                                      "at": pd.get("at")}
            s["open_issue"]["diagnosis"] = diagnose(pd.get("domains") or [])
            s.pop("pending_diagnosis", None)
            save(s)
    except Exception as e:
        log(f"heartbeat failed: {e!r}")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{now:.0f}\n")
        os.replace(tmp, path)
    except OSError as e:
        log(f"heartbeat write failed: {e!r}")
    write_status()

def backup_health():
    """Daily: the newest Supervisor backup must be under 36 h old and contain the HA config."""
    s = load()
    try:
        data = sup("/backups/info") or {}
        backups = (data.get("data") or {}).get("backups") or []
        backups = sorted(backups, key=lambda b: b.get("date") or "")
        if not backups:
            s["backup"] = {"at": None, "age_hours": None, "ok": False, "name": None, "type": None}
            bump(s, "backup_bad")
            save(s)
            log("backup: no backups returned")
            notify("backup_stale", "HA: backup needs attention", "There are no backups at all.", key="backup_stale")
            write_status()
            return
        b = backups[-1]
        when = parse_dt(b.get("date"))
        now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
        age = (now - when).total_seconds() / 3600
        has_ha = bool((b.get("content") or {}).get("homeassistant"))
        ok = has_ha and age < 36
        s["backup"] = {"at": b.get("date"), "age_hours": round(age, 1), "ok": ok,
                       "name": b.get("name"), "type": b.get("type")}
        if not ok:
            bump(s, "backup_bad")
        save(s)
        log(f"backup: {b.get('name')!r} {b.get('date')} age={age:.1f}h homeassistant={has_ha} ok={ok}")
        if not ok:
            notify("backup_stale", "HA: backup needs attention",
                   f"Newest backup is {age:.1f}h old and {'includes' if has_ha else 'does not include'} the HA config.",
                   key="backup_stale")
    except Exception as e:
        log(f"backup_health failed: {e!r}")
    write_status()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "selftest":
        selftest()
        log("selftest ok")                     # proof, when run through shell_command, that the deployed file works
        sys.exit()
    if cmd not in ("selftest", "heartbeat", "status"):     # those run every minute; do not flood the log
        for p in CONFIG_PROBLEMS:
            log(f"CONFIG ERROR: {p}")
    if cmd == "probe":
        log(f"probe config: ai_entity={AI_ENTITY} notify={NOTIFY} core_update={CORE_UPDATE} window={WINDOW} stuck_min={STUCK_MIN} ha_url={HA_URL}")
        for name, f in (("supervisor /core/info", lambda: sup("/core/info")["data"]["version"]), ("ha /config", version)):
            try: log(f"probe {name}: ok {f()}")
            except Exception as e: log(f"probe {name}: FAIL {e!r}")
        sys.exit()
    if cmd not in ("preflight", "check", "sweep", "diagnose", "dryrun", "status", "heartbeat", "backup_health", "network"):
        sys.exit(f"usage: {sys.argv[0]} preflight|check|sweep|diagnose <domain..>|dryrun <version>"
                 f"|status|heartbeat|backup_health|network|selftest")
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
            dryrun([a for a in sys.argv[2:] if not a.startswith("--")][0])
        else:
            globals()[cmd]()
        try:                                        # a success ends the streak
            st = load()
            if (st.get("errors") or {}).pop(cmd, None):
                save(st)
                log(f"{cmd}: recovered, error streak cleared")
        except Exception:
            pass
    except Exception as e:
        log(f"{cmd} failed: {e!r}")
        if cmd != "sweep":                            # a stuck sweep retries; a stuck command tells you after 3 in a row
            try:
                s = load()
                errs = s.setdefault("errors", {})
                n = int((errs.get(cmd) or {}).get("count", 0)) + 1
                errs[cmd] = {"count": n, "at": datetime.datetime.now().isoformat()}
                save(s)
                if n >= 3:
                    notify("updater_error", f"HA: smart updater {cmd} failing",
                           f"{cmd} failed {n} times in a row; last error: {e!r}", key=f"error:{cmd}")
            except Exception as e2:
                log(f"error bookkeeping failed: {e2!r}")
