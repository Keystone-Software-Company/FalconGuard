import copy
import getpass
import queue
import threading
import urllib.parse
import urllib.request
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

VERSION = "1"

# --- Windows 2000 system colours ---------------------------------------------
FACE = "#d4d0c8"      # COLOR_3DFACE
HILIGHT = "#ffffff"   # COLOR_3DHILIGHT
SHADOW = "#808080"    # COLOR_3DSHADOW
DKSHADOW = "#404040"  # COLOR_3DDKSHADOW
INK = "#000000"       # COLOR_WINDOWTEXT
GRAYTEXT = "#808080"  # COLOR_GRAYTEXT
SELECT = "#0a246a"    # COLOR_HIGHLIGHT / active caption
TITLE_END = "#a6caf0" # COLOR_GRADIENTACTIVECAPTION
WINDOW = "#ffffff"    # COLOR_WINDOW
BLOCKED = "#6a96e8"   # time grid: blocked hour
GRIDLINE = "#1e2c50"  # time grid lines
ALWAYS_FG = "#006000"
NEVER_FG = "#a00000"

# Bevel colour sets: (outer top-left, outer bottom-right, inner top-left, inner bottom-right)
RAISED = (FACE, DKSHADOW, HILIGHT, SHADOW)       # windows, buttons, property sheets
SUNKEN = (SHADOW, HILIGHT, DKSHADOW, FACE)       # text fields, lists, trees
THIN_SUNKEN = (SHADOW, HILIGHT, None, None)      # status bar panels

# --- Content ---------------------------------------------------------------------
CATEGORIES = {
    "Web Filter": [
        "Only websites on the Approved Sites list can be opened.",
        "Blocks adult content, violence, gambling, drugs and strong language.",
        "Blocks only explicit adult content and the most harmful websites.",
        "No websites are blocked by the filter.",
    ],
}
LEVEL_NAMES = ["Restricted", "Moderated", "Limited", "Unrestricted"]

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# Default schedule: nights blocked, homework time on weekdays.
EXAMPLE_BLOCKED = {d: set(range(0, 7)) for d in range(7)}
for _d in range(1, 6):
    EXAMPLE_BLOCKED[_d] |= {17, 18}          # 5 PM - 7 PM, Monday to Friday
for _d in (0, 1, 2, 3, 4):
    EXAMPLE_BLOCKED[_d] |= {21, 22, 23}      # school nights end at 9 PM
for _d in (5, 6):
    EXAMPLE_BLOCKED[_d] |= {22, 23}          # weekends end at 10 PM
EXAMPLE_LIMITS = [240, 120, 120, 120, 120, 180, 240]  # minutes per day, Sunday first

OPTIONS = [
    ("allow_unrated", "Users can see sites that have no rating"),
    ("supervisor_override", "Supervisor can type a password to allow users to\n"
                            "view restricted content"),
    ("use_schedule", "Use the schedule on the Time Management tab"),
]

ZONES = [
    ("internet", "Internet",
     "This zone contains all websites you\nhaven't placed in other zones."),
    ("trusted", "Trusted sites",
     "This zone contains websites that you\ntrust to be safe for your child."),
    ("restricted", "Restricted sites",
     "This zone contains websites that\ncould be harmful to your child."),
]
ZONE_NAMES = {k: n for k, n, _ in ZONES}
DEFAULT_ZONE_LEVEL = {"internet": 1, "trusted": 2, "restricted": 0}

PROTECTION = [
    ("High", ["- The safest way to browse, and the most limited",
              "- Downloads and pop-ups are blocked",
              "- Chat and messaging sites are blocked",
              "- Recommended for children under 10"]),
    ("Medium", ["- Most unsuitable content is blocked",
                "- Downloads ask for the supervisor password",
                "- Some chat sites are blocked",
                "- Recommended for children 10 to 13"]),
    ("Low", ["- Only the most harmful content is blocked",
             "- Downloads are allowed",
             "- Chat sites are allowed",
             "- Recommended for children above 13"]),
]

LIMIT_VALUES = list(range(30, 24 * 60 + 1, 30)) + [None]   # half hours, then "No limit"
MAX_LOG_ENTRIES = 5000
FAILED_ATTEMPTS_ALLOWED = 3


def fmt_minutes(m):
    if m is None:
        return "No limit"
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h {m % 60:02d}m" if m % 60 else f"{m // 60}h"


def fmt_hour(h):
    """0 -> '12:00 AM', 13 -> '1:00 PM', 24 -> 'midnight'."""
    if h >= 24:
        return "midnight"
    return f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'}"


def today_index(now=None):
    """Index into DAYS (Sunday first) for a datetime."""
    return ((now or datetime.now()).weekday() + 1) % 7


def screen_time(settings, now=None, used_seconds=0, bonus=None):
    """Work out today's screen time from saved settings and the time used so far.

    left / left_seconds: the most time that can still be used today (all allowed hours).
    run_seconds: how long the child can keep going right now before the limit or the
    next blocked hour stops them (0 when the computer is blocked right now)."""
    now = now or datetime.now()
    d = today_index(now)
    limit = settings["daily_limits"][d]
    if bonus is None:
        bonus = bonus_today(settings, now=now)
    used_seconds = max(0, int(used_seconds))
    schedule_on = settings["options"]["use_schedule"]
    blocked = set(settings["blocked_hours"][d]) if schedule_on else set()
    into_hour = now.minute * 60 + now.second
    allowed_left_s = sum((3600 - into_hour) if h == now.hour else 3600
                         for h in range(now.hour, 24) if h not in blocked)
    blocked_now = now.hour in blocked
    change = next((h for h in range(now.hour + 1, 24) if (h in blocked) != blocked_now), None)
    budget_s = None if limit is None else (limit + bonus) * 60 - used_seconds
    left_s = allowed_left_s if budget_s is None else max(0, min(budget_s, allowed_left_s))
    if blocked_now:
        run_s = 0
    else:
        until = (change if change is not None else 24) * 3600 - (now.hour * 3600 + into_hour)
        run_s = until if budget_s is None else max(0, min(budget_s, until))
        # Reaching midnight isn't a stop: the day's time starts again. Only count midnight
        # as the end if tomorrow's schedule blocks 12 AM.
        stops_at_midnight = change is None and (budget_s is None or budget_s > until)
        if stops_at_midnight:
            tomorrow_blocked = schedule_on and 0 in settings["blocked_hours"][(d + 1) % 7]
            if not tomorrow_blocked:
                run_s = until + 24 * 3600
    return {"day": DAYS[d], "limit": limit, "bonus": bonus, "used": used_seconds // 60,
            "budget": None if budget_s is None else max(0, budget_s) // 60,
            "allowed_left": allowed_left_s // 60, "left": left_s // 60,
            "left_seconds": left_s, "run_seconds": run_s, "blocked_now": blocked_now,
            "next_change": change, "schedule_on": schedule_on}


COUPON_VALUES = [15, 30, 45, 60, 90, 120, 180, 240]
COUPON_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L mix-ups
COUPON_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")


def new_coupon_code(existing=()):
    while True:
        raw = "".join(secrets.choice(COUPON_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        if code not in existing:
            return code


def clean_code(text):
    """'k7pd 3xqm' -> 'K7PD-3XQM' (or None if it can't be a coupon code)."""
    raw = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else None


def redemption_times(settings, state, guard):
    """code -> time it was first redeemed. Combines the child-writable state file, the
    guard's protected copy and older settings files, so deleting a record never makes a
    used coupon usable again."""
    first = {}
    records = list(state.get("redemptions", [])) + list(guard.get("redemptions", []))
    records += [{"code": c["code"], "time": c["redeemed"]} for c in settings["coupons"]
                if c.get("redeemed")]
    for r in records:
        code, when = r.get("code"), r.get("time")
        if isinstance(code, str) and isinstance(when, str) and (code not in first or when < first[code]):
            first[code] = when
    return first


GRANT_ITERATIONS = 200_000


def wrap_grant_key(key, password):
    """Store the grant key so it can only be recovered with the supervisor password."""
    salt = secrets.token_bytes(16)
    pad = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, GRANT_ITERATIONS, 32)
    return {"salt": salt.hex(), "wrapped": bytes(a ^ b for a, b in zip(key, pad)).hex(),
            "check": hmac.new(key, b"falcon", "sha256").hexdigest()[:16]}


def unwrap_grant_key(wrap, password):
    try:
        pad = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(wrap["salt"]),
                                  GRANT_ITERATIONS, 32)
        key = bytes(a ^ b for a, b in zip(bytes.fromhex(wrap["wrapped"]), pad))
        if hmac.compare_digest(hmac.new(key, b"falcon", "sha256").hexdigest()[:16], wrap["check"]):
            return key
    except (KeyError, ValueError, TypeError):
        pass
    return None


def grant_mac(key, g):
    msg = f"{g['date']}|{g['minutes']}|{g['nonce']}|{g['time']}".encode()
    return hmac.new(key, msg, "sha256").hexdigest()


def make_grant(key, minutes, now=None):
    """Extra time given by the supervisor on the child's account (time's up screen)."""
    now = now or datetime.now()
    g = {"date": now.date().isoformat(), "minutes": int(minutes),
         "nonce": secrets.token_hex(8), "time": now.isoformat(timespec="seconds")}
    g["mac"] = grant_mac(key, g)
    return g


def grant_is_valid(key, g):
    try:
        return hmac.compare_digest(grant_mac(key, g), str(g.get("mac", "")))
    except (KeyError, TypeError):
        return False


def allow_mac(key, a):
    msg = f"allow|{a['site']}|{a['time']}|{a['nonce']}".encode()
    return hmac.new(key, msg, "sha256").hexdigest()


def make_allow(key, site, now=None):
    """'Always allow' given on the child's account (Falcon Advisor), signed like a grant."""
    a = {"site": site, "time": (now or datetime.now()).isoformat(timespec="seconds"),
         "nonce": secrets.token_hex(8)}
    a["mac"] = allow_mac(key, a)
    return a


def allow_is_valid(key, a):
    try:
        return hmac.compare_digest(allow_mac(key, a), str(a.get("mac", "")))
    except (KeyError, TypeError):
        return False


def bonus_today(settings, state=None, guard=None, now=None):
    """Extra minutes today = the supervisor's extra time (settings) + coupons first redeemed
    today + time grants from the time's up screen, minus anything before a reset."""
    now = now or datetime.now()
    today = now.date().isoformat()
    state, guard = state or {}, guard or {}
    b = settings["bonus"]
    total = b["minutes"] if b.get("date") == today else 0
    reset = b.get("reset", "") if b.get("date") == today else ""
    valid = {c["code"]: c["minutes"] for c in settings["coupons"]}
    for code, when in redemption_times(settings, state, guard).items():
        if code in valid and when[:10] == today and when > reset and \
                not any(c["code"] == code and c.get("redeemed") for c in settings["coupons"]):
            total += valid[code]
    # older files counted coupon time inside settings["bonus"]; don't count it twice
    for g in state.get("grants", []):
        if g.get("date") == today and str(g.get("time", "")) > reset and \
                isinstance(g.get("minutes"), int) and 0 < g["minutes"] <= 240:
            total += g["minutes"]
    return total


# --- Storage ---------------------------------------------------------------------
def data_dir():
    if os.environ.get("FALCON_DATA_DIR"):
        return Path(os.environ["FALCON_DATA_DIR"])
    if sys.platform == "win32":
        # Shared by every Windows account, so the parent's settings reach the child's account.
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Falcon"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Falcon"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "falcon"


def default_settings():
    return {
        "version": 1,
        "enabled": False,
        "password": None,
        "hint": "No password configured",
        "web_filter_level": 1,
        "sites": [],
        "options": {"allow_unrated": False, "supervisor_override": False,
                    "use_schedule": False},
        "blocked_hours": [sorted(EXAMPLE_BLOCKED[d]) for d in range(7)],
        "daily_limits": list(EXAMPLE_LIMITS),
        "zones": {k: {"level": DEFAULT_ZONE_LEVEL[k], "sites": []} for k, _, _ in ZONES},
        "coupons": [],
        "bonus": {"date": "", "minutes": 0},
        "child_account": "",
    }


def sanitize(data):
    """Merge a loaded settings file onto the defaults, dropping anything invalid."""
    s = default_settings()
    if not isinstance(data, dict):
        return s
    if isinstance(data.get("enabled"), bool):
        s["enabled"] = data["enabled"]
    pw = data.get("password")
    if (isinstance(pw, dict) and all(isinstance(pw.get(k), str) for k in ("salt", "hash"))
            and isinstance(pw.get("iterations"), int)):
        s["password"] = {"salt": pw["salt"], "hash": pw["hash"], "iterations": pw["iterations"]}
    if isinstance(data.get("hint"), str):
        s["hint"] = data["hint"][:100]
    if data.get("web_filter_level") in range(len(LEVEL_NAMES)):
        s["web_filter_level"] = data["web_filter_level"]
    sites = {}
    for item in data.get("sites", []) if isinstance(data.get("sites"), list) else []:
        if isinstance(item, dict) and item.get("rule") in ("always", "never"):
            site = normalize_site(str(item.get("site", "")))
            if site:
                sites[site] = item["rule"]
    s["sites"] = [{"site": k, "rule": v} for k, v in sorted(sites.items())]
    opts = data.get("options")
    if isinstance(opts, dict):
        for k, _ in OPTIONS:
            if isinstance(opts.get(k), bool):
                s["options"][k] = opts[k]
    hours = data.get("blocked_hours")
    if isinstance(hours, list) and len(hours) == 7:
        s["blocked_hours"] = [sorted({h for h in day if isinstance(h, int) and 0 <= h < 24})
                              if isinstance(day, list) else s["blocked_hours"][d]
                              for d, day in enumerate(hours)]
    limits = data.get("daily_limits")
    if isinstance(limits, list) and len(limits) == 7:
        s["daily_limits"] = [m if m in LIMIT_VALUES else s["daily_limits"][d]
                             for d, m in enumerate(limits)]
    zones = data.get("zones")
    if isinstance(zones, dict):
        for k in s["zones"]:
            z = zones.get(k)
            if not isinstance(z, dict):
                continue
            if z.get("level") in range(len(PROTECTION)):
                s["zones"][k]["level"] = z["level"]
            if k != "internet" and isinstance(z.get("sites"), list):
                s["zones"][k]["sites"] = sorted({n for n in (normalize_site(str(x))
                                                             for x in z["sites"]) if n})
    coupons, seen = [], set()
    for c in data.get("coupons", []) if isinstance(data.get("coupons"), list) else []:
        if (isinstance(c, dict) and isinstance(c.get("code"), str) and COUPON_RE.match(c["code"])
                and c["code"] not in seen and c.get("minutes") in COUPON_VALUES):
            seen.add(c["code"])
            coupons.append({"code": c["code"], "minutes": c["minutes"],
                            "note": str(c.get("note", ""))[:60],
                            "created": str(c.get("created", "")),
                            "redeemed": c["redeemed"] if isinstance(c.get("redeemed"), str) else None})
    s["coupons"] = coupons
    if isinstance(data.get("child_account"), str):
        s["child_account"] = data["child_account"].strip()[:100]
    b = data.get("bonus")
    if (isinstance(b, dict) and isinstance(b.get("date"), str)
            and isinstance(b.get("minutes"), int) and b["minutes"] >= 0):
        s["bonus"] = {"date": b["date"], "minutes": b["minutes"]}
        if isinstance(b.get("reset"), str):
            s["bonus"]["reset"] = b["reset"]
    w = data.get("grant_wrap")
    if isinstance(w, dict) and all(isinstance(w.get(k), str) for k in ("salt", "wrapped", "check")):
        s["grant_wrap"] = {k: w[k] for k in ("salt", "wrapped", "check")}
    return s


def hash_password(password, iterations=200_000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": iterations}


def check_password(password, record):
    if not record:
        return False
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(record["salt"]), record["iterations"])
        return hmac.compare_digest(digest.hex(), record["hash"])
    except (ValueError, KeyError, TypeError):
        return False


SITE_RE = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def normalize_site(text):
    """Turn 'https://WWW.Example.com/page' into 'www.example.com'. None if invalid."""
    t = text.strip().lower()
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t)
    t = t.split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".")
    return t if SITE_RE.match(t) else None


class Store:
    """Reads and writes settings.json and activity.log in the data folder."""

    def __init__(self, folder=None):
        self.folder = Path(folder) if folder else data_dir()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.folder / "settings.json"
        self.log_path = self.folder / "activity.log"
        self.usage_path = self.folder / "usage.json"        # child-writable: time used today
        self.state_path = self.folder / "state.json"        # child-writable: redemptions, grants
        self.guard_path = self.folder / "guard.json"        # written only by the guard (SYSTEM)
        self.secret_path = self.folder / "secret.json"      # readable only by admins/SYSTEM
        self.supervisor_path = self.folder / "supervisor.json"
        self.subscription_path = self.folder / "subscription.json"   # last answer from the gist
        if not folder and sys.platform == "win32":
            self._migrate_from_appdata()

    def _migrate_from_appdata(self):
        """Draft 0.5 and earlier kept settings in %APPDATA%\\Falcon; copy them once."""
        old = Path(os.environ.get("APPDATA", "")) / "Falcon"
        if self.settings_path.exists() or not (old / "settings.json").exists():
            return
        for name in ("settings.json", "activity.log"):
            try:
                if (old / name).exists():
                    shutil.copy2(old / name, self.folder / name)
            except OSError:
                pass

    # usage.json: screen time counted today on the child's account
    def load_usage(self):
        today = datetime.now().date().isoformat()
        try:
            u = json.loads(self.usage_path.read_text(encoding="utf-8"))
            if not isinstance(u, dict):
                raise ValueError
        except (OSError, ValueError):
            u = {}
        if u.get("date") != today:
            u = {"date": today, "seconds": 0, "warned": [],
                 "dashboard_date": u.get("dashboard_date", ""), "heartbeat": u.get("heartbeat", 0)}
        u.setdefault("seconds", 0)
        u.setdefault("warned", [])
        u.setdefault("dashboard_date", "")
        if not isinstance(u["seconds"], (int, float)) or u["seconds"] < 0:
            u["seconds"] = 0
        return u

    def save_usage(self, usage):
        usage["heartbeat"] = time.time()     # the guard checks this to know Falcon is running
        self._write_in_place(self.usage_path, usage)

    @staticmethod
    def _write_in_place(path, value):
        """For files the child's account may write: no temp file is needed in the folder."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(value, indent=1))

    def used_seconds_today(self):
        u = self.load_usage()
        g = self.load_guard().get("usage", {})
        best = max(u["seconds"], g.get("seconds", 0) if g.get("date") == u["date"] else 0)
        return int(best)

    def _read_json(self, path, default):
        try:
            v = json.loads(path.read_text(encoding="utf-8"))
            return v if isinstance(v, dict) else default
        except (OSError, ValueError):
            return default

    def _write_json(self, path, value):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def load_state(self):
        st = self._read_json(self.state_path, {})
        st["redemptions"] = [r for r in st.get("redemptions", []) if isinstance(r, dict)
                             and isinstance(r.get("code"), str) and isinstance(r.get("time"), str)]
        st["grants"] = [g for g in st.get("grants", []) if isinstance(g, dict)]
        st["allows"] = [a for a in st.get("allows", []) if isinstance(a, dict)
                        and isinstance(a.get("site"), str)]
        return st

    def save_state(self, st):
        self._write_in_place(self.state_path, st)

    def load_guard(self):
        g = self._read_json(self.guard_path, {})
        g.setdefault("redemptions", [])
        return g

    def save_guard(self, g):
        self._write_json(self.guard_path, g)

    def grant_key(self, create=False):
        """The secret used to check time grants. None if it can't be read (child's account)."""
        sec = self._read_json(self.secret_path, None)
        if sec and isinstance(sec.get("grant_key"), str):
            try:
                return bytes.fromhex(sec["grant_key"])
            except ValueError:
                pass
        if create and not self.secret_path.exists():
            key = secrets.token_bytes(32)
            try:
                self._write_json(self.secret_path, {"grant_key": key.hex()})
                return key
            except OSError:
                return None
        return None

    # subscription.json: the last subscription details read from the gist. Written by the
    # supervisor's window and the guard (SYSTEM); only read on the child's account, so the
    # expiry date still applies when the gist can't be reached.
    def load_subscription(self):
        raw = self._read_json(self.subscription_path, None)
        sub = parse_subscription(raw)
        if sub:
            sub["checked"] = str(raw.get("checked", ""))
        return sub

    def save_subscription(self, sub):
        try:
            self._write_json(self.subscription_path, sub)
        except OSError:
            pass

    def refresh_subscription(self, fetch=None):
        """Use the gist if it can be reached (saving the answer), otherwise the saved copy."""
        sub = (fetch or fetch_subscription)()
        if sub:
            self.save_subscription(sub)
            return sub
        return self.load_subscription()

    # supervisor.json: "a supervisor is signed in right now" heartbeat
    def supervisor_heartbeat(self, seconds=20):
        try:
            self.supervisor_path.write_text(json.dumps({"until": time.time() + seconds}),
                                            encoding="utf-8")
        except OSError:
            pass

    def supervisor_active(self):
        try:
            return json.loads(self.supervisor_path.read_text(encoding="utf-8"))["until"] > time.time()
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def end_supervisor_heartbeat(self):
        try:
            self.supervisor_path.unlink()
        except OSError:
            pass

    def load(self):
        if not self.settings_path.exists():
            return default_settings()
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                os.replace(self.settings_path, self.settings_path.with_suffix(".damaged.json"))
            except OSError:
                pass
            self.log("Settings file was damaged and has been reset")
            return default_settings()
        return sanitize(data)

    def save(self, settings):
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, self.settings_path)

    def log(self, event):
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "event": event}
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            if self.log_path.stat().st_size > 1_000_000:
                entries = self.read_log()[-MAX_LOG_ENTRIES // 2:]
                with open(self.log_path, "w", encoding="utf-8") as f:
                    f.write("".join(json.dumps(e) + "\n" for e in entries))
        except OSError:
            pass  # the log must never stop the settings window from working

    def read_log(self):
        entries = []
        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict) and "time" in e and "event" in e:
                            entries.append(e)
                    except ValueError:
                        continue
        except OSError:
            pass
        return entries

    def _write_log(self, entries):
        tmp = self.log_path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        os.replace(tmp, self.log_path)

    def clear_log(self):
        with open(self.log_path, "w", encoding="utf-8"):
            pass


def describe_changes(old, new):
    """Plain-language list of what changed between two settings dicts, for the log."""
    out = []
    if old["web_filter_level"] != new["web_filter_level"]:
        out.append(f"Web Filter level changed from {LEVEL_NAMES[old['web_filter_level']]} "
                   f"to {LEVEL_NAMES[new['web_filter_level']]}")
    o = {s["site"]: s["rule"] for s in old["sites"]}
    n = {s["site"]: s["rule"] for s in new["sites"]}
    for site in sorted(n.keys() - o.keys()):
        out.append(f"{site} added to the {n[site].capitalize()} list")
    for site in sorted(o.keys() - n.keys()):
        out.append(f"{site} removed from the approved and blocked list")
    for site in sorted(o.keys() & n.keys()):
        if o[site] != n[site]:
            out.append(f"{site} moved to the {n[site].capitalize()} list")
    for key, text in OPTIONS:
        if old["options"][key] != new["options"][key]:
            state = "on" if new["options"][key] else "off"
            out.append(f"Option turned {state}: {' '.join(text.split())}")
    if old.get("child_account") != new.get("child_account"):
        out.append(f"Child's account changed to {new['child_account'] or '(none)'}")
    for d, day in enumerate(DAYS):
        if old["blocked_hours"][d] != new["blocked_hours"][d]:
            out.append(f"Allowed hours changed for {day} "
                       f"({24 - len(new['blocked_hours'][d])}h allowed)")
    for d, day in enumerate(DAYS):
        if old["daily_limits"][d] != new["daily_limits"][d]:
            out.append(f"Daily time limit for {day} changed from "
                       f"{fmt_minutes(old['daily_limits'][d])} to "
                       f"{fmt_minutes(new['daily_limits'][d])}")
    for kind, name, _ in ZONES:
        oz, nz = old["zones"][kind], new["zones"][kind]
        if oz["level"] != nz["level"]:
            out.append(f"Protection level for {name} changed from "
                       f"{PROTECTION[oz['level']][0]} to {PROTECTION[nz['level']][0]}")
        for site in sorted(set(nz["sites"]) - set(oz["sites"])):
            out.append(f"{site} added to {name}")
        for site in sorted(set(oz["sites"]) - set(nz["sites"])):
            out.append(f"{site} removed from {name}")
    return out


# --- Enforcement helpers (Windows) ---------------------------------------------------
IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# Browsers Falcon closes when a blocked website is opened.
BROWSERS = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
            "vivaldi.exe", "iexplore.exe", "chromium.exe", "waterfox.exe", "librewolf.exe",
            "yandex.exe", "browser.exe"]
CLOUDFLARE_FAMILY_DOH = "https://family.cloudflare-dns.com/dns-query"
VERDICT_CACHE_SECONDS = 6 * 3600
LOCK_EVERY_SECONDS = 5
GRACE_AFTER_TYPING_SECONDS = 7   # the lock waits this long after the last key typed on the time's up screen
GRACE_MAX_SECONDS = 120          # but typing can hold it off for at most this long, once every 5 minutes
WARN_AT = (5, 1)            # minutes before time runs out


def current_user():
    return os.environ.get("USERNAME") or getpass.getuser()


def session_active():
    """True while this account is logged on, unlocked and on screen (not switched away)."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        u = ctypes.windll.user32
        u.OpenInputDesktop.restype = ctypes.c_void_p
        u.SwitchDesktop.argtypes = [ctypes.c_void_p]
        u.CloseDesktop.argtypes = [ctypes.c_void_p]
        desk = u.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
        if not desk:
            return False                               # locked, or another account is active
        ok = u.SwitchDesktop(desk)
        u.CloseDesktop(desk)
        return bool(ok)
    except Exception:
        return True


IDLE_LIMIT_SECONDS = 75   # no mouse or keyboard for 1 min 15 s: time counting pauses
IDLE_HOLD_SECONDS = 15    # quiet for longer than this: time is held back, and only counted
                          # if the mouse or keyboard is used again before IDLE_LIMIT_SECONDS


def idle_seconds():
    """Seconds since the last mouse or keyboard input in this Windows session."""
    if IS_WINDOWS:
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
                return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0   # handles the 49-day wrap
        except Exception:
            pass
        return 0.0
    if shutil.which("xprintidle"):
        try:
            return int(subprocess.run(["xprintidle"], capture_output=True, text=True,
                                      timeout=5).stdout.strip()) / 1000.0
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    return 0.0


def lock_workstation():
    """The same as pressing Windows+L."""
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.LockWorkStation()
        except Exception:
            pass


def notify(title, text):
    """Show a Windows notification (toast). Falls back quietly if it can't."""
    try:
        if IS_WINDOWS:
            q = lambda v: v.replace("'", "''")
            script = (
                "$m=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
                "ContentType=WindowsRuntime];"
                "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$t=$x.GetElementsByTagName('text');"
                f"$null=$t.Item(0).AppendChild($x.CreateTextNode('{q(title)}'));"
                f"$null=$t.Item(1).AppendChild($x.CreateTextNode('{q(text)}'));"
                "$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'"
                ").Show($n)")
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle",
                              "Hidden", "-Command", script], creationflags=CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{text}" with title "{title}"'])
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, text])
    except Exception:
        pass


def kill_browsers(user=None):
    """Close every browser Falcon knows about (only that account's windows; default: this one)."""
    if IS_WINDOWS and user is None:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            left = 0
            for pid in _browser_pids_api():
                h = k32.OpenProcess(0x0001, False, pid)            # PROCESS_TERMINATE
                if h:
                    if not k32.TerminateProcess(h, 1):
                        left += 1
                    k32.CloseHandle(h)
                else:
                    left += 1
            if not left:
                return
        except Exception:
            pass                                  # fall back to taskkill
    user = user or current_user()
    for exe in BROWSERS:
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/IM", exe, "/FI", f"USERNAME eq {user}"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
            elif shutil.which("pkill"):
                subprocess.run(["pkill", "-u", user, "-x", exe.removesuffix(".exe")],
                               capture_output=True, timeout=10)
        except Exception:
            pass


def flush_dns():
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.dnsapi.DnsFlushResolverCache()
        except Exception:
            pass


def _is_ip(value):
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def parse_displaydns_entries(text):
    """{website name: addresses it was given} from "ipconfig /displaydns". Each block starts
    with the name on a line followed by a line of dashes; its address lines end in an IP.
    Nothing depends on the words, so this works in every Windows language."""
    lines = [l.strip() for l in text.splitlines()]
    entries, current = {}, None
    for i, line in enumerate(lines):
        if line and i + 1 < len(lines) and re.fullmatch(r"-{6,}", lines[i + 1]):
            current = line.lower().rstrip(".")
            entries.setdefault(current, set())
        elif current and ":" in line and " . " in line:
            value = line.split(" : ", 1)[-1].strip() if " : " in line else line.rsplit(":", 1)[-1].strip()
            if _is_ip(value):
                entries[current].add(value)
    return entries


def parse_displaydns(text):
    """Website names from "ipconfig /displaydns"."""
    return set(parse_displaydns_entries(text))


def _displaydns():
    return subprocess.run(["ipconfig", "/displaydns"], capture_output=True, text=True,
                          errors="replace", creationflags=CREATE_NO_WINDOW, timeout=15).stdout


def _dns_cache_api():
    """{name: addresses} straight from the Windows DNS Client service: the list of cached
    names (DnsGetCacheDataTable), then each name's cached addresses (DnsQuery_W, cache only,
    so nothing goes out on the network). Milliseconds instead of seconds for ipconfig."""
    import ctypes
    import socket
    from ctypes import wintypes
    dns = ctypes.windll.dnsapi

    class CACHE_ENTRY(ctypes.Structure):
        pass
    CACHE_ENTRY._fields_ = [("pNext", ctypes.POINTER(CACHE_ENTRY)), ("pszName", wintypes.LPWSTR),
                            ("wType", ctypes.c_ushort), ("wDataLength", ctypes.c_ushort),
                            ("dwFlags", wintypes.DWORD)]

    class DATA(ctypes.Union):
        _fields_ = [("a", ctypes.c_ubyte * 4), ("aaaa", ctypes.c_ubyte * 16),
                    ("pad", ctypes.c_void_p * 8)]

    class RECORD(ctypes.Structure):
        pass
    RECORD._fields_ = [("pNext", ctypes.POINTER(RECORD)), ("pName", wintypes.LPWSTR),
                       ("wType", ctypes.c_ushort), ("wDataLength", ctypes.c_ushort),
                       ("Flags", wintypes.DWORD), ("dwTtl", wintypes.DWORD),
                       ("dwReserved", wintypes.DWORD), ("Data", DATA)]

    dns.DnsGetCacheDataTable.argtypes = [ctypes.POINTER(ctypes.POINTER(CACHE_ENTRY))]
    dns.DnsQuery_W.argtypes = [wintypes.LPCWSTR, ctypes.c_ushort, wintypes.DWORD, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.POINTER(RECORD)), ctypes.c_void_p]
    dns.DnsQuery_W.restype = ctypes.c_long
    dns.DnsFree.argtypes = [ctypes.c_void_p, ctypes.c_int]
    table = ctypes.POINTER(CACHE_ENTRY)()
    if not dns.DnsGetCacheDataTable(ctypes.byref(table)):
        raise OSError("DnsGetCacheDataTable failed")
    wanted = []
    try:
        e = table
        while e:
            if e.contents.pszName and e.contents.wType in (1, 28):     # A, AAAA
                wanted.append((e.contents.pszName, e.contents.wType))
            e = e.contents.pNext
    finally:
        dns.DnsFree(table, 1)                       # DnsFreeRecordList
    entries = {}
    for name, rtype in wanted:
        host = name.lower().rstrip(".")
        ips = entries.setdefault(host, set())
        rec = ctypes.POINTER(RECORD)()
        if dns.DnsQuery_W(name, rtype, 0x10, None, ctypes.byref(rec), None) != 0:   # NO_WIRE_QUERY
            continue                                # e.g. a "no such name" entry
        try:
            r = rec
            while r:
                c = r.contents
                if c.wType == 1:
                    ips.add(socket.inet_ntop(socket.AF_INET, bytes(c.Data.a)))
                elif c.wType == 28:
                    ips.add(socket.inet_ntop(socket.AF_INET6, bytes(c.Data.aaaa)))
                r = c.pNext
        finally:
            dns.DnsFree(rec, 1)
    return entries


def dns_cache_entries():
    """{name: addresses} for the website names this computer looked up recently: exactly the
    addresses the browser was given, so its connections can be matched to websites."""
    if not IS_WINDOWS:
        try:
            return {n: set() for n in dns_cache_names()}
        except Exception:
            return {}
    try:
        return _dns_cache_api()
    except Exception:
        pass                                        # fall back to ipconfig
    return parse_displaydns_entries(_displaydns())   # errors reach the watcher, which logs them


def dns_cache_names():
    """Website names this computer has looked up recently (the Windows DNS cache)."""
    if not IS_WINDOWS:
        return set()
    try:
        return parse_displaydns(_displaydns())
    except Exception:
        return set()


# Browser policies Falcon manages (Chrome, Edge and Brave read these from the user's registry).
POLICY_KEYS = [r"Software\Policies\Google\Chrome", r"Software\Policies\Microsoft\Edge",
               r"Software\Policies\BraveSoftware\Brave"]
POLICY_NAMES = ["ForceGoogleSafeSearch", "ForceYouTubeRestrict", "ForceBingSafeSearch",
                "DnsOverHttpsMode", "QuicAllowed", "BuiltInDnsClientEnabled",
                "StartupBoostEnabled", "BackgroundModeEnabled", "NetworkPredictionOptions"]
# Edge's "Startup boost" and Chrome/Edge background mode keep the browser running after its
# windows close, so it would never restart and never read Falcon's settings.
# NetworkPredictionOptions=2: don't look up / preload sites the browser guesses you'll open
# (for example from history at start-up); those lookups aren't visits.
KEEP_CLOSED = {"StartupBoostEnabled": 0, "BackgroundModeEnabled": 0, "NetworkPredictionOptions": 2}
# Pages where Google's SafeSearch setting is changed. Blocking them keeps it where it is,
# which is how the Limited level keeps Google on Blur.
SAFESEARCH_PAGES = ["google.com/safesearch", "google.com/setprefs", "google.com/preferences",
                    "google.co.uk/safesearch", "google.co.uk/setprefs", "google.co.uk/preferences"]


def browser_policies(level, enabled):
    """The SafeSearch policies for a Web Filter level. Empty = remove Falcon's policies."""
    if not enabled or level == 3:                       # off, or Unrestricted
        return {}
    # QuicAllowed=0 makes browsers use normal connections Falcon can see.
    # BuiltInDnsClientEnabled=0: Chrome and Edge otherwise look websites up with their own
    # DNS client, which skips Windows' list of lookups, so Falcon would never see them.
    if level == 2:                                      # Limited: "blur"
        # Google has no policy that turns Blur on, but Blur is Google's default and the
        # pages that change it are blocked (SAFESEARCH_PAGES), so it stays on Blur.
        return {"ForceYouTubeRestrict": 1, "ForceBingSafeSearch": 1, "DnsOverHttpsMode": "off",
                "QuicAllowed": 0, "BuiltInDnsClientEnabled": 0, **KEEP_CLOSED}
    # Restricted and Moderated: SafeSearch locked to strict (Filter)
    return {"ForceGoogleSafeSearch": 1, "ForceYouTubeRestrict": 2, "ForceBingSafeSearch": 2,
            "DnsOverHttpsMode": "off", "QuicAllowed": 0, "BuiltInDnsClientEnabled": 0, **KEEP_CLOSED}


def policy_problems(settings):
    """Policies the browsers need on this account (for Falcon to see their lookups) that
    aren't set, as 'Edge: QuicAllowed' strings. Empty when everything is in place."""
    if not IS_WINDOWS:
        return []
    import winreg
    wanted = browser_policies(settings["web_filter_level"], settings["enabled"])
    out = []
    for key, browser in zip(POLICY_KEYS[:2], ("Chrome", "Edge")):
        for name in ("DnsOverHttpsMode", "BuiltInDnsClientEnabled", "QuicAllowed"):
            if name not in wanted:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    ok = winreg.QueryValueEx(k, name)[0] == wanted[name]
            except OSError:
                ok = False
            if not ok:
                out.append(f"{browser}: {name}")
    return out


def apply_browser_policies(level, enabled, sid=None, profiles=None):
    """Write the policies to HKEY_CURRENT_USER, or with sid to that account's registry
    (HKEY_USERS\\SID: the guard does this as SYSTEM, because Windows doesn't let a standard
    account write its own browser policies). Returns True if a Chrome/Edge/Brave policy
    value actually changed (so the browsers need to restart to use it).
    DnsOverHttpsMode=off makes the browser use Windows' own lookups, which Falcon watches."""
    if not IS_WINDOWS:
        return False
    import winreg
    wanted = browser_policies(level, enabled)
    changed = False
    hive = winreg.HKEY_USERS if sid else winreg.HKEY_CURRENT_USER
    pre = f"{sid}\\" if sid else ""
    for key in POLICY_KEYS:
        try:
            k = winreg.CreateKeyEx(hive, pre + key, 0,
                                   winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        except OSError:
            continue
        with k:
            for name in POLICY_NAMES:
                try:
                    current = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    current = None
                try:
                    if name in wanted:
                        v = wanted[name]
                        if current != v:
                            winreg.SetValueEx(k, name, 0, winreg.REG_SZ if isinstance(v, str)
                                              else winreg.REG_DWORD, v)
                            changed = True
                    elif current is not None:
                        winreg.DeleteValue(k, name)
                        changed = True
                except OSError:
                    pass
        try:   # URLBlocklist is a list policy: values "1", "2", ... (Falcon owns this list)
            if wanted:
                with winreg.CreateKeyEx(hive, pre + key + r"\URLBlocklist", 0,
                                        winreg.KEY_SET_VALUE) as lk:
                    for i, page in enumerate(SAFESEARCH_PAGES, 1):
                        winreg.SetValueEx(lk, str(i), 0, winreg.REG_SZ, page)
            else:
                winreg.DeleteKey(hive, pre + key + r"\URLBlocklist")
        except OSError:
            pass
    apply_firefox_prefs(bool(wanted), profiles)
    try:   # Firefox: turn off its own DNS-over-HTTPS so Falcon can see its lookups
        with winreg.CreateKeyEx(hive, pre + r"Software\Policies\Mozilla\Firefox\DNSOverHTTPS", 0,
                                winreg.KEY_SET_VALUE) as k:
            for name, v in (("Enabled", 0), ("Locked", 1)):
                if wanted:
                    winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, v)
                else:
                    try:
                        winreg.DeleteValue(k, name)
                    except OSError:
                        pass
    except OSError:
        pass
    return changed


FIREFOX_START, FIREFOX_END = "// Falcon settings start", "// Falcon settings end"
FIREFOX_PREFS = [("network.trr.mode", 5),                 # no DNS-over-HTTPS (Falcon must see lookups)
                 ("network.http.http3.enable", "false"),  # no QUIC
                 ("network.dns.disablePrefetch", "true")]


def firefox_profiles():
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Mozilla" / \
        "Firefox" / "Profiles"
    try:
        return [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []


def apply_firefox_prefs(enabled, profiles=None):
    """Firefox may ignore per-user policies, but it always applies user.js in each profile
    when it starts. Falcon keeps its own block in that file and removes it when not needed."""
    done = 0
    for prof in profiles if profiles is not None else firefox_profiles():
        f = prof / "user.js"
        try:
            text = f.read_text(encoding="utf-8") if f.exists() else ""
            text = re.sub(re.escape(FIREFOX_START) + r".*?" + re.escape(FIREFOX_END) + r"\n?", "",
                          text, flags=re.S)
            if enabled:
                text += "\n".join([FIREFOX_START] + [f'user_pref("{k}", {v});'
                                                     for k, v in FIREFOX_PREFS] + [FIREFOX_END]) + "\n"
            f.write_text(text, encoding="utf-8")
            done += 1
        except OSError:
            pass
    return done


def account_sid(name):
    """The security ID (S-1-5-21-...) of a Windows account, or None."""
    if not IS_WINDOWS or not name:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        adv = ctypes.windll.advapi32
        sid_size, dom_size, use = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
        adv.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None,
                               ctypes.byref(dom_size), ctypes.byref(use))
        sid = ctypes.create_string_buffer(sid_size.value)
        dom = ctypes.create_unicode_buffer(dom_size.value)
        if not adv.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size), dom,
                                      ctypes.byref(dom_size), ctypes.byref(use)):
            return None
        text = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        value = text.value
        ctypes.windll.kernel32.LocalFree(text)
        return value
    except Exception:
        return None


def profile_dir(sid):
    """An account's profile folder (C:\\Users\\name) from its SID, or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "ProfileImagePath")[0]))
    except (OSError, ImportError):
        return None


def hive_loaded(sid):
    try:
        import winreg
        winreg.OpenKey(winreg.HKEY_USERS, sid).Close()
        return True
    except (OSError, ImportError):
        return False


def site_matches(host, site):
    """Does host (www.games.com) fall under a list entry (games.com, *.games.com)?"""
    base = site[2:] if site.startswith("*.") else site
    if base.startswith("www."):
        base = base[4:]
    return host == base or host.endswith("." + base)


def worth_checking(host):
    return ("." in host and not re.match(r"^[0-9.]+$", host) and ":" not in host
            and not host.endswith((".local", ".lan", ".home", ".arpa", ".internal"))
            and not host.endswith("cloudflare-dns.com"))


CLOUDFLARE_FAMILY_DNS = ("1.1.1.3", "1.0.0.3")     # Cloudflare for Families: malware + adult
CLOUDFLARE_ERROR = ""                             # why the last check failed (for the self-test)


def dns_a_records(host, server, timeout=3):
    """Ask a DNS server for a name's IPv4 addresses over plain UDP (port 53)."""
    import socket
    import struct
    qid = secrets.randbits(16)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in host.rstrip(".").split("."):
        query += bytes([len(part)]) + part.encode("idna")
    query += b"\0" + struct.pack(">HH", 1, 1)               # type A, class IN
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(query, (server, 53))
        while True:
            data, _ = sock.recvfrom(4096)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                break
    answers = struct.unpack(">H", data[6:8])[0]
    pos = 12
    while data[pos]:                                      # skip the question's name
        pos += data[pos] + 1
    pos += 5
    ips = []
    for _ in range(answers):
        if data[pos] & 0xC0 == 0xC0:                      # compressed name
            pos += 2
        else:
            while data[pos]:
                pos += data[pos] + 1
            pos += 1
        rtype, _cls, _ttl, length = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and length == 4:
            ips.append(".".join(str(b) for b in data[pos:pos + 4]))
        pos += length
    return ips


CANARY_HOST = "pornhub.com"     # Cloudflare for Families always answers 0.0.0.0 for this
_plain_dns = {"ok": None, "checked": 0.0}


def plain_dns_trusted():
    """Is plain DNS really reaching Cloudflare for Families? Some routers and internet
    providers redirect every DNS question to their own server; then the canary site comes
    back with a real address and Falcon must use DNS over HTTPS instead. Checked hourly."""
    if _plain_dns["ok"] is not None and time.time() - _plain_dns["checked"] < 3600:
        return _plain_dns["ok"]
    ok = False
    for server in CLOUDFLARE_FAMILY_DNS:
        try:
            ok = "0.0.0.0" in dns_a_records(CANARY_HOST, server)
            break
        except Exception:
            continue
    _plain_dns.update(ok=ok, checked=time.time())
    return ok


def _doh_answer(host, timeout):
    """DNS over HTTPS. Python first; if that fails (a fresh Windows can be missing the
    certificate), Windows' own downloader, which fetches missing certificates itself."""
    url = f"{CLOUDFLARE_FAMILY_DOH}?{urllib.parse.urlencode({'name': host, 'type': 'A'})}"
    req = urllib.request.Request(url, headers={"accept": "application/dns-json",
                                               "user-agent": "Falcon"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if not IS_WINDOWS:
            raise
        ok, text = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              f"(Invoke-WebRequest -UseBasicParsing -Uri '{url}' "
                              "-Headers @{accept='application/dns-json'}).Content"],
                             timeout=timeout + 20)
        if not ok:
            raise OSError(f"{type(e).__name__}: {e}; Windows downloader: {text[:200]}")
        return json.loads(text)


def cloudflare_says_blocked(host, timeout=5):
    """Ask Cloudflare for Families (free) whether a website is unsafe. It answers 0.0.0.0
    for adult content and malware. Returns True/False, or None if it couldn't be reached.
    Plain DNS when it really reaches Cloudflare (no certificates needed), else DNS over HTTPS."""
    global CLOUDFLARE_ERROR
    if plain_dns_trusted():
        for server in CLOUDFLARE_FAMILY_DNS:
            try:
                ips = dns_a_records(host, server, timeout=min(timeout, 3))
                CLOUDFLARE_ERROR = ""
                return "0.0.0.0" in ips
            except Exception:
                continue
    try:
        data = _doh_answer(host, timeout)
    except Exception as e:
        CLOUDFLARE_ERROR = ("plain DNS is redirected on this network; " if _plain_dns["ok"] is False
                            else "") + f"HTTPS: {type(e).__name__}: {e}"
        return None
    CLOUDFLARE_ERROR = ""
    return any(a.get("data") == "0.0.0.0" for a in data.get("Answer", []))


# Behind-the-scenes sites most pages need. On the Restricted level these are allowed too,
# so approved sites still work. Anything else the browser connects to is blocked.
SUPPORT_SITES = [
    # content networks pages load fonts and scripts from
    "gstatic.com", "googleapis.com", "ggpht.com", "jsdelivr.net", "cdnjs.cloudflare.com",
    "akamaized.net", "akamaihd.net", "azureedge.net", "azurefd.net", "trafficmanager.net",
    # the browsers' own services (updates, sign-in, sync, new tab page)
    "microsoft.com", "msedge.net", "msn.com", "bing.com", "bing.net", "live.com", "skype.com",
    "microsoftonline.com", "msftauth.net", "msauth.net", "aka.ms", "windows.com", "windows.net",
    "windowsupdate.com", "office.net", "msftconnecttest.com", "gvt1.com", "gvt2.com",
    "mozilla.net", "mozilla.com", "firefox.com", "services.mozilla.com",
    # certificate checks (every secure website needs these)
    "lencr.org", "letsencrypt.org", "digicert.com", "pki.goog", "sectigo.com", "usertrust.com",
    "globalsign.com", "identrust.com", "amazontrust.com"]


def _browser_pids_api():
    """Process ids of the browsers running in this Windows session (straight from Windows)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    my_session = wintypes.DWORD()
    if not k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(my_session)):
        raise OSError("ProcessIdToSessionId failed")
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)            # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    pids = set()
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() in BROWSERS:
                sess = wintypes.DWORD()
                if k32.ProcessIdToSessionId(e.th32ProcessID, ctypes.byref(sess)) \
                        and sess.value == my_session.value:
                    pids.add(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return pids


def _browser_connections_api():
    """browser_connections() straight from Windows (milliseconds, no programs started)."""
    import ctypes
    import socket
    from ctypes import wintypes
    iphlp = ctypes.windll.iphlpapi
    pids = _browser_pids_api()
    if not pids:
        return set()
    out = set()
    for family, row_size in ((socket.AF_INET, 24), (23, 56)):    # IPv4 rows, IPv6 rows
        size = wintypes.DWORD(0)
        buf = None
        for _ in range(5):                                  # the table can grow between calls
            buf = ctypes.create_string_buffer(size.value or 65536)
            size.value = len(buf)
            r = iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, 5, 0)
            if r == 0:
                break
            if r != 122:                                     # ERROR_INSUFFICIENT_BUFFER
                raise OSError(f"GetExtendedTcpTable error {r}")
        else:
            raise OSError("GetExtendedTcpTable: table kept growing")
        raw = buf.raw
        count = int.from_bytes(raw[0:4], "little")
        for i in range(count):
            row = raw[4 + i * row_size: 4 + (i + 1) * row_size]
            if family == socket.AF_INET:
                state, pid = int.from_bytes(row[0:4], "little"), int.from_bytes(row[20:24], "little")
                ip = socket.inet_ntop(socket.AF_INET, row[12:16])
            else:
                state, pid = int.from_bytes(row[48:52], "little"), int.from_bytes(row[52:56], "little")
                ip = socket.inet_ntop(socket.AF_INET6, row[24:40])
                if ip.startswith("::ffff:"):
                    ip = ip[7:]
            if state == 5 and pid in pids and not ip.startswith(("127.", "0.", "::1")):   # ESTABLISHED
                out.add(ip)
    return out


RETRY_CLOUDFLARE_SECONDS = 10   # Cloudflare didn't answer: allowed for now, asked again after this
WATCH_EVERY_SECONDS = 1.5        # how often the website filter looks at what the browser opened


def browser_connections():
    """Remote addresses the browsers on this account are connected to right now."""
    if not IS_WINDOWS:
        return set()
    try:
        return _browser_connections_api()
    except Exception:
        pass                                  # fall back to Windows' own programs
    try:
        tl = subprocess.run(["tasklist", "/fo", "csv", "/nh", "/fi", f"USERNAME eq {current_user()}"],
                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
                            timeout=15).stdout
        pids = {row[1] for row in (line.strip('"').split('","') for line in tl.splitlines())
                if len(row) > 1 and row[0].lower() in BROWSERS}
        if not pids:
            return set()
        ns = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,     # TCP over IPv4 and IPv6
                            creationflags=CREATE_NO_WINDOW, timeout=15).stdout
        out = set()
        for line in ns.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "ESTABLISHED" and parts[4] in pids:
                ip = parts[2].rsplit(":", 1)[0].strip("[]")
                if not ip.startswith(("127.", "0.", "::1")):
                    out.add(ip)
        return out
    except Exception:
        return set()


class SiteChecker:
    """Decides whether a website name is blocked under the current settings."""

    def __init__(self, ask_cloudflare=cloudflare_says_blocked):
        self.ask = ask_cloudflare
        self.cache = {}           # host -> (blocked, checked_at)
        self.allowed_once = set() # hosts the supervisor allowed "only this time"
        self.signed_allows = ()   # "always allow" sites saved on the child's account (state.json)
        self.addresses = {}       # host -> (set of IPs, looked up at)

    def _extra_allowed(self, host):
        # tuple() copies in one step, so the Tk thread adding a site can't break this loop
        return any(site_matches(host, a) for a in tuple(self.allowed_once) + tuple(self.signed_allows))

    def allowed_restricted(self, host, settings):
        """On the Restricted level: is this host approved (or a support site)?"""
        if self._extra_allowed(host):
            return True
        approved = [s["site"] for s in settings["sites"] if s["rule"] == "always"]
        approved += settings["zones"]["trusted"]["sites"] + SUPPORT_SITES
        return any(site_matches(host, a) for a in approved)

    def ips_of(self, host):
        hit = self.addresses.get(host)
        if hit and time.time() - hit[1] < 300:
            return hit[0]
        try:
            import socket
            ips = set(socket.gethostbyname_ex(host)[2])   # answered from Windows' cache
        except OSError:
            ips = set()
        self.addresses[host] = (ips, time.time())
        return ips

    def restricted_violation(self, hosts, connections, settings):
        """Restricted level: find a site the browser is connected to that isn't approved.
        An address shared by an approved site and another site counts as approved."""
        if not connections:
            return None
        known = hosts if isinstance(hosts, dict) else {h: set() for h in hosts}
        by_ip, new_lookups = {}, 0
        for host, cached_ips in known.items():
            if not worth_checking(host):
                continue
            if cached_ips:                     # exactly what the browser was given
                ips = cached_ips
            elif isinstance(hosts, dict):      # listed without an address (a failed lookup):
                continue                       # nothing the browser can be connected to
            else:                              # names only: look it up (a few per cycle)
                hit = self.addresses.get(host)
                if not (hit and time.time() - hit[1] < 300):
                    if new_lookups >= 40:
                        continue
                    new_lookups += 1
                ips = self.ips_of(host)
            for ip in ips:
                by_ip.setdefault(ip, set()).add(host)
        for ip in connections:
            names = by_ip.get(ip)
            if names and not any(self.allowed_restricted(h, settings) for h in names):
                return sorted(names, key=len)[0]
        return None

    def reasons(self, host, settings):
        """List of reasons the host is blocked (empty list = allowed)."""
        host = host.lower().rstrip(".")
        if self._extra_allowed(host):
            return []
        sites = settings["sites"]
        if any(s["rule"] == "always" and site_matches(host, s["site"]) for s in sites):
            return []
        if any(site_matches(host, t) for t in settings["zones"]["trusted"]["sites"]):
            return []
        out = []
        if any(s["rule"] == "never" and site_matches(host, s["site"]) for s in sites):
            out.append("This website is on the Never list.")
        if any(site_matches(host, r) for r in settings["zones"]["restricted"]["sites"]):
            out.append("This website is in the Restricted sites zone.")
        if not out and settings["web_filter_level"] in (1, 2) and worth_checking(host) \
                and not self._asking(host):     # still being asked: decided on the next check
            hit = self.cache.get(host)
            if hit and time.time() - hit[1] < VERDICT_CACHE_SECONDS:
                blocked = hit[0]
            else:
                blocked = self.ask(host)
                if blocked is None:   # couldn't reach Cloudflare: allow, try again soon
                    self.cache[host] = (False, time.time() - VERDICT_CACHE_SECONDS + RETRY_CLOUDFLARE_SECONDS)
                else:
                    self.cache[host] = (blocked, time.time())
            if blocked:
                out.append("Rated unsafe by Cloudflare for Families (adult or malware).")
        return out

    def warm(self, hosts, settings, workers=8, max_wait=3.0):
        """Ask Cloudflare about every new site at once (up to `workers` at a time), so
        reasons() finds the answers ready instead of waiting for each one in turn."""
        if settings["web_filter_level"] not in (1, 2):
            return
        now = time.time()
        todo = []
        for h in hosts:
            h = h.lower().rstrip(".")
            hit = self.cache.get(h)
            if hit and now - hit[1] < VERDICT_CACHE_SECONDS or not worth_checking(h):
                continue
            if self._extra_allowed(h) or any(site_matches(h, s["site"]) for s in settings["sites"]) \
                    or any(site_matches(h, t) for t in settings["zones"]["trusted"]["sites"]) \
                    or any(site_matches(h, r) for r in settings["zones"]["restricted"]["sites"]):
                continue                     # decided by the lists, Cloudflare isn't needed
            todo.append(h)
        if not todo:
            return
        from concurrent.futures import ThreadPoolExecutor, wait
        if getattr(self, "pool", None) is None:
            self.pool, self.pending = ThreadPoolExecutor(max_workers=workers), {}
        futures = []
        for h in todo:
            if not self._asking(h):          # not already being asked
                self.pending[h] = self.pool.submit(self._ask_and_store, h)
            futures.append(self.pending[h])
        wait(futures, timeout=max_wait)      # slow answers keep coming in the background

    def _ask_and_store(self, h):
        try:
            if self.ask is cloudflare_says_blocked:
                plain_dns_trusted()          # checked once an hour (cached)
            blocked = self._ask_safely(h)
            if blocked is None:              # couldn't reach Cloudflare: allow, try again soon
                self.cache[h] = (False, time.time() - VERDICT_CACHE_SECONDS + RETRY_CLOUDFLARE_SECONDS)
            else:
                self.cache[h] = (blocked, time.time())
        except Exception:
            pass

    def _asking(self, h):
        f = getattr(self, "pending", {}).get(h)
        return f is not None and not f.done()

    def _ask_safely(self, host):
        try:
            return self.ask(host)
        except Exception:
            return None


# --- Subscription (read from a GitHub gist) --------------------------------------------
# Put the "Raw" address of your gist here (open the gist, click "Raw", copy the address and
# remove the long commit id from it so it always gives the newest version), e.g.
#   https://gist.githubusercontent.com/<your-user>/<gist-id>/raw/falcon-subscription.json
# The gist file holds JSON like:
#   {"expires": "2026-12-31", "price": 4.99, "currency": "USD", "edition": "Home"}
SUBSCRIPTION_URL = os.environ.get(
    "FALCON_SUBSCRIPTION_URL",
    "https://gist.githubusercontent.com/keystonetechnologies-official/"
    "4fe1b691d4a65ed52f7be2d018947433/raw/falconsubscription.json")
SUBSCRIPTION_REFRESH_SECONDS = 3600     # check the gist every hour
WARN_DAYS, CRITICAL_DAYS = 3, 1
CURRENCY_SIGNS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$",
                  "NZD": "NZ$", "INR": "₹", "JPY": "¥"}


def parse_subscription(data):
    """Check the gist's JSON. Returns a clean dict, or None if it isn't valid."""
    if not isinstance(data, dict):
        return None
    try:
        y, m, d = (int(x) for x in str(data["expires"]).strip()[:10].split("-"))
        # a day past the end of the month (e.g. 2026-11-31) means the last day of that month
        import calendar
        expires = datetime(y, m, min(max(d, 1), calendar.monthrange(y, m)[1])).date()
    except (KeyError, ValueError, TypeError):
        return None
    try:
        price = round(float(data.get("price", 0)), 2)
    except (TypeError, ValueError):
        price = None
    return {"expires": expires.isoformat(), "price": price,
            "currency": str(data.get("currency", "USD")).upper()[:5],
            "edition": str(data.get("edition", "")).strip()[:40] or "Unknown"}


def fetch_subscription(url=None, timeout=8):
    """Download the subscription details. None if the gist can't be reached or is invalid."""
    url = url or SUBSCRIPTION_URL
    if "YOUR-GIST-ID" in url:
        return None
    # GitHub keeps raw gist files in its cache for 5 minutes; a unique query string makes it
    # send the newest version every time.
    url += ("&" if "?" in url else "?") + f"t={time.time_ns()}"
    req = urllib.request.Request(url, headers={"user-agent": "Falcon", "cache-control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8-sig"))
    except Exception:
        # A fresh Windows may not have the certificate Python needs yet; Windows' own
        # downloader fetches missing certificates by itself.
        if not IS_WINDOWS:
            return None
        ok, text = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              f"(Invoke-WebRequest -UseBasicParsing -Uri '{url}').Content"],
                             timeout=timeout + 20)
        try:
            data = json.loads(text) if ok else None
        except ValueError:
            data = None
        if data is None:
            return None
    sub = parse_subscription(data)
    if sub:
        sub["checked"] = datetime.now().isoformat(timespec="seconds")
    return sub


def subscription_status(sub, today=None):
    """{"level": unknown|ok|warning|critical|expired, "days": days left, "text": ...}"""
    if not sub:
        return {"level": "unknown", "days": None,
                "text": "Falcon couldn't check your subscription yet."}
    today = today or datetime.now().date()
    expires = datetime.strptime(sub["expires"], "%Y-%m-%d").date()
    days = (expires - today).days
    on = expires.strftime("%d %B %Y").lstrip("0")
    if days <= 0:
        return {"level": "expired", "days": days,
                "text": f"Your Falcon subscription expired on {on}. Protection is turned off "
                        "and can't be turned back on until you renew."}
    if days <= CRITICAL_DAYS:
        return {"level": "critical", "days": days,
                "text": f"Your Falcon subscription expires tomorrow ({on}). Renew it today or "
                        "protection will be turned off."}
    if days <= WARN_DAYS:
        return {"level": "warning", "days": days,
                "text": f"Your Falcon subscription expires in {days} days, on {on}. Renew it "
                        "to keep protection on."}
    return {"level": "ok", "days": days, "text": f"Active until {on}."}


def subscription_expired(sub, today=None):
    return subscription_status(sub, today)["level"] == "expired"


def fmt_price(sub):
    if not sub or sub.get("price") is None:
        return "Unknown"
    if sub["price"] == 0:
        return "Free"
    sign = CURRENCY_SIGNS.get(sub["currency"])
    amount = f"{sub['price']:.2f}"
    return f"{sign}{amount} per month" if sign else f"{amount} {sub['currency']} per month"


def fmt_days_left(days):
    if days is None:
        return "Unknown"
    if days < 0:
        return f"Expired {-days} day{'s' if days != -1 else ''} ago"
    if days == 0:
        return "Expired today"
    return f"{days} day{'s' if days != 1 else ''}"


# --- Shared edits (used by the FalconGuard window and the web dashboard) -------------
def add_coupon(s, minutes, note):
    code = new_coupon_code({c["code"] for c in s["coupons"]})
    s["coupons"].append({"code": code, "minutes": minutes, "note": note[:60],
                         "created": datetime.now().isoformat(timespec="seconds"), "redeemed": None})
    return code


def remove_coupon(s, code):
    s["coupons"] = [c for c in s["coupons"] if c["code"] != code]
    return True


def add_extra(s, minutes):
    today = datetime.now().date().isoformat()
    if s["bonus"]["date"] != today:
        s["bonus"] = {"date": today, "minutes": 0}
    s["bonus"]["minutes"] += minutes
    return True


def reset_extra(s):
    now = datetime.now()
    s["bonus"] = {"date": now.date().isoformat(), "minutes": 0,
                  "reset": now.isoformat(timespec="seconds")}
    return True


def redeem_coupon(store, settings, typed):
    """Record a coupon redemption in state.json. Returns (minutes | "missing" | "used", code).
    Raises OSError if the record can't be saved."""
    code = clean_code(typed.strip())
    coupon = next((c for c in settings["coupons"] if c["code"] == code), None) if code else None
    if coupon is None:
        return "missing", code
    if code in redemption_times(settings, store.load_state(), store.load_guard()):
        return "used", code
    st = store.load_state()
    st["redemptions"].append({"code": code, "time": datetime.now().isoformat(timespec="seconds")})
    store.save_state(st)
    return coupon["minutes"], code


def password_problem(new, confirm, hint):
    """What's wrong with a new password, or None. Returns (message, which field)."""
    if len(new) < 4:
        return "The password must be at least 4 characters long.", "new"
    if new != confirm:
        return ("The passwords do not match. Type the new password again in the Confirm "
                "password box.", "confirm")
    if hint and new.lower() in hint.lower():
        return "The hint cannot contain the password.", "hint"
    return None


def monitor_status(store, settings):
    """(running, text) for the background part on the child's account, from its heartbeat."""
    child = settings.get("child_account", "")
    if not child:
        return False, "Choose the child's Windows account so FalconGuard knows where to run."
    age = time.time() - store.load_usage().get("heartbeat", 0)
    if age < MONITOR_TIMEOUT:
        return True, f"FalconGuard is running on {child}'s account."
    return False, (f"FalconGuard isn't running on {child}'s account. It starts when {child} "
                   "signs in to Windows.")


# --- Guard: protection that runs as SYSTEM (installed by build.bat) -------------------
MONITOR_TIMEOUT = 120      # seconds without a heartbeat before Falcon counts as closed


def wts_sessions():
    """[(session id, user name)] for the logged-on Windows sessions."""
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    class INFO(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR),
                    ("State", ctypes.c_int)]
    wts = ctypes.windll.wtsapi32
    ptr, count = ctypes.POINTER(INFO)(), wintypes.DWORD()
    out = []
    if not wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(ptr), ctypes.byref(count)):
        return out
    try:
        for i in range(count.value):
            sid = ptr[i].SessionId
            buf, size = wintypes.LPWSTR(), wintypes.DWORD()
            if wts.WTSQuerySessionInformationW(None, sid, 5, ctypes.byref(buf), ctypes.byref(size)):
                if buf.value:
                    out.append((sid, buf.value))
                wts.WTSFreeMemory(buf)
    finally:
        wts.WTSFreeMemory(ptr)
    return out


def child_session(child):
    return next((sid for sid, user in wts_sessions() if user.lower() == child.lower()), None)


def disconnect_session(sid):
    """Send a session back to the lock screen (works from the SYSTEM account)."""
    if IS_WINDOWS and sid is not None:
        try:
            import ctypes
            ctypes.windll.wtsapi32.WTSDisconnectSession(None, sid, False)
        except Exception:
            pass


def apply_child_browser_settings(store, settings, child, protecting):
    """Guard (SYSTEM): write the browser settings into the child's account while they're
    signed in. Windows doesn't let a standard account write its own browser policies. When
    something changed, the child's browsers are closed so they reopen with the settings."""
    sid = account_sid(child) if child else None
    if not sid or not hive_loaded(sid):
        return []
    home = profile_dir(sid)
    ff = home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles" if home else None
    profiles = [p for p in ff.iterdir() if p.is_dir()] if ff and ff.is_dir() else []
    try:
        changed = apply_browser_policies(settings["web_filter_level"], protecting, sid=sid,
                                         profiles=profiles)
    except Exception as e:
        return [f"Couldn't set the browser settings for {child}: {type(e).__name__}: {e}"]
    if changed and protecting:
        kill_browsers(child)
        g = store.load_guard()
        g["browsers_restarted"] = time.time()
        try:
            store.save_guard(g)
        except OSError:
            pass
        store.log(f"Browser settings updated on {child}'s account; browsers closed to use them")
        return [f"Browser settings updated on {child}'s account"]
    return []


def run_guard(store, now=None, find_session=child_session, lock_session=disconnect_session):
    """One guard check (build.bat runs this every minute as SYSTEM). It:
       - locks the child's session if Falcon isn't running there (closed in Task Manager)
       - undoes screen time that was wound back in usage.json
       - restores deleted coupon redemptions, so a used coupon can't be used again
       - removes forged time grants (they need the supervisor password to make)
    Returns a list of what it found."""
    now = now or time.time()
    settings = store.load()
    child = settings.get("child_account", "")
    sub = store.load_subscription()
    guard0 = store.load_guard()
    stale = not sub or time.time() - _iso_to_ts(sub.get("checked", "")) > SUBSCRIPTION_REFRESH_SECONDS
    urgent = subscription_status(sub)["level"] in ("critical", "expired")
    wait = 300 if urgent else SUBSCRIPTION_REFRESH_SECONDS   # near/after expiry: every 5 minutes
    if (stale or urgent) and time.time() - guard0.get("sub_attempt", 0) > wait:
        guard0["sub_attempt"] = time.time()     # try at most once an hour, even when offline
        try:
            store.save_guard(guard0)
        except OSError:
            pass
        sub = store.refresh_subscription()
    protecting = settings["enabled"] and bool(child) and not subscription_expired(sub)
    note = apply_child_browser_settings(store, settings, child, protecting)
    if subscription_expired(sub):
        return ["The subscription has expired: protection is off."] + note
    if not settings["enabled"] or not child:
        return ["Falcon is off or no child account is chosen."] + note
    found, tamper = [], []
    guard = store.load_guard()
    state = store.load_state()
    usage = store.load_usage()
    today = datetime.now().date().isoformat()

    # coupon redemptions: keep a protected copy; put back any that were deleted
    known = {(r["code"], r["time"]) for r in guard["redemptions"]}
    current = {(r["code"], r["time"]) for r in state["redemptions"]}
    guard["redemptions"] = [{"code": c, "time": t} for c, t in sorted(known | current)]
    if known - current:
        state["redemptions"] = list(guard["redemptions"])
        tamper.append("coupon records were deleted (put back)")

    # time grants from the time's up screen must carry the supervisor's signature
    key = store.grant_key()
    if key is not None:
        good = [g for g in state["grants"] if grant_is_valid(key, g)]
        if len(good) != len(state["grants"]):
            tamper.append(f"{len(state['grants']) - len(good)} forged extra time grant(s) removed")
            state["grants"] = good
        ok_allows = [a for a in state["allows"] if allow_is_valid(key, a)]
        if len(ok_allows) != len(state["allows"]):
            tamper.append(f"{len(state['allows']) - len(ok_allows)} forged website allowance(s) removed")
            state["allows"] = ok_allows

    # screen time can only go up during a day
    gu = guard.get("usage", {})
    if gu.get("date") == today and usage["seconds"] + 5 < gu.get("seconds", 0):
        usage["seconds"] = gu["seconds"]
        tamper.append("screen time used today was wound back (put back)")
        try:
            store._write_in_place(store.usage_path, usage)
        except OSError:
            pass
    guard["usage"] = {"date": today, "seconds": max(usage["seconds"],
                                                    gu.get("seconds", 0) if gu.get("date") == today else 0)}

    if tamper:
        try:
            store.save_state(state)
        except OSError:
            pass

    # is Falcon running on the child's account?
    sid = find_session(child)
    if sid is not None:
        if now - usage.get("heartbeat", 0) > MONITOR_TIMEOUT:
            guard["missing"] = guard.get("missing", 0) + 1
            if guard["missing"] >= 2:          # two checks in a row: not just starting up
                found.append(f"Falcon wasn't running on {child}'s account: session locked")
                lock_session(sid)
        else:
            guard["missing"] = 0
    else:
        guard["missing"] = 0                   # child not logged on: start counting afresh
    for t in tamper:
        found.append(f"Tampering found on {child}'s account: {t}")
    if tamper and sid is not None:
        lock_session(sid)
    for f in found:
        store.log(f)
    try:
        store.save_guard(guard)
    except OSError:
        pass
    return found


def _iso_to_ts(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return 0.0


SID_ADMINS, SID_SYSTEM, SID_USERS = "*S-1-5-32-544", "*S-1-5-18", "*S-1-5-32-545"


def protect_commands(folder):
    """icacls commands that lock down Falcon's folder (run as administrator):
    settings are read-only for everyone but admins/SYSTEM; the child's account may only
    write its own usage, coupon/grant records and the log; secret.json is hidden."""
    f = str(folder)
    cmds = [["icacls", f, "/inheritance:r", "/grant:r", f"{SID_ADMINS}:(OI)(CI)F",
             f"{SID_SYSTEM}:(OI)(CI)F", f"{SID_USERS}:(OI)(CI)RX"]]
    for name in ("usage.json", "state.json", "activity.log"):
        cmds.append(["icacls", str(Path(f) / name), "/grant", f"{SID_USERS}:M"])
    cmds.append(["icacls", str(Path(f) / "secret.json"), "/inheritance:r", "/grant:r",
                 f"{SID_ADMINS}:F", f"{SID_SYSTEM}:F"])
    return cmds


def protect(store):
    """--protect: create every file, then set the permissions. Needs administrator rights."""
    store.grant_key(create=True)
    for path, empty in ((store.settings_path, None), (store.usage_path, {}), (store.state_path, {}),
                        (store.guard_path, {}), (store.log_path, "")):
        if not path.exists():
            if empty is None:
                store.save(default_settings())
            elif empty == "":
                path.write_text("", encoding="utf-8")
            else:
                path.write_text(json.dumps(empty), encoding="utf-8")
    results = []
    for cmd in protect_commands(store.folder):
        if IS_WINDOWS:
            r = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            results.append((" ".join(cmd), r.returncode == 0, (r.stdout + r.stderr).strip()))
        else:
            results.append((" ".join(cmd), False, "only works on Windows"))
    return results


# --- Install / uninstall (called by build.bat and uninstall.bat) ---------------------
INSTALL_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "FalconGuard"
START_MENU = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / \
    "Start Menu" / "Programs" / "FalconGuard"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\FalconGuard"
FIREWALL_RULE = "FalconGuard Web Dashboard"
TASKS = ("Guard", "Monitor", "Web")


def run_quiet(cmd, timeout=60):
    """Run a Windows command without a console window. Returns (ok, output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           creationflags=CREATE_NO_WINDOW, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def task_xml(kind, exe, child=""):
    """A plain Task Scheduler definition (no scripts) for FalconGuard's three tasks."""
    from xml.sax.saxutils import escape
    exe, child = escape(str(exe)), escape(child)
    if kind == "Guard":       # every minute as SYSTEM
        triggers = ("<TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false"
                    "</StopAtDurationEnd></Repetition><StartBoundary>2026-01-01T00:00:00"
                    "</StartBoundary><Enabled>true</Enabled></TimeTrigger>")
        principal = "<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>"
        limit, args, restart = "PT2M", "--guard", ""
    elif kind == "Web":       # from start-up as SYSTEM, restarted if it stops
        triggers = "<BootTrigger><Enabled>true</Enabled></BootTrigger>"
        principal = "<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>"
        limit, args = "PT0S", "--web"
        restart = "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>"
    else:                     # Monitor: on the child's account at logon, unlock and reconnect
        changes = "".join(f"<SessionStateChangeTrigger><Enabled>true</Enabled><StateChange>{c}"
                          f"</StateChange><UserId>{child}</UserId></SessionStateChangeTrigger>"
                          for c in ("SessionUnlock", "ConsoleConnect", "RemoteConnect"))
        triggers = f"<LogonTrigger><Enabled>true</Enabled><UserId>{child}</UserId></LogonTrigger>" + changes
        principal = (f"<UserId>{child}</UserId><LogonType>InteractiveToken</LogonType>"
                     "<RunLevel>LeastPrivilege</RunLevel>")
        limit, args, restart = "PT0S", "--monitor", ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        f"<RegistrationInfo><Description>FalconGuard {kind}</Description></RegistrationInfo>"
        f"<Triggers>{triggers}</Triggers>"
        f'<Principals><Principal id="Author">{principal}</Principal></Principals>'
        "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        f"<StartWhenAvailable>true</StartWhenAvailable>{restart}"
        f"<ExecutionTimeLimit>{limit}</ExecutionTimeLimit><Enabled>true</Enabled></Settings>"
        f'<Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>{args}</Arguments>'
        "</Exec></Actions></Task>")


def make_shortcut(path, target, args=""):
    """A Start menu shortcut (Windows Script Host, called plainly)."""
    q = lambda v: str(v).replace("'", "''")
    script = (f"$l=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(path)}');"
              f"$l.TargetPath='{q(target)}';$l.Arguments='{q(args)}';"
              f"$l.WorkingDirectory='{q(Path(target).parent)}';$l.Save()")
    return run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def install_system(store, child, exe=None):
    """--setup CHILD (build.bat, as administrator): everything after copying the program.
    Returns [(step, ok, detail)]."""
    import tempfile
    import winreg
    exe = Path(exe or sys.executable)
    out = []
    s = store.load()
    s["child_account"] = child[:100]
    store.save(s)
    store.log(f"Child's account set to {child} by setup")
    out.append(("Child's account saved", True, child))
    results = protect(store)
    out.append(("Settings folder protected", all(ok for _, ok, _ in results),
                "; ".join(d for _, ok, d in results if not ok)))
    for kind in TASKS:
        x = Path(tempfile.gettempdir()) / f"falconguard-{kind}.xml"
        x.write_text(task_xml(kind, exe, child), encoding="utf-16")
        ok, msg = run_quiet(["schtasks", "/Create", "/TN", f"FalconGuard\\{kind}", "/XML", str(x), "/F"])
        x.unlink()
        out.append((f"Scheduled task: {kind}", ok, "" if ok else msg))
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "FalconGuard", 0, winreg.REG_SZ, f'"{exe}" --monitor')
        out.append(("Starts at every logon", True, ""))
    except OSError as e:
        out.append(("Starts at every logon", False, str(e)))
    run_quiet(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
    ok, msg = run_quiet(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}",
                         "dir=in", "action=allow", "protocol=TCP", f"localport={WEB_PORT}",
                         "profile=private,domain", f"program={exe}"])
    out.append((f"Firewall: port {WEB_PORT} on private networks", ok, "" if ok else msg))
    START_MENU.mkdir(parents=True, exist_ok=True)
    made = [make_shortcut(START_MENU / f"{name}.lnk", exe, args)[0]
            for name, args in (("FalconGuard Settings", ""), ("FalconGuard Dashboard", "--dashboard"),
                               ("FalconGuard Self-Test", "--selftest"),
                               ("Uninstall FalconGuard", "--uninstall"))]
    out.append(("Start menu shortcuts", all(made), ""))
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE) as k:
            for name, value in (("DisplayName", "FalconGuard"), ("DisplayVersion", str(VERSION)),
                                ("Publisher", "FalconGuard"), ("DisplayIcon", str(exe)),
                                ("InstallLocation", str(exe.parent)),
                                ("UninstallString", f'"{exe}" --uninstall')):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
            for name in ("NoModify", "NoRepair"):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)
        out.append(("Listed in Settings > Apps", True, ""))
    except OSError as e:
        out.append(("Listed in Settings > Apps", False, str(e)))
    for kind in ("Web", "Monitor"):     # the monitor only starts if the child is signed in
        run_quiet(["schtasks", "/Run", "/TN", f"FalconGuard\\{kind}"])
    return out


def network_addresses():
    """This PC's addresses for the web dashboard, and whether the network is set to Public."""
    import socket
    try:
        ips = sorted({ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                      if not ip.startswith(("127.", "169.254."))})
    except OSError:
        ips = []
    ok, cat = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "(Get-NetConnectionProfile).NetworkCategory"])
    return ips, "Public" in cat


def cleanup_policies_all_users():
    """Remove the browser settings FalconGuard wrote on every account (and Firefox's)."""
    import winreg
    names = POLICY_NAMES
    profiles = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList") as pl:
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(pl, i)
                except OSError:
                    break
                i += 1
                if len(sid) < 15:              # skip SYSTEM, LOCAL SERVICE, NETWORK SERVICE
                    continue
                with winreg.OpenKey(pl, sid) as k:
                    path = os.path.expandvars(winreg.QueryValueEx(k, "ProfileImagePath")[0])
                profiles.append((sid, Path(path)))
    except OSError:
        pass

    def clean(root):
        for key in POLICY_KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_USERS, f"{root}\\{key}", 0, winreg.KEY_ALL_ACCESS) as k:
                    for n in names:
                        try:
                            winreg.DeleteValue(k, n)
                        except OSError:
                            pass
                winreg.DeleteKey(winreg.HKEY_USERS, f"{root}\\{key}\\URLBlocklist")
            except OSError:
                pass
        try:
            winreg.DeleteKey(winreg.HKEY_USERS, f"{root}\\Software\\Policies\\Mozilla\\Firefox\\DNSOverHTTPS")
        except OSError:
            pass

    for sid, path in profiles:
        try:
            winreg.OpenKey(winreg.HKEY_USERS, sid).Close()
            clean(sid)                          # account signed in: its settings are loaded
        except OSError:
            hive = path / "NTUSER.DAT"
            if hive.exists() and run_quiet(["reg", "load", r"HKU\FalconTmp", str(hive)])[0]:
                clean("FalconTmp")
                run_quiet(["reg", "unload", r"HKU\FalconTmp"])
        ff = path / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
        if ff.is_dir():
            apply_firefox_prefs(False, [p for p in ff.iterdir() if p.is_dir()])


def delete_later(path):
    """Delete a file or empty folder at the next restart (it's in use now)."""
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.MoveFileExW(str(path), None, 4))   # DELAY_UNTIL_REBOOT
    except Exception:
        return False


def uninstall_system(store, wipe=False):
    """--uninstall (as administrator): remove FalconGuard. Returns ([(step, ok, detail)], restart needed)."""
    import winreg
    out = []
    run_quiet(["schtasks", "/End", "/TN", "FalconGuard\\Web"])
    run_quiet(["taskkill", "/F", "/IM", "FalconGuard.exe", "/FI", f"PID ne {os.getpid()}"])
    for kind in TASKS:
        run_quiet(["schtasks", "/Delete", "/TN", f"FalconGuard\\{kind}", "/F"])
    out.append(("Scheduled tasks removed", True, ""))
    for key, value in ((RUN_KEY, "FalconGuard"),):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, value)
        except OSError:
            pass
    out.append(("Start-up entry removed", True, ""))
    run_quiet(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
    out.append(("Firewall rule removed", True, ""))
    cleanup_policies_all_users()
    out.append(("Browser settings removed on every account", True, ""))
    shutil.rmtree(START_MENU, ignore_errors=True)
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
    except OSError:
        pass
    out.append(("Start menu and Apps entry removed", True, ""))
    if store.folder.exists():
        run_quiet(["icacls", str(store.folder), "/reset", "/T", "/C", "/Q"])
        if wipe:
            shutil.rmtree(store.folder, ignore_errors=True)
    out.append(("Settings and log deleted" if wipe else "Settings and log kept (unlocked)", True,
                str(store.folder)))
    restart = False
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        if INSTALL_DIR.exists():               # this program is still running from there
            for f in sorted(INSTALL_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                delete_later(f)
            delete_later(INSTALL_DIR)
            restart = True
    out.append(("Program removed" + (" (the rest goes when the PC restarts)" if restart else ""),
                True, str(INSTALL_DIR)))
    return out, restart


def show_uninstall(store):
    """The uninstall window: confirm, optionally delete settings, then remove everything."""
    root = tk.Tk()
    root.withdraw()
    setup_fonts(root)
    d = Dialog(root, "Uninstall FalconGuard")
    row = tk.Frame(d.body, bg=FACE)
    row.pack(fill="x")
    icon(row, "falcon", 32).pack(side="left", anchor="n")
    label(row, "This removes FalconGuard from this PC: screen time limits, the web\n"
               "filter, the web dashboard, its scheduled tasks and its browser settings.").pack(
        side="left", padx=12)
    wipe = CheckBox(d.body, "Also delete FalconGuard's settings and activity log")
    wipe.pack(fill="x", pady=(12, 0))
    d.button_row([("Uninstall", lambda: d.finish("go"), True), ("Cancel", d.cancel, False)])
    if d.show() != "go":
        root.destroy()
        return
    results, restart = uninstall_system(store, wipe.value)
    failed = [f"{step}: {detail}" for step, ok, detail in results if not ok]
    text = "FalconGuard was removed." + (" Restart the PC to finish removing its files."
                                         if restart else "")
    if failed:
        text += "\n\nSome steps didn't work:\n" + "\n".join(failed)
    message(root, "Uninstall FalconGuard", text, "warning" if failed else "info")
    root.destroy()


def settings_writable(store):
    """Can this process save the settings? (After build.bat, only administrators can.)
    Opening for append changes nothing in the file."""
    try:
        if store.settings_path.exists():
            with open(store.settings_path, "a", encoding="utf-8"):
                pass
        else:
            t = store.folder / "write-test.tmp"
            t.write_text("x", encoding="utf-8")
            t.unlink()
        return True
    except OSError:
        return False


def relaunch_as_admin(extra=()):
    """Ask Windows (UAC) to start Falcon again with administrator rights."""
    if not IS_WINDOWS:
        return False
    import ctypes
    args = [] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]
    params = " ".join(f'"{a}"' for a in args + list(extra))
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1) > 32


HANDOFF_MAX_AGE = 15 * 60


def write_handoff(settings):
    """Unsaved settings for the administrator copy of Falcon to save (see --resume).
    The supervisor password is only ever in it as its hash."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="falconguard-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"settings": settings, "written": time.time()}, f)
    return path


def read_handoff(path):
    """The settings from a hand-over file (deleted after reading), or None if missing/stale."""
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        p.unlink()
        if time.time() - float(data.get("written", 0)) > HANDOFF_MAX_AGE:
            return None
        return sanitize(data["settings"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def self_test(store, try_lock=False):
    """--selftest: check each Windows feature on this PC. [(check, ok, detail)]"""
    out = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    add("Running on Windows", IS_WINDOWS, sys.platform)
    settings = store.load()
    on_child = settings.get("child_account", "").lower() == current_user().lower()
    usage = store.load_usage()
    age = time.time() - usage.get("heartbeat", 0)
    diag = usage.get("diag", {})
    running = age < MONITOR_TIMEOUT
    add("FalconGuard is running on the child's account", running,
        f"last heard from {age:.0f} seconds ago" if running else
        f"not running (sign in to {settings.get('child_account') or 'the child'}'s account)")
    if running:
        if diag.get("protecting"):
            add("Protection is on", True, f"Web Filter level {LEVEL_NAMES[diag.get('level', 1)]}, "
                f"subscription until {diag.get('sub_expires') or 'unknown'}")
        else:
            why = ("Falcon is turned off" if not diag.get("enabled") else
                   f"the subscription it has saved ends {diag.get('sub_expires')}" if diag.get("expired")
                   else "not started yet")
            add("Protection is on", False, why)
        cycle_age = time.time() - diag.get("cycle", 0)
        add("Website filter is checking", cycle_age < 30 and diag.get("active") is not False,
            f"last check {cycle_age:.0f} s ago, screen active: {diag.get('active')}, "
            f"{diag.get('names', 0)} sites in Windows' list ({diag.get('with_addresses', 0)} with addresses), "
            f"{diag.get('connections', 'no')} browser connections"
            + (f" ({diag['unmatched']} to addresses not in Windows' list)" if diag.get("unmatched") else "")
            + (f", each check takes {diag['check_ms']} ms" if "check_ms" in diag else "")
            + (f", last blocked: {diag['last_block']}" if diag.get("last_block") else "")
            + (f", problem: {diag['problem']}" if diag.get("problem") else ""))
    try:
        t = store.folder / "selftest.tmp"
        t.write_text("x", encoding="utf-8")
        t.unlink()
        writable = True
    except OSError:
        writable = False
    if on_child:
        add("Settings are protected from this account", not writable,
            "good: the child can't change them" if not writable else
            "the child's account can change the settings: run build.bat again")
    else:
        add("Settings folder can be written", writable,
            str(store.folder) + ("" if writable else " (run as administrator to change settings)"))
    acct = settings.get("child_account") or "(not chosen)"
    add("Child's account chosen", settings.get("child_account"), f"child: {acct}, this account: {current_user()}")
    add("This session is detected as on screen", session_active())
    idle = idle_seconds()
    add("Mouse/keyboard activity is detected", idle < IDLE_LIMIT_SECONDS, f"idle for {idle:.0f} seconds")
    notify("Falcon Advisory", "Falcon self-test: if you can see this, notifications work")
    add("Notification sent", True, "check that 'Falcon Advisory' appeared in the corner")
    try:
        import socket
        socket.gethostbyname("example.com")
    except OSError:
        pass
    try:
        names = dns_cache_names()
    except Exception:
        names = set()
    add("Website lookups can be read (DNS cache)", "example.com" in names, f"{len(names)} names in the cache")
    cf = (cloudflare_says_blocked("pornhub.com"), cloudflare_says_blocked("example.com"))
    add("Cloudflare for Families answers", cf == (True, False), f"adult site blocked={cf[0]}, "
        f"example.com blocked={cf[1]}" + (f" - {CLOUDFLARE_ERROR}" if CLOUDFLARE_ERROR else ""))
    if IS_WINDOWS:
        try:
            tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                creationflags=CREATE_NO_WINDOW, timeout=15).stdout.lower()
            running = sorted({b for b in BROWSERS if f'"{b}"' in tl})
            add("Browsers found", True, ", ".join(running) or "none open right now")
        except Exception as e:
            add("Browsers found", False, str(e))
        if settings.get("child_account", "").lower() == current_user().lower():
            missing = policy_problems(settings)
            add("Browsers must use Windows' lookups (policies)", not missing,
                "Chrome and Edge settings are in place (check edge://policy if unsure)" if not missing
                else "not set: " + ", ".join(missing) + ". The guard writes these within a minute of "
                "signing in; if they stay missing, run build.bat again as administrator")
        conns = browser_connections()
        add("Browser connections can be read", True, f"{len(conns)} connection(s)"
            + ("" if conns else " (open a website in a browser and run the test again)"))
        if conns:
            try:
                entries = dns_cache_entries()
            except Exception:
                entries = {}
            known_ips = {ip for ips in entries.values() for ip in ips}
            matched = sorted({h for h, ips in entries.items() if ips & conns})
            add("Browser connections matched to websites (Restricted level)",
                bool(conns & known_ips), f"{len(conns & known_ips)} of {len(conns)} matched"
                + (f": {', '.join(matched[:5])}" if matched else
                   " - the browser may still be using its own lookups; restart it"))
        sessions = wts_sessions()
        add("Windows sessions can be listed (guard)", bool(sessions),
            ", ".join(f"{u} (#{i})" for i, u in sessions))
    add("Firefox profiles found", True, f"{len(firefox_profiles())}")
    # end to end: look up a known adult site the way a browser would, then check that the
    # name shows up in Windows' list and that Falcon would block it
    # Blocked sites are recognised: asked straight from Cloudflare, never looked up through
    # Windows, so the test itself doesn't make the website filter pop up afterwards.
    s2 = dict(settings, web_filter_level=1)
    blocked = bool(SiteChecker().reasons(CANARY_HOST, s2))
    allowed = not SiteChecker().reasons("example.com", s2)
    try:
        entries = dns_cache_entries()
    except Exception:
        entries = {}
    has_ips = bool(entries.get("example.com"))
    add("Blocked sites are recognised", blocked and allowed,
        f"adult site blocked: {'yes' if blocked else 'no'}, example.com allowed: "
        f"{'yes' if allowed else 'no'}")
    add("Website addresses can be read (for matching visits)", has_ips,
        f"example.com: {', '.join(sorted(entries.get('example.com', set()))) or 'no addresses found'}")
    if IS_WINDOWS:
        try:
            import winreg
            found = {}
            for key, name in ((POLICY_KEYS[0], "Chrome"), (POLICY_KEYS[1], "Edge")):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                        found[name] = winreg.QueryValueEx(k, "BuiltInDnsClientEnabled")[0] == 0
                except OSError:
                    found[name] = False
            add("Browser settings in place on this account", all(found.values()),
                ", ".join(f"{n}: {'yes' if ok else 'no'}" for n, ok in found.items()) +
                ("" if all(found.values()) else " (FalconGuard's guard sets these within a minute "
                 "of the child signing in while Falcon is on; it then restarts the browsers)"))
        except Exception as e:
            add("Browser settings in place on this account", False, str(e))
    key_readable = store.grant_key() is not None
    if on_child:
        add("Secret key hidden from this account", not key_readable,
            "good" if not key_readable else "the child can read it: run build.bat again")
    else:
        add("Secret key readable (supervisor)", key_readable)
    if try_lock:
        lock_workstation()
        add("Computer locked (Windows+L)", True, "it should have locked just now")
    return out


# --- Web dashboard (port 7675, runs as SYSTEM, started by build.bat) -------------------
WEB_PORT = 7675
WEB_SESSION_SECONDS = 30 * 60
WEB_LOCKOUT_SECONDS = 5 * 60
EDITABLE = ("web_filter_level", "sites", "options", "blocked_hours", "daily_limits", "zones",
            "child_account")


class ServiceClient:
    """Lets the FalconGuard window save through the background service (running as SYSTEM)
    when its own account isn't allowed to write the protected settings. Every request
    needs the supervisor password."""

    def __init__(self, password):
        self.password = password
        self.token = ""

    def _call(self, path, body):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}{path}",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "X-Falcon-Token": self.token})
        try:
            with opener.open(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (OSError, ValueError):
            raise OSError("FalconGuard's background service isn't running, so the change "
                          "couldn't be saved. Restart the PC, or run FalconGuard Settings as "
                          "administrator.")

    def request(self, path, body):
        for _ in range(2):
            if not self.token:
                status, data = self._call("/api/login", {"password": self.password})
                if status != 200:
                    raise OSError(data.get("error") or "The background service refused the password.")
                self.token = data["token"]
            status, data = self._call(path, body)
            if status == 401:            # session ended: sign in again once
                self.token = ""
                continue
            if status != 200:
                raise OSError(data.get("error") or f"The background service said no ({status}).")
            return data
        raise OSError("The background service refused the password.")


class WebDashboard:
    """The FalconGuard window as a web page for parents on the same network. Every change
    goes through the same checks, files and activity log as the window."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.sessions = {}        # token -> expires at
        self.failures = {}        # ip -> (wrong passwords in a row, locked until)
        self.page = (SOUNDS / "web.html").read_bytes()

    # --- helpers ---------------------------------------------------------------------
    def client_allowed(self, ip):
        """Only this PC and devices on the home network."""
        import ipaddress
        try:
            a = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return a.is_private or a.is_loopback or a.is_link_local

    def signed_in(self, token):
        exp = self.sessions.get(token or "")
        if not exp or exp < time.time():
            self.sessions.pop(token or "", None)
            return False
        self.sessions[token] = time.time() + WEB_SESSION_SECONDS
        return True

    def subscription(self):
        return self.store.load_subscription()

    def dashboard_data(self, s):
        used = self.store.used_seconds_today()
        t = screen_time(s, used_seconds=used, bonus=bonus_today(s, self.store.load_state(),
                                                                self.store.load_guard()))
        redeemed = redemption_times(s, self.store.load_state(), self.store.load_guard())
        coupons = [dict(c, redeemed=redeemed.get(c["code"])) for c in s["coupons"]]
        return {"time": t, "child_account": s.get("child_account", ""), "coupons": coupons}

    def full_state(self):
        s = self.store.load()
        sub = self.subscription()
        return {
            "settings": {k: s[k] for k in EDITABLE},
            "enabled": s["enabled"],
            "has_password": bool(s["password"]),
            "hint": s["hint"],
            "dashboard": self.dashboard_data(s),
            "subscription": sub,
            "sub_status": subscription_status(sub),
            "price": fmt_price(sub),
            "days_left": fmt_days_left(subscription_status(sub)["days"]),
            "version": VERSION,
            "folder": str(self.store.folder),
            "accounts": [u for _, u in wts_sessions()] or [current_user()],
            "monitor": dict(zip(("running", "text"), monitor_status(self.store, s))),
            "constants": {
                "levels": LEVEL_NAMES, "level_text": CATEGORIES["Web Filter"],
                "protection": PROTECTION, "zones": ZONES, "default_zone": DEFAULT_ZONE_LEVEL,
                "days": DAYS, "limit_values": LIMIT_VALUES, "coupon_values": COUPON_VALUES,
                "options": OPTIONS, "lock_seconds": LOCK_EVERY_SECONDS},
        }

    def log(self, text, ip):
        self.store.log(f"{text} (web, {ip})")

    # --- requests --------------------------------------------------------------------
    def handle(self, method, path, body, token, ip):
        """Returns (status, payload dict)."""
        if path == "/api/hello":
            s = self.store.load()
            return 200, {"has_password": bool(s["password"]), "hint": s["hint"], "version": VERSION}
        if path == "/api/public":                          # the child's Dashboard (no password)
            s = self.store.load()
            return 200, {"dashboard": self.dashboard_data(s),
                         "constants": {"days": DAYS}, "version": VERSION}
        if path == "/api/redeem" and method == "POST":
            with self.lock:
                s = self.store.load()
                try:
                    result, code = redeem_coupon(self.store, s, str(body.get("code", "")))
                except OSError as e:
                    return 500, {"error": f"Falcon could not save the coupon. {e}"}
                if result == "missing":
                    self.log("Wrong coupon code entered", ip)
                elif result == "used":
                    self.log(f"Used coupon {code} entered again", ip)
                else:
                    self.log(f"Coupon {code} redeemed: +{fmt_minutes(result)} screen time today", ip)
                return 200, {"result": result, "dashboard": self.dashboard_data(self.store.load())}
        if path == "/api/login" and method == "POST":
            return self.login(str(body.get("password", "")), ip)
        if path == "/api/logout" and method == "POST":
            self.sessions.pop(token or "", None)
            return 200, {}
        if not self.signed_in(token):
            return 401, {"error": "Sign in first."}

        with self.lock:
            if path == "/api/state":
                return 200, self.full_state()
            if method != "POST":
                return 404, {"error": "Not found"}
            if path == "/api/apply":
                return self.apply(body.get("settings") or {}, ip)
            if path == "/api/enabled":
                return self.set_enabled(bool(body.get("on")), ip)
            if path == "/api/settings/save":
                return self.save_settings(body.get("settings"), ip)
            if path == "/api/verify":
                ok = check_password(str(body.get("password", "")), self.store.load()["password"])
                if not ok:
                    self.log("Wrong supervisor password entered (coupons)", ip)
                return 200, {"ok": ok}
            if path == "/api/password":
                return self.change_password(body, ip)
            if path in ("/api/coupons/create", "/api/coupons/delete"):
                s = self.store.load()
                if not check_password(str(body.get("password", "")), s["password"]):
                    self.log("Wrong supervisor password entered (coupons)", ip)
                    return 403, {"error": "The password is incorrect."}
                if path.endswith("create"):
                    minutes = body.get("minutes")
                    if minutes not in COUPON_VALUES:
                        return 400, {"error": "Choose an amount of extra time."}
                    code = add_coupon(s, minutes, str(body.get("note", "")).strip())
                    self.store.save(s)
                    self.log(f"Coupon {code} created (+{fmt_minutes(minutes)})", ip)
                    return 200, {"code": code, "state": self.full_state()}
                code = str(body.get("code", ""))
                remove_coupon(s, code)
                self.store.save(s)
                self.log(f"Coupon {code} deleted", ip)
                return 200, {"state": self.full_state()}
            if path == "/api/extra":
                minutes = body.get("minutes")
                if minutes not in COUPON_VALUES:
                    return 400, {"error": "Choose an amount of extra time."}
                s = self.store.load()
                add_extra(s, minutes)
                self.store.save(s)
                self.log(f"Extra time given: +{fmt_minutes(minutes)} today", ip)
                return 200, {"state": self.full_state()}
            if path == "/api/extra/remove":
                s = self.store.load()
                reset_extra(s)
                self.store.save(s)
                self.log("Extra time for today removed", ip)
                return 200, {"state": self.full_state()}
            if path == "/api/log":
                return 200, {"entries": self.store.read_log()[::-1]}
            if path == "/api/log/clear":
                self.store.clear_log()
                self.log("Activity log cleared", ip)
                return 200, {"entries": self.store.read_log()[::-1]}
            if path == "/api/subscription/check":
                sub = fetch_subscription()
                if sub:
                    self.store.save_subscription(sub)
                return 200, {"reached": bool(sub), "state": self.full_state()}
        return 404, {"error": "Not found"}

    def login(self, password, ip):
        fails, until = self.failures.get(ip, (0, 0))
        if until > time.time():
            mins = int((until - time.time()) // 60) + 1
            return 429, {"error": f"The password was typed incorrectly {FAILED_ATTEMPTS_ALLOWED} "
                                  f"times. Try again in {mins} minute{'s' if mins != 1 else ''}."}
        s = self.store.load()
        if not s["password"]:
            return 403, {"error": "No supervisor password has been set up. Create one in "
                                  "FalconGuard on the PC first."}
        if not check_password(password, s["password"]):
            fails += 1
            self.log("Wrong supervisor password entered", ip)
            if fails >= FAILED_ATTEMPTS_ALLOWED:
                self.failures[ip] = (0, time.time() + WEB_LOCKOUT_SECONDS)
                self.log(f"Web sign-in locked for {WEB_LOCKOUT_SECONDS // 60} minutes after "
                         f"{FAILED_ATTEMPTS_ALLOWED} wrong passwords", ip)
                return 429, {"error": f"The password was typed incorrectly {FAILED_ATTEMPTS_ALLOWED} "
                                      f"times. Try again in {WEB_LOCKOUT_SECONDS // 60} minutes."}
            self.failures[ip] = (fails, 0)
            return 403, {"error": "The password is incorrect. Check that Caps Lock is off, and "
                                  "then try again."}
        self.failures.pop(ip, None)
        if "grant_wrap" not in s:                # set up the grant key after an upgrade
            key = self.store.grant_key(create=True)
            if key:
                s["grant_wrap"] = wrap_grant_key(key, password)
                self.store.save(s)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + WEB_SESSION_SECONDS
        self.log("Supervisor signed in", ip)
        return 200, {"token": token}

    def apply(self, incoming, ip):
        cur = self.store.load()
        merged = dict(cur)
        for k in EDITABLE:
            if k in incoming:
                merged[k] = incoming[k]
        new = sanitize(merged)                 # same checks as loading a settings file
        changes = describe_changes(cur, new)
        if changes:
            self.store.save(new)
            for c in changes:
                self.log(c, ip)
        return 200, {"state": self.full_state()}

    def save_settings(self, incoming, ip):
        """The FalconGuard window on an account that can't write the protected settings
        sends them here after the supervisor password was typed. The password, hint and
        grant key can only be changed through /api/password. The window logs its own
        changes, so nothing is logged twice."""
        if not isinstance(incoming, dict):
            return 400, {"error": "Bad request"}
        cur = self.store.load()
        merged = dict(incoming)
        merged["password"], merged["hint"] = cur["password"], cur["hint"]
        merged.pop("grant_wrap", None)
        if "grant_wrap" in cur:
            merged["grant_wrap"] = cur["grant_wrap"]
        self.store.save(sanitize(merged))
        return 200, {}

    def set_enabled(self, on, ip):
        s = self.store.load()
        if on and subscription_expired(self.subscription()):
            return 403, {"error": "Your Falcon subscription has expired, so protection can't be "
                                  "turned on. Renew your subscription, then click Check Now on "
                                  "the My Account tab."}
        s["enabled"] = on
        self.store.save(s)
        self.log("Falcon turned on" if on else "Falcon turned off", ip)
        return 200, {"state": self.full_state()}

    def change_password(self, body, ip):
        s = self.store.load()
        if not check_password(str(body.get("old", "")), s["password"]):
            return 403, {"error": "The old password is incorrect.", "field": "old"}
        new, confirm = str(body.get("new", "")), str(body.get("confirm", ""))
        hint = str(body.get("hint", "")).strip()
        problem = password_problem(new, confirm, hint)
        if problem:
            return 400, {"error": problem[0], "field": problem[1]}
        s["password"], s["hint"] = hash_password(new), hint[:100]
        key = self.store.grant_key(create=True)
        if key:
            s["grant_wrap"] = wrap_grant_key(key, new)
        self.store.save(s)
        self.log("Supervisor password changed", ip)
        return 200, {}

    # --- server ----------------------------------------------------------------------
    def serve(self, port=WEB_PORT, host="0.0.0.0"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        dash = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "FalconGuard"
            sys_version = ""

            def log_message(self, *a):
                pass

            def _send(self, status, data, ctype="application/json; charset=utf-8"):
                body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                ip = self.client_address[0]
                if not dash.client_allowed(ip):
                    return self._send(403, {"error": "Only devices on this network can use "
                                                     "the FalconGuard dashboard."})
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(200, dash.page, "text/html; charset=utf-8")
                if path.startswith("/sounds/") and path[8:] in ("error.mp3", "logon.mp3", "logoff.mp3"):
                    f = SOUNDS / path[8:]
                    return self._send(200, f.read_bytes() if f.exists() else b"", "audio/mpeg")
                if not path.startswith("/api/"):
                    return self._send(404, {"error": "Not found"})
                body = {}
                if method == "POST":
                    try:
                        n = min(int(self.headers.get("Content-Length", 0)), 200_000)
                        body = json.loads(self.rfile.read(n) or b"{}")
                        if not isinstance(body, dict):
                            body = {}
                    except (ValueError, OSError):
                        return self._send(400, {"error": "Bad request"})
                try:
                    status, payload = dash.handle(method, path, body,
                                                  self.headers.get("X-Falcon-Token"), ip)
                except PermissionError:
                    status, payload = 500, {"error": "FalconGuard isn't allowed to save its "
                                                     "settings. Is the web dashboard running as "
                                                     "SYSTEM?"}
                except Exception as e:      # never take the server down
                    dash.store.log(f"Web dashboard error: {type(e).__name__}: {e}")
                    status, payload = 500, {"error": f"Something went wrong: {e}"}
                self._send(status, payload)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        self.store.log(f"Web dashboard started on port {port}")
        server.serve_forever()


# --- Falcon icon (.ico) and the notification-area (tray) icon -----------------------
FALCON_BODY = [(1, 9), (6, 4), (10, 5), (15, 2), (13, 7), (9, 9), (8, 14), (6, 11), (1, 10)]
FALCON_BEAK = [(10, 5), (12.5, 4.2), (11, 6.2)]


def _inside(x, y, poly):
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def _near_edge(x, y, poly, width):
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        if (x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2 < width * width:
            return True
    return False


def falcon_pixels(size, ss=4):
    """The falcon from the title bar, drawn at size x size with smooth edges. [(r,g,b,a)]"""
    body, line, beak = (200, 134, 46), (90, 50, 8), (244, 208, 112)
    scale = size / 16.0
    out = []
    for py in range(size):
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) / scale
                    y = (py + (sy + 0.5) / ss) / scale
                    if (x - 11.1) ** 2 + (y - 4.7) ** 2 < 0.36:
                        c = (0, 0, 0)
                    elif _inside(x, y, FALCON_BEAK):
                        c = beak
                    elif _near_edge(x, y, FALCON_BODY, 0.55 if size <= 16 else 0.4):
                        c = line
                    elif _inside(x, y, FALCON_BODY):
                        c = body
                    else:
                        continue
                    acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 1
            n = acc[3]
            out.append((round(acc[0] / n), round(acc[1] / n), round(acc[2] / n),
                        round(255 * n / (ss * ss))) if n else (0, 0, 0, 0))
    return out


def write_falcon_icon(path, sizes=(16, 24, 32, 48)):
    """Write a Windows .ico with the falcon in several sizes (32-bit, with transparency)."""
    import struct
    images = []
    for size in sizes:
        px = falcon_pixels(size)
        rows = [px[y * size:(y + 1) * size] for y in range(size)][::-1]      # bottom-up
        xor = b"".join(bytes((b, g, r, a)) for row in rows for (r, g, b, a) in row)
        mask_row = ((size + 31) // 32) * 4
        data = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
        images.append((size, data + xor + bytes(mask_row * size)))
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    Path(path).write_bytes(head + entries + blobs)
    return Path(path)


def falcon_icon_path():
    """falcon.ico next to the program (build.bat bundles it), or a fresh one in the temp folder."""
    bundled = SOUNDS / "falcon.ico"
    if bundled.exists():
        return bundled
    import tempfile
    p = Path(tempfile.gettempdir()) / "falconguard.ico"
    try:
        if not p.exists():
            write_falcon_icon(p)
        return p
    except OSError:
        return None


class TrayIcon:
    """FalconGuard's icon in the Windows notification area. Click: open the Dashboard.
    Right-click: a menu. Runs its own small Windows message loop on a background thread;
    choices are handed to the Tk window through a queue."""

    WM_TRAY = 0x8001            # WM_APP + 1
    ID_OPEN, ID_QUIT = 1, 2

    def __init__(self, tip="FalconGuard"):
        self.actions = queue.Queue()
        self.tip = tip
        self.hwnd = None
        self._thread = None

    def start(self):
        if not IS_WINDOWS:
            return False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.hwnd is not None

    def set_tip(self, text):
        self.tip = text[:127]
        if self.hwnd:
            self._notify(1)      # NIM_MODIFY

    def stop(self):
        if self.hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)    # WM_CLOSE
            if self._thread:
                self._thread.join(timeout=3)

    # --- Windows side (background thread) ----------------------------------------
    def _structs(self):
        import ctypes
        from ctypes import wintypes

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]
        return NOTIFYICONDATAW

    def _notify(self, action):
        import ctypes
        nid = self._NID()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4             # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tip
        ctypes.windll.shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    def _run(self):
        import ctypes
        from ctypes import wintypes
        try:
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM)
            u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.DefWindowProcW.restype = LRESULT
            u.CreateWindowExW.restype = wintypes.HWND
            u.LoadImageW.restype = wintypes.HANDLE
            u.CreatePopupMenu.restype = wintypes.HMENU
            self._NID = self._structs()
            taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")

            def proc(hwnd, msg, wparam, lparam):
                if msg == self.WM_TRAY:
                    event = lparam & 0xFFFF
                    if event in (0x0202, 0x0203):             # left click / double click
                        self.actions.put("open")
                    elif event == 0x0205:                     # right click
                        self._menu(hwnd)
                    return 0
                if msg == taskbar_created:                    # Explorer restarted
                    self._notify(0)
                    return 0
                if msg == 0x0002:                             # WM_DESTROY
                    self._notify(2)                           # NIM_DELETE
                    u.PostQuitMessage(0)
                    return 0
                return u.DefWindowProcW(hwnd, msg, wparam, lparam)
            self._proc = WNDPROC(proc)                        # keep a reference

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = k.GetModuleHandleW(None)
            wc.lpszClassName = "FalconGuardTray"
            u.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u.CreateWindowExW(0, "FalconGuardTray", "FalconGuard", 0, 0, 0, 0, 0,
                                          None, None, wc.hInstance, None)
            icon = falcon_icon_path()
            self.hicon = u.LoadImageW(None, str(icon), 1, 16, 16, 0x10) if icon else None  # IMAGE_ICON, LR_LOADFROMFILE
            if not self.hicon:
                self.hicon = u.LoadIconW(None, wintypes.LPCWSTR(32512))                     # IDI_APPLICATION
            self._notify(0)                                   # NIM_ADD
        except Exception:
            self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None

    def _menu(self, hwnd):
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, 0, self.ID_OPEN, "Open Dashboard")
        u.AppendMenuW(menu, 0x800, 0, None)                   # separator
        u.AppendMenuW(menu, 0, self.ID_QUIT, "Quit FalconGuard...")
        u.SetMenuDefaultItem(menu, self.ID_OPEN, 0)
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(hwnd)
        choice = u.TrackPopupMenu(menu, 0x100 | 0x80 | 0x2, pt.x, pt.y, 0, hwnd, None)
        u.PostMessageW(hwnd, 0, 0, 0)                         # WM_NULL: lets the menu close properly
        u.DestroyMenu(menu)
        if choice == self.ID_OPEN:
            self.actions.put("open")
        elif choice == self.ID_QUIT:
            self.actions.put("quit")


# --- Fonts -------------------------------------------------------------------------
FONT = FONT_BOLD = None  # set in setup_fonts() once Tk exists
_RETIRED_FONTS = []      # fonts of earlier Tk windows; kept alive so Tk never frees them twice
HAS_MARLETT = False


def setup_fonts(root):
    """Pick Tahoma (the Windows 2000 UI font) at 11 px, falling back cleanly."""
    global FONT, FONT_BOLD, HAS_MARLETT
    if FONT is not None:
        _RETIRED_FONTS.extend((FONT, FONT_BOLD))
    families = set(tkfont.families(root))
    fam = "Helvetica"
    for name in ("Tahoma", "MS Sans Serif", "Microsoft Sans Serif", "Verdana",
                 "DejaVu Sans"):
        if name in families:
            fam = name
            break
    FONT = tkfont.Font(root, family=fam, size=-11)
    FONT_BOLD = tkfont.Font(root, family=fam, size=-11, weight="bold")
    HAS_MARLETT = "Marlett" in families
    root.option_add("*Font", FONT)


def mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# --- Hand-drawn icons ----------------------------------------------------------
def draw_icon(c, kind, size, bg, selected=False):
    """Draw a small icon on canvas c. Selected icons get the Windows blue tint."""
    c.delete("all")
    s = size / 16.0

    def col(x):
        return mix(x, SELECT, 0.5) if selected else x

    def p(*pts):
        return [v * s for v in pts]

    lw = max(1, round(1.5 * s))
    if kind == "falcon":
        c.create_polygon(p(1, 9, 6, 4, 10, 5, 15, 2, 13, 7, 9, 9, 8, 14, 6, 11, 1, 10),
                         fill=col("#c8862e"), outline=col("#5a3208"))
        c.create_polygon(p(10, 5, 12.5, 4.2, 11, 6.2), fill=col("#f4d070"), outline="")
        c.create_oval(p(10.6, 4.2, 11.6, 5.2), fill=INK, outline="")
    elif kind == "lock":
        c.create_arc(p(4.5, 1.5, 11.5, 11), start=0, extent=180, style="arc",
                     outline=col("#707070"), width=lw)
        c.create_line(p(4.5, 6.2, 4.5, 8), fill=col("#707070"), width=lw)
        c.create_line(p(11.5, 6.2, 11.5, 8), fill=col("#707070"), width=lw)
        c.create_rectangle(p(2.5, 7.5, 13.5, 15), fill=col("#f0c020"), outline=col("#7a5800"))
        c.create_line(p(3.5, 8.5, 12.5, 8.5), fill=col("#fff0a0"))
        c.create_oval(p(7, 10, 9, 12), fill=col("#5a4000"), outline="")
        c.create_line(p(8, 11.5, 8, 13.5), fill=col("#5a4000"), width=lw)
    elif kind == "key":
        c.create_oval(p(1, 5, 7, 11), fill=col("#f0c020"), outline=col("#7a5800"))
        c.create_oval(p(3, 7, 5, 9), fill=bg, outline=col("#7a5800"))
        c.create_rectangle(p(7, 7, 15, 9), fill=col("#f0c020"), outline=col("#7a5800"))
        c.create_rectangle(p(11, 9, 12.5, 11.5), fill=col("#f0c020"), outline=col("#7a5800"))
        c.create_rectangle(p(13.5, 9, 15, 11), fill=col("#f0c020"), outline=col("#7a5800"))
    elif kind == "internet":
        c.create_oval(p(1.5, 1.5, 14.5, 14.5), fill=col("#2f6fd6"), outline=col("#0a2a7a"))
        c.create_polygon(p(4, 4, 7, 3.5, 8, 6, 6.5, 8, 7.5, 10.5, 6, 13, 4.5, 11, 3, 7),
                         fill=col("#3cae3c"), outline="", smooth=True)
        c.create_polygon(p(10, 3, 13, 5, 13.5, 8, 11.5, 9, 10, 6.5), fill=col("#3cae3c"),
                         outline="", smooth=True)
        c.create_arc(p(3, 3, 9, 9), start=90, extent=90, style="arc",
                     outline=col("#a8d0ff"), width=lw)
    elif kind == "trusted":
        c.create_oval(p(1.5, 1.5, 14.5, 14.5), fill=col("#22a345"), outline=col("#0a5a1c"))
        c.create_line(p(4.5, 8, 7, 10.5, 11.5, 5), fill=WINDOW, width=max(2, round(2 * s)))
    elif kind == "restricted":
        c.create_oval(p(1.5, 1.5, 14.5, 14.5), fill=col("#d42020"), outline=col("#6a0000"))
        c.create_rectangle(p(4, 6.8, 12, 9.2), fill=WINDOW, outline="")
    elif kind == "sites":
        c.create_arc(p(6.5, 3, 12.5, 11.5), start=0, extent=180, style="arc",
                     outline=col("#707070"), width=lw)
        c.create_line(p(6.5, 7.2, 6.5, 9), fill=col("#707070"), width=lw)
        c.create_line(p(12.5, 7.2, 12.5, 9), fill=col("#707070"), width=lw)
        c.create_rectangle(p(5, 8.5, 14, 15), fill=col("#f0c020"), outline=col("#7a5800"))
        c.create_oval(p(8.7, 10.5, 10.3, 12.1), fill=col("#5a4000"), outline="")
        c.create_rectangle(p(1, 1, 7, 9), fill=WINDOW, outline=INK)
        for y in (3, 5, 7):
            c.create_line(p(2.5, y, 5.5, y), fill=SELECT)
    elif kind == "password":
        c.create_oval(p(1, 3, 8, 10), fill=col("#f0c020"), outline=col("#7a5800"))
        c.create_oval(p(3, 5, 5, 7), fill=bg, outline=col("#7a5800"))
        for pts in ((7, 9, 14, 15), (11, 12, 13, 10), (9, 10.5, 10.5, 9)):
            c.create_line(p(*pts), fill=col("#7a5800"), width=max(2, round(2 * s)))
    elif kind == "log":
        c.create_rectangle(p(3, 1, 13, 15), fill=WINDOW, outline=INK)
        c.create_rectangle(p(3, 1, 13, 3.5), fill=SELECT, outline=INK)
        for y in (6, 8, 10, 12):
            c.create_line(p(4.5, y, 11.5, y), fill=SHADOW)
    elif kind in ("info", "question", "error"):
        fill = {"info": "#ffffff", "question": "#ffffff", "error": "#e02020"}[kind]
        outline = {"info": "#000080", "question": "#000080", "error": "#700000"}[kind]
        c.create_oval(p(1, 1, 15, 15), fill=fill, outline=outline, width=lw)
        if kind == "info":
            c.create_rectangle(p(7, 3.5, 9, 5.5), fill="#0000c0", outline="")
            c.create_rectangle(p(7, 6.5, 9, 12.5), fill="#0000c0", outline="")
        elif kind == "question":
            c.create_text(8 * s, 8.3 * s, text="?", fill="#0000c0",
                          font=(FONT.actual("family"), -round(10 * s), "bold"))
        else:
            c.create_line(p(5, 5, 11, 11), fill=WINDOW, width=max(2, round(1.8 * s)))
            c.create_line(p(11, 5, 5, 11), fill=WINDOW, width=max(2, round(1.8 * s)))
    elif kind == "coupon":
        c.create_polygon(p(1, 4, 15, 4, 15, 6.5, 14, 8, 15, 9.5, 15, 12, 1, 12, 1, 9.5, 2, 8, 1, 6.5),
                         fill=col("#ffcc33"), outline=col("#8a5a00"))
        for y in range(5, 12, 2):
            c.create_line(p(10.5, y, 10.5, y + 1), fill=col("#8a5a00"))
        c.create_text(5.8 * s, 8 * s, text="+", fill=col("#8a3a00"),
                      font=(FONT.actual("family"), -round(9 * s), "bold"))
    elif kind == "clock":
        c.create_oval(p(1, 1, 15, 15), fill=WINDOW, outline=col("#1e2c50"), width=lw)
        for a, b2, c2, d2 in ((8, 2.5, 8, 3.5), (13.5, 8, 12.5, 8), (8, 13.5, 8, 12.5), (2.5, 8, 3.5, 8)):
            c.create_line(p(a, b2, c2, d2), fill=col("#1e2c50"))
        c.create_line(p(8, 8, 8, 4), fill=INK, width=lw)
        c.create_line(p(8, 8, 11, 9.5), fill=INK, width=lw)
        c.create_oval(p(7.3, 7.3, 8.7, 8.7), fill="#c00000", outline="")
    elif kind == "warning":
        c.create_polygon(p(8, 1, 15, 14.5, 1, 14.5), fill="#ffd800", outline=INK)
        c.create_rectangle(p(7.2, 5, 8.8, 10.5), fill=INK, outline="")
        c.create_rectangle(p(7.2, 11.5, 8.8, 13), fill=INK, outline="")


def icon(parent, kind, size=16, bg=FACE, selected=False):
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
    draw_icon(c, kind, size, bg, selected)
    return c


# --- Windows 2000 widgets --------------------------------------------------------
class Bevel(tk.Frame):
    """A frame with an exact Windows bevel (1 or 2 px). Put children in .body."""

    def __init__(self, parent, colors=RAISED, bg=FACE, **kw):
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0)
        width = 1 if colors[2] is None else 2
        self.body = tk.Frame(self, bg=bg, bd=0, highlightthickness=0, **kw)
        self.body.pack(fill="both", expand=True, padx=width, pady=width)
        for i in range(width):
            tl, br = colors[2 * i], colors[2 * i + 1]
            # top/left first so the bottom/right colour wins the corners, as in Windows
            for spec, color in (
                (dict(x=i, y=i, relwidth=1, width=-2 * i, height=1), tl),
                (dict(x=i, y=i, width=1, relheight=1, height=-2 * i), tl),
                (dict(x=i, rely=1, y=-1 - i, relwidth=1, width=-2 * i, height=1), br),
                (dict(relx=1, x=-1 - i, y=i, width=1, relheight=1, height=-2 * i), br),
            ):
                tk.Frame(self, bg=color, bd=0, highlightthickness=0).place(**spec)


class Button(tk.Canvas):
    """Windows 2000 push button with pressed state, focus rectangle and
    embossed disabled text."""

    def __init__(self, parent, text, disabled=False, default=False, command=None,
                 min_width=75):
        self.min_width = min_width
        w = max(min_width, FONT.measure(text) + 20)
        super().__init__(parent, width=w, height=23, bg=FACE, highlightthickness=0,
                         bd=0, takefocus=0 if disabled else 1)
        self.text, self.disabled, self.default = text, disabled, default
        self.command = command
        self.pressed = self._held = False
        self.w = w
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        self.bind("<Leave>", lambda e: self._set(False))
        self.bind("<Enter>", lambda e: self._set(self._held))
        self.bind("<FocusIn>", lambda e: self._draw())
        self.bind("<FocusOut>", lambda e: self._draw())
        self.bind("<space>", lambda e: self._fire())
        self.bind("<Return>", lambda e: self._fire())
        self._draw()

    def set_enabled(self, enabled):
        if self.disabled == (not enabled):
            return
        self.disabled = not enabled
        self.config(takefocus=1 if enabled else 0)
        self._draw()

    def set_text(self, text):
        self.text = text
        self.w = max(self.min_width, FONT.measure(text) + 20)
        self.config(width=self.w)
        self._draw()

    def _set(self, v):
        if self.pressed != v:
            self.pressed = v
            self._draw()

    def _down(self, e):
        if self.disabled:
            return
        self.focus_set()
        self._held = True
        self._set(True)

    def _up(self, e):
        was = self.pressed
        self._held = False
        self._set(False)
        if was:
            self._fire()

    def _fire(self):
        if not self.disabled and self.command:
            self.command()

    def _rect(self, x0, y0, x1, y1, tl, br):
        self.create_line(x0, y1, x0, y0, x1, y0, fill=tl)
        self.create_line(x1, y0, x1, y1, x0 - 1, y1, fill=br)

    def _draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = self.w - 1, 22
        focused = self.focus_get() is self
        o = 1 if (self.default or focused) and not self.disabled else 0
        if o:
            self.create_rectangle(0, 0, w, h, outline=INK)
        if self.pressed:
            self.create_rectangle(o, o, w - o, h - o, outline=SHADOW)
        else:
            self._rect(o, o, w - o, h - o, FACE, DKSHADOW)
            self._rect(o + 1, o + 1, w - o - 1, h - o - 1, HILIGHT, SHADOW)
        d = 1 if self.pressed else 0
        cx, cy = self.w // 2 + d, 11 + d
        if self.disabled:
            self.create_text(cx + 1, cy + 1, text=self.text, font=FONT, fill=HILIGHT)
            self.create_text(cx, cy, text=self.text, font=FONT, fill=GRAYTEXT)
        else:
            self.create_text(cx, cy, text=self.text, font=FONT, fill=INK)
        if focused and not self.disabled:
            self.create_rectangle(4, 4, w - 4, h - 4, outline=INK, dash=(1, 1))


class CheckBox(tk.Frame):
    """13x13 sunken white box with a black tick, label to the right."""

    def __init__(self, parent, text, value=False, on_change=None):
        super().__init__(parent, bg=FACE)
        self.value = value
        self.on_change = on_change
        self.box = tk.Canvas(self, width=13, height=13, bg=WINDOW,
                             highlightthickness=0, bd=0, takefocus=1)
        self.box.pack(side="left", anchor="n", pady=1)
        self.lbl = tk.Label(self, text=text, font=FONT, bg=FACE, fg=INK,
                            justify="left", anchor="w", padx=1, pady=0, bd=0)
        self.lbl.pack(side="left", padx=4, anchor="n")
        for w in (self, self.box, self.lbl):
            w.bind("<Button-1>", self.toggle)
        self.box.bind("<space>", self.toggle)
        self._draw()

    def toggle(self, e=None):
        self.value = not self.value
        self.box.focus_set()
        self._draw()
        if self.on_change:
            self.on_change()

    def _draw(self):
        c = self.box
        c.delete("all")
        c.create_line(0, 12, 0, 0, 12, 0, fill=SHADOW)
        c.create_line(1, 11, 1, 1, 11, 1, fill=DKSHADOW)
        c.create_line(12, 0, 12, 12, -1, 12, fill=HILIGHT)
        c.create_line(11, 1, 11, 11, 0, 11, fill=FACE)
        if self.value:
            for dy in range(3):
                c.create_line(3, 5 + dy, 5, 7 + dy, 10, 2 + dy, fill=INK)


class GroupBox(tk.Frame):
    """Etched group box with its caption sitting on the top line."""

    def __init__(self, parent, title, padx=10, pady=8):
        super().__init__(parent, bg=FACE)
        self.body = tk.Frame(self, bg=FACE)
        self.body.pack(fill="both", expand=True, padx=padx, pady=(18, pady))
        for spec, color in (
            (dict(x=0, y=6, relwidth=1, width=-1, height=1), SHADOW),
            (dict(x=1, y=7, relwidth=1, width=-3, height=1), HILIGHT),
            (dict(x=0, y=6, width=1, relheight=1, height=-7), SHADOW),
            (dict(x=1, y=7, width=1, relheight=1, height=-9), HILIGHT),
            (dict(x=0, rely=1, y=-2, relwidth=1, width=-1, height=1), SHADOW),
            (dict(x=0, rely=1, y=-1, relwidth=1, height=1), HILIGHT),
            (dict(relx=1, x=-2, y=6, width=1, relheight=1, height=-7), SHADOW),
            (dict(relx=1, x=-1, y=6, width=1, relheight=1, height=-6), HILIGHT),
        ):
            tk.Frame(self, bg=color).place(**spec)
        self.caption = tk.Label(self, text=title, font=FONT, bg=FACE, fg=INK, padx=2, pady=0, bd=0)
        self.caption.place(x=7, y=0)
        self.caption.lift()


class Trackbar(tk.Canvas):
    """Windows 2000 trackbar: sunken groove, tick marks and the pointed thumb."""

    def __init__(self, parent, steps, value=0, orient="horizontal",
                 length=320, command=None):
        self.horizontal = orient == "horizontal"
        w, h = (length, 30) if self.horizontal else (40, length)
        super().__init__(parent, width=w, height=h, bg=FACE,
                         highlightthickness=0, bd=0, takefocus=1)
        self.steps, self.value, self.command = steps, value, command
        self.pad = 13
        self.length = length
        self.bind("<Button-1>", self._click)
        self.bind("<B1-Motion>", self._click)
        for k, d in (("Left", -1), ("Up", -1), ("Right", 1), ("Down", 1)):
            self.bind(f"<{k}>", lambda e, d=d: self.set(self.value + d))
        self.bind("<FocusIn>", lambda e: self._draw())
        self.bind("<FocusOut>", lambda e: self._draw())
        self.bind("<Configure>", self._resize)
        self._draw()

    def _resize(self, e):
        new = e.width if self.horizontal else e.height
        if new != self.length:
            self.length = new
            self._draw()

    def _pos(self, i):
        span = self.length - 2 * self.pad
        return round(self.pad + span * i / (self.steps - 1))

    def _groove(self, x0, y0, x1, y1):
        self.create_line(x0, y1, x0, y0, x1, y0, fill=SHADOW)
        self.create_line(x0 + 1, y1 - 1, x0 + 1, y0 + 1, x1 - 1, y0 + 1, fill=DKSHADOW)
        self.create_line(x1, y0, x1, y1, x0 - 1, y1, fill=HILIGHT)
        self.create_line(x1 - 1, y0 + 1, x1 - 1, y1 - 1, x0, y1 - 1, fill=FACE)

    def _draw(self):
        self.delete("all")
        L = self.length
        focused = self.focus_get() is self
        if self.horizontal:
            self._groove(4, 8, L - 5, 11)
            for i in range(self.steps):
                x = self._pos(i)
                self.create_line(x, 24, x, 28, fill=INK)
            x = self._pos(self.value)
            l, r, t, b = x - 5, x + 5, 0, 15
            self.create_polygon(l, t, r, t, r, b, x, b + 5, l, b, fill=FACE, outline="")
            self.create_line(l, b, l, t, r, t, fill=HILIGHT)
            self.create_line(l, b, x, b + 5, fill=HILIGHT)
            self.create_line(r, t, r, b, x, b + 5, fill=DKSHADOW)
            self.create_line(r - 1, t + 1, r - 1, b, x, b + 4, fill=SHADOW)
            if focused:
                self.create_rectangle(0, 0, L - 1, 29, outline=INK, dash=(1, 1))
        else:
            x = 18
            self._groove(x, 4, x + 3, L - 5)
            for i in range(self.steps):
                y = self._pos(i)
                self.create_line(4, y, 8, y, fill=INK)
            y = self._pos(self.value)
            l, r, t, b = 9, 30, y - 5, y + 5
            self.create_rectangle(l, t, r, b, fill=FACE, outline="")
            self.create_line(l, b, l, t, r, t, fill=HILIGHT)
            self.create_line(r, t, r, b, l - 1, b, fill=DKSHADOW)
            self.create_line(r - 1, t + 1, r - 1, b - 1, l, b - 1, fill=SHADOW)
            if focused:
                self.create_rectangle(0, 0, 39, L - 1, outline=INK, dash=(1, 1))

    def _click(self, e):
        self.focus_set()
        pos = e.x if self.horizontal else e.y
        span = self.length - 2 * self.pad
        self.set(round((pos - self.pad) / span * (self.steps - 1)))

    def set(self, i):
        i = max(0, min(self.steps - 1, i))
        if i != self.value:
            self.value = i
            if self.command:
                self.command(i)
        self._draw()


class TabControl(tk.Frame):
    """Property-sheet tabs drawn the Windows 2000 way."""

    def __init__(self, parent):
        super().__init__(parent, bg=FACE)
        self.strip = tk.Canvas(self, height=20, bg=FACE, highlightthickness=0, bd=0)
        self.strip.pack(fill="x")
        self.sheet = Bevel(self, RAISED)
        self.sheet.pack(fill="both", expand=True)
        self.sheet.body.rowconfigure(0, weight=1)
        self.sheet.body.columnconfigure(0, weight=1)
        self.cover = tk.Frame(self.sheet, bg=FACE, height=2)
        self.pages, self.names, self.current = [], [], 0
        self.strip.bind("<Button-1>", self._click)
        self.strip.bind("<Configure>", lambda e: self._draw())

    def add(self, name):
        page = tk.Frame(self.sheet.body, bg=FACE, padx=10, pady=10)
        page.grid(row=0, column=0, sticky="nsew")  # one cell: window keeps one size
        self.pages.append(page)
        self.names.append(name)
        self.pages[0].tkraise()
        return page

    def _layout(self):
        x, boxes = 2, []
        for n in self.names:
            w = FONT.measure(n) + 9
            boxes.append((x, x + w))
            x += w
        return boxes

    def _draw(self):
        c = self.strip
        c.delete("all")
        boxes = self._layout()
        order = [i for i in range(len(boxes)) if i != self.current] + [self.current]
        for i in order:
            x0, x1 = boxes[i]
            sel = i == self.current
            top, bottom = (0, 20) if sel else (2, 19)
            if sel:
                x0, x1 = x0 - 2, x1 + 2
            c.create_rectangle(x0, top + 1, x1, bottom, fill=FACE, outline="")
            c.create_line(x0, bottom, x0, top + 2, x0 + 2, top, x1 - 2, top, fill=HILIGHT)
            c.create_line(x1 - 2, top + 1, x1 - 1, top + 2, x1 - 1, bottom, fill=DKSHADOW)
            c.create_line(x1 - 2, top + 2, x1 - 2, bottom, fill=SHADOW)
            ty = top + 10 if sel else top + 9
            c.create_text((x0 + x1) // 2, ty, text=self.names[i], font=FONT, fill=INK)
            if sel:
                self.cover.place(x=x0 + 1, y=0, width=x1 - x0 - 3, height=2)
                self.cover.lift()

    def _click(self, e):
        for i, (x0, x1) in enumerate(self._layout()):
            if x0 <= e.x < x1:
                self.select(i)

    def select(self, i):
        self.current = i
        self.pages[i].tkraise()
        self._draw()


class HourGrid(tk.Canvas):
    """Week x 24-hour grid. Click or drag to block/allow hours; click a day
    name to flip the whole day, or an hour number to flip that hour all week."""

    CW, CH = 15, 15   # cell size
    LW = 66           # day-name column
    HH = 30           # two header rows

    def __init__(self, parent, blocked, on_change=None):
        w = self.LW + 24 * self.CW + 8
        h = self.HH + 7 * self.CH + 1
        super().__init__(parent, width=w, height=h, bg=FACE, highlightthickness=0, bd=0)
        self.blocked = [[hr in blocked[d] for hr in range(24)] for d in range(7)]
        self.on_change = on_change
        self.paint = None
        self.cells = {}
        self._draw()
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "paint", None))

    def _draw(self):
        L, CW, CH, HH = self.LW, self.CW, self.CH, self.HH
        self.create_text(L - 3, 7, text="Midnight (AM)", font=FONT_BOLD, fill=INK, anchor="w")
        noon = L + 12 * CW
        self.create_line(noon, 1, noon, 15, fill=INK)
        self.create_text(noon + 4, 7, text="Noon (PM)", font=FONT_BOLD, fill=INK, anchor="w")
        for c in range(25):
            n = 12 if c % 12 == 0 else c % 12
            self.create_text(L + c * CW, HH - 2, text=str(n), font=FONT, fill=INK,
                             anchor="s")
        for d, name in enumerate(DAYS):
            self.create_text(0, HH + d * CH + CH / 2, text=name, font=FONT, fill=INK,
                             anchor="w")
            for hr in range(24):
                x, y = L + hr * CW, HH + d * CH
                self.cells[d, hr] = self.create_rectangle(
                    x, y, x + CW, y + CH, outline=GRIDLINE,
                    fill=BLOCKED if self.blocked[d][hr] else WINDOW)

    def _cell(self, e):
        hr = (e.x - self.LW) // self.CW
        d = (e.y - self.HH) // self.CH
        return int(d), int(hr)

    def _set(self, d, hr, value):
        if self.blocked[d][hr] != value:
            self.blocked[d][hr] = value
            self.itemconfig(self.cells[d, hr], fill=BLOCKED if value else WINDOW)

    def _press(self, e):
        d, hr = self._cell(e)
        if 0 <= d < 7 and 0 <= hr < 24:
            self.paint = not self.blocked[d][hr]
            self._set(d, hr, self.paint)
        elif 0 <= d < 7 and e.x < self.LW:          # day name: flip the whole day
            value = not all(self.blocked[d])
            for h in range(24):
                self._set(d, h, value)
        elif 15 < e.y < self.HH and 0 <= hr < 24:   # hour number: flip the column
            value = not all(self.blocked[day][hr] for day in range(7))
            for day in range(7):
                self._set(day, hr, value)
        else:
            return
        if self.on_change:
            self.on_change()

    def _drag(self, e):
        if self.paint is None:
            return
        d, hr = self._cell(e)
        if 0 <= d < 7 and 0 <= hr < 24 and self.blocked[d][hr] != self.paint:
            self._set(d, hr, self.paint)
            if self.on_change:
                self.on_change()

    def allowed_hours(self, d):
        return 24 - sum(self.blocked[d])


class TimeSpin(tk.Frame):
    """Windows 2000 spin box for a duration: sunken field plus up/down arrows.
    Steps in half hours from 0h 30m to 24h, with "No limit" at the top."""

    def __init__(self, parent, minutes=None, on_change=None, values=None):
        super().__init__(parent, bg=FACE)
        self.values = values or LIMIT_VALUES
        self.index = self.values.index(minutes)
        self.on_change = on_change
        field = Bevel(self, SUNKEN, bg=WINDOW)
        field.pack()
        row = field.body
        self.text = tk.Label(row, font=FONT, bg=WINDOW, fg=INK, width=8, anchor="w",
                             padx=3, pady=1, bd=0, takefocus=1)
        self.text.pack(side="left")
        self.arrows = tk.Canvas(row, width=16, height=17, bg=FACE,
                                highlightthickness=0, bd=0)
        self.arrows.pack(side="left")
        self.held = None
        self._repeat = None
        self.arrows.bind("<ButtonPress-1>", self._press)
        self.arrows.bind("<ButtonRelease-1>", self._release)
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self.text.bind("<Up>", lambda e: self.step(1))
        self.text.bind("<Down>", lambda e: self.step(-1))
        for w in (self.text, self.arrows):
            w.bind("<MouseWheel>", lambda e: self.step(1 if e.delta > 0 else -1))
            w.bind("<Button-4>", lambda e: self.step(1))
            w.bind("<Button-5>", lambda e: self.step(-1))
        self._render()

    @property
    def minutes(self):
        return self.values[self.index]

    def _render(self):
        self.text.config(text=fmt_minutes(self.minutes))
        c = self.arrows
        c.delete("all")
        for k, (y0, y1) in enumerate(((0, 8), (9, 17))):
            pressed = self.held == (1 if k == 0 else -1)
            if pressed:
                c.create_rectangle(0, y0, 15, y1 - 1 + (k == 0), outline=SHADOW, fill=FACE)
            else:
                c.create_line(0, y1, 0, y0, 15, y0, fill=FACE)
                c.create_line(15, y0, 15, y1, -1, y1, fill=DKSHADOW)
                c.create_line(1, y1 - 1, 1, y0 + 1, 14, y0 + 1, fill=HILIGHT)
                c.create_line(14, y0 + 1, 14, y1 - 1, 0, y1 - 1, fill=SHADOW)
            d = 1 if pressed else 0
            cx, cy = 8 + d, (y0 + y1) // 2 + d
            if k == 0:
                c.create_polygon(cx - 3, cy + 1, cx + 3, cy + 1, cx, cy - 2, fill=INK, outline=INK)
            else:
                c.create_polygon(cx - 3, cy - 1, cx + 3, cy - 1, cx, cy + 2, fill=INK, outline=INK)

    def step(self, direction):
        new = max(0, min(len(self.values) - 1, self.index + direction))
        if new != self.index:
            self.index = new
            if self.on_change:
                self.on_change()
        self._render()

    def _press(self, e):
        self.text.focus_set()
        self.held = 1 if e.y < 9 else -1
        self.step(self.held)
        self._repeat = self.after(400, self._auto)

    def _auto(self):
        if self.held:
            self.step(self.held)
            self._repeat = self.after(80, self._auto)

    def _release(self, e):
        self.held = None
        if self._repeat:
            self.after_cancel(self._repeat)
        self._render()


def label(parent, text="", bold=False, bg=FACE, fg=INK, **kw):
    kw.setdefault("anchor", "w")
    kw.setdefault("padx", 0)
    kw.setdefault("pady", 0)
    return tk.Label(parent, text=text, font=FONT_BOLD if bold else FONT, bg=bg,
                    fg=fg, justify="left", bd=0, **kw)


def entry(parent, show="", width=30):
    """A sunken text field. Returns (frame, Entry)."""
    box = Bevel(parent, SUNKEN, bg=WINDOW)
    e = tk.Entry(box.body, font=FONT, relief="flat", bd=2, bg=WINDOW, fg=INK, show=show,
                 width=width, insertwidth=1, highlightthickness=0,
                 selectbackground=SELECT, selectforeground=WINDOW)
    e.pack(fill="x")
    return box, e


def listbox(parent, height=8, width=40):
    """A sunken list with a scroll bar. Returns (frame, Listbox)."""
    box = Bevel(parent, SUNKEN, bg=WINDOW)
    lb = tk.Listbox(box.body, font=FONT, relief="flat", bd=0, bg=WINDOW, fg=INK,
                    highlightthickness=0, activestyle="none", height=height, width=width,
                    selectbackground=SELECT, selectforeground=WINDOW, exportselection=False)
    sb = tk.Scrollbar(box.body, orient="vertical", command=lb.yview, bg=FACE,
                      activebackground=FACE, troughcolor="#eae8e3", relief="flat",
                      bd=1, highlightthickness=0, elementborderwidth=2, width=16)
    lb.config(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    lb.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=(1, 0))
    return box, lb


# --- Title bar and dialogs ---------------------------------------------------------
def take_focus(widget):
    """Borderless windows need forced focus on Windows to get the keyboard. On Linux,
    Tk's forced focus can crash (seen when several Tk windows are made in one process),
    and normal focus is enough there."""
    try:
        if IS_WINDOWS:
            widget.focus_force()
        else:
            widget.focus_set()
    except tk.TclError:
        pass


def caption_button(parent, kind, command):
    c = tk.Canvas(parent, width=16, height=14, bg=FACE, highlightthickness=0, bd=0)

    def draw(pressed=False):
        c.delete("all")
        if pressed:
            c.create_line(0, 13, 0, 0, 15, 0, fill=DKSHADOW)
            c.create_line(15, 0, 15, 13, -1, 13, fill=HILIGHT)
            c.create_line(1, 12, 1, 1, 14, 1, fill=SHADOW)
        else:
            c.create_line(0, 13, 0, 0, 15, 0, fill=FACE)
            c.create_line(15, 0, 15, 13, -1, 13, fill=DKSHADOW)
            c.create_line(1, 12, 1, 1, 14, 1, fill=HILIGHT)
            c.create_line(14, 1, 14, 12, 0, 12, fill=SHADOW)
        d = 1 if pressed else 0
        if kind == "close" and HAS_MARLETT:
            c.create_text(8 + d, 7 + d, text="r", font=("Marlett", -9), fill=INK)
        elif kind == "close":
            for k in (0, 1):
                c.create_line(4 + k + d, 3 + d, 10 + k + d, 10 + d, fill=INK)
                c.create_line(10 + k + d, 3 + d, 4 + k + d, 10 + d, fill=INK)
        else:  # the "?" glyph, drawn pixel by pixel like the system bitmap
            for x0, y0, x1, y1 in ((5, 3, 10, 4), (4, 4, 6, 6), (9, 4, 11, 7),
                                   (8, 7, 10, 8), (7, 8, 9, 9), (7, 10, 9, 12)):
                c.create_rectangle(x0 + d, y0 + d, x1 + d - 1, y1 + d - 1,
                                   fill=INK, outline="")

    c.bind("<ButtonPress-1>", lambda e: draw(True))
    c.bind("<ButtonRelease-1>", lambda e: (draw(False), command()))
    draw()
    return c


def title_bar(win, parent, title, on_close, on_help=None):
    """Windows 2000 caption: blue gradient, bold title, caption buttons, drag to move."""
    win.overrideredirect(True)
    bar = tk.Canvas(parent, height=18, bg=SELECT, highlightthickness=0, bd=0)
    bar.pack(fill="x")
    ic = icon(bar, "falcon", 16, bg=SELECT)
    bar.create_window(2, 1, window=ic, anchor="nw")
    bar.create_text(21, 9, text=title, font=FONT_BOLD, fill=WINDOW, anchor="w")
    bar.create_window(0, 2, window=caption_button(bar, "close", on_close),
                      anchor="nw", tags="close")
    if on_help:
        bar.create_window(0, 2, window=caption_button(bar, "help", on_help),
                          anchor="nw", tags="help")

    def layout(e):
        w = e.width
        bar.delete("grad")
        for x in range(0, w, 2):
            bar.create_rectangle(x, 0, x + 2, 18, outline="", tags="grad",
                                 fill=mix(SELECT, TITLE_END, x / max(1, w - 1)))
        bar.tag_lower("grad")
        bar.coords("close", w - 18, 2)
        bar.coords("help", w - 36, 2)

    bar.bind("<Configure>", layout)
    drag = {}

    def start(e):
        drag["d"] = (e.x_root - win.winfo_x(), e.y_root - win.winfo_y())

    def move(e):
        dx, dy = drag.get("d", (0, 0))
        win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    for w in (bar, ic):
        w.bind("<Button-1>", start)
        w.bind("<B1-Motion>", move)
    return bar


class Dialog(tk.Toplevel):
    """Modal Windows 2000 dialog. Subclasses fill self.body and call finish()."""

    def __init__(self, master, title):
        super().__init__(master, bg=FACE)
        self.withdraw()
        frame = Bevel(self, RAISED, padx=2, pady=2)
        frame.pack(fill="both", expand=True)
        title_bar(self, frame.body, title, self.cancel)
        self.body = tk.Frame(frame.body, bg=FACE, padx=11, pady=11)
        self.body.pack(fill="both", expand=True)
        self.result = None
        self.bind("<Escape>", lambda e: self.cancel())

    def button_row(self, specs, parent=None, center=False):
        """specs: list of (text, command, default). Returns the Buttons."""
        row = tk.Frame(parent or self.body, bg=FACE)
        row.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(row, bg=FACE)
        inner.pack(anchor="center" if center else "e")
        out = []
        for text, cmd, default in specs:
            b = Button(inner, text, default=default, command=cmd)
            b.pack(side="left", padx=(6, 0) if out else 0)
            out.append(b)
        return out

    def show(self, focus=None):
        self.update_idletasks()
        m = self.master
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        if m.winfo_viewable():
            x = m.winfo_rootx() + (m.winfo_width() - w) // 2
            y = m.winfo_rooty() + (m.winfo_height() - h) // 3
        else:
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            if int(m.attributes("-topmost")):
                self.attributes("-topmost", True)
        except (tk.TclError, ValueError):
            pass
        self.deiconify()
        self.lift()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        take_focus(focus or self)
        self.wait_window()
        if isinstance(m, Dialog) and m.winfo_exists():   # hand the grab back to the parent dialog
            try:
                m.grab_set()
                take_focus(m)
            except tk.TclError:
                pass
        return self.result

    def finish(self, result):
        self.result = result
        self.destroy()

    def cancel(self):
        self.finish(None)


def message(master, title, text, kind="info", buttons=("OK",), default=0):
    """Windows-style message box. Returns the clicked button's text (None if closed).
    Error boxes play error.mp3."""
    try:
        if not master.winfo_exists():        # its window has closed (e.g. restarted as admin)
            return None
    except tk.TclError:
        return None
    if kind == "error":
        play_sound(ERROR_SOUND)
    d = Dialog(master, title)
    row = tk.Frame(d.body, bg=FACE)
    row.pack(fill="x")
    icon(row, kind, 32).pack(side="left", anchor="n")
    label(row, text, wraplength=320).pack(side="left", padx=(12, 0), pady=(2, 0))
    d.button_row([(b, lambda b=b: d.finish(b), i == default)
                  for i, b in enumerate(buttons)], center=True)
    d.bind("<Return>", lambda e: d.finish(buttons[default]))
    return d.show()


class PasswordDialog(Dialog):
    """Create or change the supervisor password. Result: (password record, hint)."""

    def __init__(self, master, current=None, hint=""):
        creating = current is None
        super().__init__(master, "Create Supervisor Password" if creating
                         else "Change Supervisor Password")
        self.current = current
        row = tk.Frame(self.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "password", 32).pack(side="left", anchor="n")
        label(row, "Create a supervisor password. You will need it to change\n"
                   "Falcon settings or to turn Falcon on or off." if creating else
                   "Type the old password, and then type the new password\n"
                   "twice to confirm it.").pack(side="left", padx=12)
        grid = tk.Frame(self.body, bg=FACE)
        grid.pack(fill="x", pady=(12, 0))
        grid.columnconfigure(1, weight=1)
        rows = ([] if creating else [("Old password:", "*")]) + [
            ("New password:", "*"), ("Confirm password:", "*"), ("Hint (optional):", "")]
        self.fields = []
        for r, (text, show) in enumerate(rows):
            label(grid, text).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            box, e = entry(grid, show=show, width=28)
            box.grid(row=r, column=1, sticky="ew", pady=3)
            self.fields.append(e)
        if creating:
            self.old = None
            self.new, self.confirm, self.hint = self.fields
        else:
            self.old, self.new, self.confirm, self.hint = self.fields
            self.hint.insert(0, hint)
        label(self.body, "The hint helps you remember the password. Anyone can see it.",
              fg=GRAYTEXT).pack(fill="x", pady=(4, 0))
        self.button_row([("OK", self.ok, True), ("Cancel", self.cancel, False)])
        self.bind("<Return>", lambda e: self.ok())

    def _error(self, text, field):
        message(self, "Supervisor Password", text, "error")
        field.focus_set()
        field.select_range(0, "end")

    def ok(self):
        new, confirm, hint = self.new.get(), self.confirm.get(), self.hint.get().strip()
        if self.old is not None and not check_password(self.old.get(), self.current):
            return self._error("The old password is incorrect.", self.old)
        problem = password_problem(new, confirm, hint)
        if problem:
            if problem[1] == "confirm":
                self.confirm.delete(0, "end")
            return self._error(problem[0], getattr(self, problem[1]))
        self.old_plain = self.old.get() if self.old is not None else None
        self.finish((hash_password(new), hint[:100], new))


class ZoneSitesDialog(Dialog):
    """Add and remove websites in a zone. Result: the new list of sites."""

    def __init__(self, master, name, sites):
        super().__init__(master, name)
        self.sites = sorted(sites)
        row = tk.Frame(self.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "restricted" if name.startswith("Restricted") else "trusted", 32).pack(
            side="left", anchor="n")
        label(row, f"You can add and remove websites from this zone. All websites\n"
                   f"in this zone will use the zone's protection level.").pack(
            side="left", padx=12)
        label(self.body, "Add this website to the zone:").pack(fill="x", pady=(12, 3))
        add_row = tk.Frame(self.body, bg=FACE)
        add_row.pack(fill="x")
        box, self.field = entry(add_row, width=30)
        box.pack(side="left", fill="x", expand=True)
        self.add_btn = Button(add_row, "Add", disabled=True, command=self.add)
        self.add_btn.pack(side="left", padx=(10, 0))
        label(self.body, "Websites:").pack(fill="x", pady=(10, 3))
        list_row = tk.Frame(self.body, bg=FACE)
        list_row.pack(fill="x")
        box, self.list = listbox(list_row, height=8)
        box.pack(side="left", fill="x", expand=True)
        self.remove_btn = Button(list_row, "Remove", disabled=True, command=self.remove)
        self.remove_btn.pack(side="left", padx=(10, 0), anchor="n")
        self.field.bind("<KeyRelease>", lambda e: self.add_btn.set_enabled(bool(self.field.get().strip())))
        self.field.bind("<Return>", lambda e: (self.add(), "break")[1])
        self.list.bind("<<ListboxSelect>>",
                       lambda e: self.remove_btn.set_enabled(bool(self.list.curselection())))
        self.button_row([("OK", lambda: self.finish(self.sites), True),
                         ("Cancel", self.cancel, False)])
        self._refresh()

    def _refresh(self):
        self.list.delete(0, "end")
        for s in self.sites:
            self.list.insert("end", s)
        self.remove_btn.set_enabled(False)

    def add(self):
        site = normalize_site(self.field.get())
        if not site:
            message(self, "Falcon", "Type a website address, such as www.example.com.",
                    "warning")
            return
        if site not in self.sites:
            self.sites = sorted(self.sites + [site])
        self.field.delete(0, "end")
        self.add_btn.set_enabled(False)
        self._refresh()

    def remove(self):
        sel = self.list.curselection()
        if sel:
            del self.sites[sel[0]]
            self._refresh()


class LogDialog(Dialog):
    """Shows the activity log, newest first."""

    def __init__(self, master, store):
        super().__init__(master, "Falcon Activity Log")
        self.store = store
        row = tk.Frame(self.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "log", 32).pack(side="left", anchor="n")
        label(row, "Falcon records sign-ins, wrong passwords and every change\n"
                   "to its settings. The newest entries are at the top.").pack(
            side="left", padx=12)
        box, self.list = listbox(self.body, height=16, width=72)
        box.pack(fill="both", expand=True, pady=(12, 0))
        self.count = label(self.body, fg=GRAYTEXT)
        self.count.pack(fill="x", pady=(5, 0))
        self.button_row([("Clear Log...", self.clear, False), ("Close", self.cancel, True)])
        self.bind("<Return>", lambda e: self.cancel())
        self._refresh()

    def _refresh(self):
        entries = self.store.read_log()
        self.list.delete(0, "end")
        for e in reversed(entries):
            try:
                when = datetime.fromisoformat(e["time"]).strftime("%Y-%m-%d   %H:%M")
            except (ValueError, TypeError):
                when = str(e["time"])
            self.list.insert("end", f"{when}      {e['event']}")
            if "Wrong" in str(e["event"]) or "locked" in str(e["event"]):
                self.list.itemconfig("end", fg=NEVER_FG)
        n = len(entries)
        self.count.config(text="The log is empty." if n == 0 else
                          f"{n} {'entry' if n == 1 else 'entries'}")

    def clear(self):
        if message(self, "Clear Activity Log", "Are you sure you want to delete every "
                   "entry in the activity log?", "question", ("Yes", "No"), default=1) == "Yes":
            self.store.clear_log()
            self.store.log("Activity log cleared")
            self._refresh()


class UnlockDialog(Dialog):
    """Asks for the supervisor password before an edit. Result: True when correct.
    After 3 wrong tries the box closes (Falcon keeps running)."""

    def __init__(self, master, store, settings, reason, what="coupons"):
        super().__init__(master, "Supervisor Password")
        self.store, self.settings, self.attempts, self.what = store, settings, 0, what
        self.password = None
        row = tk.Frame(self.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "password", 32).pack(side="left", anchor="n")
        label(row, reason).pack(side="left", padx=12)
        label(self.body, "Password:").pack(fill="x", pady=(12, 3))
        box, self.pw = entry(self.body, show="*", width=36)
        box.pack(fill="x")
        if settings.get("hint"):
            label(self.body, f"Hint: {settings['hint']}", fg=GRAYTEXT).pack(fill="x", pady=(5, 0))
        self.button_row([("OK", self.ok, True), ("Cancel", self.cancel, False)])
        self.bind("<Return>", lambda e: self.ok())

    def ok(self):
        if check_password(self.pw.get(), self.settings["password"]):
            self.password = self.pw.get()
            self.finish(True)
            return
        self.attempts += 1
        self.store.log(f"Wrong supervisor password entered ({self.what})")
        if self.attempts >= FAILED_ATTEMPTS_ALLOWED:
            message(self, "Supervisor Password", f"The password was typed incorrectly "
                    f"{FAILED_ATTEMPTS_ALLOWED} times.", "error")
            self.finish(False)
            return
        message(self, "Supervisor Password", "The password is incorrect. Check that Caps "
                "Lock is off, and then try again.", "error")
        self.pw.delete(0, "end")
        self.pw.focus_set()

    def cancel(self):
        self.finish(False)


class CouponDialog(Dialog):
    """Anyone can see the coupons and copy a code to redeem it on the Dashboard.
    Creating and deleting coupons needs the supervisor password (asked once per
    opening of this box)."""

    def __init__(self, master, app):
        super().__init__(master, "Manage Coupons")
        self.app = app
        self.unlocked = False
        row = tk.Frame(self.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "coupon", 32).pack(side="left", anchor="n")
        label(row, "Coupons give extra screen time. Create one, give the code to\n"
                   "your child, and they can redeem it once on the Dashboard.\n"
                   "The extra time counts for the day it is redeemed.").pack(side="left", padx=12)

        lock = Bevel(self.body, THIN_SUNKEN, padx=6, pady=5)
        lock.pack(fill="x", pady=(12, 0))
        self.lock_icon = icon(lock.body, "lock")
        self.lock_icon.pack(side="left")
        self.lock_text = label(lock.body)
        self.lock_text.pack(side="left", padx=8)
        self.unlock_btn = Button(lock.body, "Unlock...", command=self.unlock)
        self.unlock_btn.pack(side="right")

        box = GroupBox(self.body, "Create a coupon")
        box.pack(fill="x", pady=(12, 0))
        grid = tk.Frame(box.body, bg=FACE)
        grid.pack(fill="x")
        grid.columnconfigure(3, weight=1)
        label(grid, "Extra time:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.amount = TimeSpin(grid, 30, values=COUPON_VALUES)
        self.amount.grid(row=0, column=1, sticky="w")
        label(grid, "Note:").grid(row=0, column=2, sticky="w", padx=(14, 8))
        nbox, self.note = entry(grid, width=20)
        nbox.grid(row=0, column=3, sticky="ew")
        Button(grid, "Create", command=self.create).grid(row=0, column=4, padx=(10, 0))
        label(box.body, "The note is optional, for example \"Tidied bedroom\".", fg=GRAYTEXT).pack(
            fill="x", pady=(5, 0))

        label(self.body, "Coupons:").pack(fill="x", pady=(10, 3))
        lrow = tk.Frame(self.body, bg=FACE)
        lrow.pack(fill="x")
        lbox, self.list = listbox(lrow, height=9, width=56)
        lbox.pack(side="left", fill="x", expand=True)
        side = tk.Frame(lrow, bg=FACE)
        side.pack(side="left", anchor="n", padx=(10, 0))
        self.copy_btn = Button(side, "Copy Code", disabled=True, command=self.copy)
        self.copy_btn.pack()
        self.delete_btn = Button(side, "Delete", disabled=True, command=self.delete)
        self.delete_btn.pack(pady=6)
        self.list.bind("<<ListboxSelect>>", lambda e: self._selected())
        self.summary = label(self.body, fg=GRAYTEXT)
        self.summary.pack(fill="x", pady=(5, 0))
        self.button_row([("Close", self.cancel, True)])
        self.bind("<Return>", lambda e: self.cancel())
        self._refresh()

    def _refresh(self):
        used = self.app._redeemed()
        coupons = [dict(c, redeemed=used.get(c["code"])) for c in self.app.saved["coupons"]]
        # ready coupons first (newest at the top), then used ones (most recently used first)
        self.rows = (sorted((c for c in coupons if not c["redeemed"]),
                            key=lambda c: c["created"], reverse=True) +
                     sorted((c for c in coupons if c["redeemed"]),
                            key=lambda c: c["redeemed"], reverse=True))
        self.list.delete(0, "end")
        for c in self.rows:
            if c["redeemed"]:
                when = datetime.fromisoformat(c["redeemed"]).strftime("%d %b %H:%M")
                state = f"Used {when}"
            else:
                state = "Ready"
            note = f"   {c['note']}" if c["note"] else ""
            self.list.insert("end", f"{c['code']}     +{fmt_minutes(c['minutes']):<8} {state:<16}{note}")
            if c["redeemed"]:
                self.list.itemconfig("end", fg=GRAYTEXT)
        ready = sum(1 for c in self.rows if not c["redeemed"])
        used = len(self.rows) - ready
        self.summary.config(text=f"{ready} ready to use, {used} used.")
        if self.unlocked:
            self.lock_text.config(text="Unlocked. You can create and delete coupons.")
            self.unlock_btn.pack_forget()
            draw_icon(self.lock_icon, "key", 16, FACE)
        else:
            self.lock_text.config(text="Anyone can copy a code and redeem it. Creating or\n"
                                       "deleting coupons needs the supervisor password.")
        self._selected()

    def unlock(self):
        """Ask for the supervisor password. Returns True once unlocked."""
        if self.unlocked:
            return True
        app = self.app
        if not app.saved["password"]:
            if app.mode == "supervisor" and app.ensure_password():
                ok = True
            else:
                if app.mode != "supervisor":
                    message(self, "Manage Coupons", "No supervisor password has been set "
                            "up yet. Ask your parent to set one up in Falcon first.", "warning")
                return False
        else:
            dlg = UnlockDialog(self, app.store, app.saved, "Type the supervisor password to\n"
                               "create or delete coupons.")
            ok = dlg.show()
            if ok and not app.service:
                app.service = ServiceClient(dlg.password)
        if ok:
            self.unlocked = True
            app.store.log("Coupons unlocked")
            self._refresh()
        return bool(ok)

    def _selected(self):
        has = bool(self.list.curselection())
        self.copy_btn.set_enabled(has)
        self.delete_btn.set_enabled(has)

    def _current(self):
        sel = self.list.curselection()
        return self.rows[sel[0]] if sel else None

    def create(self):
        if not self.unlock():
            return
        code = self.app.create_coupon(self.amount.minutes, self.note.get().strip())
        if code:
            self.note.delete(0, "end")
            self._refresh()
            self._copy_text(code)
            message(self, "Coupon Created",
                    f"Coupon {code} gives {fmt_minutes(self.amount.minutes)} of extra screen "
                    "time. The code has been copied so you can paste it or write it down "
                    "for your child.")

    def _copy_text(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    def copy(self):
        c = self._current()
        if c:
            self._copy_text(c["code"])
            message(self, "Copy Code", f"{c['code']} was copied." + (
                "" if c["redeemed"] else " Paste it into Coupon code on the Dashboard "
                "and click Redeem."))

    def delete(self):
        c = self._current()
        if not c or not self.unlock():
            return
        what = "used coupon" if c["redeemed"] else "coupon. It can no longer be redeemed"
        if message(self, "Delete Coupon", f"Delete {c['code']}? This removes the {what}.",
                   "question", ("Yes", "No"), default=1) == "Yes":
            self.app.delete_coupon(c["code"])
            self._refresh()


class RadioGroup:
    """Windows 2000 option buttons: sunken white circles with a black dot."""

    def __init__(self, parent, options, value):
        self.value = value
        self.items = []
        for key, text in options:
            row = tk.Frame(parent, bg=FACE)
            row.pack(fill="x", pady=1)
            c = tk.Canvas(row, width=12, height=12, bg=FACE, highlightthickness=0, bd=0)
            c.pack(side="left")
            lb = label(row, text, padx=1)
            lb.pack(side="left", padx=4)
            for w in (row, c, lb):
                w.bind("<Button-1>", lambda e, k=key: self.set(k))
            self.items.append((key, c))
        self._draw()

    def set(self, key):
        self.value = key
        self._draw()

    def _draw(self):
        for key, c in self.items:
            c.delete("all")
            c.create_oval(0, 0, 11, 11, fill=WINDOW, outline="")
            c.create_arc(0, 0, 11, 11, start=45, extent=180, style="arc", outline=SHADOW)
            c.create_arc(0, 0, 11, 11, start=225, extent=180, style="arc", outline=HILIGHT)
            c.create_arc(1, 1, 10, 10, start=45, extent=180, style="arc", outline=DKSHADOW)
            c.create_arc(1, 1, 10, 10, start=225, extent=180, style="arc", outline=FACE)
            if key == self.value:
                c.create_oval(4, 4, 7, 7, fill=INK, outline=INK)


class BlockDialog(tk.Toplevel):
    """'Falcon Advisor': shown after a blocked website was opened and the browsers closed.
    The supervisor can type the password to allow the site always or just this time."""

    def __init__(self, master, host, reasons, allow_override, on_allow, on_wrong_password):
        super().__init__(master, bg=FACE)
        self.withdraw()
        self.host, self.on_allow, self.on_wrong = host, on_allow, on_wrong_password
        frame = Bevel(self, RAISED, padx=2, pady=2)
        frame.pack(fill="both", expand=True)
        title_bar(self, frame.body, "Falcon Advisor", self.destroy)
        body = tk.Frame(frame.body, bg=FACE, padx=11, pady=11)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg=FACE)
        top.pack(fill="x")
        left = tk.Frame(top, bg=FACE)
        left.pack(side="left", fill="x", expand=True, anchor="n")
        label(left, "Sorry! Falcon will not allow you to see this site.").pack(fill="x", pady=(4, 2))
        label(left, host, bold=True).pack(fill="x", pady=(0, 10))
        label(left, "This page may contain some or all of the following:").pack(fill="x")
        btns = tk.Frame(top, bg=FACE)
        btns.pack(side="right", anchor="n", padx=(12, 0))
        Button(btns, "OK", default=True, command=self.ok).pack()
        Button(btns, "Cancel", command=self.destroy).pack(pady=(6, 0))

        lbox, lst = listbox(body, height=4, width=60)
        lbox.pack(fill="x", pady=(4, 10))
        for r in reasons:
            lst.insert("end", r)
        lst.config(selectbackground=WINDOW, selectforeground=INK)

        panel = GroupBox(body, "")
        panel.caption.place_forget()
        panel.pack(fill="x")
        row = tk.Frame(panel.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "password", 32).pack(side="left", anchor="n")
        self.pw = None
        if allow_override:
            label(row, "If you still want to see this site, someone must type in the\n"
                       "supervisor password.").pack(side="left", padx=10)
            opts = tk.Frame(panel.body, bg=FACE)
            opts.pack(fill="x", padx=(42, 0), pady=(8, 0))
            self.choice = RadioGroup(opts, [("always", "Always allow this website to be viewed"),
                                            ("once", "Allow viewing only this time")], "once")
            prow = tk.Frame(panel.body, bg=FACE)
            prow.pack(fill="x", pady=(10, 0))
            label(prow, "Password:").pack(side="left", padx=(8, 12))
            pbox, self.pw = entry(prow, show="*", width=34)
            pbox.pack(side="left", fill="x", expand=True)
            self.bind("<Return>", lambda e: self.ok())
        else:
            label(row, "If you need this site, ask your parent to add it to the\n"
                       "Approved Sites list in Falcon.").pack(side="left", padx=10)
        self.bind("<Escape>", lambda e: self.destroy())

    def show(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}+{(self.winfo_screenheight() - h) // 3}")
        self.attributes("-topmost", True)
        self.deiconify()
        self.lift()
        take_focus(self)
        if self.pw:
            self.pw.focus_set()

    def ok(self):
        if self.pw is None or not self.pw.get():
            self.destroy()
            return
        result = self.on_allow(self.host, self.choice.value, self.pw.get())
        if result == "once":
            self.destroy()
            message(self.master, "Falcon Advisor", "Falcon couldn't save this website as always "
                    "allowed, so it's allowed this time only. Add it to Approved Sites in "
                    "FalconGuard Settings.", "warning")
        elif result:
            self.destroy()
        else:
            self.on_wrong()
            message(self, "Falcon Advisor", "The password is incorrect.", "error")
            self.pw.delete(0, "end")
            self.pw.focus_set()


class TimesUpWindow(tk.Toplevel):
    """Shown on top while screen time is over. The computer keeps locking every few
    seconds. The child can redeem a coupon here; the supervisor can add time."""

    def __init__(self, master, app):
        super().__init__(master, bg=FACE)
        self.withdraw()
        self.app = app
        frame = Bevel(self, RAISED, padx=2, pady=2)
        frame.pack(fill="both", expand=True)
        title_bar(self, frame.body, "Falcon Advisory", lambda: None)
        body = tk.Frame(frame.body, bg=FACE, padx=11, pady=11)
        body.pack(fill="both", expand=True)
        row = tk.Frame(body, bg=FACE)
        row.pack(fill="x")
        icon(row, "clock", 32).pack(side="left", anchor="n")
        txt = tk.Frame(row, bg=FACE)
        txt.pack(side="left", padx=12)
        self.headline = label(txt, bold=True)
        self.headline.pack(fill="x")
        label(txt, f"This computer locks every {LOCK_EVERY_SECONDS} seconds until more time "
                   "is added.").pack(fill="x", pady=(3, 0))

        box = GroupBox(body, "Redeem a coupon")
        box.pack(fill="x", pady=(12, 0))
        r = tk.Frame(box.body, bg=FACE)
        r.pack(fill="x")
        label(r, "Coupon code:").pack(side="left", padx=(0, 8))
        cbox, self.code = entry(r, width=14)
        self.code.config(font=FONT_BOLD)
        cbox.pack(side="left")
        Button(r, "Redeem", command=self.redeem).pack(side="right")

        box = GroupBox(body, "Supervisor")
        box.pack(fill="x", pady=(10, 0))
        g = tk.Frame(box.body, bg=FACE)
        g.pack(fill="x")
        label(g, "Password:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        pbox, self.pw = entry(g, show="*", width=20)
        pbox.grid(row=0, column=1, sticky="w", pady=2)
        label(g, "Extra time:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        self.amount = TimeSpin(g, 30, values=COUPON_VALUES)
        self.amount.grid(row=1, column=1, sticky="w", pady=2)
        Button(g, "Add Time", command=self.add_time).grid(row=1, column=2, padx=(12, 0))
        label(body, f"The lock waits until {GRACE_AFTER_TYPING_SECONDS} seconds after you stop typing here.", fg=GRAYTEXT).pack(
            fill="x", pady=(8, 0))
        for e in (self.code, self.pw):
            e.bind("<Key>", lambda ev: self.app.start_grace())
        self.code.bind("<Return>", lambda e: self.redeem())

    def update_text(self, t):
        if t["blocked_now"] and t["schedule_on"]:
            nxt = t["next_change"]
            head = (f"Screen time isn't allowed right now. It's allowed again at {fmt_hour(nxt)}."
                    if nxt is not None else "Screen time isn't allowed for the rest of today.")
        else:
            head = "Your screen time is over for today."
        self.headline.config(text=head)

    def show(self, t):
        self.update_text(t)
        if not self.winfo_viewable():
            self.update_idletasks()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}"
                          f"+{(self.winfo_screenheight() - h) // 3}")
            self.attributes("-topmost", True)
            self.deiconify()
            self.lift()

    def redeem(self):
        result = self.app.redeem_code(self.code.get())
        if result == "missing":
            message(self, "Redeem a Coupon", "That coupon code doesn't exist.", "error")
        elif result == "used":
            message(self, "Redeem a Coupon", "This coupon has already been used.", "warning")
        elif result:
            self.code.delete(0, "end")
            self.app.after(10, self.app.monitor_tick_now)
            message(self, "Redeem a Coupon", f"You got {fmt_minutes(result)} of extra screen time!")

    def add_time(self):
        if not check_password(self.pw.get(), self.app.saved["password"]):
            self.app.store.log("Wrong supervisor password entered (time's up screen)")
            self.app.grace_until = 0
            self.pw.delete(0, "end")
            message(self, "Supervisor", "The password is incorrect.", "error")
            return
        password = self.pw.get()
        self.pw.delete(0, "end")
        minutes = self.amount.minutes
        if not self.app.add_grant(minutes, password):
            message(self, "Supervisor", "Falcon could not save the extra time.", "error")
            return
        self.app.store.log(f"Extra time given at the time's up screen: +{fmt_minutes(minutes)} today")
        self.app.after(10, self.app.monitor_tick_now)
        message(self, "Supervisor", f"{fmt_minutes(minutes)} of extra screen time was added.")


# --- Sounds -------------------------------------------------------------------------
# Next to falcon.py, or inside FalconGuard.exe when built by build.bat (PyInstaller).
SOUNDS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ERROR_SOUND = SOUNDS / "error.mp3"     # played for every error
LOGON_SOUND = SOUNDS / "logon.mp3"     # played when Falcon starts
LOGOFF_SOUND = SOUNDS / "logoff.mp3"   # played when Falcon closes


def play_sound(path, wait=False, max_wait=10.0):
    """Play an MP3 without extra packages. Does nothing if the file or a player is missing.
    With wait=True (used when Falcon closes) it returns once the sound has finished,
    so Windows doesn't cut it off when the program exits."""
    path = Path(path)
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            import ctypes
            mci = ctypes.windll.winmm.mciSendStringW
            mci("close falcon_sound", None, 0, None)
            mci(f'open "{path}" type mpegvideo alias falcon_sound', None, 0, None)
            mci("play falcon_sound", None, 0, None)
            if wait:
                buf = ctypes.create_unicode_buffer(32)
                end = time.monotonic() + max_wait
                while time.monotonic() < end:
                    mci("status falcon_sound mode", buf, 32, None)
                    if buf.value != "playing":
                        break
                    time.sleep(0.05)
                mci("close falcon_sound", None, 0, None)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", str(path)])
        else:
            for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
                        ["mpg123", "-q"], ["mpv", "--no-video", "--really-quiet"],
                        ["cvlc", "--play-and-exit", "--quiet"]):
                if shutil.which(cmd[0]):
                    subprocess.Popen(cmd + [str(path)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    break
    except Exception:
        pass  # a missing sound must never stop the screen from showing


# --- The app ---------------------------------------------------------------------
class Falcon(tk.Tk):
    def __init__(self, store):
        super().__init__()
        self.withdraw()
        self.report_callback_exception = self._unexpected_error
        setup_fonts(self)
        self.store = store
        self.saved = store.load()
        self._ready = False
        self.mode = None          # "supervisor" (all tabs) or "child" (Dashboard only)
        self.locked = False       # Settings opened without the password: view only
        self.service = None       # ServiceClient once the supervisor password was typed
        self.sub = store.load_subscription()     # last known subscription details
        self._sub_result = None
        self._sub_running = False
        self._sub_done = False
        self._sub_callbacks = []
        self.configure(bg=FACE)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def _build(self, mode):
        self.mode = mode
        child = mode in ("child", "monitor")
        title = "Falcon Dashboard" if child else "Falcon Parental Controls"
        self.title(title)
        frame = Bevel(self, RAISED, padx=2, pady=2)
        frame.pack(fill="both", expand=True)
        outer = frame.body
        title_bar(self, outer, title,
                  self.hide_dashboard if mode == "monitor" else self.cancel, self.about)

        body = tk.Frame(outer, bg=FACE, padx=6, pady=8)
        body.pack(fill="both", expand=True)

        self.banner = tk.Frame(body, bg=FACE)        # subscription warnings (supervisor only)
        self.banner.pack(fill="x")
        self.tabs = TabControl(body)
        self.tabs.pack(fill="both", expand=True)
        self._dashboard(self.tabs.add("Dashboard"))
        if not child:
            self._content_filter(self.tabs.add("Content Filter"))
            self._sites(self.tabs.add("Approved Sites"))
            self._general(self.tabs.add("General"))
            self._time(self.tabs.add("Time Management"))
            self._zones(self.tabs.add("Zones"))
            self._account(self.tabs.add("My Account"))

        foot = tk.Frame(body, bg=FACE)
        foot.pack(fill="x", pady=(7, 0))
        if mode == "monitor":
            Button(foot, "Hide", default=True, command=self.hide_dashboard).pack(side="right")
            Button(foot, "Quit Falcon...", command=self.quit_monitor).pack(side="right", padx=6)
        elif child:
            Button(foot, "Close", default=True, command=self.cancel).pack(side="right")
        else:
            self.apply_btn = Button(foot, "Apply", disabled=True, command=self.apply)
            self.apply_btn.pack(side="right")
            Button(foot, "Cancel", command=self.cancel).pack(side="right", padx=6)
            Button(foot, "OK", default=True, command=self.ok).pack(side="right")
            self.lock_bar = tk.Frame(foot, bg=FACE)
            self.lock_bar.pack(side="left", fill="y")
            self.lock_icon = icon(self.lock_bar, "lock")
            self.lock_icon.pack(side="left")
            self.edit_btn = Button(self.lock_bar, "Edit Settings...", command=self.unlock)
            self.edit_btn.pack(side="left", padx=(6, 0))

        status = tk.Frame(outer, bg=FACE)
        status.pack(fill="x")
        panel = Bevel(status, THIN_SUNKEN, padx=3, pady=2)
        panel.pack(side="left", padx=(0, 2))
        self.status_state = label(panel.body, width=16)
        self.status_state.pack(fill="x")
        panel = Bevel(status, THIN_SUNKEN, padx=3, pady=2)
        panel.pack(side="left", fill="x", expand=True)
        label(panel.body, f"Draft {VERSION}").pack(fill="x")

        self._update_enabled_ui()
        if not child:
            self._update_password_ui()
            self._install_lock()
            self._show_lock_state()
            self._heartbeat()
        self._show_subscription()
        self.check_subscription()
        self._ready = True

    # --- view only / edit -------------------------------------------------------------
    def _install_lock(self):
        """While Settings is view only, clicks and keys that would change something ask for
        the supervisor password instead. Looking around (tabs, zones, lists) still works."""
        self.bind_class("FalconLocked", "<ButtonPress-1>", self._locked_event)
        for seq in ("<ButtonRelease-1>", "<B1-Motion>", "<Key>", "<MouseWheel>",
                    "<Button-4>", "<Button-5>"):
            self.bind_class("FalconLocked", seq, lambda e: "break" if self.locked else None)
        view_ok = {self.check_btn}
        for _, ic, lb in self.zone_cells:
            view_ok |= {ic, lb, ic.master}

        def walk(w):
            if w in view_ok:
                return
            if not isinstance(w, (tk.Scrollbar,)):
                w.bindtags(("FalconLocked",) + tuple(t for t in w.bindtags() if t != "FalconLocked"))
            for c in w.winfo_children():
                walk(c)
        for page in self.tabs.pages[1:]:            # the Dashboard tab works like the child's view
            walk(page)

    def _locked_event(self, event):
        if not self.locked:
            return None
        self.after_idle(self.unlock)
        return "break"

    def unlock(self):
        """Ask for the supervisor password to change settings. True when unlocked."""
        if not self.locked:
            return True
        if getattr(self, "_unlocking", False):
            return False
        self._unlocking = True
        try:
            dlg = UnlockDialog(self, self.store, self.saved, "Type the supervisor password to\n"
                               "change FalconGuard settings.", what="settings")
            ok = dlg.show()
        finally:
            self._unlocking = False
        if not ok:
            return False
        self.locked = False
        self.service = ServiceClient(dlg.password)
        self.store.log("Supervisor unlocked Settings")
        self.store.supervisor_heartbeat()
        self._show_lock_state()
        return True

    def _show_lock_state(self):
        if not hasattr(self, "lock_bar"):
            return
        if self.locked:
            draw_icon(self.lock_icon, "lock", 16, FACE)
            self.edit_btn.set_text("Edit Settings...")
            self.edit_btn.set_enabled(True)
        else:
            draw_icon(self.lock_icon, "key", 16, FACE)
            self.edit_btn.set_text("Editing")
            self.edit_btn.set_enabled(False)
            self._update_enabled_ui()

    # --- subscription -----------------------------------------------------------------
    def expired(self):
        return subscription_expired(self.sub)

    def protection_on(self):
        """Falcon's switch is on and the subscription hasn't expired."""
        return self.saved["enabled"] and not self.expired()

    def check_subscription(self, then=None):
        """Read the gist in the background (it can take a few seconds). If a check is
        already running, wait for that one instead of starting another."""
        if then:
            self._sub_callbacks.append(then)
        if self._sub_running:
            return
        self._sub_running = True
        self._sub_done = False

        def work():
            try:
                self._sub_result = self.store.refresh_subscription() if self.mode == "supervisor" \
                    else (fetch_subscription() or self.store.load_subscription())
            finally:
                self._sub_done = True
        self._sub_thread = threading.Thread(target=work, daemon=True)
        self._sub_thread.start()
        self.after(300, self._subscription_arrived)

    def _subscription_arrived(self):
        if not self._sub_done:
            self.after(300, self._subscription_arrived)
            return
        was_expired = self.expired()
        if self._sub_result:
            self.sub = self._sub_result
        self._sub_result, self._sub_running = None, False
        callbacks, self._sub_callbacks = self._sub_callbacks, []
        if self.expired() and not was_expired:
            self.store.log("Subscription expired: protection turned off")
        self._show_subscription()
        for cb in callbacks:
            cb()

    def _show_subscription(self):
        """Banner (3 days before: warning, 1 day before: critical, expired: critical)."""
        if not hasattr(self, "banner"):
            return
        for w in self.banner.winfo_children():
            w.destroy()
        st = subscription_status(self.sub)
        if self.mode == "supervisor" and st["level"] in ("warning", "critical", "expired"):
            critical = st["level"] != "warning"
            bg, fg = ("#c00000", WINDOW) if critical else ("#ffffe1", INK)
            box = Bevel(self.banner, THIN_SUNKEN, bg=bg, padx=6, pady=5)
            box.pack(fill="x", pady=(0, 7))
            icon(box.body, "error" if critical else "warning", 16, bg=bg).pack(side="left", anchor="n")
            label(box.body, st["text"], bold=critical, bg=bg, fg=fg, wraplength=430).pack(
                side="left", padx=(8, 0), fill="x")
        self._update_enabled_ui()
        if hasattr(self, "acct_values"):
            self._refresh_account()

    def _heartbeat(self):
        """While the supervisor has unlocked editing, tell the monitor not to lock the screen."""
        if not self.locked:
            self.store.supervisor_heartbeat()
        self.after(10_000, self._heartbeat)

    def _unexpected_error(self, exc, value, tb):
        """Any error Falcon didn't expect: play error.mp3, write it to the log, tell the user."""
        import traceback
        detail = "".join(traceback.format_exception(exc, value, tb))
        try:
            self.store.log(f"Unexpected error: {exc.__name__}: {value}")
            with open(self.store.folder / "errors.txt", "a", encoding="utf-8") as f:
                f.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n{detail}\n")
        except OSError:
            pass
        try:
            message(self, "Falcon", f"Something went wrong: {value}\n\nThe details were saved "
                    "to errors.txt in Falcon's settings folder.", "error")
        except tk.TclError:
            play_sound(ERROR_SOUND)

    # --- start-up ----------------------------------------------------------------
    def start(self, dashboard_only=False, monitor=False, resume=None):
        """Play the logon sound, ask for the supervisor password (if one is set),
        then show the window. The child can open the Dashboard without a password.
        monitor=True runs Falcon in the background on the child's account."""
        if monitor:
            return self._start_monitor()
        play_sound(LOGON_SOUND)
        mode = "supervisor"
        resumed = self._resume(resume) if resume else None
        if dashboard_only:
            mode = "child"
        elif resumed:
            pass                                 # just saved with administrator rights
        elif self.saved["password"]:
            # Anyone can look at the settings; changing them needs the supervisor password.
            self.locked = True
            self.store.log("Settings opened (view only)")
        else:
            self.store.log("Settings opened (no supervisor password set)")
        if mode == "child":
            self.store.log("Dashboard opened")
        self._build(mode)
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}"
                      f"+{(self.winfo_screenheight() - h) // 3}")
        self.deiconify()
        self.after(10, self._show_in_taskbar)
        if resumed:
            self.after(300, lambda: message(self, "Falcon", "Your changes were saved."))
        elif resumed is False:
            self.after(300, lambda: message(self, "Falcon", "Falcon couldn't find the changes from "
                                            "before the restart. Please make them again.", "warning"))
        return True

    def _resume(self, path):
        """Restarted as administrator: save the changes handed over by the first window.
        Returns True if saved, False if there was nothing usable."""
        new = read_handoff(path)
        if new is None:
            return False
        old = self.store.load()
        try:
            self.store.save(new)
        except OSError as e:
            message(self, "Falcon", f"Falcon could not save its settings.\n\n{e}", "error")
            return False
        if new["password"] and not old["password"]:
            self.store.log("Supervisor password created")
        elif new["password"] != old["password"]:
            self.store.log("Supervisor password changed")
        if new["enabled"] != old["enabled"]:
            self.store.log("Falcon turned on" if new["enabled"] else "Falcon turned off")
        for change in describe_changes(old, new):
            self.store.log(change)
        self.saved = self.store.load()
        self.sub = self.store.load_subscription()
        return True

    def _show_in_taskbar(self):
        """Keep a taskbar entry on Windows even without the native frame."""
        try:
            ico = falcon_icon_path()
            if ico:
                self.iconbitmap(default=str(ico))          # the falcon on the taskbar button
        except tk.TclError:
            pass
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            GWL_EXSTYLE, WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = -20, 0x40000, 0x80
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            self.withdraw()
            self.after(10, self.deiconify)
        except (AttributeError, OSError):
            pass  # not on Windows

    # --- saving --------------------------------------------------------------------
    def collect(self):
        """The settings as they are in the window right now."""
        return {
            "version": 1,
            "enabled": self.saved["enabled"],
            "password": self.saved["password"],
            "hint": self.saved["hint"],
            "web_filter_level": self.rating_bar.value,
            "sites": [dict(s) for s in self.sites],
            "options": {k: cb.value for k, cb in self.opts.items()},
            "blocked_hours": [[h for h in range(24) if self.hour_grid.blocked[d][h]]
                              for d in range(7)],
            "daily_limits": [s.minutes for s in self.limits],
            "zones": {k: {"level": z["level"], "sites": list(z["sites"])}
                      for k, z in self.zone_state.items()},
            "coupons": copy.deepcopy(self.saved["coupons"]),
            "bonus": dict(self.saved["bonus"]),
            "child_account": self.child_entry.get().strip()[:100],
            **({"grant_wrap": self.saved["grant_wrap"]} if "grant_wrap" in self.saved else {}),
        }

    def dirty(self):
        return self.mode == "supervisor" and self.collect() != self.saved

    def changed(self):
        if self._ready:
            self.apply_btn.set_enabled(self.dirty())

    def _write(self, settings):
        """Save the settings file directly, or through the background service (with the
        supervisor password) when this account isn't allowed to write it."""
        try:
            self.store.save(settings)
        except PermissionError:
            if not self.service:
                raise
            self.service.request("/api/settings/save", {"settings": settings})

    def _save(self, settings):
        """Write settings. Coupons and bonus time are always taken from disk, so a coupon
        redeemed on the Dashboard in another window is never undone."""
        try:
            fresh = self.store.load()
            settings["coupons"], settings["bonus"] = fresh["coupons"], fresh["bonus"]
            self.saved["coupons"], self.saved["bonus"] = fresh["coupons"], fresh["bonus"]
            self._write(settings)
            return True
        except PermissionError:
            self._offer_admin(settings)
            return False
        except OSError as e:
            message(self, "Falcon", f"Falcon could not save its settings.\n\n{e}", "error")
            return False

    def _offer_admin(self, settings=None):
        """This account can't write the protected settings: offer to restart as administrator,
        handing over every unsaved change so nothing has to be typed again."""
        if not IS_WINDOWS:
            message(self, "Falcon", "Falcon isn't allowed to save its settings here.", "error")
            return
        if message(self, "Falcon", "Falcon's settings are protected. Saving them needs "
                   "administrator rights.\n\nRestart Falcon as administrator? Your changes "
                   "will be kept and saved.", "question", ("Yes", "No")) != "Yes":
            return
        if self.mode == "supervisor" and self._ready:
            handoff = self.collect()             # everything in the window, saved or not
            if settings:
                for k in ("coupons", "bonus", "password", "hint", "enabled"):
                    if k in settings:
                        handoff[k] = settings[k]
                if "grant_wrap" in settings:
                    handoff["grant_wrap"] = settings["grant_wrap"]
        else:
            handoff = settings or self.store.load()
        try:
            path = write_handoff(handoff)
        except OSError as e:
            message(self, "Falcon", f"Falcon couldn't keep your changes for the restart.\n\n{e}", "error")
            return
        if not relaunch_as_admin(["--resume", path]):
            try:
                os.remove(path)
            except OSError:
                pass
            return
        if self.mode != "monitor":
            self.close(sound=False)          # the background monitor keeps running

    def apply(self):
        if not self.dirty():
            return True
        if not self.ensure_password():
            message(self, "Falcon", "Your changes were not saved. Create a supervisor "
                    "password to save Falcon settings.", "warning")
            return False
        new = self.collect()
        if not self._save(new):
            return False
        for change in describe_changes(self.saved, new):
            self.store.log(change)
        self.saved = new
        self.changed()
        return True

    def ok(self):
        if self.locked or self.apply():
            self.store.log("Settings closed")
            self.close()

    def cancel(self):
        if self.dirty():
            self.store.log("Settings closed without saving changes")
        else:
            self.store.log("Settings closed")
        self.close()

    def close(self, sound=True):
        """Hide every window, play the logoff sound to the end, then quit."""
        t = getattr(self, "_sub_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=10)       # quitting mid-request can crash Python while it shuts down
        if self.mode == "supervisor":
            self.store.end_supervisor_heartbeat()
        if self.mode == "monitor":
            self._stopping = True
            if getattr(self, "tray", None):
                self.tray.stop()
            try:
                self.store.save_usage(self.usage)
            except OSError:
                pass
        self.withdraw()
        for w in self.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.withdraw()
        self.update()
        if sound:
            play_sound(LOGOFF_SOUND, wait=True)
        self.destroy()

    def about(self):
        message(self, "About Falcon",
                f"FalconGuard for Windows\nDraft {VERSION}\n\n"
                f"This is an official release of FalconGuard. "
                f"Configuration files are locked and cannot be edited, "
                f"but can be viewed in the settings folder:\n\n{self.store.folder}")

    # --- supervisor password ---------------------------------------------------------
    def ensure_password(self):
        """Make sure a supervisor password exists, asking to create one if needed."""
        if self.saved["password"]:
            return True
        result = PasswordDialog(self).show()
        if not result:
            return False
        self.saved["password"], self.saved["hint"], plain = result
        self._set_grant_wrap(plain)
        if not self._save(self.saved):
            self.saved["password"], self.saved["hint"] = None, ""
            self.saved.pop("grant_wrap", None)
            return False
        self.store.log("Supervisor password created")
        self._update_password_ui()
        return True

    def change_password(self):
        if not self.saved["password"]:
            if self.ensure_password():
                message(self, "Supervisor Password", "The supervisor password was created.")
            return
        pdlg = PasswordDialog(self, self.saved["password"], self.saved["hint"])
        result = pdlg.show()
        if not result:
            return
        self._last_password_dialog_old = pdlg.old_plain
        dlg_old = getattr(self, "_last_password_dialog_old", None)
        if not settings_writable(self.store) and self.service:
            # protected install, no admin rights: the background service changes it
            try:
                self.service.request("/api/password", {"old": dlg_old, "new": result[2],
                                                       "confirm": result[2], "hint": result[1]})
            except OSError as e:
                message(self, "Supervisor Password", str(e), "error")
                return
            self.service = ServiceClient(result[2])
            self.saved = self.store.load()
            self.store.log("Supervisor password changed")
            message(self, "Supervisor Password", "The supervisor password was changed.")
            return
        old = (self.saved["password"], self.saved["hint"], self.saved.get("grant_wrap"))
        self.saved["password"], self.saved["hint"], plain = result
        self._set_grant_wrap(plain)
        if not self._save(self.saved):
            self.saved["password"], self.saved["hint"] = old[:2]
            if old[2]:
                self.saved["grant_wrap"] = old[2]
            return
        self.store.log("Supervisor password changed")
        message(self, "Supervisor Password", "The supervisor password was changed.")

    def _set_grant_wrap(self, password):
        """Lock the grant key with the supervisor password, so time can be added on the
        child's account (time's up screen) in a way the guard can check."""
        key = self.store.grant_key(create=True)
        if key:
            self.saved["grant_wrap"] = wrap_grant_key(key, password)

    def _update_password_ui(self):
        has = bool(self.saved["password"])
        self.pw_btn.set_text("Change Password..." if has else "Create Password...")
        self.pw_text.config(text=(
            "The supervisor password lets you change Falcon\n"
            "settings or turn Falcon on or off. To change the\n"
            "supervisor password, click Change Password." if has else
            "No supervisor password is set, so anyone can\n"
            "change these settings. Click Create Password to\n"
            "protect them."))

    # --- on / off ------------------------------------------------------------------
    def toggle_enabled(self):
        if not self.saved["enabled"] and self.expired():
            message(self, "Falcon", "Your Falcon subscription has expired, so protection can't "
                    "be turned on. Renew your subscription, then click Check Now on the My "
                    "Account tab.", "error")
            return
        if not self.ensure_password():
            return
        self.saved["enabled"] = not self.saved["enabled"]
        if not self._save(self.saved):
            self.saved["enabled"] = not self.saved["enabled"]
            return
        on = self.saved["enabled"]
        self.store.log("Falcon turned on" if on else "Falcon turned off")
        self._update_enabled_ui()
        if on and not self.saved.get("child_account"):
            message(self, "Falcon", "Falcon is on. Next, choose the child's Windows account "
                    "under Child's account on this tab, and click Apply.", "warning")
        elif on:
            message(self, "Falcon", f"Falcon is on. Screen time and the web filter are "
                    f"enforced on {self.saved['child_account']}'s account while Falcon runs there.")

    def _refresh_monitor_status(self):
        """General tab: is FalconGuard running on the child's account right now?"""
        if not getattr(self, "monitor_text", None) or not self.monitor_text.winfo_exists():
            return
        running, text = monitor_status(self.store, self.saved)
        draw_icon(self.monitor_icon, "trusted" if running else "warning", 16, FACE)
        self.monitor_text.config(text=text)
        self.after(10_000, self._refresh_monitor_status)

    def _update_enabled_ui(self):
        if not hasattr(self, "status_state"):
            return
        expired = self.expired()
        on = self.saved["enabled"] and not expired
        self.status_state.config(text="Expired - off" if expired else
                                 "Falcon is on" if on else "Falcon is off")
        if self.mode != "supervisor":
            return
        self.enabled_text.config(text=(
            "Protection is off because your\nsubscription has expired." if expired else
            "Falcon is on. Screen time and the web\nfilter are enforced on the child's\naccount."
            if on else
            "Falcon is off. Nothing is counted\nor blocked."))
        self.toggle_btn.set_text("Turn Off..." if self.saved["enabled"] and not expired else "Turn On...")
        self.toggle_btn.set_enabled(not expired)

    # --- Dashboard tab -------------------------------------------------------------------
    def _dashboard(self, page):
        child = self.mode == "child"
        self.time_box = GroupBox(page, "Screen time today")
        self.time_box.pack(fill="x")
        top = tk.Frame(self.time_box.body, bg=FACE)
        top.pack(fill="x")
        icon(top, "clock", 32).pack(side="left", anchor="n")
        big = tk.Frame(top, bg=FACE)
        big.pack(side="left", padx=10)
        self.left_big = tk.Label(big, font=(FONT.actual("family"), -26, "bold"), bg=FACE,
                                 fg=SELECT, anchor="w", bd=0, padx=0, pady=0)
        self.left_big.pack(anchor="w")
        self.left_sub = label(big)
        self.left_sub.pack(anchor="w")
        self.progress = tk.Canvas(self.time_box.body, height=18, bg=FACE,
                                  highlightthickness=0, bd=0)
        self.progress.pack(fill="x", pady=(10, 8))
        self.progress.bind("<Configure>", lambda e: self._draw_progress())
        grid = tk.Frame(self.time_box.body, bg=FACE)
        grid.pack(fill="x")
        self.dash_values = {}
        for r, key in enumerate(("Daily limit:", "Extra time today:", "Time used:",
                                 "Schedule:")):
            label(grid, key).grid(row=r, column=0, sticky="w", pady=1)
            v = label(grid)
            v.grid(row=r, column=1, sticky="w", padx=(12, 0), pady=1)
            self.dash_values[key] = v
        self.used_note = label(self.time_box.body, fg=GRAYTEXT)
        self.used_note.pack(fill="x", pady=(6, 0))

        box = GroupBox(page, "Redeem a coupon")
        box.pack(fill="x", pady=(10, 0))
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "coupon", 32).pack(side="left", anchor="n")
        label(row, "Got a coupon from your parent? Type the code to get\n"
                   "extra screen time today.").pack(side="left", padx=10)
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x", pady=(8, 0))
        label(row, "Coupon code:").pack(side="left", padx=(0, 8))
        cbox, self.coupon_entry = entry(row, width=14)
        self.coupon_entry.config(font=FONT_BOLD)
        cbox.pack(side="left")
        self.redeem_btn = Button(row, "Redeem", disabled=True, command=self.redeem)
        self.redeem_btn.pack(side="left", padx=(10, 0))
        self.coupon_entry.bind("<KeyRelease>", lambda e: self.redeem_btn.set_enabled(
            bool(self.coupon_entry.get().strip())))
        self.coupon_entry.bind("<Return>", lambda e: self.redeem())

        box = GroupBox(page, "Coupons")
        box.pack(fill="x", pady=(10, 0))
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x")
        self.coupon_summary = label(row)
        self.coupon_summary.pack(side="left")
        Button(row, "Manage Coupons...",
               command=lambda: (CouponDialog(self, self).show(),
                                self._refresh_dashboard())).pack(side="right")
        self._refresh_dashboard()
        self._tick()

    def _tick(self):
        """Keep the time left current while the window is open."""
        self._refresh_dashboard()
        self.after(30_000, self._tick)

    def _bonus(self):
        return bonus_today(self.saved, self.store.load_state(), self.store.load_guard())

    def _redeemed(self):
        return redemption_times(self.saved, self.store.load_state(), self.store.load_guard())

    def _used_seconds(self):
        if self.mode == "monitor":
            return int(self.usage["seconds"])
        return self.store.used_seconds_today()

    def _refresh_dashboard(self):
        t = screen_time(self.saved, used_seconds=self._used_seconds(), bonus=self._bonus())
        acct = self.saved.get("child_account")
        self.used_note.config(text=(f"Time counts while {acct} is using the mouse or keyboard."
                                    if acct else "Time used is counted once the supervisor "
                                    "chooses the child's account."))
        self.dash_time = t
        self.time_box.caption.config(text=f"Screen time today ({t['day']})")
        self.left_big.config(text=fmt_minutes(t["left"]) if t["left"] else "No time")
        if not t["left"]:
            sub = "There is no screen time left today."
        elif t["blocked_now"] and t["next_change"] is not None:
            sub = f"left today, from {fmt_hour(t['next_change'])}"
        else:
            sub = "left today"
        self.left_sub.config(text=sub)
        v = self.dash_values
        v["Daily limit:"].config(text=fmt_minutes(t["limit"]))
        v["Extra time today:"].config(text=f"+{fmt_minutes(t['bonus'])}" if t["bonus"] else "None")
        v["Time used:"].config(text=fmt_minutes(t["used"]))
        if not t["schedule_on"]:
            sched = "Not used"
        elif t["blocked_now"]:
            sched = (f"Blocked now, allowed from {fmt_hour(t['next_change'])}"
                     if t["next_change"] is not None else "Blocked for the rest of the day")
        else:
            sched = (f"Allowed now, blocked from {fmt_hour(t['next_change'])}"
                     if t["next_change"] is not None else "Allowed for the rest of the day")
        v["Schedule:"].config(text=sched)
        used = self._redeemed()
        ready = sum(1 for c in self.saved["coupons"] if c["code"] not in used)
        self.coupon_summary.config(
            text=f"{ready} coupon{'s' if ready != 1 else ''} ready to use. Open\n"
                 "Manage Coupons to see and copy the codes.")
        self._draw_progress()
        self._refresh_extra()

    def _draw_progress(self):
        """Windows 2000 progress bar: sunken box filled with navy blocks."""
        c = self.progress
        c.delete("all")
        w = c.winfo_width()
        if w < 10 or not hasattr(self, "dash_time"):
            return
        t = self.dash_time
        total = t["budget"] if t["budget"] is not None else max(t["allowed_left"], 1)
        frac = 0 if total <= 0 else max(0.0, min(1.0, t["left"] / total))
        c.create_line(0, 17, 0, 0, w - 1, 0, fill=SHADOW)
        c.create_line(w - 1, 0, w - 1, 17, -1, 17, fill=HILIGHT)
        c.create_rectangle(1, 1, w - 2, 16, fill=WINDOW, outline="")
        inner = w - 6
        x, end = 3, 3 + inner * frac
        while x + 8 <= end + 0.5:
            c.create_rectangle(x, 3, x + 7, 14, fill=SELECT, outline="")
            x += 10

    def _modify_disk(self, change):
        """Apply change() to the settings on disk (coupons and bonus only) and save."""
        try:
            fresh = self.store.load()
            result = change(fresh)
            self._write(fresh)
        except PermissionError:
            self._offer_admin(fresh)
            return None
        except OSError as e:
            message(self, "Falcon", f"Falcon could not save its settings.\n\n{e}", "error")
            return None
        self.saved["coupons"], self.saved["bonus"] = fresh["coupons"], fresh["bonus"]
        return result

    def create_coupon(self, minutes, note):
        code = self._modify_disk(lambda s: add_coupon(s, minutes, note))
        if code:
            self.store.log(f"Coupon {code} created (+{fmt_minutes(minutes)})")
            self._refresh_dashboard()
        return code

    def delete_coupon(self, code):
        if self._modify_disk(lambda s: remove_coupon(s, code)):
            self.store.log(f"Coupon {code} deleted")
            self._refresh_dashboard()

    def redeem(self):
        typed = self.coupon_entry.get().strip()
        if not typed:
            return
        result = self.redeem_code(typed)
        if result is None:
            return
        if result == "missing":
            message(self, "Redeem a Coupon", "That coupon code doesn't exist. Check the code "
                    "and try again. Codes look like K7PD-3XQM.", "error")
            self.coupon_entry.select_range(0, "end")
            return
        if result == "used":
            message(self, "Redeem a Coupon", "This coupon has already been used.", "warning")
            return
        self.coupon_entry.delete(0, "end")
        self.redeem_btn.set_enabled(False)
        self._refresh_dashboard()
        t = self.dash_time
        extra = ""
        if t["budget"] is not None and t["left"] < t["budget"]:
            extra = ("\n\nThe schedule still ends your screen time at "
                     f"{fmt_hour(t['next_change'] or 24)}, so you can't use all of it today.")
        message(self, "Redeem a Coupon", f"You got {fmt_minutes(result)} of extra screen time! "
                f"You have {fmt_minutes(t['left']) if t['left'] else 'no time'} left today.{extra}")

    def redeem_code(self, typed):
        """Redeem a coupon. Returns its minutes, "missing", "used" or None (save failed).
        The redemption is written to state.json, which the child's account may write; a
        record only counts if its code is a real coupon from the protected settings."""
        code = clean_code(typed.strip())
        if code and not any(c["code"] == code for c in self.saved["coupons"]):
            self.saved["coupons"] = self.store.load()["coupons"]   # maybe just made
        try:
            result, code = redeem_coupon(self.store, self.saved, typed)
        except OSError as e:
            message(self, "Falcon", f"Falcon could not save the coupon.\n\n{e}", "error")
            return None
        if result == "missing":
            self.store.log("Wrong coupon code entered")
        elif result == "used":
            self.store.log(f"Used coupon {code} entered again")
        else:
            self.store.log(f"Coupon {code} redeemed: +{fmt_minutes(result)} screen time today")
            self._bonus_cache = None
            self._refresh_dashboard()
        return result

    def add_grant(self, minutes, password):
        """Time's up screen: the supervisor typed the password to add time on the child's
        account. Saved as a signed grant the guard can check. Returns True if saved."""
        key = unwrap_grant_key(self.saved.get("grant_wrap", {}), password)
        if key is None:                         # not set up yet: fall back to settings
            if not settings_writable(self.store):
                try:                            # protected install: the background service saves it
                    ServiceClient(password).request("/api/extra", {"minutes": minutes})
                    self.saved = self.store.load()
                    self._settings_mtime = self._mtime()
                    self._bonus_cache = None
                    return True
                except OSError:
                    pass
            return self.add_bonus(minutes) is not None
        try:
            st = self.store.load_state()
            st["grants"].append(make_grant(key, minutes))
            self.store.save_state(st)
        except OSError:
            return False
        self._bonus_cache = None
        return True

    # --- Background monitor (runs on the child's account) -------------------------------
    def _start_monitor(self):
        child = self.saved.get("child_account", "")
        if not child or child.lower() != current_user().lower():
            self.destroy()             # not the child's account: do nothing at all
            return False
        if not self._single_instance():
            self.destroy()
            return False
        play_sound(LOGON_SOUND)
        self.store.log(f"Falcon started on {current_user()}'s account")
        self.usage = self.store.load_usage()
        self.usage["seconds"] = self.store.used_seconds_today()   # never less than the guard's copy
        self._stopping = False
        self.checker = SiteChecker()
        self.events = queue.Queue()
        self.last_tick = time.monotonic()
        self.last_lock = 0.0
        self.grace_until = 0.0
        self.grace_next = 0.0
        self.was_out = False
        self.timesup = None
        self.block_dialog = None
        self.tick_count = 0
        self.diag = {"version": VERSION}
        self._restart_seen = self.store.load_guard().get("browsers_restarted", 0)
        self._protecting = False
        self._bonus_cache = None
        self._settings_mtime = self._mtime()
        self._policy_state = None
        self._apply_policies()
        self._load_signed_allows()
        try:
            self.store.save_usage(self.usage)       # heartbeat straight away for the guard
        except OSError:
            pass
        self._build("monitor")
        today = datetime.now().date().isoformat()
        if self.usage.get("dashboard_date") != today:     # first logon today: show it
            self.usage["dashboard_date"] = today
            self.store.save_usage(self.usage)
            self.update_idletasks()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}"
                          f"+{(self.winfo_screenheight() - h) // 3}")
            self.deiconify()
            self.after(10, self._show_in_taskbar)
        threading.Thread(target=self._watch_dns, daemon=True).start()
        self.tray = TrayIcon()
        self.tray.start()
        self._update_tray_tip()
        self.after(1000, self._monitor_tick)
        self.after(500, self._poll_events)
        return True

    def show_dashboard(self):
        """Tray icon: bring the Dashboard back."""
        self._refresh_dashboard()
        self.update_idletasks()
        if not self.winfo_viewable():
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}"
                          f"+{(self.winfo_screenheight() - h) // 3}")
            self.deiconify()
            self.after(10, self._show_in_taskbar)
        self.lift()
        take_focus(self)

    def _update_tray_tip(self):
        if not getattr(self, "tray", None):
            return
        if not self.protection_on():
            tip = "FalconGuard: protection is off"
        else:
            t = screen_time(self.saved, used_seconds=self.usage["seconds"],
                            bonus=self._bonus_cache if self._bonus_cache is not None else self._bonus())
            tip = (f"FalconGuard: {fmt_minutes(t['left'])} left today" if t["left"]
                   else "FalconGuard: no screen time left today")
        self.tray.set_tip(tip)

    def _single_instance(self):
        """Only one monitor per Windows logon."""
        if IS_WINDOWS:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)   # reliable GetLastError
            self._mutex = k32.CreateMutexW(None, False, "Local\\FalconMonitor")
            return ctypes.get_last_error() != 183                   # ERROR_ALREADY_EXISTS
        try:
            import fcntl
            self._lockfile = open(self.store.folder / f"monitor-{current_user()}.lock", "w")
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ImportError):
            return False

    def _mtime(self):
        try:
            return self.store.settings_path.stat().st_mtime
        except OSError:
            return None

    def _apply_policies(self):
        state = (self.protection_on(), self.saved["web_filter_level"])
        if state != self._policy_state:
            self._policy_state = state
            try:
                changed = apply_browser_policies(state[1], state[0])
            except Exception:
                return      # a standard account can't write policies; the guard does it
            if changed and state[0]:
                # browsers only read their settings when they start: restart them once
                kill_browsers()
                self.store.log("Browsers closed so FalconGuard's settings take effect")
                notify("Falcon Advisory", "Your browser was closed so FalconGuard's settings "
                       "can take effect. You can open it again.")

    def hide_dashboard(self):
        self.withdraw()

    def quit_monitor(self):
        if self.saved["password"] and not UnlockDialog(
                self, self.store, self.saved, "Type the supervisor password to close Falcon.\n"
                "Screen time and the web filter stop until the next logon.").show():
            return
        self.store.log(f"Falcon closed by the supervisor on {current_user()}'s account")
        self.close()

    def start_grace(self):
        """Each key typed on the time's up screen holds off the lock for 7 more seconds,
        so it locks 7 seconds after typing stops. Typing can hold it off for at most
        2 minutes, once every 5 minutes (so a held-down key can't keep the PC unlocked)."""
        now = time.time()
        if now >= self.grace_next:                       # a new stretch of typing
            self.grace_start = now
            self.grace_next = now + 300
        if now - getattr(self, "grace_start", now) < GRACE_MAX_SECONDS:
            self.grace_until = min(now + GRACE_AFTER_TYPING_SECONDS,
                                   self.grace_start + GRACE_MAX_SECONDS)

    def monitor_tick_now(self):
        self._monitor_tick(reschedule=False)

    def _monitor_tick(self, reschedule=True):
        if self._stopping:
            return
        try:
            self._tick_body()
        finally:
            if reschedule:
                self.after(1000, self._monitor_tick)

    def _tick_body(self):
        m = self._mtime()
        if m != self._settings_mtime:                     # settings changed elsewhere
            self._settings_mtime = m
            self.saved = self.store.load()
            self._bonus_cache = None                      # extra time may have been added
            self._apply_policies()
        now_m = time.monotonic()
        elapsed = min(now_m - self.last_tick, 5.0)        # don't count sleep/hibernate
        self.last_tick = now_m
        today = datetime.now().date().isoformat()
        if self.usage.get("date") != today:               # past midnight: a new day
            self.usage = self.store.load_usage()
        enabled = self.protection_on()
        urgent = subscription_status(self.sub)["level"] in ("critical", "expired")
        if self.tick_count and self.tick_count % (300 if urgent else SUBSCRIPTION_REFRESH_SECONDS) == 0:
            self.check_subscription(then=self._apply_policies)
        self._protecting = enabled
        active = session_active()
        idle = idle_seconds()
        if enabled and active and idle < IDLE_LIMIT_SECONDS:
            # Only count time that was actually allowed. While the time is up (between locks,
            # or typing on the time's up screen) nothing counts, or extra time given later
            # would be eaten up by the time spent locked out.
            if self._bonus_cache is None:
                self._bonus_cache = self._bonus()
            now_t = screen_time(self.saved, used_seconds=self.usage["seconds"], bonus=self._bonus_cache)
            if idle > IDLE_HOLD_SECONDS:
                # Quiet for a while: hold this time back. If the child comes back within
                # 1 min 15 s it's counted then; if not, they were away and none of it counts.
                self._held_seconds = getattr(self, "_held_seconds", 0.0) + elapsed
            else:
                add = getattr(self, "_held_seconds", 0.0) + elapsed
                self._held_seconds = 0.0
                self.usage["seconds"] += min(add, max(0, now_t["run_seconds"]))
        else:
            self._held_seconds = 0.0              # away (or locked): the quiet time doesn't count
        self.tick_count += 1
        if self.tick_count % 30 == 0:
            self._load_signed_allows()
            restarted = self.store.load_guard().get("browsers_restarted", 0)
            if restarted > self._restart_seen:
                self._restart_seen = restarted
                notify("Falcon Advisory", "Your browser was closed so FalconGuard's settings "
                       "can take effect. You can open it again.")
        if self.tick_count % 15 == 0:
            self.usage["diag"] = dict(self.diag, enabled=self.saved["enabled"], expired=self.expired(),
                                      sub_expires=(self.sub or {}).get("expires"),
                                      level=self.saved["web_filter_level"])
            try:
                self.store.save_usage(self.usage)
            except OSError:
                pass
        if self.tick_count % 5 == 0 or self._bonus_cache is None:
            self._bonus_cache = self._bonus()
        t = screen_time(self.saved, used_seconds=self.usage["seconds"], bonus=self._bonus_cache)
        if self.tick_count % 30 == 0:
            self._update_tray_tip()
        if self.tick_count % 5 == 0 and self.winfo_viewable():
            self._refresh_dashboard()
        if not enabled:
            if self.timesup is not None:
                self.timesup.withdraw()
            return
        run = t["run_seconds"]
        warned = self.usage["warned"]
        for mins in list(warned):                         # more time was added: warn again later
            if run > mins * 60 + 30:
                warned.remove(mins)
        if 0 < run <= 60 and 1 not in warned:
            notify("Falcon Advisory", "You have 1 minute remaining")
            warned.extend(m for m in (1, 5) if m not in warned)
        elif 60 < run <= 300 and 5 not in warned:
            notify("Falcon Advisory", "You have 5 minutes remaining")
            warned.append(5)
        out = run <= 0
        if out:
            if not self.was_out:
                self.store.log("Screen time is up: the computer will lock")
                try:
                    self.store.save_usage(self.usage)
                except OSError:
                    pass
            if self.timesup is None:
                self.timesup = TimesUpWindow(self, self)
            self.timesup.show(t)
            paused = time.time() < self.grace_until or self.store.supervisor_active()
            if active and not paused and time.time() - self.last_lock >= LOCK_EVERY_SECONDS:
                self.last_lock = time.time()
                lock_workstation()
        elif self.timesup is not None and self.timesup.winfo_viewable():
            self.timesup.withdraw()
        self.was_out = out

    def _watch_dns(self):
        """Background thread: check every website name Windows looks up."""
        recent, refused_at, reported = {}, {}, set()
        seen_names, seen_ips = {}, {}       # when each name / browser connection first showed up
        while not self._stopping:
            settings = self.saved
            try:
                protecting = getattr(self, "_protecting", False)
                active = session_active() if protecting else None
                self.diag.update(cycle=time.time(), protecting=protecting, active=active)
                if protecting and active:
                    started = time.monotonic()
                    entries = dns_cache_entries()
                    names = set(entries)
                    found = None
                    # A name in Windows' list isn't a visit: it may be left over, or a lookup
                    # the browser made in the background. Only names the browser is really
                    # connected to (or whose lookup was refused) are checked.
                    conns = browser_connections()
                    now = time.time()
                    for host in names:
                        seen_names.setdefault(host, now)
                    for ip in conns:
                        seen_ips.setdefault(ip, now)
                    known_ips = set().union(*entries.values()) if entries else set()
                    self.diag.update(names=len(names), with_addresses=sum(1 for v in entries.values() if v),
                                     unmatched=len(conns - known_ips))
                    visited = [h for h in names if time.time() - recent.get(h, 0) >= 20
                               and (entries[h] & conns or (entries[h] and entries[h] <= {"0.0.0.0", "::"}))]
                    self.checker.warm(visited, settings)
                    for host in sorted(visited):
                        reasons = self.checker.reasons(host, settings)
                        if not reasons:
                            continue
                        ips = entries.get(host, set())
                        refused = bool(ips) and ips <= {"0.0.0.0", "::"}
                        if ips & conns or refused:
                            if refused and not ips & conns:
                                if time.time() - refused_at.get(host, 0) < 300:
                                    continue     # a refused lookup counts once every 5 minutes
                                refused_at[host] = time.time()
                            found = (host, reasons)
                            break
                    if found is None and settings["web_filter_level"] == 0:
                        host = self.checker.restricted_violation(entries, conns, settings) if conns else None
                        if host and time.time() - recent.get(host, 0) >= 20:
                            found = (host, ["This website isn't on the Approved Sites list "
                                            "(Restricted level)."])
                    self.diag["connections"] = len(conns)
                    self.diag["check_ms"] = int((time.monotonic() - started) * 1000)
                    if found:
                        recent[found[0]] = time.time()
                        self.diag["last_block"] = found[0]
                        kill_browsers()
                        flush_dns()
                        self.events.put(found)
                        host_ips = entries.get(found[0], set()) & conns
                        connected = min((seen_ips[i] for i in host_ips if i in seen_ips), default=None)
                        self.store.log(f"Blocked {found[0]}: noticed "
                                       f"{time.time() - seen_names.get(found[0], time.time()):.0f} s after "
                                       f"Windows looked it up"
                                       + (f", {time.time() - connected:.0f} s after the browser "
                                          f"connected" if connected else "")
                                       + f" (each check takes {self.diag['check_ms']} ms)")
                    if len(seen_names) > 5000:
                        seen_names.clear()
                    if len(seen_ips) > 5000:
                        seen_ips.clear()
            except Exception as e:
                problem = f"{type(e).__name__}: {e}"
                self.diag["problem"] = problem
                if problem not in reported:          # write each kind of problem once
                    reported.add(problem)
                    self.store.log(f"Website filter problem: {problem}")
            time.sleep(WATCH_EVERY_SECONDS)

    def _load_signed_allows(self):
        self.checker.signed_allows = tuple(a["site"] for a in self.store.load_state()["allows"])

    def _poll_events(self):
        if self._stopping:
            return
        tray = getattr(self, "tray", None)
        while tray is not None and not tray.actions.empty():
            action = tray.actions.get_nowait()
            if action == "open":
                self.show_dashboard()
            elif action == "quit":
                self.after(10, self.quit_monitor)
        try:
            while True:
                host, reasons = self.events.get_nowait()
                self.store.log(f"Blocked {host}: {reasons[0]}")
                if self.block_dialog is None or not self.block_dialog.winfo_exists():
                    self.block_dialog = BlockDialog(
                        self, host, reasons, self.saved["options"]["supervisor_override"],
                        self._allow_site, lambda: self.store.log(
                            "Wrong supervisor password entered (Falcon Advisor)"))
                    self.block_dialog.show()
        except queue.Empty:
            pass
        self.after(500, self._poll_events)

    def _allow_site(self, host, how, password):
        """Falcon Advisor: the supervisor typed the password to allow a site."""
        if not check_password(password, self.saved["password"]):
            return False
        if how == "always":
            try:            # works where the settings can be written (not protected yet)
                fresh = self.store.load()
                fresh["sites"] = [x for x in fresh["sites"] if x["site"] != host] + \
                                 [{"site": host, "rule": "always"}]
                fresh["sites"].sort(key=lambda x: x["site"])
                self.store.save(fresh)
                self.saved = fresh
                self._settings_mtime = self._mtime()
                self.store.log(f"{host} added to the Always list from Falcon Advisor")
                return True
            except OSError:
                pass
            # Protected install: save a signed allowance on the child's account instead.
            key = unwrap_grant_key(self.saved.get("grant_wrap", {}), password)
            try:
                if key is None:
                    raise OSError("no grant key")
                st = self.store.load_state()
                st["allows"].append(make_allow(key, host))
                self.store.save_state(st)
            except OSError:
                self.checker.allowed_once.add(host)
                self.store.log(f"{host} couldn't be saved as always allowed: allowed this time")
                return "once"
            self._load_signed_allows()
            self.store.log(f"{host} always allowed from Falcon Advisor")
        else:
            self.checker.allowed_once.add(host)
            self.store.log(f"{host} allowed this time from Falcon Advisor")
        return True

    # --- My Account tab -----------------------------------------------------------------
    def _account(self, page):
        top = tk.Frame(page, bg=FACE)
        top.pack(fill="x")
        icon(top, "falcon", 32).pack(side="left", anchor="n")
        label(top, "Your FalconGuard subscription. Falcon checks it when it starts\n"
                   "and every hour. Click Check Now to check straight away.").pack(side="left", padx=10)

        box = GroupBox(page, "Subscription")
        box.pack(fill="x", pady=(12, 0))
        grid = tk.Frame(box.body, bg=FACE)
        grid.pack(fill="x")
        self.acct_values = {}
        for r, key in enumerate(("Falcon Edition:", "Price:", "Expires on:", "Days remaining:",
                                 "Status:")):
            label(grid, key).grid(row=r, column=0, sticky="w", pady=3)
            v = label(grid, bold=key in ("Falcon Edition:", "Days remaining:"))
            v.grid(row=r, column=1, sticky="w", padx=(16, 0), pady=3)
            self.acct_values[key] = v

        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x", pady=(10, 0))
        self.acct_checked = label(row, fg=GRAYTEXT)
        self.acct_checked.pack(side="left", anchor="s")
        self.check_btn = Button(row, "Check Now", command=self._check_now)
        self.check_btn.pack(side="right")

        box = GroupBox(page, "When your subscription ends")
        box.pack(fill="x", pady=(10, 0))
        label(box.body, f"{WARN_DAYS} days before: a warning appears at the top of this window.\n"
                        f"{CRITICAL_DAYS} day before: a critical warning appears.\n"
                        "On the day it ends: protection turns off and can't be turned\n"
                        "back on until the subscription is renewed.").pack(fill="x")
        self._refresh_account()

    def _refresh_account(self):
        sub, st = self.sub, subscription_status(self.sub)
        v = self.acct_values
        v["Falcon Edition:"].config(text=sub["edition"] if sub else "Unknown")
        v["Price:"].config(text=fmt_price(sub))
        v["Expires on:"].config(text=datetime.strptime(sub["expires"], "%Y-%m-%d").strftime(
            "%d %B %Y").lstrip("0") if sub else "Unknown")
        v["Days remaining:"].config(text=fmt_days_left(st["days"]),
                                    fg=NEVER_FG if st["level"] in ("critical", "expired") else INK)
        v["Status:"].config(text={"ok": "Active", "warning": "Expires soon",
                                  "critical": "Expires tomorrow", "expired": "Expired",
                                  "unknown": "Not checked yet"}[st["level"]])
        if sub and sub.get("checked"):
            try:
                when = datetime.fromisoformat(sub["checked"]).strftime("%d %b %Y at %H:%M").lstrip("0")
            except ValueError:
                when = sub["checked"]
            self.acct_checked.config(text=f"Last checked {when}.")
        else:
            self.acct_checked.config(text="Falcon couldn't reach the subscription service yet.")

    def _check_now(self):
        self.check_btn.set_enabled(False)
        self.acct_checked.config(text="Checking...")

        def done():
            self.check_btn.set_enabled(True)
            if not self.sub or not self.sub.get("checked") or \
                    time.time() - _iso_to_ts(self.sub["checked"]) > 60:
                message(self, "My Account", "Falcon couldn't reach the subscription service. "
                        "Check the internet connection and try again.", "error")
        self.check_subscription(then=done)

    # --- Content Filter tab ----------------------------------------------------------
    def _content_filter(self, page):
        label(page, "Select a category to view the rating levels:").pack(fill="x")
        tree = Bevel(page, SUNKEN, bg=WINDOW, height=88, padx=2, pady=2)
        tree.pack(fill="x", pady=(3, 9))
        inner = tree.body
        inner.pack_propagate(False)

        root = tk.Frame(inner, bg=WINDOW)
        root.pack(fill="x")
        icon(root, "lock", bg=WINDOW).pack(side="left")
        label(root, "CloudflareFML", bg=WINDOW, padx=2).pack(side="left", padx=2)

        self.cat_labels = {}
        for name in CATEGORIES:
            row = tk.Frame(inner, bg=WINDOW)
            row.pack(fill="x", padx=(19, 0))
            ic = icon(row, "key", bg=WINDOW)
            ic.pack(side="left")
            lb = label(row, name, bg=WINDOW, padx=2, pady=1)
            lb.pack(side="left", padx=2)
            self.cat_labels[name] = lb
            for w in (row, ic, lb):
                w.bind("<Button-1>", lambda e, n=name: self._pick_cat(n))

        label(page, "Adjust the slider to specify what users are allowed to see:").pack(fill="x")
        self.rating_bar = Trackbar(page, steps=len(LEVEL_NAMES), length=366,
                                   value=self.saved["web_filter_level"],
                                   command=lambda v: (self._show_level(), self.changed()))
        self.rating_bar.pack(fill="x", pady=(7, 0))
        self.level_text = label(page, anchor="center")
        self.level_text.pack(fill="x", pady=(1, 9))

        box = GroupBox(page, "Description")
        box.pack(fill="x")
        self.desc_text = label(box.body, height=2, anchor="nw")
        self.desc_text.pack(fill="x")
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x", pady=(6, 0))
        label(row, "To read what each level blocks, click More Info.").pack(
            side="left", anchor="s")
        Button(row, "More Info...", command=lambda: play_sound(ERROR_SOUND)).pack(side="right", anchor="s")
        self._pick_cat("Web Filter")

    def _pick_cat(self, name):
        self.current_cat = name
        for n, lb in self.cat_labels.items():
            on = n == name
            lb.config(bg=SELECT if on else WINDOW, fg=WINDOW if on else INK)
        self._show_level()

    def _show_level(self):
        v = self.rating_bar.value
        self.level_text.config(text=f"Level {v}:  {LEVEL_NAMES[v]}")
        self.desc_text.config(text=CATEGORIES[self.current_cat][v])

    # --- Approved Sites tab -----------------------------------------------------
    def _sites(self, page):
        self.sites = [dict(s) for s in self.saved["sites"]]
        top = tk.Frame(page, bg=FACE)
        top.pack(fill="x", pady=(0, 12))
        icon(top, "sites", 32).pack(side="left", anchor="n")
        label(top, "You can create a list of websites that are always\n"
                   "viewable or never viewable regardless of how they\n"
                   "are rated.").pack(side="left", padx=10, anchor="n")

        grid = tk.Frame(page, bg=FACE)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.rowconfigure(3, weight=1)
        label(grid, "Allow this website:").grid(row=0, column=0, sticky="w", pady=(0, 3))
        field, self.site_entry = entry(grid)
        field.grid(row=1, column=0, sticky="ew")
        self.always_btn = Button(grid, "Always", disabled=True,
                                 command=lambda: self._add_site("always"))
        self.always_btn.grid(row=1, column=1, padx=(10, 0), sticky="n")

        label(grid, "List of approved and blocked websites:").grid(
            row=2, column=0, sticky="w", pady=(12, 3))
        lst, self.site_list = listbox(grid, height=4)
        lst.grid(row=3, column=0, sticky="nsew")
        side = tk.Frame(grid, bg=FACE)
        side.grid(row=3, column=1, sticky="n", padx=(10, 0))
        self.never_btn = Button(side, "Never", disabled=True,
                                command=lambda: self._add_site("never"))
        self.never_btn.pack()
        self.remove_btn = Button(side, "Remove", disabled=True, command=self._remove_site)
        self.remove_btn.pack(pady=6)

        def typed(e=None):
            has = bool(self.site_entry.get().strip())
            self.always_btn.set_enabled(has)
            self.never_btn.set_enabled(has)

        self.site_entry.bind("<KeyRelease>", typed)
        self.site_entry.bind("<Return>", lambda e: self._add_site("always"))
        self._typed = typed
        self.site_list.bind("<<ListboxSelect>>",
                            lambda e: self.remove_btn.set_enabled(bool(self.site_list.curselection())))
        self._refresh_sites()

    def _refresh_sites(self):
        self.site_list.delete(0, "end")
        for s in self.sites:
            always = s["rule"] == "always"
            self.site_list.insert("end", f"{'Always' if always else 'Never'}:   {s['site']}")
            self.site_list.itemconfig("end", fg=ALWAYS_FG if always else NEVER_FG)
        self.remove_btn.set_enabled(False)

    def _add_site(self, rule):
        site = normalize_site(self.site_entry.get())
        if not site:
            if self.site_entry.get().strip():
                message(self, "Falcon", "Type a website address, such as www.example.com.",
                        "warning")
            return
        self.sites = [s for s in self.sites if s["site"] != site] + [{"site": site, "rule": rule}]
        self.sites.sort(key=lambda s: s["site"])
        self.site_entry.delete(0, "end")
        self._typed()
        self._refresh_sites()
        self.changed()

    def _remove_site(self):
        sel = self.site_list.curselection()
        if sel:
            del self.sites[sel[0]]
            self._refresh_sites()
            self.changed()

    # --- General tab -------------------------------------------------------------
    def _general(self, page):
        box = GroupBox(page, "Falcon")
        box.pack(fill="x")
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "falcon", 32).pack(side="left", anchor="n")
        self.enabled_text = label(row)
        self.enabled_text.pack(side="left", padx=10)
        self.toggle_btn = Button(row, "Turn On...", command=self.toggle_enabled)
        self.toggle_btn.pack(side="right", anchor="s")
        srow = tk.Frame(box.body, bg=FACE)
        srow.pack(fill="x", pady=(8, 0))
        self.monitor_icon = icon(srow, "trusted")
        self.monitor_icon.pack(side="left", anchor="n")
        self.monitor_text = label(srow, wraplength=380)
        self.monitor_text.pack(side="left", padx=(6, 0), fill="x")
        self._refresh_monitor_status()

        box = GroupBox(page, "Child's account")
        box.pack(fill="x", pady=(10, 0))
        label(box.body, "Falcon counts screen time and filters websites only on this\n"
                        "Windows account:").pack(fill="x")
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x", pady=(6, 0))
        abox, self.child_entry = entry(row, width=24)
        abox.pack(side="left")
        self.child_entry.insert(0, self.saved.get("child_account", ""))
        self.child_entry.bind("<KeyRelease>", lambda e: self.changed())

        def use_this():
            self.child_entry.delete(0, "end")
            self.child_entry.insert(0, current_user())
            self.changed()
        Button(row, "Use This Account", command=use_this).pack(side="right")

        box = GroupBox(page, "User options")
        box.pack(fill="x", pady=(10, 0))
        self.opts = {}
        for key, text in OPTIONS:
            cb = CheckBox(box.body, text, self.saved["options"][key], on_change=self.changed)
            cb.pack(fill="x", pady=3)
            self.opts[key] = cb

        box = GroupBox(page, "Supervisor password")
        box.pack(fill="x", pady=10)
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x")
        icon(row, "password", 32).pack(side="left", anchor="n")
        self.pw_text = label(row)
        self.pw_text.pack(side="left", padx=10)
        self.pw_btn = Button(box.body, "Change Password...", command=self.change_password)
        self.pw_btn.pack(anchor="e", pady=(8, 0))

        box = GroupBox(page, "Activity log")
        box.pack(fill="x")
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x")
        label(row, "Falcon records sign-ins, wrong passwords and\nevery change to its settings.").pack(
            side="left")
        Button(row, "View Log...", command=lambda: LogDialog(self, self.store).show()).pack(
            side="right")

    # --- Time Management tab ---------------------------------------------------------
    def _time(self, page):
        label(page, "Choose the hours when the user can use the computer, how much\n"
                    "time they can spend on it each day, and give extra time today.").pack(fill="x")

        box = GroupBox(page, "Allowed hours")
        box.pack(fill="x", pady=(8, 0))
        self.hour_grid = HourGrid(box.body, [set(h) for h in self.saved["blocked_hours"]],
                                  on_change=lambda: (self._update_allowed(), self.changed()))
        self.hour_grid.pack(anchor="w")
        key = tk.Frame(box.body, bg=FACE)
        key.pack(fill="x", pady=(8, 0))
        for text, fill in (("Allowed", WINDOW), ("Blocked", BLOCKED)):
            sw = tk.Canvas(key, width=13, height=13, bg=FACE, highlightthickness=0, bd=0)
            sw.create_rectangle(0, 0, 12, 12, fill=fill, outline=GRIDLINE)
            sw.pack(side="left")
            label(key, text).pack(side="left", padx=(4, 14))
        label(key, "Click or drag across squares to change them.", fg=GRAYTEXT).pack(
            side="right")

        box = GroupBox(page, "Daily time limit")
        box.pack(fill="x", pady=(10, 0))
        label(box.body, "Set the most time the user can spend on the computer each day.").pack(
            fill="x", pady=(0, 6))
        grid = tk.Frame(box.body, bg=FACE)
        grid.pack(fill="x")
        grid.columnconfigure(3, weight=1)
        self.limits, self.allowed_labels = [], []
        for d, name in enumerate(DAYS):
            col = 0 if d < 4 else 4
            r = d if d < 4 else d - 4
            label(grid, name, width=10).grid(row=r, column=col, sticky="w", pady=2)
            spin = TimeSpin(grid, self.saved["daily_limits"][d], on_change=self.changed)
            spin.grid(row=r, column=col + 1, sticky="w", pady=2)
            lb = label(grid, fg=GRAYTEXT)
            lb.grid(row=r, column=col + 2, sticky="w", padx=(5, 0))
            self.limits.append(spin)
            self.allowed_labels.append(lb)
        self._update_allowed()

        box = GroupBox(page, "Extra time today")
        box.pack(fill="x", pady=(10, 0))
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x")
        label(row, "Give extra screen time for today only:").pack(side="left")
        self.extra_amount = TimeSpin(row, 30, values=COUPON_VALUES)
        self.extra_amount.pack(side="left", padx=(8, 0))
        Button(row, "Add Time", command=self.add_extra_time).pack(side="right")
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x", pady=(6, 0))
        self.extra_text = label(row, fg=GRAYTEXT)
        self.extra_text.pack(side="left")
        self.remove_extra_btn = Button(row, "Remove Extra Time", command=self.remove_extra_time)
        self.remove_extra_btn.pack(side="right")
        self._refresh_extra()

    def _refresh_extra(self):
        """Show today's extra time (from this tab and from coupons)."""
        if not hasattr(self, "extra_text"):
            return
        minutes = self._bonus()
        self.extra_text.config(text=f"+{fmt_minutes(minutes)} extra today, from here and from coupons."
                               if minutes else "No extra time has been given today.")
        self.remove_extra_btn.set_enabled(minutes > 0)

    def add_extra_time(self):
        """Add extra time for today straight away (no coupon needed, no Apply needed)."""
        minutes = self.extra_amount.minutes
        total = self.add_bonus(minutes)
        if total is None:
            return
        self.store.log(f"Extra time given: +{fmt_minutes(minutes)} today")
        self._refresh_dashboard()
        t = self.dash_time
        capped = ""
        if t["budget"] is not None and t["left"] < t["budget"]:
            capped = (" The schedule on this tab ends screen time at "
                      f"{fmt_hour(t['next_change'] or 24)}, so not all of it can be used today.")
        message(self, "Extra Time", f"{fmt_minutes(minutes)} of extra screen time was added "
                f"for today. Today's extra time is now {fmt_minutes(total)}.\n\n"
                f"Screen time left today: {fmt_minutes(t['left'])}.{capped}")

    def add_bonus(self, minutes):
        """Add extra time for today. Returns today's new total (None if saving failed)."""
        if self._modify_disk(lambda s: add_extra(s, minutes)) is None:
            return None
        self._bonus_cache = None
        return self._bonus()

    def remove_extra_time(self):
        if message(self, "Remove Extra Time", "Remove all of today's extra time, including "
                   "time from coupons redeemed today?", "question", ("Yes", "No"),
                   default=1) != "Yes":
            return

        if self._modify_disk(reset_extra):
            self.store.log("Extra time for today removed")
            self._refresh_dashboard()

    def _update_allowed(self):
        """Show each day's allowed hours next to its limit."""
        for d, lb in enumerate(self.allowed_labels):
            lb.config(text=f"of {self.hour_grid.allowed_hours(d)}h")

    # --- Zones tab -----------------------------------------------------------------
    def _zones(self, page):
        self.zone_state = copy.deepcopy(self.saved["zones"])
        self.current_zone = None
        label(page, "Select a web content zone to specify its protection settings.").pack(fill="x")
        strip = Bevel(page, SUNKEN, bg=WINDOW, pady=6)
        strip.pack(fill="x", pady=(3, 9))
        self.zone_cells = []
        for i, (kind, name, _) in enumerate(ZONES):
            strip.body.columnconfigure(i, weight=1)
            cell = tk.Frame(strip.body, bg=WINDOW)
            cell.grid(row=0, column=i)
            ic = icon(cell, kind, 32, bg=WINDOW)
            ic.pack()
            lb = label(cell, name, bg=WINDOW, padx=2, pady=1)
            lb.pack(pady=(4, 0))
            self.zone_cells.append((kind, ic, lb))
            for w in (cell, ic, lb):
                w.bind("<Button-1>", lambda e, i=i: self._pick_zone(i))

        info = tk.Frame(page, bg=FACE)
        info.pack(fill="x", pady=(0, 10))
        self.zone_icon = icon(info, "restricted", 32)
        self.zone_icon.pack(side="left", anchor="n")
        txt = tk.Frame(info, bg=FACE)
        txt.pack(side="left", padx=8, anchor="n")
        self.zone_name = label(txt, bold=True)
        self.zone_name.pack(fill="x")
        self.zone_desc = label(txt)
        self.zone_desc.pack(fill="x", pady=(2, 0))
        right = tk.Frame(info, bg=FACE)
        right.pack(side="right", anchor="n")
        self.zone_sites_btn = Button(right, "Sites...", command=self._edit_zone_sites)
        self.zone_sites_btn.pack(anchor="e")
        self.zone_count = label(right)
        self.zone_count.pack(anchor="e", pady=(6, 0))

        box = GroupBox(page, "Protection level for this zone")
        box.pack(fill="x")
        label(box.body, "Move the slider to set the protection level for this zone.").pack(fill="x")
        row = tk.Frame(box.body, bg=FACE)
        row.pack(fill="x", pady=(8, 6))
        self.zone_bar = Trackbar(row, steps=len(PROTECTION), orient="vertical", length=90,
                                 command=self._zone_level_changed)
        self.zone_bar.pack(side="left")
        self.level_lines = tk.Frame(row, bg=FACE)
        self.level_lines.pack(side="left", padx=8, anchor="n")
        btns = tk.Frame(box.body, bg=FACE)
        btns.pack(fill="x")
        self.default_btn = Button(btns, "Default Level", command=self._default_level)
        self.default_btn.pack(side="right")
        Button(btns, "Custom Level...", command=lambda: message(
            self, "Custom Level", "Custom protection levels will be added in a later "
            "version of Falcon.")).pack(side="right", padx=6)
        self._pick_zone(2)

    def _pick_zone(self, i):
        for j, (kind, ic, lb) in enumerate(self.zone_cells):
            on = j == i
            draw_icon(ic, kind, 32, WINDOW, selected=on)
            lb.config(bg=SELECT if on else WINDOW, fg=WINDOW if on else INK)
        kind, name, desc = ZONES[i]
        self.current_zone = kind
        draw_icon(self.zone_icon, kind, 32, FACE)
        self.zone_name.config(text=name)
        self.zone_desc.config(text=desc)
        self.zone_bar.set(self.zone_state[kind]["level"])
        self._show_protection(self.zone_state[kind]["level"])
        self._update_zone_info()

    def _zone_level_changed(self, i):
        self.zone_state[self.current_zone]["level"] = i
        self._show_protection(i)
        self._update_zone_info()
        self.changed()

    def _default_level(self):
        self.zone_bar.set(DEFAULT_ZONE_LEVEL[self.current_zone])

    def _update_zone_info(self):
        kind = self.current_zone
        if kind == "internet":
            self.zone_sites_btn.set_enabled(False)
            self.zone_count.config(text="Includes every other website.")
        else:
            self.zone_sites_btn.set_enabled(True)
            n = len(self.zone_state[kind]["sites"])
            self.zone_count.config(text="No sites are in this zone." if n == 0 else
                                   "1 site is in this zone." if n == 1 else
                                   f"{n} sites are in this zone.")
        self.default_btn.set_enabled(self.zone_state[kind]["level"] != DEFAULT_ZONE_LEVEL[kind])

    def _edit_zone_sites(self):
        kind = self.current_zone
        result = ZoneSitesDialog(self, ZONE_NAMES[kind], self.zone_state[kind]["sites"]).show()
        if result is not None:
            self.zone_state[kind]["sites"] = result
            self._update_zone_info()
            self.changed()

    def _show_protection(self, i):
        for w in self.level_lines.winfo_children():
            w.destroy()
        name, lines = PROTECTION[i]
        label(self.level_lines, name, bold=True).pack(fill="x", pady=(0, 2))
        for line in lines:
            label(self.level_lines, line).pack(fill="x")


def show_selftest(results):
    """Show the self-test results in a Windows 2000-style window."""
    root = tk.Tk()
    root.withdraw()
    setup_fonts(root)
    d = Dialog(root, "Falcon Self-Test")
    passed = sum(ok for _, ok, _ in results)
    label(d.body, f"{passed} of {len(results)} checks passed. The results are also saved in "
                  "selftest.txt\nin Falcon's settings folder.").pack(fill="x")
    box, lst = listbox(d.body, height=min(18, len(results) + 1), width=96)
    box.pack(fill="both", expand=True, pady=(10, 0))
    for name, ok, detail in results:
        lst.insert("end", f"{'PASS' if ok else 'FAIL'}    {name}" + (f"   ({detail})" if detail else ""))
        lst.itemconfig("end", fg=ALWAYS_FG if ok else NEVER_FG)
    d.button_row([("Close", d.cancel, True)])
    d.show()
    root.destroy()


def main():
    if sys.platform == "win32":
        try:  # draw crisp pixels instead of letting Windows blur-scale the window
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    try:
        store = Store()
    except OSError as e:
        print(f"Falcon could not open its settings folder: {e}", file=sys.stderr)
        sys.exit(1)
    args = sys.argv[1:]
    if "--guard" in args:            # build.bat: every minute, as SYSTEM
        for line in run_guard(store):
            print(line)
        return
    if "--make-icon" in args:        # build.bat: falcon.ico for FalconGuard.exe
        i = args.index("--make-icon")
        write_falcon_icon(args[i + 1] if i + 1 < len(args) else "falcon.ico")
        return
    if "--web" in args:              # build.bat: at start-up, as SYSTEM, always running
        try:
            WebDashboard(store).serve()
        except OSError as e:         # port in use: another copy is already running
            print(f"Web dashboard not started: {e}", file=sys.stderr)
        return
    if "--setup" in args:            # build.bat: --setup CHILD (as administrator)
        i = args.index("--setup")
        child = args[i + 1].strip() if i + 1 < len(args) else ""
        results = install_system(store, child)
        ips, public = network_addresses()
        lines = [("OK    " if ok else "FAIL  ") + step + (f"  ({detail})" if detail and not ok else "")
                 for step, ok, detail in results]
        lines += ["", "Web dashboard for parents on the same Wi-Fi:"]
        lines += [f"    http://{ip}:{WEB_PORT}" for ip in ips]
        lines.append(f"    http://{os.environ.get('COMPUTERNAME', 'this-pc')}:{WEB_PORT}")
        if public:
            lines += ["", "[!] This PC's network is set to Public, so other devices can't reach the",
                      "    dashboard. Set it to Private in Settings > Network & Internet."]
        try:      # the .exe has no console, so build.bat shows this file
            (store.folder / "setup.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        print("\n".join(lines))
        sys.exit(0 if all(ok for _, ok, _ in results) else 1)
    if "--uninstall" in args:        # uninstall.bat, Start menu, Settings > Apps
        if IS_WINDOWS and not settings_writable(store) and relaunch_as_admin(["--uninstall"]):
            return                       # Windows asks for administrator rights; that copy uninstalls
        show_uninstall(store)
        return
    if "--set-child" in args:        # --set-child NAME
        i = args.index("--set-child")
        name = args[i + 1].strip() if i + 1 < len(args) else ""
        s = store.load()
        s["child_account"] = name[:100]
        store.save(s)
        store.log(f"Child's account set to {name} by build.bat")
        return
    if "--protect" in args:          # build.bat: once, as administrator
        results = protect(store)
        lines = [("OK    " if ok else "FAIL  ") + cmd + (f"\n      {detail}" if detail and not ok else "")
                 for cmd, ok, detail in results]
        print("\n".join(lines))
        try:      # the built .exe has no console, so build.bat reads this file
            (store.folder / "protect.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        sys.exit(0 if all(ok for _, ok, _ in results) else 1)
    if "--selftest" in args:
        results = self_test(store, try_lock="--selftest-lock" in args)
        report = "\n".join(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -  {d}" if d else "")
                           for name, ok, d in results)
        print(report)
        try:
            (store.folder / "selftest.txt").write_text(report + "\n", encoding="utf-8")
        except OSError:
            pass
        show_selftest(results)
        return
    app = Falcon(store)
    # "python falcon.py --dashboard" opens the child's Dashboard directly (no password),
    # handy for a desktop shortcut on the child's account.
    # "python falcon.py --monitor" runs Falcon in the background on the child's account:
    # it counts screen time, warns, locks when time is up and filters websites.
    resume = args[args.index("--resume") + 1] if "--resume" in args and \
        args.index("--resume") + 1 < len(args) else None
    if app.start(dashboard_only="--dashboard" in args, monitor="--monitor" in args, resume=resume):
        app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        try:      # the built .exe has no console, so leave a trace in the activity log
            import traceback
            where = traceback.extract_tb(e.__traceback__)[-1]
            Store().log(f"FalconGuard stopped with an error ({' '.join(sys.argv[1:]) or 'settings'}): "
                        f"{type(e).__name__}: {e} [line {where.lineno}]")
        except Exception:
            pass
        play_sound(ERROR_SOUND, wait=True)
        raise
