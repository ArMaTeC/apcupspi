#!/usr/bin/env python3
"""
apc-nmc-web.py - APC Network Management Card 2 web interface emulation.

Serves an NMC2-lookalike UI on :443 (https) with :80 redirecting to it.
All data is live from NUT/upsd; Control/Tests/Configuration actions are
translated to NUT instant commands and SET VARs.
"""

# Daemon style, intentional: broad excepts keep the service alive through UPS
# or sysfs hiccups, try/except/pass tolerates missing system files, and %-format
# is used because the HTML templates contain literal { } (CSS/JS).
# ruff: noqa: BLE001, S110, UP031
import configparser
import fcntl
import hashlib
import html
import io
import itertools
import glob
import json
import os
import re
import secrets
import signal
import smtplib
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

CONF = "/etc/apc-nmc/nmc.conf"
STATE_FILE = "/var/lib/apc-nmc/state.json"
EVENTS_FILE = "/var/lib/apc-nmc/events.json"
SESSIONS_FILE = "/var/lib/apc-nmc/sessions.json"
DATA_FILE = "/var/lib/apc-nmc/datalog.json"
MAX_EVENTS = 400
MAX_DATA = 1500              # ~25h at 60s sampling
SESSION_TTL = 8 * 3600       # sliding
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT = 300          # seconds
VERSION_FILE = "/etc/apc-nmc/version"
UPDATE_STATE = "/var/lib/apc-nmc/update.json"
UPDATE_STATUS = "/var/lib/apc-nmc/update-status.json"
UPDATE_LOG = "/var/lib/apc-nmc/update.log"
UPDATE_TOOL = "/usr/local/sbin/apc-nmc-update.sh"

_cp = configparser.ConfigParser()
_cp.read(CONF)


def cfg(sec, key, default=""):
    try:
        return _cp.get(sec, key)
    except Exception:
        return default


UPSD_HOST = cfg("upsd", "host", "127.0.0.1")
UPSD_PORT = _cp.getint("upsd", "port", fallback=3493)
UPSNAME = cfg("upsd", "ups", "myups")
UPSD_USER = cfg("upsd", "user", "")
UPSD_PASS = cfg("upsd", "password", "")

WEB_USER = cfg("web", "user", "apc")
WEB_PASS = cfg("web", "password", "apc")
API_TOKEN = cfg("web", "api_token", "")   # bearer token for /metrics + /api/v1
LOCATION = cfg("device", "location", "Server Room")
CONTACT = cfg("device", "contact", "admin@localhost")
NMC_MODEL = "AP9631"
NMC_SERIAL = cfg("device", "nmc_serial", "5A2436E12345")
NMC_FW = cfg("device", "nmc_fw", "v7.1.2")
WEB_VER = "2.0"

sessions = {}          # tok -> {"exp": ts, "csrf": str}
_sessions_lock = threading.Lock()
_login_fails = {}      # ip -> {"n": int, "until": ts}


# ============================================================ NUT client
# NOTE: Nut, _atomic_json and event_sev mirror apc-nmc-agent.py - keep in sync.
class NutError(Exception):
    pass


class Nut:
    def __init__(self):
        self.sock = None
        self.f = None
        self.authed = False

    def _connect(self):
        if self.sock is not None:
            return
        self.sock = socket.create_connection((UPSD_HOST, UPSD_PORT), 5)
        self.sock.settimeout(5)
        self.f = self.sock.makefile("rw", encoding="utf-8", newline="\n")
        self.authed = False

    def _close(self):
        try:
            if self.f is not None:
                self.f.close()
        except Exception:
            pass
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        self.f = None
        self.authed = False

    def _req(self, line):
        for _ in (0, 1):
            try:
                self._connect()
                assert self.f is not None
                self.f.write(line + "\n")
                self.f.flush()
                r = self.f.readline()
                if r == "":
                    raise NutError("closed")
                return r.rstrip("\n")
            except Exception:
                self._close()
        raise NutError("upsd unreachable")

    def _auth(self):
        if self.authed:
            return
        if not self._req("USERNAME " + UPSD_USER).startswith("OK"):
            raise NutError("username refused")
        if not self._req("PASSWORD " + UPSD_PASS).startswith("OK"):
            raise NutError("password refused")
        self.authed = True

    def _list(self, cmd, tag, ncols):
        """Shared LIST VAR/CMD/RW reader. On ANY mid-read failure the
        socket is closed: leftover response lines would otherwise be
        served as fresh answers to later commands (stale-data desync)."""
        try:
            r = self._req(cmd)
            if not r.startswith("BEGIN " + cmd):
                raise NutError(r)
            out = []
            while True:
                assert self.f is not None
                line = self.f.readline().rstrip("\n")
                if line.startswith("END " + cmd):
                    break
                if line == "":
                    raise NutError("eof mid-response")
                p = line.split(None, ncols)
                if len(p) == ncols + 1 and p[0] == tag:
                    out.append(p)
            return out
        except Exception:
            self._close()
            raise

    def list_vars(self, ups):
        out = {}
        for p in self._list("LIST VAR " + ups, "VAR", 3):
            v = p[3]
            out[p[2]] = v[1:-1] if v.startswith('"') else v
        return out

    def list_cmds(self, ups):
        return [p[2] for p in self._list("LIST CMD " + ups, "CMD", 2)]

    def list_rw(self, ups):
        return {p[2] for p in self._list("LIST RW " + ups, "RW", 3)}

    def instcmd(self, ups, cmd):
        self._auth()
        return self._req("INSTCMD %s %s" % (ups, cmd))

    def set_var(self, ups, var, value):
        if re.search(r'["\r\n]', value):
            return "ERR INVALID-VALUE"
        self._auth()
        return self._req('SET VAR %s %s "%s"' % (ups, var, value))


nut = Nut()
_nut_lock = threading.Lock()


def ups_vars():
    with _nut_lock:
        return nut.list_vars(UPSNAME)


def ups_cmds():
    with _nut_lock:
        return nut.list_cmds(UPSNAME)


def ups_rw():
    with _nut_lock:
        return nut.list_rw(UPSNAME)


def ups_cmd(cmd):
    with _nut_lock:
        r = nut.instcmd(UPSNAME, cmd)
    if cmd in ("beeper.mute", "beeper.enable", "beeper.disable") \
            and r.startswith(("OK", "PENDING")):
        save_state({"beeper_muted": cmd == "beeper.mute"})
    return r


def ups_setvar(var, val):
    with _nut_lock:
        return nut.set_var(UPSNAME, var, val)


# ============================================================ shared state
def _atomic_json(path, mut):
    """mut(old) -> new; writes atomically under a lock file."""
    try:
        with open(path + ".lock", "w") as lf:
            try:    # the agent (Debian-snmp) shares this lock
                import grp
                os.chown(path + ".lock", -1,
                         grp.getgrnam("Debian-snmp").gr_gid)
                os.chmod(path + ".lock", 0o660)
            except Exception:
                pass
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with open(path) as f:
                    old = json.load(f)
            except Exception:
                old = None
            new = mut(old)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new, f)
            os.chmod(tmp, 0o640)   # state.cfg can hold secrets; grp reads
            os.replace(tmp, path)
            fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    def m(old):
        old = old or {}
        old.update(st)
        return old
    _atomic_json(STATE_FILE, m)


def cfg_get(key, default):
    return load_state().get("cfg", {}).get(key, default)


def cfg_set(key, val):
    """Atomic single-key cfg write — mutate inside the flock so a
    concurrent cfg_set can't overwrite this key with a stale snapshot."""
    def m(old):
        old = old or {}
        c = dict(old.get("cfg") or {})
        c[key] = val
        old["cfg"] = c
        return old
    _atomic_json(STATE_FILE, m)


# ============================================================ sessions
def _persist_sessions():
    try:
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sessions, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSIONS_FILE)
    except Exception:
        pass


def load_sessions():
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    now = time.time()
    with _sessions_lock:
        sessions.update({t: s for t, s in data.items()
                         if s.get("exp", 0) > now})


def new_session(ip="", user=""):
    tok = secrets.token_hex(16)
    with _sessions_lock:
        # prune expired while we're here
        dead = [t for t, s in sessions.items() if s["exp"] <= time.time()]
        for t in dead:
            sessions.pop(t, None)
        sessions[tok] = {"exp": time.time() + SESSION_TTL,
                         "csrf": secrets.token_hex(16),
                         "ip": ip, "created": time.time(), "user": user}
        _persist_sessions()
    return tok


def get_session(tok):
    with _sessions_lock:
        s = sessions.get(tok)
        if s and s["exp"] > time.time():
            old = s["exp"]
            s["exp"] = time.time() + SESSION_TTL   # sliding renewal
            if s["exp"] - old > 60:                # persist ~1/min max
                _persist_sessions()
            return s
    return None


def drop_session(tok):
    with _sessions_lock:
        if tok in sessions:
            sessions.pop(tok, None)
            _persist_sessions()


def web_password():
    return cfg_get("web_pass", WEB_PASS)


_PBKDF = ("sha256", 200_000)


def web_password_ok(pw):
    h = cfg_get("web_pass_hash", "")
    if h:
        salt, _, digest = h.partition("$")
        d = hashlib.pbkdf2_hmac(_PBKDF[0], pw.encode(),
                                bytes.fromhex(salt), _PBKDF[1])
        return secrets.compare_digest(d.hex(), digest)
    return secrets.compare_digest(pw, cfg_get("web_pass", WEB_PASS))


def _user_ok(u, pw):
    """Primary web user, plus extra accounts from the [users] conf
    section (name = password).  Proportionate multi-user auth in lieu
    of RADIUS/LDAP (queued item D4)."""
    if secrets.compare_digest(u, WEB_USER) and web_password_ok(pw):
        return True
    if _cp.has_section("users"):
        for name in set(_cp.options("users")) - set(_cp.defaults()):
            if secrets.compare_digest(u, name) and \
                    secrets.compare_digest(pw, _cp.get("users", name)):
                return True
    return False


# ============================================================ event log
_last_event = {"text": None, "ts": 0}


def event_sev(text):
    t = text.lower()
    if "no longer" in t:
        return "info"
    if any(w in t for w in ("lost", "fail", "shut down", "shutdown",
                            "overload", "replace", "discharg", "too low",
                            "turned off", "off via")):
        return "crit"
    if any(w in t for w in ("battery", "off", "bypass", "reboot", "cycle",
                            "calibrat", "self test", "sleep", "cleared",
                            "configuration", "login failed", "log on",
                            "boost", "trim", "alarm")):
        return "warn"
    return "info"


# Friendly names for cfg keys surfaced in "settings changed" events.
_CFG_LABEL = {
    "ups_name": "UPS name", "location": "location", "contact": "contact",
    "ups_id": "ups.id", "rated_output_voltage": "rated voltage",
    "sensitivity": "sensitivity",
    "low_battery_runtime": "low battery runtime",
    "shutoff_delay": "shutdown delay",
    "min_return_capacity": "min return capacity",
    "web_pass_hash": "web password",
    "notify_min_sev": "min severity", "notify_syslog": "syslog",
    "notify_webhook": "webhook", "notify_discord": "discord",
    "notify_discord_mention": "discord ping", "notify_teams": "teams",
    "notify_smtp_host": "smtp host", "notify_smtp_port": "smtp port",
    "notify_smtp_tls": "smtp security", "notify_smtp_user": "smtp user",
    "notify_smtp_pass": "smtp password", "notify_smtp_from": "mail from",
    "notify_smtp_to": "mail to",
}
_CFG_SECRET = {"notify_webhook", "notify_discord", "notify_teams",
               "notify_smtp_pass", "web_pass_hash"}


def _cfg_diff(before, after):
    """["label: old -> new"] for changed cfg keys; secrets only get
    set/changed/cleared so values never reach the event log."""
    out = []
    for k in sorted(set(before) | set(after)):
        o, n = before.get(k, ""), after.get(k, "")
        if o == n:
            continue
        if k.startswith("notify_route_"):
            kind = k[13:]
            lbl = "route " + (dict(NOTIFY_KINDS).get(kind) or kind)
        else:
            lbl = str(_CFG_LABEL.get(k) or k)
        if k in _CFG_SECRET:
            out.append(lbl + ": " +
                       ("set" if not o else
                        "cleared" if not n else "changed"))
        else:
            o_s, n_s = str(o)[:40], str(n)[:40]
            out.append(lbl + ": " + (
                (n_s or "cleared") if not o_s else
                "%s -> %s" % (o_s, n_s or "cleared")))
    return out


def log_event(text, sev=None, kind=None, notify=True):
    try:
        now = time.time()
        if _last_event["text"] == text and now - _last_event["ts"] < 120:
            return
        _last_event.update(text=text, ts=now)
        sev = sev or event_sev(text)

        def m(old):
            ev = old or []
            ev.append({"ts": int(now), "text": text, "sev": sev})
            return ev[-MAX_EVENTS:]
        _atomic_json(EVENTS_FILE, m)
        if notify:
            threading.Thread(target=_notify_async,
                             args=(text, sev, kind or "other"),
                             daemon=True).start()
        try:
            import pwd
            os.chown(EVENTS_FILE, pwd.getpwnam("Debian-snmp").pw_uid, -1)
        except Exception:
            pass
    except Exception:
        pass


def load_events():
    try:
        with open(EVENTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


# ---- outbound event notification ------------------------------------
# Channels are configured in the UI (state.cfg.notify_*); nmc.conf
# [notify] syslog/webhook/min_sev remain as baseline defaults.

_SEV_ORDER = {"info": 0, "warn": 1, "crit": 2}
_SEV_COLOR = {"info": 0x3DCD58, "warn": 0xB7791F, "crit": 0xD00000}
_SEV_ICON = {"info": "ℹ️", "warn": "⚠️", "crit": "🚨"}

# Discord embed dressing per trigger kind: emoji + short headline.
_KIND_META = {
    "on_battery":    ("🔌", "On Battery — Power Failure"),
    "off_battery":   ("✅", "Power Restored"),
    "low_battery":   ("🪫", "Low Battery"),
    "discharged":    ("🔻", "Battery Discharged"),
    "replace_batt":  ("🔧", "Replace Battery"),
    "overload":      ("⚡", "Overload Condition"),
    "ups_off":       ("🔴", "UPS Power State"),
    "shutdown":      ("🛑", "Shutdown In Progress"),
    "bypass":        ("🔀", "On Bypass"),
    "avr":           ("📈", "Voltage Regulation"),
    "charging":      ("🔋", "Battery Charging"),
    "selftest":      ("🧪", "Self Test / Calibration"),
    "comm_lost":     ("📡", "UPS Comms Lost"),
    "comm_restored": ("📶", "UPS Comms Restored"),
    "alarm":         ("🚨", "Alarm Condition"),
    "other":         ("🔔", "NMC Notice"),
}


def notify_cfg():
    """Effective notify settings: UI cfg.notify_* over nmc.conf [notify]."""
    return {
        "min_sev": cfg_get("notify_min_sev",
                           cfg("notify", "min_sev", "warn") or "warn"),
        "syslog": cfg_get("notify_syslog", cfg("notify", "syslog", "")),
        "webhook": cfg_get("notify_webhook", cfg("notify", "webhook", "")),
        "discord": cfg_get("notify_discord", ""),
        "discord_mention": cfg_get("notify_discord_mention", ""),
        "teams": cfg_get("notify_teams", ""),
        "smtp_host": cfg_get("notify_smtp_host", ""),
        "smtp_port": str(cfg_get("notify_smtp_port", "")),
        "smtp_tls": cfg_get("notify_smtp_tls", "starttls"),
        "smtp_user": cfg_get("notify_smtp_user", ""),
        "smtp_pass": cfg_get("notify_smtp_pass", ""),
        "smtp_from": cfg_get("notify_smtp_from", ""),
        "smtp_to": cfg_get("notify_smtp_to", ""),
        "routes": notify_routes(),
    }


# Routing kinds: each STATUS_EVENTS flag edge maps to one; "other"
# catches every non-flag event (logins, config changes, NMC notices).
NOTIFY_CHANNELS = ("syslog", "webhook", "discord", "teams", "email")

NOTIFY_KINDS = (
    ("on_battery",    "On battery (power failure)"),
    ("off_battery",   "Power restored"),
    ("low_battery",   "Low battery"),
    ("discharged",    "Battery discharged"),
    ("replace_batt",  "Battery needs replacement"),
    ("overload",      "Overload"),
    ("ups_off",       "UPS turned off / on"),
    ("shutdown",      "Shutdown started"),
    ("bypass",        "On bypass"),
    ("avr",           "AVR boost / trim"),
    ("charging",      "Battery charging state"),
    ("selftest",      "Self test / calibration"),
    ("comm_lost",     "UPS communication lost"),
    ("comm_restored", "UPS communication restored"),
    ("alarm",         "Alarm condition"),
    ("other",         "All other events (logins, config, NMC notices)"),
)

_KIND_FOR = {
    ("OB", "on"): "on_battery", ("OB", "off"): "off_battery",
    ("LB", "on"): "low_battery",
    ("DISCHRG", "on"): "discharged",
    ("RB", "on"): "replace_batt", ("RB", "off"): "replace_batt",
    ("OVER", "on"): "overload",
    ("OFF", "on"): "ups_off", ("OFF", "off"): "ups_off",
    ("SD", "on"): "shutdown", ("FSD", "on"): "shutdown",
    ("BYPASS", "on"): "bypass", ("BYPASS", "off"): "bypass",
    ("BOOST", "on"): "avr", ("BOOST", "off"): "avr",
    ("TRIM", "on"): "avr", ("TRIM", "off"): "avr",
    ("CHRG", "on"): "charging", ("CHRG", "off"): "charging",
    ("TEST", "on"): "selftest", ("TEST", "off"): "selftest",
    ("CAL", "on"): "selftest", ("CAL", "off"): "selftest",
    ("ALARM", "on"): "alarm", ("ALARM", "off"): "alarm",
    ("HB", "on"): "alarm",
}


def notify_routes():
    """{kind: set(channels)|None} — None means unset = all channels."""
    out = {}
    for k, _lbl in NOTIFY_KINDS:
        raw = cfg_get("notify_route_" + k, None)
        out[k] = ({c for c in raw.split(",") if c}
                  if raw is not None else None)
    return out


def _post_json(url, payload, timeout=5):
    # Discord/Cloudflare edge 403s Python-urllib's default UA.
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "apc-nmc/%s (UPS monitor)" % NMC_FW})
    urllib.request.urlopen(req, timeout=timeout).read()


def _send_syslog(host, text, sev):
    pri = {"crit": 130, "warn": 132, "info": 134}.get(sev, 134)
    pkt = ("<%d>apc-nmc: %s" % (pri, text)).encode()
    if host in ("local", "/dev/log"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect("/dev/log")
        s.send(pkt)
        s.close()
    else:
        hp = host.split(":")
        addr = (hp[0], int(hp[1]) if len(hp) > 1 else 514)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(pkt, addr)
        s.close()


def _send_webhook(url, text, sev):
    _post_json(url, {"text": text, "sev": sev,
                     "ts": int(time.time()), "source": "apc-nmc"})


def _vitals():
    """Live UPS readings as (label, value) pairs; [] when unreachable."""
    try:
        d = ups_vars() or {}
    except Exception:
        return []
    out = []

    def f(label, fmt, key):
        v = num(d, key)
        if v is not None:
            out.append((label, fmt % v))
    f("🔋 Charge", "%.0f%%", "battery.charge")
    f("⚡ Load", "%.0f%%", "ups.load")
    f("🔌 Input", "%.0f V", "input.voltage")
    f("🔌 Output", "%.0f V", "output.voltage")
    rt = num(d, "battery.runtime")
    if rt:
        h, m = divmod(int(rt) // 60, 60)
        out.append(("⏱ Runtime", ("%dh %dm" % (h, m)) if h else "%dm" % m))
    st = str(d.get("ups.status") or "").strip()
    if st:
        out.append(("📟 Status", st))
    return out[:6]


def _send_discord(url, text, sev, kind="other", ups_name=None,
                  mention=None):
    icon, title = _KIND_META.get(kind, _KIND_META["other"])
    emb = {
        "author": {"name": "APC UPS — %s" % (ups_name or "Smart-UPS")},
        "title": "%s %s" % (icon, title),
        "description": text,
        "color": _SEV_COLOR.get(sev, 0x3DCD58),
        "fields": [{"name": "Severity",
                    "value": "%s %s" % (_SEV_ICON.get(sev, "ℹ️"),
                                        sev.upper()),
                    "inline": True}] +
                [{"name": n, "value": v, "inline": True}
                 for n, v in _vitals()],
        "footer": {"text": "APC Network Management Card"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime()),
    }
    payload = {"username": "APC UPS", "embeds": [emb]}
    if mention and sev == "crit":
        payload["content"] = mention     # pings @here / role on critical
    _post_json(url, payload)


def _send_teams(url, text, sev):
    """Office365 'Incoming Webhook' connector MessageCard payload."""
    _post_json(url, {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "%06X" % _SEV_COLOR.get(sev, 0x3DCD58),
        "summary": text,
        "sections": [{
            "activityTitle": "APC UPS event \u2014 %s" % sev.upper(),
            "text": text,
            "facts": [{"name": n, "value": v} for n, v in _vitals()],
        }],
    })


def _send_email(n, text, sev):
    tls = n.get("smtp_tls", "starttls")
    port = int(n.get("smtp_port") or
               {"ssl": 465, "starttls": 587}.get(tls, 25))
    msg = EmailMessage()
    msg["Subject"] = "[APC UPS] %s: %s" % (sev.upper(), text[:100])
    msg["From"] = n.get("smtp_from") or "apc-nmc@localhost"
    msg["To"] = n["smtp_to"]
    vit = " | ".join("%s %s" % (k, v) for k, v in _vitals())
    body = text + ("\n\n" + vit if vit else "")
    msg.set_content("%s\n\n-- APC Network Management Card (%s)\n"
                    % (body, time.strftime("%Y-%m-%d %H:%M:%S")))
    if tls == "ssl":
        s = smtplib.SMTP_SSL(n["smtp_host"], port, timeout=10)
    else:
        s = smtplib.SMTP(n["smtp_host"], port, timeout=10)
        s.ehlo()
        if tls == "starttls":
            s.starttls()
            s.ehlo()
    try:
        if n.get("smtp_user"):
            s.login(n["smtp_user"], n.get("smtp_pass", ""))
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            s.close()


def notify_dispatch(text, sev, n=None, kind="other"):
    """Fire each channel routed for `kind`. Returns ["channel: err"]."""
    n = n or notify_cfg()
    route = (n.get("routes") or {}).get(kind or "other")
    if route is None:
        route = NOTIFY_CHANNELS          # unset = all channels
    errs = []
    for name, enabled, fn in (
            ("syslog", n["syslog"], lambda: _send_syslog(
                n["syslog"], text, sev)),
            ("webhook", n["webhook"], lambda: _send_webhook(
                n["webhook"], text, sev)),
            ("discord", n["discord"], lambda: _send_discord(
                n["discord"], text, sev, kind,
                cfg_get("ups_name", ""),
                n.get("discord_mention") or None)),
            ("teams", n["teams"], lambda: _send_teams(
                n["teams"], text, sev)),
            ("email", n["smtp_host"] and n["smtp_to"],
             lambda: _send_email(n, text, sev))):
        if name not in route or not enabled:
            continue
        try:
            fn()
        except Exception as e:
            errs.append("%s: %s" % (name, e))
    return errs


_notify_err_last = {"text": "", "ts": 0.0}


def _notify_err(text):
    """Log a notify failure directly to events.json (no notify spawn —
    that would recurse). Dedup identical messages for 10 min."""
    try:
        now = time.time()
        if _notify_err_last["text"] == text and \
                now - _notify_err_last["ts"] < 600:
            return
        _notify_err_last.update(text=text, ts=now)

        def m(old):
            ev = old or []
            ev.append({"ts": int(now), "text": text, "sev": "warn"})
            return ev[-MAX_EVENTS:]
        _atomic_json(EVENTS_FILE, m)
    except Exception:
        pass


def _notify_hosts(n):
    """Hostnames of configured network channels (for the DNS wait)."""
    hosts = []
    for url in (n["webhook"], n["discord"], n["teams"]):
        try:
            h = urlparse(url).hostname
        except Exception:
            h = None
        if h:
            hosts.append(h)
    if n["smtp_host"] and n["smtp_to"]:
        hosts.append(n["smtp_host"])
    return hosts


def _net_up(hosts, timeout=90):
    """Wait for outbound DNS, up to `timeout`s. Events raised right after
    boot (e.g. "interface started") would otherwise fail instantly with
    a name-resolution error — networking isn't up yet. Cheap no-op once
    any configured host resolves."""
    end = time.time() + timeout
    while hosts and time.time() < end:
        for h in hosts:
            try:
                socket.getaddrinfo(h, None)
                return True
            except OSError:
                pass
        time.sleep(2)
    return False


def _notify_async(text, sev, kind="other"):
    try:
        n = notify_cfg()
        if _SEV_ORDER.get(sev, 0) < _SEV_ORDER.get(n["min_sev"], 1):
            return
        _net_up(_notify_hosts(n))
        errs = notify_dispatch(text, sev, n, kind)
        if errs:
            _notify_err("NMC: Notification failed \u2014 "
                        + "; ".join(errs))
    except Exception:
        pass


# ---- SNMP traps (nmc.conf [snmp]) ----------------------------------
# PowerNet-MIB v1-specific trap numbers for each STATUS_EVENTS flag
# (on-trap, off-trap).  Sent as v2c OID .1.3.6.1.4.1.318.0.<n>.
TRAP_EVENTS = {
    "OB": (5, 9), "LB": (7, 11), "RB": (17, 36), "OVER": (2, 33),
    "DISCHRG": (4, 38), "BYPASS": (22, 23), "CAL": (28, 37),
    "TEST": (None, 10), "FSD": (39, None), "OFF": (12, 30),
    "BOOST": (6, 34), "TRIM": (31, 35), "SD": (39, None),
}


def _conf_sinks():
    comm = cfg("snmp", "trapcommunity", "public")
    out = []
    for part in cfg("snmp", "trapsink", "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append((part if ":" in part else part + ":162", comm))
    return out


def _snmp_sink_slots():
    """Four writable receiver slots persisted in state.cfg.snmp_sinks
    (set via mconfigTrapReceiverTable SNMP SETs)."""
    try:
        slots = (load_state().get("cfg") or {}).get("snmp_sinks") or []
    except Exception:
        slots = []
    return (list(slots) + [None] * 4)[:4]


def _trap_sinks():
    out = _conf_sinks()
    for s in _snmp_sink_slots():
        if s and s.get("ip") not in (None, "", "0.0.0.0"):
            out.append((s["ip"] + ":162", s.get("comm") or "public"))
    return out


def fire_trap(num, flags=""):
    """Send enterprise trap .1.3.6.1.4.1.318.0.<num> to each configured
    sink via the snmptrap binary; failures are ignored."""
    if num is None:
        return
    for dest, comm in _trap_sinks():
        try:
            subprocess.run(
                ["snmptrap", "-v2c", "-c", comm, dest, "",
                 ".1.3.6.1.4.1.318.0.%d" % num,
                 ".1.3.6.1.4.1.318.1.1.1.11.1.1.0", "s", flags],
                capture_output=True, timeout=4, check=False)
        except Exception:
            pass


STATUS_EVENTS = {
    "OB": ("UPS: On battery power in response to an input power problem.",
           "UPS: No longer on battery power."),
    "LB": (("UPS: The battery power is too low to continue to support the "
            "load; the UPS will shut down if input power does not return to "
            "normal soon."), None),
    "RB": ("UPS: The battery needs to be replaced.",
           "UPS: The battery no longer needs to be replaced."),
    "OVER": ("UPS: An output overload condition exists.", None),
    "DISCHRG": ("UPS: The battery is discharged.", None),
    "BYPASS": ("UPS: On bypass.", "UPS: No longer on bypass."),
    "CAL": ("UPS: Started a runtime calibration.",
            "UPS: Stopped a runtime calibration."),
    "TEST": ("UPS: Started a self test.", "UPS: Finished a self test."),
    "FSD": ("UPS: A graceful shutdown process has started.", None),
    "OFF": ("UPS: Turned off.", "UPS: Turned on."),
    "BOOST": ("UPS: On automatic voltage regulation boost.",
              "UPS: No longer boosting input voltage."),
    "TRIM": ("UPS: On automatic voltage regulation trim.",
             "UPS: No longer trimming input voltage."),
    "CHRG": ("UPS: The battery is charging.",
             "UPS: The battery is no longer charging."),
    "HB": ("UPS: The battery voltage is too high.", None),
    "SD": ("UPS: A shutdown process has started.", None),
    "ALARM": ("UPS: An alarm condition is present.",
              "UPS: No longer in an alarm condition."),
}


def _run_hook(flag, onoff, status=""):
    """Fire the user hook script on UPS events ([notify] hook), if set."""
    script = cfg("notify", "hook", "").strip()
    if not script:
        return

    def run():
        try:
            subprocess.run([script, flag, onoff, status],
                           capture_output=True, timeout=10, check=False)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def _maybe_scheduled_selftest(ok):
    """Calendar self-test ([schedule] selftest_weekday/selftest_hour).
    Fires test.battery.start at most once per matching day."""
    wd, hr = (cfg("schedule", "selftest_weekday", "").strip(),
              cfg("schedule", "selftest_hour", "").strip())
    if not wd or not hr or not ok:
        return
    try:
        wd_i, hr_i = int(wd), int(hr)
    except ValueError:
        return
    t = time.localtime()
    today = time.strftime("%Y-%m-%d")
    if t.tm_wday == wd_i and t.tm_hour == hr_i and \
            cfg_get("selftest_last", "") != today:
        cfg_set("selftest_last", today)
        log_event("UPS: Scheduled self-test started.", "info")
        ups_cmd("test.battery.start")


def _auto_unmute_beeper(d, ok):
    """Re-arm the beeper after a card-tracked mute once the UPS leaves
    the alarm state, so the next alarm is audible again."""
    if not ok or not load_state().get("beeper_muted"):
        return
    if alarms(d)[1] != "pill-ok" or d.get("ups.alarm"):
        return
    b = (d.get("ups.beeper.status") or "").strip().lower()
    if b in ("enabled", "disabled"):
        save_state({"beeper_muted": False})
        return
    r = ups_cmd("beeper.enable")   # success also clears beeper_muted
    if r.startswith("OK"):
        log_event("UPS: Audible alarm re-enabled; the muted alarm "
                  "condition cleared.")
    elif "CMD-NOT-SUPPORTED" in r or "INVALID-ARGUMENT" in r:
        save_state({"beeper_muted": False})


def status_poller():
    prev = None
    comm_ok = None
    batt_age_checked = False
    while True:
        try:
            d = ups_vars()
            ok = True
        except Exception:
            d = {}
            ok = False
        if comm_ok is False and ok:
            log_event("UPS: Restored the local network management "
                      "interface-to-UPS communication.",
                      kind="comm_restored")
            fire_trap(8)    # communicationEstablished
            _run_hook("COMM", "on")
        elif comm_ok and not ok:
            log_event("UPS: Lost the local network management "
                      "interface-to-UPS communication.",
                      kind="comm_lost")
            fire_trap(1)    # communicationLost
            _run_hook("COMM", "off")
        comm_ok = ok
        _maybe_scheduled_selftest(ok)
        if ok and not batt_age_checked:
            batt_age_checked = True
            age = battery_age_years()
            raw_d = str(cfg_get(
                "battery_replace_date",
                cfg("device", "battery_replace_date", "")))
            if age is not None and age >= 3 and \
                    cfg_get("batt_age_warned", "") != raw_d:
                log_event("UPS: The battery is %.0f years old; "
                          "plan a replacement." % age, "warn")
                cfg_set("batt_age_warned", raw_d)
        ts = toks(d)
        if prev is not None:
            for flag, (on, off) in STATUS_EVENTS.items():
                trap_on, trap_off = TRAP_EVENTS.get(flag, (None, None))
                if flag in ts and flag not in prev:
                    if on:
                        log_event(on, kind=_KIND_FOR.get((flag, "on")))
                    fire_trap(trap_on, " ".join(sorted(ts)))
                    _run_hook(flag, "on", " ".join(sorted(ts)))
                elif flag in prev and flag not in ts:
                    if off:
                        log_event(off, kind=_KIND_FOR.get((flag, "off")))
                    fire_trap(trap_off, " ".join(sorted(ts)))
                    _run_hook(flag, "off", " ".join(sorted(ts)))
            if "OB" in ts and "OB" not in prev:
                save_state({"last_transfer": time.strftime("%m/%d/%Y %H:%M:%S")})
        _auto_unmute_beeper(d, ok)
        prev = ts
        time.sleep(10)


_datalog = {"arr": None, "dirty": 0}
_datalog_lock = threading.Lock()


def _flush_datalog():
    with _datalog_lock:
        if _datalog["arr"] is None or not _datalog["dirty"]:
            return
        arr = list(_datalog["arr"])
        _datalog["dirty"] = 0
    _atomic_json(DATA_FILE, lambda old: arr)


def data_logger():
    """Sample key metrics every 60s; flush to SD in batches (N1:
    one file rewrite per `datalog_batch` samples instead of per sample)."""
    keys = ("battery.charge", "battery.runtime", "battery.temperature",
            "battery.voltage", "ups.load", "ups.realpower", "ups.power",
            "input.voltage", "output.voltage", "output.frequency")
    while True:
        try:
            d = ups_vars()
            rec = {"ts": int(time.time())}
            for k in keys:
                v = d.get(k)
                if v is not None:
                    rec[k] = v
            rec["st"] = d.get("ups.status", "")

            keep = int(cfg_get("datalog_hours", 25) or 25) * 60
            batch = int(cfg_get("datalog_batch", 15) or 15)
            with _datalog_lock:
                if _datalog["arr"] is None:
                    try:
                        with open(DATA_FILE) as f:
                            _datalog["arr"] = json.load(f)
                    except Exception:
                        _datalog["arr"] = []
                _datalog["arr"].append(rec)
                del _datalog["arr"][:-keep]
                _datalog["dirty"] += 1
                n = _datalog["dirty"]
            if n >= batch:
                _flush_datalog()
        except Exception:
            pass
        time.sleep(60)


def load_data():
    with _datalog_lock:
        if _datalog["arr"] is not None:
            return list(_datalog["arr"])
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return []


# ============================================================ helpers
def toks(d):
    ts = set(d.get("ups.status", "").split())
    # apc_modbus's CHRG bit flaps (it even pulses during discharges), so
    # a raw CHRG produces bogus "charging"/"no longer" flapping. Normalize:
    # charging requires OL; charge<100 counts as charging, and charge>=100
    # clears a lingering hardware CHRG.
    c = num(d, "battery.charge")
    if "OL" not in ts or (c is not None and c >= 100):
        ts.discard("CHRG")
    elif c is not None:
        ts.add("CHRG")
    return ts


def esc(x):
    return html.escape(str(x))


def num(d, key, default=None):
    try:
        return float(d.get(key))
    except (TypeError, ValueError):
        return default


def battery_age_years():
    """Years since battery_replace_date (MM/DD/YY or MM/DD/YYYY);
    None if unset or unparseable."""
    raw = str(cfg_get("battery_replace_date",
                      cfg("device", "battery_replace_date", ""))).strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            dt = time.strptime(raw, fmt)
            return (time.time() - time.mktime(dt)) / (365.25 * 86400)
        except (ValueError, OverflowError):
            continue
    return None


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def humanize(v):
    """LoadTooLow -> Load Too Low; AcceptableInput -> Acceptable Input."""
    if not v or not isinstance(v, str):
        return v
    s = _CAMEL.sub(" ", v)
    s = s.replace("_", " ")
    return s[0].upper() + s[1:] if s else s


def test_result_str(v):
    """'Refused, Source: Internal, Modifier: StateOfChargeNotAcceptable'
    -> 'Refused (state of charge not acceptable)'"""
    if not v:
        return v
    head = v.split(",")[0]
    mod = ""
    m = re.search(r"Modifier:\s*(\w+)", v)
    if m:
        mod = " (%s)" % humanize(m.group(1)).lower()
    return head + mod


def fmt_runtime(d):
    try:
        s = int(float(d.get("battery.runtime", 0)))
        return "%dhr %dmin %dsec" % (s // 3600, s % 3600 // 60, s % 60)
    except Exception:
        return "unknown"


def power_state(d):
    ts = toks(d)
    if not d:
        return ("Communication Lost", "st-crit")
    if "OFF" in ts:
        return ("Off", "st-crit")
    if "OB" in ts:
        return ("On Battery", "st-warn")
    if "BYPASS" in ts:
        return ("On Bypass", "st-warn")
    if "OL" in ts:
        return ("On Line", "st-ok")
    return ("Unknown", "st-warn")


def alarms(d):
    ts = toks(d)
    if not d:
        return ("Communication Lost", "pill-crit")
    if ts & {"ALARM", "LB", "RB", "OVER", "DISCHRG"}:
        return ("Critical Alarm Present", "pill-crit")
    if ts & {"OB", "BYPASS", "SD", "FSD"}:
        return ("Warning Alarm Present", "pill-warn")
    return ("No Alarms", "pill-ok")


def alarm_lines(d):
    ts = toks(d)
    out = []
    if "OL" in ts:
        out.append("UPS is Online.")
    if "OB" in ts:
        out.append("UPS is On Battery.")
    if "LB" in ts:
        out.append("UPS battery is low.")
    if "RB" in ts:
        out.append("UPS battery needs replacement.")
    if "CHRG" in ts:
        out.append("UPS battery is charging.")
    if "DISCHRG" in ts:
        out.append("UPS battery is discharged.")
    if "CAL" in ts:
        out.append("UPS is performing a runtime calibration.")
    if "TEST" in ts:
        out.append("UPS is performing a self test.")
    if "OVER" in ts:
        out.append("UPS has an overload.")
    if "BYPASS" in ts:
        out.append("UPS is on bypass.")
    if "OFF" in ts:
        out.append("UPS is Off.")
    a = d.get("ups.alarm")
    if a:
        out.append("Alarm: " + humanize(a))
    if not d:
        out.append("UPS communication lost.")
    return out or ["UPS is Online."]


def fmt_cmd_result(r):
    if r.startswith("OK"):
        return ("msg-ok", "Command issued successfully.")
    if "CMD-NOT-SUPPORTED" in r:
        return ("msg-err", "The connected UPS does not support this command.")
    if "ACCESS-DENIED" in r:
        return ("msg-err", "Access denied by the UPS daemon.")
    if "ALREADY-" in r:
        return ("msg-err",
                "Command already in progress / already in that state.")
    return ("msg-err", "UPS returned: " + humanize(r.removeprefix("ERR ")))


def diag_schedule_str():
    """PowerNet-MIB upsTestDiagnosticsSchedule enum (1-12)."""
    n = int(cfg_get("diag_schedule", 8))
    if n == 1:
        return "Unknown"
    if n == 2:
        return "Biweekly"
    if n == 3:
        return "Weekly"
    if 4 <= n <= 12:
        return "Every %d weeks" % (4 * (n - 3))
    return "Schedule %d" % n


def nr(v):
    """Render a value, or a muted 'Not reported' placeholder."""
    if v is None or v == "":
        return '<span class="nr">Not reported by UPS</span>'
    return v


# ============================================================ page frame
_SVG = {
    "home": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9 21v-6h6v6"/></svg>',
    "status": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 8L9 4l-3 8H2"/></svg>',
    "control": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="9" cy="6" r="2.2" fill="currentColor"/><circle cx="15" cy="12" r="2.2" fill="currentColor"/><circle cx="7" cy="18" r="2.2" fill="currentColor"/></svg>',
    "config": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 8.86 1.7 1.7 0 0 0 4.26 7l-.06-.06A2 2 0 1 1 7.03 4.1l.06.06A1.7 1.7 0 0 0 8.96 4.5 1.7 1.7 0 0 0 10 2.94V3a2 2 0 1 1 4 0v.09c.68.28 1.4.28 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9c.28.68.92 1.03 1.56 1.03H21a2 2 0 1 1 0 4h-.09c-.64 0-1.23.35-1.51.97z"/></svg>',
    "tests": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2v7.5L4.5 20a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 9.5V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/></svg>',
    "logs": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
    "about": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
}

NAV = [
    ("Home", "/home", "home", []),
    ("Status", "/status/ups", "status", [("UPS Status", "/status/ups")]),
    ("Control", "/control/ups", "control",
     [("UPS Control", "/control/ups"),
      ("Outlet Groups", "/control/outlets")]),
    ("Configuration", "/config/ups", "config", [("UPS", "/config/ups")]),
    ("Tests", "/test/diag", "tests", [("Diagnostics", "/test/diag")]),
    ("Logs", "/log/event", "logs",
     [("Event Log", "/log/event"), ("Data Log", "/log/data"),
      ("events.txt", "/events.txt"), ("data.txt", "/data.txt")]),
    ("About", "/about/system", "about",
     [("System", "/about/system"), ("Network", "/about/network"),
      ("Host", "/about/host"), ("UPS Variables", "/about/nut")]),
]


# Static css/js live in apc-nmc-web/ beside this script so they can be
# edited with proper tooling.  _asset() re-reads a file when its mtime
# changes, so edits take effect on the next page load — no restart needed.
ASSET_DIR = os.environ.get(
    "APC_NMC_ASSETS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "apc-nmc-web"))
_asset_cache = {}
_asset_lock = threading.Lock()


def _asset(name):
    try:
        mt = os.stat(os.path.join(ASSET_DIR, name)).st_mtime_ns
    except OSError:
        mt = -1
    with _asset_lock:
        hit = _asset_cache.get(name)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            with open(os.path.join(ASSET_DIR, name),
                      encoding="utf-8") as f:
                data = f.read()
        except OSError:
            print("apc-nmc-web: cannot read asset %s/%s"
                  % (ASSET_DIR, name), file=sys.stderr)
            data = ""
        _asset_cache[name] = (mt, data)
        return data


def _active_section(path):
    for name, link, icon, sub in NAV:
        if path in [link] + [u for _, u in sub]:
            return name
    return ""


def csrf_input(d):
    return ('<input type="hidden" name="_csrf" value="%s">'
            % esc(d.get("__csrf", "")))


def crumbs(section, leaf):
    return ('<div class="crumbs"><a href="/home">Home</a> &rsaquo; %s '
            '&rsaquo; <b>%s</b></div>' % (esc(section), esc(leaf)))


SUN = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
       'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
       '<circle cx="12" cy="12" r="4.5"/>'
       '<path d="M12 2v2.5M12 19.5V22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8'
       'M2 12h2.5M19.5 12H22M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>')


def page(title, body, d, path="", section="", leaf=""):
    alabel, acls = alarms(d)
    active = _active_section(path)
    nav = ""
    mnav = ""
    for name, link, icon, sub in NAV:
        cls = 'here' if name == active and (not sub or path == link) else ""
        nav += ('<a href="%s" class="%s">%s<span>%s</span></a>'
                % (link, cls, _SVG[icon], esc(name)))
        mnav += '<a href="%s" class="%s">%s</a>' % (link, cls, esc(name))
        if sub:
            items = "".join(
                '<a href="%s"%s>%s</a>'
                % (u, ' class="here" aria-current="page"' if u == path
                   else "", esc(t)) for t, u in sub)
            nav += '<div class="sub">%s</div>' % items
            mnav += '<div class="sub">%s</div>' % items
    crumb = crumbs(section, leaf) if leaf else ""
    updbar = ""
    us = _load_json(UPDATE_STATE)
    if us.get("available"):
        updbar = ('<div class="updbar">Software update '
                  '<a href="/about/system">%s</a> is available</div>'
                  % esc(us.get("latest", "?")))
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<script>%s</script><title>APC | %s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="same-origin">
<style>%s</style></head><body>
<div class="topbar">
 <div class="brand">
  <div class="se-mark">SE</div>
  <div class="brand-txt"><b>APC</b>
   <span>%s &mdash; NMC</span></div>
 </div>
 <div class="topright">
  <span class="livedot" id="liveDot" title="Receiving live UPS data"></span>
  <a class="pill %s" id="alarmPill" href="/status/ups"
   title="View UPS status">%s</a>
  <button class="themebtn" onclick="nmcTheme()" title="Toggle dark/light mode"
   aria-label="Toggle theme">%s</button>
  <span class="uinfo">%s</span>
  <a href="/logoff">Log Off</a>
 </div>
</div>
%s
<div class="shell">
<nav class="side">%s</nav>
<details class="mnav"><summary>Menu</summary>%s</details>
<main><div class="content">%s%s</div>
<div class="ftr"><div><a href="/about/system">System</a> &middot;
<a href="/about/network">Network</a> &middot;
<a href="/log/event">Event Log</a></div>
<div>UPS Network Management Card 2 &middot; %s &middot; Updated: %s</div>
</div></main>
</div>
<script>%s</script>
</body></html>""" % (
        _asset("theme.js"), esc(title), _asset("nmc.css"),
        esc(d.get("ups.model", "Smart-UPS")),
        acls, esc(alabel), SUN, esc(WEB_USER),
        updbar, nav, mnav, crumb, body,
        esc(NMC_FW), time.strftime("%m/%d/%Y at %H:%M"),
        _asset("nmc.js"))


def login_page(err="", next_url="/home", locked=0):
    e = '<div class="msg-err">%s</div>' % esc(err) if err else ""
    if locked:
        e = ('<div class="msg-err">Too many failed attempts. '
             'Try again in %d minutes.</div>' % max(1, locked // 60 + 1))
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<script>%s</script><title>APC | Log On</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>%s</style></head><body><div class="loginwrap">
<div class="loginbox">
<div style="display:flex;align-items:center;gap:12px">
<div class="se-mark">SE</div>
<div style="font-weight:bold;font-size:16px">APC <span style="font-weight:normal;color:var(--mut)">by</span> Schneider Electric</div></div>
<h2>Log On</h2>
<div class="sub">UPS Network Management Card 2</div>
<form method="post" action="/login">
<input type="hidden" name="next" value="%s">
<label for="u">User Name</label><input id="u" name="u" autocomplete="username" autofocus>
<label for="p">Password</label><input id="p" name="p" type="password" autocomplete="current-password">
<button class="btn" id="logon">Log On &gt;&gt;</button></form>%s
</div>
<div class="loginfoot">APC by Schneider Electric &mdash; AOS %s</div>
</div></body></html>""" % (_asset("theme.js"), _asset("nmc.css"),
                              esc(next_url), e, esc(NMC_FW))


# ============================================================ pages
def tile(lbl, val, sub="", bar=None, cls="", vid="", tip=""):
    b = ('<div class="bar"><i style="width:%s%%"></i></div>'
         % min(100, max(0, bar))) if bar is not None else ""
    t = ' title="%s"' % esc(tip) if tip else ""
    return ('<div class="tile %s"%s><div class="t-lbl">%s</div>'
            '<div class="t-val"%s>%s</div><div class="t-sub">%s</div>%s</div>'
            % (cls, t, esc(lbl), ' data-var="%s"' % vid if vid else "",
               esc(val), esc(sub), b))


def _home_frags(d):
    """Live regions of the Home card, keyed by element id — polled via
    /status.json?frag=1 and swapped in place (no page reload)."""
    slab, scls = power_state(d)
    lab, cls = alarms(d)
    lines = "".join("<li>%s</li>" % esc(x) for x in alarm_lines(d))
    evs = load_events()[-6:]
    rows = "".join(
        "<tr><td><span class='dot %s'></span></td><td>%s</td><td>%s</td>"
        "<td>%s</td></tr>" % (
            esc(e.get("sev", "info")),
            time.strftime("%m/%d/%Y", time.localtime(e["ts"])),
            time.strftime("%H:%M:%S", time.localtime(e["ts"])),
            esc(e["text"]))
        for e in reversed(evs)) or \
        '<tr><td colspan="4" class="note">No events logged yet.</td></tr>'
    return {
        "hs": ('<b>State</b><br><span class="state %s">%s</span>'
               % (scls, esc(slab))),
        "hp": ('<a class="pill %s" href="/status/ups">%s</a>'
               '<ul class="stat" style="margin:10px 0 0 20px;padding:0">'
               '%s</ul>' % (cls, esc(lab), lines)),
        "ht": esc(load_state().get("last_transfer", "None")),
        "he": rows,
    }


def pg_home(d):
    fr = _home_frags(d)
    loc = cfg_get("location", LOCATION)

    charge = num(d, "battery.charge", 0) or 0
    charge_cls = "crit" if charge < 20 else ("warn" if charge < 50 else "")
    loadp = num(d, "ups.load")
    watts = num(d, "ups.realpower")
    rt_sec = int(num(d, "battery.runtime", 0) or 0)
    rt_cls = "warn" if rt_sec and rt_sec < 1800 else ""
    tiles = (
        tile("Battery Charge", "%g %%" % charge, "capacity",
             bar=charge, cls=charge_cls, vid="battery.charge",
             tip="Remaining battery capacity")
        + tile("Runtime", "%dh %dm" % divmod(rt_sec // 60, 60),
               "remaining", cls=rt_cls, vid="battery.runtime",
               tip="Estimated runtime at current load")
        + (tile("Load", "%g %%" % loadp, "of capacity", bar=loadp,
                vid="ups.load") if loadp is not None else "")
        + (tile("Output", "%g W" % watts,
                "%s VA" % d.get("ups.power", "?"), vid="ups.realpower")
           if watts is not None else "")
        + (tile("Input", "%g V" % num(d, "input.voltage", 0),
                "%s Hz" % d.get("input.frequency", "?"),
                vid="input.voltage") if "input.voltage" in d else "")
        + (tile("Output V", "%g V" % num(d, "output.voltage", 0),
                "%s Hz" % d.get("output.frequency", "?"),
                vid="output.voltage") if "output.voltage" in d else "")
        + (tile("Battery Temp", "%g \u00b0C"
                % num(d, "battery.temperature", 0),
                "internal", vid="battery.temperature")
           if "battery.temperature" in d else "")
    )
    return """<h1 class="page">Home</h1>
<div class="tiles">%s</div>
<div class="card"><div class="pad"><table class="kv">
<tr><td class="lbl"><b>Model</b><br>%s</td>
    <td><b>Location</b><br>%s</td>
    <td id="hs">%s</td></tr>
<tr><td colspan="3" id="hp">%s</td></tr>
<tr><td class="lbl"><b>Last Battery Transfer</b></td>
    <td colspan="2" id="ht">%s</td></tr>
</table></div></div>
<div class="card"><h3>Recent Device Events</h3><div class="pad">
<table class="ev"><thead><tr><th style="width:20px"></th>
<th style="width:105px">Date</th><th style="width:85px">Time</th>
<th>Event</th></tr></thead><tbody id="he">%s</tbody></table>
<div style="text-align:right;margin-top:12px">
<a class="btn sec" href="/log/event">More Events &#8250;</a></div>
</div></div>
<script>%s</script>""" % (tiles, esc(d.get("ups.model", "?")), esc(loc),
                fr["hs"], fr["hp"], fr["ht"], fr["he"], _asset("home.js"))


def kv(label, val):
    return '<tr><td class="lbl">%s</td><td>%s</td></tr>' % (esc(label), val)


def _v(d, key, fmt="%s"):
    v = d.get(key)
    return esc(fmt % v) if v not in (None, "") else nr(None)


def _status_frags(d):
    """Live regions of the UPS Status page, keyed by element id —
    polled via /status.json?frag=1 and swapped in place (no reload)."""
    st = load_state()
    lt = st.get("last_transfer", "None")
    tr = d.get("input.transfer.reason")
    if tr and tr not in ("None", "NoTransfer", "AcceptableInput"):
        lt += " (%s)" % humanize(tr)

    top = kv("Last Battery Transfer", esc(lt))
    top += kv("Battery Charge", _v(d, "battery.charge", "%s %%"))
    top += kv("Runtime Remaining", fmt_runtime(d))
    top += kv("Battery Voltage", _v(d, "battery.voltage", "%s VDC"))
    if "battery.temperature" in d:
        top += kv("Battery Temperature", "%s &#8451;"
                  % esc(d["battery.temperature"]))
    if "battery.voltage.nominal" in d:
        top += kv("Nominal Battery Voltage", "%s VDC"
                  % esc(d["battery.voltage.nominal"]))
    top += kv("Battery Replacement", "Not indicated"
              if "RB" not in toks(d) else "Battery needs replacement")
    if "battery.date" in d:
        top += kv("Battery Install Date", esc(d["battery.date"]))
    if "battery.date.maintenance" in d:
        top += kv("Next Battery Maintenance", esc(d["battery.date.maintenance"]))
    age = battery_age_years()
    if age is not None:
        top += kv("Battery Age",
                  "%.1f years" % age if age < 3 else
                  '<span style="color:#b00020">%.1f years &mdash; '
                  'consider replacement</span>' % age)
    if d.get("ups.alarm"):
        top += kv("UPS Alarm",
                  '<span style="color:#b00020">%s</span>'
                  % esc(humanize(d["ups.alarm"])))
    if (d.get("ups.beeper.status") or "").strip().lower() == "muted":
        top += kv("Audible Alarm",
                  "Muted" + (" &mdash; re-arms when the alarm clears"
                             if st.get("beeper_muted") else ""))
    obc = int(st.get("ob_count", 0) or 0)
    obs = int(st.get("total_ob_seconds", 0) or 0)
    if st.get("ob_since"):
        obs += int(time.time() - st["ob_since"])
    if obc or obs:
        top += kv("Transfers to Battery", str(obc))
        top += kv("Total Time on Battery",
                  "%d hr %d min" % (obs // 3600, obs % 3600 // 60))
        if obc:
            top += kv("Average Outage",
                      "%d min %d s" % (obs // obc // 60, obs // obc % 60))
        obm = int(st.get("ob_max", 0) or 0)
        if obm:
            top += kv("Longest Outage",
                      "%d min %d s" % (obm // 60, obm % 60))

    inp = kv("Input Voltage", _v(d, "input.voltage", "%s VAC"))
    if "input.frequency" in d:
        inp += kv("Input Frequency", "%s Hz" % esc(d["input.frequency"]))
    if "input.transfer.low" in d:
        inp += kv("Transfer Range",
                  "%s &ndash; %s VAC" % (esc(d["input.transfer.low"]),
                                         esc(d.get("input.transfer.high",
                                                   "?"))))
    if tr:
        inp += kv("Last Transfer Reason", esc(humanize(tr)))

    out = kv("Output Voltage", _v(d, "output.voltage", "%s VAC"))
    if "output.frequency" in d:
        out += kv("Output Frequency", "%s Hz" % esc(d["output.frequency"]))
    out += kv("Output Load", _v(d, "ups.load", "%s %%"))
    if "ups.power" in d:
        out += kv("Output VA", "%s VA" % esc(d["ups.power"]))
    if "ups.realpower" in d:
        out += kv("Output Watts", "%s W" % esc(d["ups.realpower"]))
    if "output.current" in d:
        out += kv("Load Current", "%s Amps" % esc(d["output.current"]))
    eff = d.get("ups.efficiency")
    out += kv("Output Efficiency", esc(humanize(eff)) if eff
              else nr(None))
    out += kv("Output Energy Usage", "%s kWh"
              % esc(d["experimental.output.energy"])
              if "experimental.output.energy" in d else nr(None))

    timers = ""
    for key, lab in (("ups.timer.shutdown", "Shutdown Timer"),
                     ("ups.timer.reboot", "Reboot Timer"),
                     ("ups.timer.start", "Start Timer")):
        if key in d:
            timers += kv(lab, esc(humanize(d[key])))
    timercard = ('<div class="card"><h3>Timers</h3><div class="pad">'
                 '<table class="kv">%s</table></div></div>' % timers) \
        if timers else ""

    env = ""
    if "battery.temperature" in d:
        env = ('<div class="card"><h3>Environment</h3><div class="pad">'
               '<table class="kv">%s</table></div></div>'
               % kv("Internal Temperature",
                    "%s &#8451;" % esc(d["battery.temperature"])))

    return {"stTop": top, "stInp": inp, "stOut": out,
            "stTimers": timercard, "stEnv": env}


def pg_status(d, msg: "str | tuple" = ""):
    fr = _status_frags(d)
    m = '<div class="%s">%s</div>' % msg if msg else ""
    return """<h1 class="page">UPS Status</h1>%s
<div class="card"><h3>%s</h3><div class="pad"><table class="kv"><tbody id="stTop">%s</tbody></table>
<form method="post" action="/status/battery" style="margin-top:14px"
 onsubmit="return confirm('Mark the battery as replaced? The battery install date will be set to today and the on-battery statistics reset.')">
%s<button class="btn sec" type="submit">Battery Replaced</button></form></div></div>
<div class="card"><h3>UPS Input</h3><div class="pad"><table class="kv"><tbody id="stInp">%s</tbody></table></div></div>
<div class="card"><h3>UPS Output</h3><div class="pad"><table class="kv"><tbody id="stOut">%s</tbody></table></div></div>
<div id="stTimers">%s</div><div id="stEnv">%s</div>
<script>%s</script>""" % (m, esc(d.get("ups.model", "UPS")), fr["stTop"],
                csrf_input(d), fr["stInp"], fr["stOut"], fr["stTimers"],
                fr["stEnv"], _asset("status.js"))


def pg_control(d, msg: "str | tuple" = "", cmds=None):
    m = '<div class="%s">%s</div>' % msg if msg else ""
    cmds = cmds if cmds is not None else []
    can = set(cmds)
    opts = []
    if "shutdown.reboot" in can or not can:
        opts.append(("reboot", "Reboot UPS", 1))
    if "load.on" in can:
        opts.append(("on", "Turn UPS On", 0))
    if "load.off" in can or "load.off.delay" in can or not can:
        opts.append(("off", "Turn UPS Off", 0))
    if "shutdown.return" in can or "shutdown.stayoff" in can or not can:
        opts.append(("sleep", "Put UPS to Sleep", 0))
    if "bypass.start" in can:
        opts.append(("bypass", "Put UPS in Bypass", 0))
    if "beeper.mute" in can:
        opts.append(("mute", "Mute Beeper", 0))
    radios = "".join(
        '<label class="f"><input type="radio" name="act" value="%s"%s> '
        '%s</label>' % (v, " checked" if first else "", esc(t))
        for v, t, first in opts)
    sdel = num(d, "ups.delay.shutdown", 0) or 0
    delnote = ('<p class="note" style="margin-top:6px">Configured shutdown '
               'delay: %d s.</p>' % int(sdel)) if sdel else ""
    return """<h1 class="page">UPS Control</h1>%s
<div class="card"><h3>Control the UPS</h3><div class="pad">
<form method="post" action="/control/ups">
%s
%s
<label class="f" style="color:#666"><input type="checkbox" name="pcns" value="1"> Signal PowerChute Network Shutdown Clients</label>
<label class="f"><input type="checkbox" name="skipdel" value="1"> Skip outlet off delays</label>%s
<div style="margin-top:14px">
<button class="btn" type="submit">Next &#187;</button>
<a class="btn sec" href="/home">Cancel</a></div>
</form></div></div>""" % (m, csrf_input(d), radios, delnote)


def pg_control_confirm(d, act, skipdel, pcns):
    desc = {"reboot": "Reboot UPS", "off": "Turn UPS Off",
            "sleep": "Put UPS to Sleep", "on": "Turn UPS On",
            "bypass": "Put UPS in Bypass",
            "mute": "Mute the UPS beeper"}.get(act, act)
    impact = {
        "reboot": "The UPS will briefly remove and restore output power.",
        "off": "Output power will be removed and will NOT return "
               "automatically.",
        "on": "Output power will be restored.",
        "sleep": "Output power will be removed until input power returns.",
        "bypass": "The load will run directly from input power with no "
                  "battery backup.",
        "mute": "Output power is unaffected; the audible alarm is "
                "silenced until the next alarm condition.",
    }.get(act, "")
    return """<h1 class="page">UPS Control</h1>
<div class="card"><h3>Confirm</h3><div class="pad">
<p style="font-size:15px">Are you sure you want to <b>%s</b>?%s%s</p>
<p class="note">%s</p>
<form method="post" action="/control/ups/exec">
%s
<input type="hidden" name="act" value="%s">
<input type="hidden" name="skipdel" value="%s">
<input type="hidden" name="pcns" value="%s">
<button class="btn dan" type="submit">Apply</button>
<a class="btn sec" href="/control/ups">Cancel</a></form>
</div></div>""" % (
        esc(desc),
        " <span class='note'>(off delays will be skipped)</span>"
        if skipdel else "",
        " <span class='note'>(PowerChute clients will be signalled)</span>"
        if pcns else "", esc(impact), csrf_input(d),
        esc(act), esc(skipdel), esc(pcns))


def outlet_groups(d):
    groups = []
    try:
        n = int(d.get("outlet.group.count", 0))
    except ValueError:
        n = 0
    for i in range(n):
        g = {
            "idx": str(i),
            "id": d.get("outlet.group.%d.id" % i, str(i)),
            "name": d.get("outlet.group.%d.name" % i, "Group %d" % i),
            "status": d.get("outlet.group.%d.status" % i, "?"),
            "delay_off": d.get("outlet.group.%d.delay.shutdown" % i),
            "delay_reboot": d.get("outlet.group.%d.delay.reboot" % i),
            "delay_start": d.get("outlet.group.%d.delay.start" % i),
            "timer_shutdown": d.get("outlet.group.%d.timer.shutdown" % i),
            "timer_reboot": d.get("outlet.group.%d.timer.reboot" % i),
        }
        groups.append(g)
    return groups


def pg_outlets(d, msg: "str | tuple" = ""):
    m = '<div class="%s">%s</div>' % msg if msg else ""
    groups = outlet_groups(d)
    rows = ""
    for g in groups:
        on = g["status"] == "on"
        delays = []
        if g["delay_off"] is not None:
            delays.append("off %ss" % g["delay_off"])
        if g["delay_reboot"] is not None:
            delays.append("reboot %ss" % g["delay_reboot"])
        if g["delay_start"] is not None:
            delays.append("start %ss" % g["delay_start"])
        timers = []
        if g["timer_shutdown"] and g["timer_shutdown"] != "NotActive":
            timers.append("shutdown: " + g["timer_shutdown"])
        if g["timer_reboot"] and g["timer_reboot"] != "NotActive":
            timers.append("reboot: " + g["timer_reboot"])
        sub = "; ".join(delays + timers)
        rows += ("""<tr data-gidx="%s">
<td>%s</td><td>%s</td>
<td><span class="dot %s"></span><span class="gstat">%s</span>
<div class="note" style="font-size:12px">%s</div></td>
<td style="text-align:right;white-space:nowrap">
<form method="post" action="/control/outlets" style="display:inline;margin-right:10px">
%s
<input type="hidden" name="gid" value="%s">
<input type="hidden" name="act" value="cycle">
<button class="btn sec" type="submit">Cycle</button>
</form><form method="post" action="/control/outlets" style="display:inline">
%s
<input type="hidden" name="gid" value="%s">
<input type="hidden" name="act" class="swact" value="%s">
<label class="sw"><input type="checkbox" class="swin" onchange="this.form.submit()"%s>
<span class="tr"></span></label><span class="swtxt gsw">%s</span>
</form></td></tr>""" % (
            esc(g["idx"]), esc(g["id"]), esc(g["name"]),
            "on" if on else "off", esc(g["status"]), esc(sub),
            csrf_input(d), esc(g["idx"]), csrf_input(d), esc(g["idx"]),
            "off" if on else "on", " checked" if on else "",
            "On" if on else "Off"))
    if not rows:
        rows = ('<tr><td colspan="4" class="note">No outlet groups '
                'reported by the UPS.</td></tr>')
    cfg_rows = ""
    for g in groups:
        cfg_rows += ("""<tr><td class="lbl">%s</td>
<td><input name="g%s_name" maxlength="16" value="%s" style="width:150px"></td>
<td><input name="g%s_sd" type="number" min="0" max="3600" value="%s" style="width:80px"></td>
<td><input name="g%s_rd" type="number" min="0" max="3600" value="%s" style="width:80px"></td>
<td><input name="g%s_od" type="number" min="0" max="3600" value="%s" style="width:80px"></td></tr>"""
                     % (esc(g["name"]), esc(g["idx"]), esc(g["name"]),
                        esc(g["idx"]), esc(g["delay_off"] or ""),
                        esc(g["idx"]), esc(g["delay_reboot"] or ""),
                        esc(g["idx"]), esc(g["delay_start"] or "")))
    cfgcard = ""
    if cfg_rows:
        cfgcard = ("""<div class="card"><h3>Outlet Group Configuration</h3>
<div class="pad">
<form method="post" action="/config/outlets">%s
<table class="ev"><tr><th class="lbl" style="width:180px">Group</th><th>Name</th>
<th>Shutdown delay s</th><th>Reboot delay s</th><th>Power-on delay s</th></tr>
%s</table>
<div style="margin-top:14px"><button class="btn">Apply</button></div>
</form></div></div>""" % (csrf_input(d), cfg_rows))
    return """<h1 class="page">Outlet Groups</h1>%s
<div class="card"><h3>Switched Outlet Groups</h3><div class="pad">
<table class="ev"><tr><th style="width:80px">ID</th><th>Name</th>
<th style="width:200px">Status</th>
<th style="text-align:right;width:220px">Control</th></tr>%s</table>
<p class="note" style="margin-top:12px">Use the switch to turn a group on or
off, or 'Cycle' to remove and restore power. Every action requires
confirmation before it is sent to the UPS. This page refreshes
automatically.</p>
</div></div>%s
<script>%s</script>""" % (m, rows, cfgcard, _asset("outlets.js"))


def pg_outlet_confirm(d, gid, act):
    groups = {g["idx"]: g for g in outlet_groups(d)}
    g = groups.get(gid, {"id": gid, "name": "Group %s" % gid})
    verb = {"cycle": "cycle power on", "off": "turn OFF",
            "on": "turn ON"}.get(act, act)
    warn = {"cycle": "briefly remove and restore",
            "off": "remove", "on": "restore"}.get(act, "change")
    extra = ""
    if g["id"] == "MOG" or "main" in g["name"].lower():
        extra = (" <b style='color:#b00020'>This is the main outlet group"
                 " &mdash; it powers all connected equipment.</b>")
    return """<h1 class="page">Outlet Groups</h1>
<div class="card"><h3>Confirm</h3><div class="pad">
<p style="font-size:15px">Are you sure you want to <b>%s</b>
outlet group <b>%s &mdash; %s</b>?%s</p>
<p class="note">This will %s power to the equipment connected to this
outlet group.</p>
<form method="post" action="/control/outlets/exec">
%s
<input type="hidden" name="gid" value="%s">
<input type="hidden" name="act" value="%s">
<button class="btn dan" type="submit">Apply</button>
<a class="btn sec" href="/control/outlets">Cancel</a></form>
</div></div>""" % (esc(verb), esc(g["id"]), esc(g["name"]), extra, esc(warn),
                  csrf_input(d), esc(gid), esc(act))


def pg_tests(d, msg: "str | tuple" = ""):
    st = load_state()
    m = '<div class="%s">%s</div>' % msg if msg else ""
    result = d.get("ups.test.result") or \
        {1: "Passed", 2: "Failed", 3: "Invalid", 4: "In Progress"}.get(
            int(st.get("diag_result", 1)))
    testdate = d.get("ups.test.date") or st.get("diag_date", "UNKNOWN")
    cal = "CAL" in toks(d)
    testing = "TEST" in toks(d)
    loadp = num(d, "ups.load")
    charge = num(d, "battery.charge")
    req = ""
    if not cal:
        missing = []
        if charge is not None and charge < 99:
            missing.append("battery fully charged (now %g%%)" % charge)
        if loadp is not None and loadp < 30:
            missing.append(
                "constant load of at least ~30%% (now %g%%)" % loadp)
        if missing:
            req = ('<div class="msg-warn">Runtime calibration requires '
                   + " and ".join(missing) +
                   '. The UPS will silently ignore the start command '
                   'until these conditions are met &mdash; attach a '
                   'steady load and let the battery reach 100%%.</div>')
    return """<h1 class="page">Diagnostics</h1>
<div id="pagebody">%s
<div class="card"><h3>UPS Self Test</h3><div class="pad"><table class="kv">
%s%s%s</table>
<form method="post" action="/test/diag" style="margin-top:14px">
%s
<input type="hidden" name="test" value="self">
<button class="btn"%s>%s</button></form></div></div>
<div class="card"><h3>Runtime Calibration</h3><div class="pad"><table class="kv">
%s%s</table>%s
<form method="post" action="/test/diag" style="margin-top:14px;display:inline">
%s
<input type="hidden" name="test" value="calstart">
<button class="btn"%s>Start Runtime Calibration</button></form>
<form method="post" action="/test/diag" style="display:inline">
%s
<input type="hidden" name="test" value="calstop">
<button class="btn sec"%s>Stop Runtime Calibration</button></form></div></div>
</div>
<script>var NMC_INIT={status:%s};</script><script>%s</script>""" % (
        m,
        kv("Last Test Result", esc(test_result_str(result))),
        kv("Last Test Date", esc(testdate)),
        kv("Diagnostic Schedule", esc(diag_schedule_str())),
        csrf_input(d), " disabled" if testing else "",
        "Self Test In Progress..." if testing else "Initiate UPS Self Test",
        kv("Last Calibration Date", st.get("cal_date", "UNKNOWN")),
        kv("Calibration Status", "Running" if cal else "Not running"),
        req,
        csrf_input(d), " disabled" if cal else "",
        csrf_input(d), "" if cal else " disabled",
        json.dumps(d.get("ups.status", "")), _asset("tests.js"))


def _sessions_rows(d, cur_tok):
    """Active-session table for the config page (N5)."""
    now = time.time()
    with _sessions_lock:
        items = sorted(sessions.items(),
                       key=lambda kv: kv[1].get("created", 0))
    rows = []
    for t, s in items:
        if s.get("exp", 0) <= now:
            continue
        created = s.get("created")
        cstr = (time.strftime("%m/%d %H:%M", time.localtime(created))
                if created else "&mdash;")
        exp = time.strftime("%H:%M", time.localtime(s["exp"]))
        ip = esc(s.get("ip") or "") or "&mdash;"
        if s.get("user"):
            ip = esc(s["user"]) + "@" + ip
        if t == cur_tok:
            ip += " <b>(this session)</b>"
            btn = ""
        else:
            btn = ('<form method="post" action="/config/sessions" '
                   'style="display:inline">%s'
                   '<input type="hidden" name="drop" value="%s">'
                   '<button class="btn sec" style="padding:2px 10px;'
                   'font-size:12px">Log off</button></form>'
                   % (csrf_input(d), esc(t)))
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td>"
                    "<td style='text-align:right'>%s</td></tr>"
                    % (ip, cstr, exp, btn))
    if not rows:
        rows = ["<tr><td colspan='4' class='mut'>No active sessions</td></tr>"]
    return "".join(rows)


def pg_config(d, msg: "str | tuple" = "", cur_tok=""):
    g = lambda k, dv: esc(str(cfg_get(k, dv)))
    m = '<div class="%s">%s</div>' % msg if msg else ""
    lowrt_tt = int(cfg_get("low_battery_runtime",
                           int(num(d, "battery.runtime.low", 120)
                               or 120) * 100))
    sddel_tt = int(cfg_get("shutoff_delay",
                           int(num(d, "ups.delay.shutdown", 0) or 0) * 100))
    alarm = {"enabled": 1, "disabled": 3, "muted": 4}.get(
        d.get("ups.beeper.status", "enabled"), 1)
    opts = "".join('<option value="%d"%s>%s</option>' % (
        v, " selected" if v == alarm else "", t)
        for v, t in [(1, "on battery / timed"), (2, "at low battery"),
                     (3, "never"), (4, "mute current alarm")])
    sensopts = "".join('<option value="%d"%s>%s</option>' % (
        v, " selected" if int(cfg_get("sensitivity", 1)) == v else "", t)
        for v, t in [(1, "auto"), (2, "low"), (3, "medium"), (4, "high")])
    ratedv = g("rated_output_voltage",
               str(int(num(d, "output.voltage", 230) or 230)))
    if ratedv == "120":          # correct earlier 120V guess on 230V units
        ratedv = "230"

    nc = notify_cfg()

    def _secf(lbl, name, key, ph):
        """Secret field: shows only a configured flag; blank keeps the
        stored value, the clear checkbox removes it."""
        badge = ""
        if nc.get(key):
            badge = (' <span class="note">(configured)</span>'
                     ' <label class="f" style="font-weight:normal">'
                     '<input type="checkbox" name="clr_%s"> clear</label>'
                     % name)
        return ('<tr><td class="lbl">%s</td><td>'
                '<input name="%s" type="password" autocomplete="off" '
                'maxlength="256" placeholder="%s">%s</td></tr>'
                % (esc(lbl), name, esc(ph), badge))

    sevopts = "".join('<option value="%s"%s>%s</option>'
                      % (v, " selected" if nc["min_sev"] == v else "", v)
                      for v in ("info", "warn", "crit"))
    tlsopts = "".join('<option value="%s"%s>%s</option>'
                      % (v, " selected" if nc["smtp_tls"] == v else "", t)
                      for v, t in (("none", "None"), ("starttls", "STARTTLS"),
                                   ("ssl", "SSL/TLS")))
    tbtns = ""
    for ch, lbl in (("syslog", "Syslog"), ("webhook", "Webhook"),
                    ("discord", "Discord"), ("teams", "Teams"),
                    ("email", "Email")):
        en = {"syslog": nc["syslog"], "webhook": nc["webhook"],
              "discord": nc["discord"], "teams": nc["teams"],
              "email": nc["smtp_host"] and nc["smtp_to"]}[ch]
        if en:
            tbtns += ('<form method="post" action="/config/notify/test" '
                      'style="display:inline">%s'
                      '<input type="hidden" name="ch" value="%s">'
                      '<button class="btn sec">Test %s</button></form> '
                      % (csrf_input(d), ch, lbl))
    rt_rows = ""
    for k, lbl in NOTIFY_KINDS:
        route = nc["routes"].get(k)
        cells = ""
        for c in NOTIFY_CHANNELS:
            on = route is None or c in route
            cells += ('<td style="text-align:center">'
                      '<input type="checkbox" name="rt_%s_%s"%s></td>'
                      % (k, c, " checked" if on else ""))
        rt_rows += ('<tr><td style="padding:4px 8px 4px 0">%s</td>%s</tr>'
                    % (esc(lbl), cells))
    rt_head = ('<tr><th style="text-align:left">Trigger</th>'
               + "".join('<th style="text-align:center;width:60px">%s</th>'
                         % c.capitalize() for c in NOTIFY_CHANNELS)
               + "</tr>")
    ncard = """<div class="card"><h3>Notifications</h3><div class="pad">
<p class="mut" style="margin-top:0">Alert channels fired on events at or
above the minimum severity. Webhook URLs and the SMTP password are
secrets &mdash; they display only a configured flag; leave blank to keep
the stored value.</p>
<form method="post" action="/config/notify"><table class="kv">
%s
<tr><td class="lbl">Minimum Severity</td><td><select name="nsev">%s</select></td></tr>
<tr><td class="lbl">Syslog Target</td><td><input name="nsyslog" maxlength="64" value="%s" placeholder="local or host[:514]"></td></tr>
%s%s%s%s
<tr><td colspan="2"><b>E-mail (SMTP)</b></td></tr>
<tr><td class="lbl">SMTP Host</td><td><input name="nsmtphost" maxlength="64" value="%s" placeholder="mail.example.com"></td></tr>
<tr><td class="lbl">SMTP Port</td><td><input name="nsmtpport" type="number" min="1" max="65535" value="%s" placeholder="auto"></td></tr>
<tr><td class="lbl">SMTP Security</td><td><select name="nsmtptls">%s</select></td></tr>
<tr><td class="lbl">SMTP User</td><td><input name="nsmtpuser" maxlength="64" value="%s" autocomplete="off"></td></tr>
%s
<tr><td class="lbl">Mail From</td><td><input name="nsmtpfrom" maxlength="64" value="%s" placeholder="ups@example.com"></td></tr>
<tr><td class="lbl">Mail To</td><td><input name="nsmtpto" maxlength="128" value="%s" placeholder="admin@example.com, oncall@example.com"></td></tr>
<tr><td colspan="2"><b>Alert routing</b><br><span class="mut">Which
channels fire per trigger type. Unchecked channels are skipped for that
trigger; a fully unchecked row alerts nowhere.</span></td></tr>
<tr><td colspan="2"><table style="width:100%%;font-size:13px">
%s%s</table></td></tr>
</table>
<div style="margin-top:16px"><button class="btn">Apply</button>
<a class="btn sec" href="/config/ups">Cancel</a></div>
</form>
<div style="margin-top:10px">%s</div>
<p class="note">Teams expects an Office 365 &quot;Incoming Webhook&quot;
connector URL. Test buttons use the saved configuration &mdash; Apply
changes first.</p></div></div>""" % (
        csrf_input(d), sevopts, esc(nc["syslog"]),
        _secf("Webhook URL", "nwebhook", "webhook",
              "https://hooks.example.com/..."),
        _secf("Discord Webhook", "ndiscord", "discord",
              "https://discord.com/api/webhooks/..."),
        ('<tr><td class="lbl">Discord Ping</td><td>'
         '<input name="ndismention" maxlength="64" value="%s" '
         'placeholder="@here or &lt;@&amp;roleid&gt; — crit only">'
         '</td></tr>' % esc(nc["discord_mention"])),
        _secf("Teams Webhook", "nteams", "teams",
              "https://outlook.office.com/webhook/..."),
        esc(nc["smtp_host"]), esc(nc["smtp_port"]), tlsopts,
        esc(nc["smtp_user"]),
        _secf("SMTP Password", "nsmtppass", "smtp_pass", "password"),
        esc(nc["smtp_from"]), esc(nc["smtp_to"]),
        rt_head, rt_rows, tbtns)
    return """<h1 class="page">UPS Configuration</h1>%s
<div class="card"><h3>General</h3><div class="pad">
<form method="post" action="/config/ups"><table class="kv">
%s
<tr><td class="lbl">UPS Name</td><td><input name="ups_name" maxlength="32" value="%s"></td></tr>
<tr><td class="lbl">UPS Identifier (ups.id)</td><td><input name="ups_id" maxlength="16" value="%s"></td></tr>
<tr><td class="lbl">Location</td><td><input name="location" maxlength="64" value="%s"></td></tr>
<tr><td class="lbl">Contact</td><td><input name="contact" maxlength="64" value="%s"></td></tr>
<tr><td class="lbl">Audible Alarm</td><td><select name="alarm">%s</select></td></tr>
<tr><td class="lbl">Sensitivity</td><td><select name="sens">%s</select></td></tr>
<tr><td class="lbl">Rated Output Voltage</td><td><input name="ratedv" type="number" min="100" max="300" value="%s"></td></tr>
</table>
<h3 style="margin:18px 0 8px;font-size:14px">Power thresholds</h3>
<table class="kv">
<tr><td class="lbl">Low Battery Runtime (s)</td><td><input name="lowrt" type="number" min="0" max="36000" value="%s"></td></tr>
<tr><td class="lbl">Shutdown Delay (s)</td><td><input name="sddel" type="number" min="0" max="3600" value="%s"></td></tr>
<tr><td class="lbl">Restart Delay (s)</td><td><input name="rdel" type="number" min="0" max="3600" value="%s"></td></tr>
<tr><td class="lbl">Reboot Delay (s)</td><td><input name="rbdel" type="number" min="0" max="3600" value="%s"></td></tr>
<tr><td class="lbl">Low Battery Charge (%%)</td><td><input name="lowchg" type="number" min="0" max="100" value="%s"></td></tr>
<tr><td class="lbl">Transfer Voltage Low (VAC)</td><td><input name="trlow" type="number" min="150" max="300" value="%s"></td></tr>
<tr><td class="lbl">Transfer Voltage High (VAC)</td><td><input name="trhigh" type="number" min="150" max="300" value="%s"></td></tr>
</table>
<div style="margin-top:16px"><button class="btn">Apply</button>
<a class="btn sec" href="/config/ups">Cancel</a></div>
</form></div></div>
%s
<div class="card"><h3>Change Web Password</h3><div class="pad">
<form method="post" action="/config/security"><table class="kv">
%s
<tr><td class="lbl">Current Password</td><td><input name="curp" type="password" autocomplete="current-password"></td></tr>
<tr><td class="lbl">New Password</td><td><input name="newp" type="password" minlength="6" autocomplete="new-password"></td></tr>
<tr><td class="lbl">Confirm New Password</td><td><input name="cfmp" type="password" autocomplete="new-password"></td></tr>
</table>
<div style="margin-top:16px"><button class="btn">Change Password</button></div>
</form></div></div>
<div class="card"><h3>Active Sessions</h3><div class="pad">
<table class="kv pgtbl"><tr><th style="text-align:left">Client</th>
<th style="text-align:left">Started</th><th style="text-align:left">Expires</th>
<th></th></tr>
%s
</table>
<form method="post" action="/config/sessions" style="margin-top:12px">%s
<input type="hidden" name="act" value="others">
<button class="btn sec">Log off all other sessions</button></form>
</div></div>
<div class="card"><h3>Backup / Restore</h3><div class="pad">
<p class="mut" style="margin-top:0">Download a backup of the card
configuration and state, or restore one. Restoring replaces
nmc.conf and card state, then restarts this interface.</p>
<a class="btn sec" href="/config/backup">Download backup</a>
<input type="file" id="bkfile" accept=".tgz,.tar.gz"
 style="margin-left:12px;font-size:13px">
<button class="btn" onclick="bkRestore()" style="margin-left:8px">Restore</button>
<div id="bkmsg" class="mut" style="margin-top:8px"></div>
</div></div>
<script>var NMC_INIT={csrf:%s};</script><script>%s</script>
<p class="note">Values the UPS can store are written to it via NUT SET VAR;
the rest are kept on the management card.</p>""" % (
        m, csrf_input(d),
        g("ups_name", "APC Smart-UPS"),
        esc(d.get("ups.id", "")),
        g("location", LOCATION), g("contact", CONTACT), opts, sensopts,
        ratedv,
        lowrt_tt // 100, sddel_tt // 100,
        esc(d.get("ups.delay.start", "0")),
        esc(d.get("ups.delay.reboot", "8")),
        g("min_return_capacity", num(d, "battery.charge.low", 10) or 10),
        esc(d.get("input.transfer.low", "")),
        esc(d.get("input.transfer.high", "")),
        ncard,
        csrf_input(d),
        _sessions_rows(d, cur_tok),
        csrf_input(d),
        json.dumps(d.get("__csrf", "")), _asset("config.js"))


def pg_events(d, msg=""):
    evs = load_events()
    m = '<div class="%s">%s</div>' % msg if msg else ""
    rows = "".join(
        "<tr data-sev='%s'><td><span class='dot %s'></span></td>"
        "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(e.get("sev", "info")), esc(e.get("sev", "info")),
            time.strftime("%m/%d/%Y", time.localtime(e["ts"])),
            time.strftime("%H:%M:%S", time.localtime(e["ts"])),
            esc(e["text"]))
        for e in reversed(evs)) or \
        '<tr><td colspan="4" class="note">No events.</td></tr>'
    return """<h1 class="page">Event Log</h1>%s
<div class="card"><div class="pad">
<div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center;
 margin-bottom:12px;font-size:13px">
<label class="f" style="padding:0"><input type="checkbox" class="evf" value="crit" checked> critical</label>
<label class="f" style="padding:0"><input type="checkbox" class="evf" value="warn" checked> warning</label>
<label class="f" style="padding:0"><input type="checkbox" class="evf" value="info" checked> info</label>
<input type="text" id="evq" placeholder="Filter events&hellip;"
 style="width:220px"></div>
<table class="ev" id="evtable" data-pg-global="#evq" data-pg-sev>
<tr><th style="width:20px"></th>
<th style="width:105px">Date</th><th style="width:85px">Time</th>
<th>Event</th></tr>%s</table>
<div style="margin-top:14px;display:flex;gap:10px">
<a class="btn sec" href="/events.txt">Download events.txt</a>
<form method="post" action="/log/clear" style="display:inline"
 onsubmit="return confirm('Clear the entire event log?')">
%s<button class="btn sec">Clear Log</button></form></div>
</div></div>""" % (m, rows, csrf_input(d))


def pg_data():
    data = load_data()
    rows = ""
    for r in reversed(data):
        cells = "".join("<td>%s</td>" % esc(r.get(k, "-")) for k in
                        ("battery.charge", "ups.load", "ups.realpower",
                         "input.voltage", "output.voltage",
                         "battery.temperature", "st"))
        rows += ("<tr><td>%s</td><td>%s</td>%s</tr>"
                 % (time.strftime("%m/%d/%Y", time.localtime(r["ts"])),
                    time.strftime("%H:%M", time.localtime(r["ts"])), cells))
    if not rows:
        rows = ('<tr><td colspan="9" class="note">No samples yet &mdash; '
                'the data log starts filling within a minute of service '
                'start.</td></tr>')
    es = energy_stats()
    ecard = ""
    if es["span_h"]:
        er = kv("Energy (last 24 h)", "%.3f kWh" % es["kwh24"])
        if es["avg24"] is not None:
            er += kv("Average Power (24 h)", "%.0f W" % es["avg24"])
        er += kv("Energy (log window, %.1f h)" % es["span_h"],
                 "%.3f kWh" % es["kwh_all"])
        if es["rate"]:
            cur = esc(es["cur"])
            er += kv("Est. Cost (24 h)",
                     "%s%.3f" % (cur, es["kwh24"] * es["rate"]))
            er += kv("Rate", "%s%.4f / kWh" % (cur, es["rate"]))
        else:
            er += kv("Est. Cost", "set energy_rate in nmc.conf")
        ecard = ('<div class="card"><h3>Energy</h3><div class="pad">'
                 '<table class="kv">%s</table></div></div>' % er)
        qr = ""
        if es["vin"]:
            qr += kv("Input Voltage", "%.0f &ndash; %.0f VAC "
                     "(avg %.0f)" % es["vin"])
        if es["load"]:
            qr += kv("Load", "%.0f%% &ndash; %.0f%% "
                     "(avg %.0f%%)" % es["load"])
        if es["watts"]:
            qr += kv("Power Draw", "%.0f &ndash; %.0f W "
                     "(avg %.0f W)" % es["watts"])
        if qr:
            ecard += ('<div class="card"><h3>Power Quality'
                      ' &mdash; log window</h3><div class="pad">'
                      '<table class="kv">%s</table></div></div>' % qr)
    return """<h1 class="page">Data Log</h1>
%s<div class="card"><h3>Trend</h3><div class="pad">
<canvas id="dlc" width="1000" height="260"
 style="width:100%%;height:auto"></canvas>
<div class="note" id="dllbl" style="margin-top:6px"></div>
</div></div>
<div class="card"><div class="pad">
<table class="ev"><tr><th>Date</th><th>Time</th><th>Charge %%</th>
<th>Load %%</th><th>Watts</th><th>In V</th><th>Out V</th><th>Temp &deg;C</th>
<th>Status</th></tr>%s</table>
<div style="margin-top:14px"><a class="btn sec" href="/data.txt">Download data.txt</a>
<a class="btn sec" href="/data.txt?fmt=csv" style="margin-left:8px">Download CSV</a>
<span class="note" style="margin-left:10px">Sampled every 60 s, newest first &mdash; %s samples shown.</span></div>
</div></div>
<script>%s</script>""" % (ecard, rows, len(data), _asset("datalog.js"))


def pg_about(d, msg: "str | tuple" = ""):
    m = '<div class="%s">%s</div>' % msg if msg else ""
    try:
        with open("/sys/class/net/eth0/address") as f:
            mac = f.read().strip().upper()
    except Exception:
        mac = "?"
    up = "unknown"
    try:
        with open("/proc/uptime") as f:
            s = float(f.read().split()[0])
        up = "%d days %02d:%02d" % (s // 86400, s % 86400 // 3600,
                                   s % 3600 // 60)
    except Exception:
        pass
    kernel = "?"
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            kernel = f.read().strip()
    except Exception:
        pass
    rows = "".join(kv(a, b) for a, b in [
        ("Model", NMC_MODEL), ("Serial Number", NMC_SERIAL),
        ("Hardware Revision", "05"), ("MAC Address", mac),
        ("Manufacture Date", "04/17/2024"),
        ("Application Module", "sumx " + NMC_FW),
        ("APC OS (AOS)", NMC_FW), ("Boot Monitor", "v1.0.9"),
        ("NMC Uptime", up),
        ("Host", "Raspberry Pi &mdash; Linux %s" % kernel),
        ("Web Emulator", "apc-nmc-web %s (NUT bridge)" % WEB_VER)])
    dev = "".join(kv(a, esc(b)) for a, b in [
        ("Model", d.get("ups.model", "?")),
        ("Serial Number", d.get("ups.serial", "?")),
        ("Firmware", d.get("ups.firmware", "?")),
        ("Manufacture Date", d.get("ups.mfr.date", "?")),
        ("Rated Power", "%s VA / %s W" % (d.get("ups.power.nominal", "?"),
                                          d.get("ups.realpower.nominal",
                                                "?"))),
        ("UPS Identifier", d.get("ups.id", "?")),
        ("NUT Driver", "%s %s" % (d.get("driver.name", "?"),
                                  d.get("driver.version", "?")))])
    us = _load_json(UPDATE_STATE)
    uss = _load_json(UPDATE_STATUS)
    try:
        fin = float(uss.get("finished", 0) or 0)
    except (TypeError, ValueError):
        fin = 0
    if uss.get("state") == "ok" and fin > float(
            cfg_get("update_announced", 0) or 0):
        cfg_set("update_announced", fin)
        log_event("NMC: Software updated: %s &#8250; %s."
                  % (uss.get("from", "?"), uss.get("to", "?")))
    checked = ("never" if not us.get("checked_at") else
               time.strftime("%m/%d/%Y %H:%M",
                             time.localtime(us["checked_at"])))
    if us.get("err"):
        avail = '<span class="st-warn">check failed: %s</span>' \
                % esc(us["err"])
    elif us.get("available"):
        avail = '<span class="st-warn">yes &mdash; %s available</span>' \
                % esc(us.get("latest", "?"))
    elif us.get("checked_at"):
        avail = "no"
    else:
        avail = "?"
    upd = "".join(kv(a, b) for a, b in [
        ("Installed Version", esc(installed_version())),
        ("Latest Version", esc(us.get("latest") or "?")),
        ("Last Check", checked),
        ("Update Available", avail)])
    if us.get("available") and us.get("notes"):
        upd += kv("Release Notes",
                  '<pre class="updnotes">%s</pre>'
                  % esc("\n".join(us["notes"].splitlines()[:24])))
    if uss.get("state") == "running":
        upd += kv("Update Status", '<span class="st-warn">in progress: %s</span>'
                  % esc(uss.get("msg", "")))
    elif uss.get("state") == "failed":
        upd += kv("Last Update", '<span class="st-warn">failed: %s</span>'
                  % esc(uss.get("msg", "")))
    elif uss.get("state") == "ok":
        upd += kv("Last Update", esc("%s (to %s)"
                  % (time.strftime("%m/%d/%Y %H:%M",
                                   time.localtime(fin)) if fin else "done",
                     uss.get("to", "?"))))
    baks = update_backups()
    rollbtn = ""
    if baks:
        upd += kv("Backups", esc("; ".join(
            "%s (%s)" % (b[1], time.strftime("%m/%d %H:%M",
                                            time.localtime(b[2])) if b[2]
                         else "?") for b in baks)))
        rollbtn = ("""<form method="post" action="/about/update/rollback"
 style="display:inline" onsubmit="return confirm('Restore the most recent update backup? Services (including this interface) restart during rollback.')">"""
                   + csrf_input(d) +
                   '<button class="btn sec" type="submit">Roll Back</button></form>')
    newkeys = config_new_keys()
    if newkeys:
        upd += kv("New Config Options",
                  '<span class="st-warn">%s</span> &mdash; unset in '
                  'nmc.conf' % esc(", ".join(newkeys)))
    return """<h1 class="page">About</h1>%s
<div class="card"><h3>Software Update</h3><div class="pad">
<table class="kv">%s</table>
<form method="post" action="/about/update/check" style="display:inline">%s
<button class="btn sec" type="submit">Check for Updates</button></form>
<form method="post" action="/about/update/apply" style="display:inline"
 onsubmit="return confirm('Download and install the latest version now? Services (including this interface) restart during the update.')">
%s<button class="btn sec" type="submit">Update Now</button></form>
%s</div></div>
<div class="card"><h3>Network Management Card</h3><div class="pad">
<table class="kv">%s</table></div></div>
<div class="card"><h3>Attached UPS</h3><div class="pad">
<table class="kv">%s</table></div></div>""" % (m, upd, csrf_input(d),
                                              csrf_input(d), rollbtn,
                                              rows, dev)


def pg_update_apply():
    """Live update console — JS polls /about/update/progress and streams
    update.log into a terminal-style box, so the NUT build is visible
    as it happens. Works for both the POST landing page and a later GET
    reload while the update is still running."""
    return """<h1 class="page">Software Update</h1>
<div class="card"><h3 id="updPhase">Update in progress</h3><div class="pad">
<pre id="updCon" class="updcon">connecting&#8230;</pre>
<p id="updDone"></p>
<p><a class="btn sec" href="/about/system">Back to About &#8250;</a></p>
</div></div>
<script>
(function(){
var con=document.getElementById('updCon'),
    ph=document.getElementById('updPhase'),
    dn=document.getElementById('updDone'),
    pos=-1,lost=0,idle=0;
con.textContent='';
function esc(s){return String(s).replace(/[&<>]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function tick(){
 fetch('/about/update/progress?pos='+pos,{credentials:'same-origin'})
 .then(function(r){
   if(r.redirected||r.url.indexOf('/login')>=0)throw'reauth';
   if(!r.ok)throw'http';
   return r.json()})
 .then(function(s){
   lost=0;
   if(typeof s.pos==='number')pos=s.pos;
   if(s.log){
     con.textContent+=s.log.replace(/\\x1b\\[[0-9;]*[A-Za-z]/g,'');
     con.scrollTop=con.scrollHeight;
   }
   if(s.state==='ok'){
     ph.textContent='Update complete';
     dn.innerHTML='<span class="st-ok">Updated to '+esc(s.to||'?')+
       ' &mdash; reloading&hellip;</span>';
     setTimeout(function(){location='/about/system'},4000);
   }else if(s.state==='failed'){
     ph.textContent='Update failed';
     dn.innerHTML='<span class="st-warn">'+esc(s.msg||'failed')+'</span>';
   }else if(!s.state){
     /* a real apply writes 'running' within a second or two — no state
        after a few polls means nothing is running (plain GET reload) */
     if(++idle>=4){
       ph.textContent='No update in progress';
       dn.innerHTML='<a href="/about/system">Back to About &#8250; System</a>';
     }else{
       ph.textContent='Starting update';
       setTimeout(tick,1500);
     }
   }else{
     ph.textContent='Update in progress: '+(s.msg||'working');
     setTimeout(tick,1500);
   }
 })
 .catch(function(e){
   if(e==='reauth'){
     ph.textContent='Update still running';
     dn.innerHTML='This interface restarted and the session ended &mdash; '+
       '<a href="/about/system">log back in to About &#8250; System</a>.';
     return;
   }
   /* the updater restarts this service as its final step */
   if(++lost>=3)ph.textContent='Interface restarting, reconnecting';
   setTimeout(tick,2000);
 });
}
tick();
})();
</script>"""


def pg_network():
    out = []
    try:
        for ip in subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "scope", "global"],
                capture_output=True, text=True, check=False,
                timeout=5).stdout.splitlines():
            p = ip.split()
            out.append(kv("IPv4 Address (%s)" % p[1], p[3]))
        gw = subprocess.run(["ip", "-4", "route", "show", "default"],
                            capture_output=True, text=True,
                            check=False, timeout=5).stdout.split()
        if "via" in gw:
            out.append(kv("Default Gateway", gw[gw.index("via") + 1]))
        try:
            with open("/sys/class/net/eth0/address") as f:
                mac = f.read().strip().upper()
            out.append(kv("MAC Address (eth0)", mac))
        except Exception:
            pass
        dns = []
        try:
            with open("/etc/resolv.conf") as f:
                for ln in f:
                    if ln.startswith("nameserver"):
                        dns.append(ln.split()[1])
        except Exception:
            pass
        if dns:
            out.append(kv("DNS Servers", ", ".join(dns)))
        link = subprocess.run(["ethtool", "eth0"], capture_output=True,
                              text=True, check=False, timeout=5).stdout
        m = re.search(r"Speed: (\S+)", link)
        if m:
            out.append(kv("Link Speed (eth0)", m.group(1)))
    except Exception:
        pass
    out.append(kv("Host Name", socket.gethostname()))
    t = []
    try:
        td = {}
        for ln in subprocess.run(["timedatectl", "show"], capture_output=True,
                                 text=True, check=False,
                                 timeout=5).stdout.splitlines():
            k, _, v = ln.partition("=")
            td[k.strip()] = v.strip()
        yn = {"yes": "Yes", "no": "No"}.get(td.get("NTP", ""), "Unknown")
        t.append(kv("NTP Client Enabled", yn))
        yn = {"yes": "Yes", "no": "No"}.get(td.get("NTPSynchronized", ""),
                                            "Unknown")
        t.append(kv("Clock Synchronized", yn))
        if td.get("Timezone"):
            t.append(kv("Time Zone", td["Timezone"]))
        t.append(kv("System Time",
                    time.strftime("%m/%d/%Y %H:%M:%S %Z")))
    except Exception:
        pass
    return """<h1 class="page">About &mdash; Network</h1>
<div class="card"><h3>IPv4</h3><div class="pad">
<table class="kv">%s</table></div></div>
<div class="card"><h3>Time Synchronization</h3><div class="pad">
<table class="kv">%s</table></div></div>""" % ("".join(out), "".join(t))


def pg_nut(d):
    """Read-only dump of every live NUT variable (admin/diagnostic)."""
    main = sorted(k for k in d if not k.startswith("driver."))
    drv = sorted(k for k in d if k.startswith("driver."))
    rows = "".join("<tr><td class='lbl' style='font-family:monospace'>%s</td>"
                   "<td style='font-family:monospace'>%s</td></tr>"
                   % (esc(k), esc(d[k])) for k in main)
    drows = "".join("<tr><td class='lbl' style='font-family:monospace'>%s</td>"
                    "<td style='font-family:monospace'>%s</td></tr>"
                    % (esc(k), esc(d[k])) for k in drv)
    return """<h1 class="page">UPS Variables</h1>
<div class="card"><h3>All variables reported by upsd</h3><div class="pad">
<p class="note">Diagnostic view &mdash; the raw NUT variable set behind
every page and SNMP value.</p>
<table class="kv">%s</table></div></div>
<div class="card"><h3>Driver internals</h3><div class="pad">
<table class="kv">%s</table></div></div>""" % (rows or '<tr><td class="note">none</td></tr>',
                                              drows or '<tr><td class="note">none</td></tr>')


def pg_host():
    """Host (Raspberry Pi) health: temperature, load, memory, disk,
    service states."""
    rows = ""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            rows += kv("SoC Temperature", "%.1f &#8451;"
                       % (int(f.read().strip()) / 1000.0))
    except Exception:
        pass
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
        rows += kv("Load Average", "%s (1m) %s (5m) %s (15m)"
                   % (la[0], la[1], la[2]))
    except Exception:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                mem[k.strip()] = int(v.split()[0]) * 1024
        if "MemTotal" in mem:
            used = mem["MemTotal"] - mem.get("MemAvailable",
                                            mem["MemTotal"])
            rows += kv("Memory", "%d / %d MB (%.0f%% used)"
                       % (mem["MemTotal"] // 1048576,
                          used // 1048576,
                          100.0 * used / mem["MemTotal"]))
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        tot = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        rows += kv("Filesystem /", "%d / %d MB free (%.0f%% used)"
                   % (free // 1048576, tot // 1048576,
                      100.0 * (tot - free) / tot))
    except Exception:
        pass
    svcs = ""
    for s in ("apc-nmc-web", "snmpd", "nut-server",
              "nut-driver@myups", "nut-monitor"):
        try:
            r = subprocess.run(["systemctl", "is-active", s],
                               capture_output=True, text=True,
                               timeout=5, check=False).stdout.strip() or "?"
        except Exception:
            r = "?"
        cls = {"active": "ok", "inactive": "warn",
               "failed": "crit"}.get(r, "warn")
        svcs += ("<tr><td class='lbl'>%s</td><td><span class='state st-%s'>"
                 "%s</span></td></tr>" % (s, cls, esc(r)))
    return """<h1 class="page">Host</h1>
<div class="card"><h3>Management Card Host (Raspberry Pi)</h3>
<div class="pad"><p class="note">This card is powered by the UPS it
manages &mdash; its own health matters.</p>
<table class="kv">%s</table></div></div>
<div class="card"><h3>Services</h3><div class="pad">
<table class="kv">%s</table></div></div>""" % (rows, svcs)


def pg_404():
    return """<h1 class="page">Page Not Found</h1>
<div class="card"><div class="pad">
<p>The requested page does not exist on this management card.</p>
<p><a class="btn sec" href="/home">Return to Home</a></p></div></div>"""


def pg_500():
    return """<h1 class="page">Internal Error</h1>
<div class="card"><div class="pad">
<p>The management card could not complete this request.</p>
<p><a class="btn sec" href="/home">Return to Home</a></p></div></div>"""


def events_txt():
    lines = []
    for e in load_events():
        lines.append("%s  %s  %s" % (
            time.strftime("%m/%d/%Y", time.localtime(e["ts"])),
            time.strftime("%H:%M:%S", time.localtime(e["ts"])),
            e["text"]))
    return "\n".join(lines) + "\n"


def energy_stats():
    """Integrate ups.realpower samples (trapezoid) into kWh figures."""
    arr = load_data()
    now = time.time()

    def kwh(rows):
        e = 0.0
        for a, b in itertools.pairwise(rows):
            wa, wb = num(a, "ups.realpower"), num(b, "ups.realpower")
            if wa is None or wb is None:
                continue
            e += (wa + wb) / 2 * (b["ts"] - a["ts"]) / 3.6e6
        return e

    r24 = [r for r in arr if r.get("ts", 0) > now - 86400]
    w24 = [w for w in (num(r, "ups.realpower") for r in r24)
           if w is not None]

    def s(key):
        vs = [v for v in (num(r, key) for r in arr) if v is not None]
        return (min(vs), max(vs), sum(vs) / len(vs)) if vs else None

    try:
        rate = float(cfg("device", "energy_rate", "0") or 0)
    except ValueError:
        rate = 0
    return {"kwh24": kwh(r24), "kwh_all": kwh(arr),
            "avg24": sum(w24) / len(w24) if w24 else None,
            "span_h": ((arr[-1]["ts"] - arr[0]["ts"]) / 3600.0
                       if len(arr) > 1 else 0),
            "rate": rate,
            "cur": str(cfg("device", "energy_currency", "$"))[:4],
            "vin": s("input.voltage"), "load": s("ups.load"),
            "watts": s("ups.realpower")}


def data_txt(hours=None, csv=False):
    keys = ("battery.charge", "ups.load", "ups.realpower", "input.voltage",
            "output.voltage", "battery.temperature", "st")
    sep = "," if csv else "\t"
    lines = [sep.join(["ts", "date", "time"] + list(keys))]
    cutoff = int(time.time() - hours * 3600) if hours else 0
    for r in load_data():
        if r.get("ts", 0) < cutoff:
            continue
        lines.append(sep.join([
            str(r["ts"]),
            time.strftime("%m/%d/%Y", time.localtime(r["ts"])),
            time.strftime("%H:%M:%S", time.localtime(r["ts"]))] +
            [str(r.get(k, "")) for k in keys]))
    return "\n".join(lines) + "\n"


def metrics_txt(d):
    """Prometheus exposition built from the same vars as status.json."""
    out = []
    ts = toks(d)

    def g(name, val, help_):
        try:
            f = float(val)
        except (TypeError, ValueError):
            return
        out.append("# HELP %s %s\n# TYPE %s gauge\n%s %s"
                   % (name, help_, name, name, f))

    g("ups_battery_charge_percent", d.get("battery.charge"),
      "UPS battery charge percent")
    g("ups_battery_runtime_seconds", d.get("battery.runtime"),
      "Estimated battery runtime")
    g("ups_battery_temperature_celsius", d.get("battery.temperature"),
      "Internal battery temperature")
    g("ups_load_percent", d.get("ups.load"), "UPS load percent")
    g("ups_realpower_watts", d.get("ups.realpower"), "Output power (W)")
    g("ups_power_va", d.get("ups.power"), "Output power (VA)")
    g("ups_input_voltage_volts", d.get("input.voltage"), "Input voltage")
    g("ups_output_voltage_volts", d.get("output.voltage"),
      "Output voltage")
    g("ups_input_frequency_hz", d.get("input.frequency"),
      "Input frequency")
    g("ups_output_frequency_hz", d.get("output.frequency"),
      "Output frequency")
    st = load_state()
    out.append("# HELP ups_on_battery Currently on battery power (0/1)"
               "\n# TYPE ups_on_battery gauge\nups_on_battery %d"
               % (1 if "OB" in ts else 0))
    out.append("# HELP ups_low_battery Low battery condition (0/1)"
               "\n# TYPE ups_low_battery gauge\nups_low_battery %d"
               % (1 if "LB" in ts else 0))
    out.append("# HELP ups_replace_battery Battery needs replacement"
               " (0/1)\n# TYPE ups_replace_battery gauge\n"
               "ups_replace_battery %d" % (1 if "RB" in ts else 0))
    out.append("# HELP ups_on_line On line power (0/1)"
               "\n# TYPE ups_on_line gauge\nups_on_line %d"
               % (1 if "OL" in ts else 0))
    out.append("# HELP apcnmc_transfers_to_battery Total transitions to"
               " battery power\n# TYPE apcnmc_transfers_to_battery"
               " counter\napcnmc_transfers_to_battery %d"
               % int(st.get("ob_count", 0) or 0))
    out.append("# HELP apcnmc_on_battery_seconds_total Total time on"
               " battery\n# TYPE apcnmc_on_battery_seconds_total"
               " counter\napcnmc_on_battery_seconds_total %d"
               % int(st.get("total_ob_seconds", 0) or 0))
    return "\n".join(out) + "\n"


def favicon():
    """Minimal 16x16 32bpp .ico: APC-green filled square."""
    import struct
    px = b''
    for y in range(16):
        for x in range(16):
            if x < 2 or x > 13 or y < 2 or y > 13:
                px += b'\x00\x00\x00\x00'
            else:
                px += b'\x58\xcd\x3d\xff'      # #3dcd58 as BGRA
    andmask = b'\x00' * 32                     # 16x16 /8, all opaque
    bmp = struct.pack("<IiiHHIIiiII", 40, 16, 32, 1, 32, 0,
                      len(px) + len(andmask), 0, 0, 0, 0) + px + andmask
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack(
        "<BBBBHHII", 16, 16, 0, 0, 1, 32, len(bmp), 22)
    return ico + bmp


ROBOTS = "User-agent: *\nDisallow: /\n"


# ============================================================ actions
def do_control(act, skipdel, pcns):
    if pcns:
        log_event("Signal PowerChute Network Shutdown clients requested "
                  "(no clients registered).")
    if act == "reboot":
        r = ups_cmd("shutdown.reboot")
        if r.startswith("OK"):
            log_event("UPS: Reboot initiated via the network management card.")
            save_state({"reboot_at": int(time.time())})
        return r
    if act == "on":
        r = ups_cmd("load.on")
        if r.startswith("OK"):
            log_event("UPS: Turned on via the network management card.")
        return r
    if act == "off":
        r = ups_cmd("load.off" if skipdel else "load.off.delay")
        if r.startswith("OK"):
            log_event("UPS: Turned off via the network management card.")
        return r
    if act == "sleep":
        r = ups_cmd("shutdown.return")
        if not r.startswith("OK"):
            r = ups_cmd("shutdown.stayoff")
        if r.startswith("OK"):
            log_event("UPS: Put to sleep via the network management card.")
        return r
    if act == "bypass":
        r = ups_cmd("bypass.start")
        if r.startswith("OK"):
            log_event("UPS: Switched to bypass via the network management card.")
        return r
    if act == "mute":
        r = ups_cmd("beeper.mute")
        if r.startswith("OK"):
            log_event("UPS: Beeper muted via the network management card.")
        return r
    return "ERR INVALID"


def do_outlet(gid, act):
    if not re.fullmatch(r"\d+", gid or ""):
        return "ERR INVALID"
    cmds = {"cycle": "load.cycle", "off": "load.off", "on": "load.on"}
    base = cmds.get(act)
    if not base:
        return "ERR INVALID"
    r = ups_cmd("outlet.group.%s.%s" % (gid, base))
    if r.startswith("OK"):
        log_event("UPS: Outlet group %s turned %s via the network "
                  "management card." % (gid, act))
    return r


def do_restore(blob):
    """Validate and apply a backup archive produced by /config/backup."""
    data = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for m in tf.getmembers():
                if m.name in ("nmc.conf", "state.json", "events.json") \
                        and m.isfile() and m.size <= 4 * 1024 * 1024:
                    try:
                        fobj = tf.extractfile(m)
                        if fobj is not None:
                            data[m.name] = fobj.read()
                    except Exception:
                        pass
    except (tarfile.TarError, EOFError, OSError):
        return ("msg-err", "Not a valid backup archive.")
    if "nmc.conf" not in data:
        return ("msg-err", "Archive contains no nmc.conf.")
    cp = configparser.ConfigParser()
    try:
        cp.read_string(data["nmc.conf"].decode("utf-8", "replace"))
    except configparser.Error:
        return ("msg-err", "nmc.conf in archive does not parse.")
    if not (cp.has_section("upsd") and cp.has_section("device")):
        return ("msg-err", "nmc.conf missing required sections.")
    for name in ("state.json", "events.json"):
        if name in data:
            try:
                json.loads(data[name])
            except ValueError:
                return ("msg-err", "%s in archive does not parse." % name)
    try:
        with open(CONF, "rb") as f:
            prev = f.read()
        with open(CONF + ".pre-restore", "wb") as f:
            f.write(prev)
    except OSError:
        pass
    with open(CONF, "wb") as f:
        f.write(data["nmc.conf"])
    if "state.json" in data:
        _atomic_json(STATE_FILE, lambda o: json.loads(data["state.json"]))
    if "events.json" in data:
        _atomic_json(EVENTS_FILE, lambda o: json.loads(data["events.json"]))
    log_event("NMC: Card configuration restored from backup by %s."
              % WEB_USER, "warn")
    return ("msg-ok", "Configuration restored; restarting the interface.")


def do_outlet_config(form, d):
    """Save outlet-group name/delay fields via NUT SET VAR (OPP-017)."""
    msgs = []
    try:
        rw_vars = ups_rw()
    except Exception:
        rw_vars = set()
    for g in outlet_groups(d):
        gid = g["idx"]
        fields = (("g%s_name" % gid, "name", g["name"], 16),
                  ("g%s_sd" % gid, "delay.shutdown", g["delay_off"], 0),
                  ("g%s_rd" % gid, "delay.reboot", g["delay_reboot"], 0),
                  ("g%s_od" % gid, "delay.start", g["delay_start"], 0))
        for fname, suffix, cur, _ in fields:
            val = form.get(fname, [""])[0].strip()
            if not val or str(val) == str(cur):
                continue
            var = "outlet.group.%s.%s" % (gid, suffix)
            if suffix == "name":
                if not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,16}", val):
                    msgs.append("%s: invalid characters" % var)
                    continue
            elif not re.fullmatch(r"\d+", val):
                msgs.append("%s: not a number" % var)
                continue
            if rw_vars and var not in rw_vars:
                msgs.append("%s: not writable on this UPS" % var)
                continue
            try:
                r = ups_setvar(var, val)
                if not r.startswith("OK"):
                    msgs.append("%s: %s" % (var, r))
            except Exception as e:
                msgs.append("%s: %s" % (var, e))
    if msgs:
        return ("msg-err", "Outlet config: " + "; ".join(msgs))
    log_event("UPS: Outlet group configuration changed via the network "
              "management card.")
    return ("msg-ok", "Outlet configuration saved.")


def do_test(which):
    if which == "self":
        r = ups_cmd("test.battery.start")
        if r.startswith("OK"):
            save_state({"diag_date": time.strftime("%m/%d/%Y"),
                        "diag_result": 4})    # inProgress
            log_event("UPS: Self test initiated via the network "
                      "management card.")
        return r
    if which == "calstart":
        r = ups_cmd("calibrate.start")
        if r.startswith("OK"):
            save_state({"cal_date": time.strftime("%m/%d/%Y"),
                        "cal_result": 4})
            log_event("UPS: Runtime calibration initiated via the network "
                      "management card.")
        return r
    if which == "calstop":
        return ups_cmd("calibrate.stop")
    return "ERR INVALID"


def do_battery_replaced():
    """Mark the battery as replaced.

    * sets `battery_replace_date` (which `battery_age_years` reports and
      the 3-year age warning keys on) to today
    * pushes today's date (ISO, the only format apc_modbus accepts)
      into the UPS `battery.date` variable when the driver exposes it
      as writable — then re-reads it, because some drivers ACK the
      write even when the UPS silently ignores it
    * zeroes the locally-kept on-battery counters (transfer count,
      cumulative and longest outage); an in-progress outage keeps
      counting via `ob_since`
    """
    cfg_set("battery_replace_date", time.strftime("%m/%d/%Y"))
    today_ups = time.strftime("%Y-%m-%d")
    pushed = None      # None = no writable battery.date / upsd unreachable
    try:
        if "battery.date" in ups_rw():
            r = ups_setvar("battery.date", today_ups)
            if r.startswith("OK"):
                deadline = time.time() + 6
                while time.time() < deadline:
                    time.sleep(1.5)
                    if str(ups_vars().get("battery.date", "")).strip() \
                            == today_ups:
                        pushed = True
                        break
                if pushed is None:
                    pushed = False
            else:
                pushed = False
    except Exception:
        traceback.print_exc()
    save_state({"ob_count": 0, "total_ob_seconds": 0, "ob_max": 0})
    if pushed:
        log_event("UPS: Battery marked as replaced; install date "
                  "written to the UPS, battery statistics reset.")
        return ("msg-ok", ("Battery marked as replaced &mdash; install "
                "date written to the UPS, statistics reset."))
    if pushed is False:
        log_event("UPS: Battery marked as replaced; UPS did not update "
                  "its install date, battery statistics reset.", "warn")
        return ("msg-ok", ("Battery marked as replaced &mdash; the UPS "
                "did not update its install date; recorded locally, "
                "statistics reset."))
    log_event("UPS: Battery marked as replaced; install date recorded "
              "locally, battery statistics reset.")
    return ("msg-ok", ("Battery marked as replaced &mdash; install date "
            "recorded locally, statistics reset."))


# ============================================================ software update
def installed_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"          # install predates version stamping


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def update_backups(state_dir="/var/lib/apc-nmc"):
    """Newest-first list of (dir, restored_version, epoch) for
    update-backup-* dirs — the rollback points --rollback restores."""
    out = []
    for d in sorted(glob.glob(
            os.path.join(state_dir, "update-backup-*")), reverse=True):
        if not os.path.isdir(d):
            continue
        try:
            meta = open(os.path.join(d, "meta")).read().split()
            ver = meta[0] if meta else "?"
            ts = int(meta[2]) if len(meta) > 2 else 0
        except (OSError, ValueError, IndexError):
            ver, ts = "?", 0
        out.append((d, ver, ts))
    # newest first — mirrors --rollback's ls -dt pick, using meta ts
    out.sort(key=lambda b: (b[2], b[0]), reverse=True)
    return out


def config_new_keys(default="/etc/apc-nmc/nmc.conf.default", live=None):
    """section.key names in nmc.conf.default that are unset (or only
    commented) in the live nmc.conf — options updates have shipped that
    the admin may want to review. Never exposes values."""
    cp_new = configparser.ConfigParser()
    cp_new.read(default)
    cp_live = configparser.ConfigParser()
    cp_live.read(CONF if live is None else live)
    return ["%s.%s" % (s, k) for s in cp_new.sections()
            for k in cp_new.options(s)
            if not (cp_live.has_section(s) and k in cp_live.options(s))]


def do_update_check():
    """Run the check through apc-nmc-update.sh so UI and CLI share one
    implementation (and one cache file the About page reads)."""
    if not os.path.exists(UPDATE_TOOL):
        return ("msg-err", "Updater is not installed on this system.")
    try:
        subprocess.run([UPDATE_TOOL, "--check"], check=False,
                       capture_output=True, timeout=30)
    except Exception:
        return ("msg-err", "Update check did not complete.")
    st = _load_json(UPDATE_STATE)
    if st.get("err"):
        return ("msg-err", "Update check failed: %s" % esc(st["err"]))
    if not st:
        return ("msg-err", "Update check produced no result.")
    if st.get("available"):
        return ("msg-ok", "Update available: %s &#8250; %s"
                % (esc(st.get("installed", "?")), esc(st.get("latest", "?"))))
    return ("msg-ok", "Software is up to date (%s)."
            % esc(st.get("installed", "?")))


def do_update_apply():
    """Queue the update unit and return immediately — it deploys the
    payload and restarts this service, so nothing can block on it."""
    if not os.path.exists(UPDATE_TOOL):
        return ("msg-err", "Updater is not installed on this system.")
    try:
        subprocess.run(["systemctl", "start", "--no-block",
                        "apc-nmc-update.service"],
                       check=False, capture_output=True, timeout=15)
    except Exception:
        return ("msg-err", "Could not start the update service.")
    return ("msg-ok", "")


def do_config(form):
    def v(name):
        return form.get(name, [""])[0].strip()

    before = dict(load_state().get("cfg") or {})
    try:
        rw_vars = ups_rw()
    except Exception:
        rw_vars = None   # upsd unreachable: attempt all, errors surface
    msgs = []
    for key, skey in (("ups_name", "ups_name"), ("location", "location"),
                      ("contact", "contact"), ("ratedv", "rated_output_voltage"),
                      ("sens", "sensitivity")):
        val = v(key)
        if val:
            if key in ("ratedv", "sens"):
                try:
                    val = int(val)
                except ValueError:
                    msgs.append("%s: invalid number" % key)
                    continue
            cfg_set(skey, val)

    # string SET VAR
    if v("ups_id"):
        if not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,16}", v("ups_id")):
            msgs.append("ups.id: invalid characters "
                        "(a-z 0-9 _ . - space, max 16)")
        else:
            try:
                r = ups_setvar("ups.id", v("ups_id"))
                if not r.startswith("OK"):
                    msgs.append("ups.id: %s" % r)
            except Exception as e:
                msgs.append("ups.id: %s" % e)

    # numeric SET VARs
    for key, var in (("lowrt", "battery.runtime.low"),
                     ("sddel", "ups.delay.shutdown"),
                     ("rdel", "ups.delay.start"),
                     ("rbdel", "ups.delay.reboot"),
                     ("lowchg", "battery.charge.low"),
                     ("trlow", "input.transfer.low"),
                     ("trhigh", "input.transfer.high")):
        val = v(key)
        if val:
            if not re.fullmatch(r"\d+", val):
                msgs.append("%s: not a number" % var)
                continue
            if key == "lowrt":
                cfg_set("low_battery_runtime", int(val) * 100)
            elif key == "sddel":
                cfg_set("shutoff_delay", int(val) * 100)   # timeticks
            elif key == "lowchg":
                cfg_set("min_return_capacity", int(val))
            if rw_vars is not None and var not in rw_vars:
                continue   # card keeps it; UPS can't store it
            try:
                r = ups_setvar(var, val)
                if not r.startswith("OK"):
                    msgs.append("%s: %s" % (var, r))
            except Exception as e:
                msgs.append("%s: %s" % (var, e))

    alarm = v("alarm")
    if alarm and re.fullmatch(r"[1-4]", alarm):
        r = ups_cmd({1: "beeper.enable", 2: "beeper.enable",
                     3: "beeper.disable", 4: "beeper.mute"}[int(alarm)])
        if not r.startswith("OK"):
            msgs.append("alarm setting: " + r)
    elif alarm:
        msgs.append("alarm: invalid value")

    if msgs:
        return ("msg-err", "Saved card settings; UPS reported: "
                + "; ".join(msgs))
    diff = _cfg_diff(before, load_state().get("cfg") or {})
    log_event("UPS: Configuration changed via the network management card."
              + (" (" + "; ".join(diff)[:240] + ")" if diff else ""))
    return ("msg-ok", "Configuration saved.")


def do_password_change(form):
    cur = form.get("curp", [""])[0]
    new = form.get("newp", [""])[0]
    cnf = form.get("cfmp", [""])[0]
    if not web_password_ok(cur):
        return ("msg-err", "Current password is incorrect.")
    if len(new) < 6:
        return ("msg-err", "New password must be at least 6 characters.")
    if new != cnf:
        return ("msg-err", "New passwords do not match.")
    salt = secrets.token_hex(16)
    cfg_set("web_pass_hash",
            salt + "$" + hashlib.pbkdf2_hmac(
                _PBKDF[0], new.encode(), bytes.fromhex(salt),
                _PBKDF[1]).hex())
    cfg_set("web_pass", "")
    log_event("NMC: Web interface password changed.")
    return ("msg-ok", "Password changed. Use it at next log on.")


_NOTIFY_TEST_TEXT = ("Test notification from the APC UPS network "
                     "management card.")


def do_notify_config(form):
    def v(name):
        return form.get(name, [""])[0].strip()

    before = dict(load_state().get("cfg") or {})
    errs = []
    sev = v("nsev")
    if sev in _SEV_ORDER:
        cfg_set("notify_min_sev", sev)
    elif sev:
        errs.append("minimum severity: invalid value")
    # Non-secret fields: only written when present in the form — a
    # partial POST (API caller, stale page) must not clear config.
    if "nsyslog" in form:
        cfg_set("notify_syslog", v("nsyslog")[:64])

    for name, key in (("nwebhook", "notify_webhook"),
                      ("ndiscord", "notify_discord"),
                      ("nteams", "notify_teams")):
        if form.get("clr_" + name):
            cfg_set(key, "")
            continue
        val = v(name)
        if val:
            if not re.match(r"^https?://", val):
                errs.append("%s: must start with http(s)://" % key[7:])
                continue
            cfg_set(key, val[:256])

    if "ndismention" in form:
        men = v("ndismention")
        if men and not re.fullmatch(r"[\w@<>&!:#.-]{1,64}", men):
            errs.append("discord mention: invalid value")
        else:
            cfg_set("notify_discord_mention", men)

    if "nsmtphost" in form:
        cfg_set("notify_smtp_host", v("nsmtphost")[:64])
    if "nsmtpport" in form:
        port = v("nsmtpport")
        if port:
            if not re.fullmatch(r"\d{1,5}", port) or \
                    not 1 <= int(port) <= 65535:
                errs.append("smtp_port: invalid")
            else:
                cfg_set("notify_smtp_port", port)
        else:
            cfg_set("notify_smtp_port", "")
    tls = v("nsmtptls")
    if tls in ("none", "starttls", "ssl"):
        cfg_set("notify_smtp_tls", tls)
    elif tls:
        errs.append("smtp security: invalid value")
    if "nsmtpuser" in form:
        cfg_set("notify_smtp_user", v("nsmtpuser")[:64])
    if form.get("clr_nsmtppass"):
        cfg_set("notify_smtp_pass", "")
    elif v("nsmtppass"):
        cfg_set("notify_smtp_pass", v("nsmtppass")[:128])
    if "nsmtpfrom" in form:
        cfg_set("notify_smtp_from", v("nsmtpfrom")[:64])
    if "nsmtpto" in form:
        cfg_set("notify_smtp_to", v("nsmtpto")[:128])

    # Routing matrix: only when rt_* fields are present — a partial POST
    # (API caller, stale page) must not silently clear all routing.
    if any(k.startswith("rt_") for k in form):
        for k, _lbl in NOTIFY_KINDS:
            chans = [c for c in NOTIFY_CHANNELS
                     if form.get("rt_%s_%s" % (k, c))]
            cfg_set("notify_route_" + k, ",".join(chans))

    if errs:
        return ("msg-err", "Saved what was valid; " + "; ".join(errs))
    diff = _cfg_diff(before, load_state().get("cfg") or {})
    log_event("NMC: Notification settings changed via the network "
              "management card." +
              (" (" + "; ".join(diff)[:240] + ")" if diff else ""))
    return ("msg-ok", "Notification settings saved.")


def do_notify_test(ch):
    n = notify_cfg()
    send = {
        "syslog": (n["syslog"],
                   lambda: _send_syslog(n["syslog"],
                                        _NOTIFY_TEST_TEXT, "info")),
        "webhook": (n["webhook"],
                    lambda: _send_webhook(n["webhook"],
                                          _NOTIFY_TEST_TEXT, "info")),
        "discord": (n["discord"],
                    lambda: _send_discord(n["discord"],
                                          _NOTIFY_TEST_TEXT, "info",
                                          "other",
                                          cfg_get("ups_name", ""),
                                          n.get("discord_mention")
                                          or None)),
        "teams": (n["teams"],
                  lambda: _send_teams(n["teams"],
                                      _NOTIFY_TEST_TEXT, "info")),
        "email": (n["smtp_host"] and n["smtp_to"],
                  lambda: _send_email(n, _NOTIFY_TEST_TEXT, "info")),
    }.get(ch)
    if not send:
        return ("msg-err", "Unknown channel.")
    enabled, fn = send
    if not enabled:
        return ("msg-err",
                "%s is not configured — save settings first." % ch)
    try:
        fn()
    except Exception as e:
        return ("msg-err", "%s test failed: %s" % (ch, e))
    log_event("NMC: Test notification sent to %s." % ch,
              notify=False)   # don't re-notify the test itself
    return ("msg-ok", "Test notification sent to %s." % ch)


# ============================================================ http
SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=15552000",
    "Content-Security-Policy":
    "default-src 'self'; style-src 'unsafe-inline' 'self'; "
    "script-src 'unsafe-inline' 'self'; frame-ancestors 'none'; "
    "form-action 'self'",
}


class H(BaseHTTPRequestHandler):
    server_version = "APC-NMC/7.1.2"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing ---
    def _peer(self):
        return self.client_address[0] if self.client_address else "?"

    def _token(self):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            part = part.strip()
            if part.startswith("nmc_session="):
                return part.split("=", 1)[1]
        return None

    def _sess(self):
        tok = self._token()
        return get_session(tok) if tok else None

    def _api_ok(self):
        """Machine auth for monitoring endpoints: Authorization: Bearer
        <token> (or ?api_token=) matching [web] api_token in nmc.conf."""
        if not API_TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
        else:
            tok = parse_qs(urlparse(self.path).query
                           ).get("api_token", [""])[0]
        return bool(tok) and secrets.compare_digest(tok, API_TOKEN)

    def _send(self, code, body: "bytes | str" = b"",
              ctype="text/html", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type",
                         ctype + ("; charset=utf-8"
                                  if ctype.startswith(("text", "app"))
                                   else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _page(self, code, body, d, path="", section="", leaf=""):
        self._send(code, page("APC UPS", body, d, path, section, leaf),
                   extra={"Cache-Control": "no-store"})

    def _redir(self, url, extra=None):
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > 65536:
            self.close_connection = True
            return {}
        return parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

    def _d(self):
        if not hasattr(self, "_vars"):
            try:
                self._vars = ups_vars()
            except Exception:
                self._vars = {}
        return self._vars

    def _vars_for_js(self):
        """status.json payload: real vars minus driver internals + derived."""
        d = dict(self._d())
        for k in list(d):
            if k.startswith(("driver.parameter", "driver.flag")):
                d.pop(k)
        lab, _ = power_state(d)
        d["sys.state"] = lab
        d["sys.alarm"], d["sys.pill"] = alarms(d)
        d["sys.ts"] = str(int(time.time()))
        return d

    def _safe_next(self, f):
        nxt = f.get("next", [""])[0]
        if nxt.startswith("/") and not nxt.startswith("//"):
            return nxt
        return "/home"

    def _locked(self):
        rec = _login_fails.get(self._peer())
        if rec and rec["until"] > time.time():
            return int(rec["until"] - time.time())
        return 0

    def _record_fail(self):
        now = time.time()
        if len(_login_fails) > 512:
            for ip, rec in list(_login_fails.items()):
                if rec["until"] <= now and rec["n"] == 0:
                    _login_fails.pop(ip, None)
        rec = _login_fails.setdefault(self._peer(), {"n": 0, "until": 0})
        rec["n"] += 1
        if rec["n"] >= LOGIN_MAX_FAILS:
            rec["until"] = now + LOGIN_LOCKOUT
            rec["n"] = 0

    def _check_csrf(self, f):
        tok = self._token()
        sess = get_session(tok) if tok else None
        return sess and secrets.compare_digest(
            f.get("_csrf", [""])[0], sess["csrf"])

    # --- routing ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.__dict__.pop("_vars", None)   # _vars is per-request, but the
        p = urlparse(self.path).path        # handler persists per keep-alive conn
        if p == "/favicon.ico":
            return self._send(200, favicon(), "image/x-icon")
        if p == "/robots.txt":
            return self._send(200, ROBOTS, "text/plain")
        if p == "/status.json":
            if not self._sess():
                return self._send(401, "authentication required\n",
                                  "text/plain")
            d = self._vars_for_js()
            if "frag" in parse_qs(urlparse(self.path).query):
                fr = _status_frags(d)
                fr.update(_home_frags(d))
                d["frag"] = fr
            return self._send(200, json.dumps(d), "application/json")
        if p == "/healthz":
            return self._send(200, "ok\n", "text/plain")
        if p == "/metrics":
            if not (self._peer().startswith("127.") or self._sess()
                    or self._api_ok()):
                return self._send(403, "metrics is localhost-only\n",
                                  "text/plain")
            return self._send(200, metrics_txt(self._vars_for_js()),
                              "text/plain")
        if p == "/ca.crt":
            try:
                with open("/etc/apc-nmc/ca/ca.crt", "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, "no CA certificate\n", "text/plain")
            return self._send(200, data, "application/x-x509-ca-cert",
                              {"Content-Disposition":
                               "attachment; filename=apc-nmc-ca.crt"})
        if p == "/login":
            nxt = parse_qs(urlparse(self.path).query).get("next", [""])[0]
            if not (nxt.startswith("/") and not nxt.startswith("//")):
                nxt = "/home"
            return self._send(200, login_page(locked=self._locked(),
                                              next_url=nxt))
        if p == "/logoff":
            tok = self._token()
            if tok:
                drop_session(tok)
                log_event("NMC: User logged off.")
            return self._redir("/login", {"Set-Cookie":
                                          "nmc_session=; Max-Age=0; Path=/; "
                                          "HttpOnly; Secure; SameSite=Lax"})
        sess = self._sess()
        if not sess:
            # read-only API endpoints accept the [web] api_token instead
            if p == "/api/v1/vars" and self._api_ok():
                return self._send(200, json.dumps(self._vars_for_js()),
                                  "application/json")
            if p == "/api/v1/commands" and self._api_ok():
                try:
                    cmds = ups_cmds()
                except Exception:
                    cmds = []
                return self._send(200, json.dumps(cmds), "application/json")
            return self._redir("/login?next=" + quote(p))
        if p == "/datalog.json":
            return self._send(200, json.dumps(load_data()),
                              "application/json")
        if p == "/config/backup":
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for src, arc in ((CONF, "nmc.conf"),
                                 (STATE_FILE, "state.json"),
                                 (EVENTS_FILE, "events.json")):
                    try:
                        tf.add(src, arcname=arc)
                    except OSError:
                        pass
            return self._send(200, buf.getvalue(), "application/gzip",
                              {"Content-Disposition":
                               "attachment; filename=apc-nmc-config.tgz"})
        if p == "/about/update/progress":
            # streamed log for the apply page — pos is a byte offset so
            # each poll only carries new output. pos=-1 = first poll:
            # start at this run's marker rather than dumping old runs.
            q = parse_qs(urlparse(self.path).query)
            try:
                pos = int(q.get("pos", ["-1"])[0])
            except ValueError:
                pos = -1
            out = _load_json(UPDATE_STATUS)
            try:
                with open(UPDATE_LOG, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    if pos < 0:
                        pos = max(0, size - 65536)
                        f.seek(pos)
                        m = f.read().rfind(b"=== update started")
                        pos = pos + m if m >= 0 else max(0, size - 8192)
                    elif pos > size:
                        pos = 0       # log truncated or rotated
                    f.seek(pos)
                    chunk = f.read(262144)
                out["log"] = chunk.decode("utf-8", "replace")
                out["pos"] = pos + len(chunk)
            except OSError:
                out["log"] = ""
                out["pos"] = max(0, pos)
            return self._send(200, json.dumps(out), "application/json",
                              {"Cache-Control": "no-store"})
        d = self._d()
        if p == "/api/v1/vars":
            return self._send(200, json.dumps(d), "application/json")
        if p == "/api/v1/commands":
            try:
                cmds = ups_cmds()
            except Exception:
                cmds = []
            return self._send(200, json.dumps(cmds), "application/json")
        d["__csrf"] = sess["csrf"]
        section = leaf = ""
        try:
            if p in ("/", "/home", "/home.htm", "/NMC.htm"):
                b = pg_home(d)
            elif p == "/status/ups":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", q.get("why", [""])[0]
                           or "Battery marked as replaced.")
                elif q.get("r", [""])[0] == "err":
                    msg = ("msg-err", q.get("why", ["Update failed"])[0])
                b = pg_status(d, msg)
                section, leaf = "Status", "UPS Status"
            elif p == "/control/ups":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", "Command issued successfully.")
                elif q.get("r", [""])[0] == "err":
                    msg = fmt_cmd_result(q.get("why", [""])[0])
                try:
                    cmds = ups_cmds()
                except Exception:
                    cmds = []
                b = pg_control(d, msg, cmds)
                section, leaf = "Control", "UPS Control"
            elif p == "/control/outlets":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", "Outlet command issued successfully.")
                elif q.get("r", [""])[0] == "err":
                    msg = fmt_cmd_result(q.get("why", [""])[0])
                b = pg_outlets(d, msg)
                section, leaf = "Control", "Outlet Groups"
            elif p == "/test/diag":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", "Command issued successfully.")
                elif q.get("r", [""])[0] == "err":
                    msg = fmt_cmd_result(q.get("why", [""])[0])
                b = pg_tests(d, msg)
                section, leaf = "Tests", "Diagnostics"
            elif p == "/config/ups":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", q.get("why", [""])[0]
                           or "Configuration saved.")
                elif q.get("r", [""])[0] == "err":
                    msg = ("msg-err", q.get("why", ["Save failed"])[0])
                b = pg_config(d, msg, self._token() or "")
                section, leaf = "Configuration", "UPS"
            elif p == "/log/event":
                b = pg_events(d)
                section, leaf = "Logs", "Event Log"
            elif p == "/log/data":
                b = pg_data()
                section, leaf = "Logs", "Data Log"
            elif p == "/events.txt":
                return self._send(200, events_txt(), "text/plain",
                                  {"Content-Disposition":
                                   "attachment; filename=events.txt"})
            elif p == "/data.txt":
                q = parse_qs(urlparse(self.path).query)
                hours = None
                if q.get("hours"):
                    try:
                        hours = max(0.25, min(168,
                                              float(q["hours"][0])))
                    except ValueError:
                        pass
                if q.get("fmt", [""])[0] == "csv":
                    return self._send(200, data_txt(hours, csv=True),
                                      "text/csv",
                                      {"Content-Disposition":
                                       "attachment; filename=data.csv"})
                return self._send(200, data_txt(hours), "text/plain",
                                  {"Content-Disposition":
                                   "attachment; filename=data.txt"})
            elif p == "/about/system":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", q.get("why", [""])[0]
                           or "Done.")
                elif q.get("r", [""])[0] == "err":
                    msg = ("msg-err", q.get("why", ["Failed"])[0])
                b = pg_about(d, msg)
                section, leaf = "About", "System"
            elif p == "/about/update/apply":
                b = pg_update_apply()
                section, leaf = "About", "System"
            elif p == "/about/network":
                b = pg_network()
                section, leaf = "About", "Network"
            elif p == "/about/host":
                b = pg_host()
                section, leaf = "About", "Host"
            elif p == "/about/nut":
                b = pg_nut(d)
                section, leaf = "About", "UPS Variables"
            else:
                return self._page(404, pg_404(), d, "", "Status", "")
        except Exception:
            traceback.print_exc()
            return self._page(500, pg_500(), d)
        self._page(200, b, d, p, section, leaf)

    def do_POST(self):
        self.__dict__.pop("_vars", None)
        p = urlparse(self.path).path
        if p == "/login":
            f = self._form()
            nxt = self._safe_next(f)
            locked = self._locked()
            if locked:
                return self._send(200, login_page(locked=locked))
            u = f.get("u", [""])[0][:64]
            if _user_ok(u, f.get("p", [""])[0]):
                tok = new_session(self._peer(), u)
                log_event("NMC: User %s logged on from %s."
                          % (u, self._peer()))
                return self._redir(nxt, {
                    "Set-Cookie": "nmc_session=%s; Path=/; HttpOnly; Secure; "
                                  "SameSite=Lax" % tok})
            self._record_fail()
            log_event("NMC: Failed log on attempt from %s." % self._peer())
            return self._send(200, login_page(
                "Invalid User Name or Password.", nxt))
        sess = self._sess()
        if not sess:
            return self._redir("/login")
        if p == "/config/restore":
            q = parse_qs(urlparse(self.path).query)
            if not self._check_csrf({"_csrf": q.get("_csrf", [""])}):
                return self._send(403, "Session token missing or expired - "
                                       "reload the page and try again.")
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                n = 0
            if not 0 < n <= 4 * 1024 * 1024:
                return self._send(400, "bad upload")
            try:
                res = do_restore(self.rfile.read(n))
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Restore failed.")
            if res[0] == "msg-ok":
                threading.Timer(1.5, lambda: subprocess.run(
                    ["systemctl", "restart", "apc-nmc-web"],
                    capture_output=True, check=False)).start()
                return self._send(200, res[1], "text/plain")
            return self._send(400, res[1], "text/plain")
        f = self._form()
        if not self._check_csrf(f):
            return self._send(403, "Session token missing or expired - "
                                   "reload the page and try again.")
        d = self._d()
        d["__csrf"] = sess["csrf"]
        if p == "/control/ups":
            act = f.get("act", [""])[0]
            if act not in ("reboot", "off", "sleep", "on", "bypass",
                           "mute"):
                return self._redir("/control/ups")
            return self._send(200, page("APC UPS", pg_control_confirm(
                d, act, f.get("skipdel", [""])[0], f.get("pcns", [""])[0]),
                d, p, "Control", "UPS Control"),
                extra={"Cache-Control": "no-store"})
        if p == "/control/ups/exec":
            try:
                r = do_control(f.get("act", [""])[0],
                               f.get("skipdel", [""])[0],
                               f.get("pcns", [""])[0])
            except Exception:
                traceback.print_exc()
                r = "ERR"
            if r.startswith("OK"):
                return self._redir("/control/ups?r=ok")
            return self._redir("/control/ups?r=err&why=" + quote(r))
        if p == "/control/outlets":
            gid = f.get("gid", [""])[0]
            act = f.get("act", [""])[0]
            if act not in ("cycle", "off", "on") or \
               not re.fullmatch(r"\d+", gid or ""):
                return self._redir("/control/outlets")
            return self._send(200, page("APC UPS", pg_outlet_confirm(
                d, gid, act), d, p, "Control", "Outlet Groups"),
                extra={"Cache-Control": "no-store"})
        if p == "/control/outlets/exec":
            try:
                r = do_outlet(f.get("gid", [""])[0], f.get("act", [""])[0])
            except Exception:
                traceback.print_exc()
                r = "ERR"
            if r.startswith("OK"):
                return self._redir("/control/outlets?r=ok")
            return self._redir("/control/outlets?r=err&why=" + quote(r))
        if p == "/config/outlets":
            try:
                res = do_outlet_config(f, d)
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Save failed")
            if res[0] == "msg-ok":
                return self._redir("/control/outlets?r=ok")
            return self._redir("/control/outlets?r=err&why=" + quote(res[1]))
        if p == "/status/battery":
            try:
                res = do_battery_replaced()
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Update failed")
            if res[0] == "msg-ok":
                return self._redir("/status/ups?r=ok&why=" + quote(res[1]))
            return self._redir("/status/ups?r=err&why=" + quote(res[1]))
        if p == "/test/diag":
            try:
                r = do_test(f.get("test", [""])[0])
            except Exception:
                traceback.print_exc()
                r = "ERR"
            if r.startswith("OK"):
                return self._redir("/test/diag?r=ok")
            return self._redir("/test/diag?r=err&why=" + quote(r))
        if p == "/config/ups":
            try:
                res = do_config(f)
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Save failed")
            if res[0] == "msg-ok":
                return self._redir("/config/ups?r=ok")
            return self._redir("/config/ups?r=err&why=" + quote(res[1]))
        if p == "/config/notify":
            try:
                res = do_notify_config(f)
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Save failed")
            if res[0] == "msg-ok":
                return self._redir("/config/ups?r=ok")
            return self._redir("/config/ups?r=err&why=" + quote(res[1]))
        if p == "/config/notify/test":
            res = do_notify_test(f.get("ch", [""])[0])
            if res[0] == "msg-ok":
                return self._redir("/config/ups?r=ok&why="
                                   + quote(res[1]))
            return self._redir("/config/ups?r=err&why=" + quote(res[1]))
        if p == "/config/security":
            res = do_password_change(f)
            if res[0] == "msg-ok":
                return self._redir("/config/ups?r=ok")
            return self._redir("/config/ups?r=err&why=" + quote(res[1]))
        if p == "/config/sessions":
            cur = self._token() or ""
            if f.get("act", [""])[0] == "others":
                with _sessions_lock:
                    for t in [t for t in sessions if t != cur]:
                        sessions.pop(t, None)
                    _persist_sessions()
                log_event("NMC: All other sessions logged off by %s."
                          % WEB_USER)
                return self._redir("/config/ups?r=ok")
            drop = f.get("drop", [""])[0]
            if drop != cur and re.fullmatch(r"[0-9a-f]{32}", drop):
                drop_session(drop)
                log_event("NMC: A session was logged off by %s." % WEB_USER)
            return self._redir("/config/ups?r=ok")
        if p == "/log/clear":
            _atomic_json(EVENTS_FILE, lambda old: [])
            log_event("NMC: Event log cleared by %s." % WEB_USER)
            return self._redir("/log/event")
        if p == "/about/update/check":
            try:
                res = do_update_check()
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Update check failed")
            return self._redir("/about/system?r=%s&why=%s" % (
                "ok" if res[0] == "msg-ok" else "err", quote(res[1])))
        if p == "/about/update/apply":
            try:
                res = do_update_apply()
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Update could not be started")
            if res[0] != "msg-ok":
                return self._redir("/about/system?r=err&why=" + quote(res[1]))
            log_event("NMC: Software update started by %s." % WEB_USER)
            return self._send(200, page("APC UPS", pg_update_apply(), d,
                                        "/about/system", "About", "System"),
                              extra={"Cache-Control": "no-store"})
        if p == "/about/update/rollback":
            try:
                subprocess.run(
                    ["systemctl", "start", "--no-block",
                     "apc-nmc-update-rollback.service"],
                    check=False, capture_output=True, timeout=15)
            except Exception:
                return self._redir("/about/system?r=err&why=" +
                                   quote("Rollback could not be started"))
            log_event("NMC: Software rollback started by %s." % WEB_USER)
            return self._send(200, page("APC UPS", pg_update_apply(), d,
                                        "/about/system", "About", "System"),
                              extra={"Cache-Control": "no-store"})
        self._page(404, pg_404(), d)

    do_PUT = do_DELETE = lambda s: s._send(405, "method not allowed",
                                           "text/plain")

    def log_message(self, format="", *a):
        pass


class RedirectH(BaseHTTPRequestHandler):
    server_version = "APC-NMC/7.1.2"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0]
        host = re.sub(r"[^A-Za-z0-9.\-\[\]:]", "", host) or "localhost"
        self.send_response(301)
        self.send_header("Location", "https://%s%s" % (host, self.path))
        self.send_header("Content-Length", "0")
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    do_POST = do_HEAD = do_GET

    def log_message(self, format="", *a):
        pass


def main():
    def _term(*_a):
        _flush_datalog()
        os._exit(0)
    signal.signal(signal.SIGTERM, _term)

    load_sessions()
    log_event("NMC: Network management interface started.")
    threading.Thread(target=status_poller, daemon=True).start()
    threading.Thread(target=data_logger, daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", 80), RedirectH)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain("/etc/apc-nmc/nmc.pem")
    httpsd = ThreadingHTTPServer(("0.0.0.0", 443), H)
    httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
    httpsd.serve_forever()


if __name__ == "__main__":
    main()
