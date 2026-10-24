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
# ruff: noqa: BLE001, S110, UP031, F821, S102
# pyright: reportUndefinedVariable=false
import configparser
import fcntl
import hashlib
import html
import io
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
SESSION_MAX = 7 * 86400      # absolute cap — polling must not live forever
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT = 300          # seconds
VERSION_FILE = "/etc/apc-nmc/version"
UPDATE_STATE = "/var/lib/apc-nmc/update.json"
UPDATE_STATUS = "/var/lib/apc-nmc/update-status.json"
UPDATE_LOG = "/var/lib/apc-nmc/update.log"
UPDATE_LOCK = "/var/lib/apc-nmc/update.lock"
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
LOCATION = cfg("device", "location", "Server Room")
CONTACT = cfg("device", "contact", "admin@localhost")
NMC_MODEL = "AP9631"
NMC_SERIAL = cfg("device", "nmc_serial", "5A2436E12345")
NMC_FW = cfg("device", "nmc_fw", "v7.1.2")


def installed_version():
    """The real apcupspi release (VERSION file) — refreshed per call so the
    UI always shows the installed version, not a build-time constant."""
    for path in (VERSION_FILE,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "..", "VERSION")):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            continue
    return "?"          # install predates version stamping

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
    if not str(r).startswith(("OK", "PENDING")):  # transient hiccup?
        time.sleep(0.4)
        with _nut_lock:
            r = nut.instcmd(UPSNAME, cmd)         # one retry
    if cmd in ("beeper.mute", "beeper.enable", "beeper.disable") \
            and r.startswith(("OK", "PENDING")):
        save_state({"beeper_muted": cmd == "beeper.mute"})
    return r


def ups_setvar(var, val):
    with _nut_lock:
        r = nut.set_var(UPSNAME, var, val)
    if not str(r).startswith("OK"):          # transient modbus hiccup?
        time.sleep(0.4)
        with _nut_lock:
            r = nut.set_var(UPSNAME, var, val)  # one retry
    return r


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


def conf_set(sec, key, value):
    """Upsert `key = value` inside [sec] of nmc.conf in place — preserves
    comments and order (unlike configparser's writer), creates the
    section when absent, reloads the live parser afterwards. Used for
    settings the shell-side tools read from nmc.conf directly."""
    value = str(value).replace("\n", " ").replace("\r", " ").strip()
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    key_re = re.compile(r"^\s*#?\s*" + re.escape(key) + r"\s*=")
    out = []
    cur = ""          # current section while scanning
    done = False
    for line in lines:
        m = re.match(r"\s*\[\s*([^]]+)", line)
        if m:
            s = m.group(1).strip()
            if cur == sec and not done:      # leaving our section,
                out.append("%s = %s" % (key, value))  # key never found
                done = True
            cur = s
            out.append(line)
            continue
        if cur == sec and key_re.match(line):
            if not done:
                out.append("%s = %s" % (key, value))
                done = True
            continue          # drop commented/duplicate keys
        out.append(line)
    if not done:
        if cur == sec:                        # key missing, section at EOF
            out.append("%s = %s" % (key, value))
        else:                                 # section absent entirely
            out += ["", "[%s]" % sec, "%s = %s" % (key, value)]
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    try:
        st = os.stat(CONF)      # keep the live file's mode + group —
        os.chown(tmp, -1, st.st_gid)  # Debian-snmp needs group-read to
        os.chmod(tmp, st.st_mode & 0o777)  # reach [upsd]/[snmp] creds
    except OSError:
        os.chmod(tmp, 0o640)
    os.replace(tmp, CONF)
    _cp.read(CONF)


def conf_del(sec, key):
    """Drop `key` lines from [sec] of nmc.conf — counterpart of conf_set
    for removing entries (e.g. deleting a [users] account)."""
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    key_re = re.compile(r"^\s*#?\s*" + re.escape(key) + r"\s*=")
    out = []
    cur = ""
    for line in lines:
        m = re.match(r"\s*\[\s*([^]]+)", line)
        if m:
            cur = m.group(1).strip()
            out.append(line)
            continue
        if cur == sec and key_re.match(line):
            continue                      # remove matching entries
        out.append(line)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    try:
        st = os.stat(CONF)
        os.chown(tmp, -1, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o640)
    os.replace(tmp, CONF)
    _cp.read(CONF)


def _sysctl(*args):
    try:
        r = subprocess.run(["systemctl"] + list(args), capture_output=True,
                           text=True, check=False, timeout=20)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return 1, ""


def svc_state(name):
    """-> (enabled, active) for a systemd unit."""
    _rc, e = _sysctl("is-enabled", name)
    _rc, a = _sysctl("is-active", name)
    return e == "enabled", a == "active"


def svc_apply(name, enable):
    """Enable+start / disable+stop a unit; when already enabled a plain
    restart applies fresh config."""
    en, _act = svc_state(name)
    if enable:
        args = ("restart", name) if en else ("enable", "--now", name)
    else:
        args = ("disable", "--now", name) if en else ("stop", name)
    rc, out = _sysctl(*args)
    return rc == 0, out


def net_ifaces(prefixes):
    """Detected interface names under /sys/class/net filtered by name
    prefix (loopback excluded): ("e",) -> wired, ("wl",) -> wireless."""
    try:
        return sorted(n for n in os.listdir("/sys/class/net")
                      if n != "lo" and n.startswith(prefixes))
    except OSError:
        return []


def _run(cmd, timeout=20):
    """Run a command -> (rc, combined output); tolerates missing tools."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _nmcli(*args, timeout=15):
    return _run(["nmcli"] + list(args), timeout)


def wifi_status():
    """-> (radio state str, active wifi connection name, nm available)."""
    try:
        with open("/usr/bin/nmcli"):
            pass
    except OSError:
        return "unavailable", "", False
    _rc, radio = _nmcli("-t", "-f", "WIFI", "general")
    conn = ""
    _rc, out = _nmcli("-t", "-f", "NAME,DEVICE", "con", "show", "--active")
    for ln in out.splitlines():
        name, _, dev = ln.rpartition(":")
        if dev.startswith("wl"):
            conn = name
            break
    sig = ""
    if conn:
        _rc, out = _nmcli("-t", "-f", "IN-USE,SIGNAL", "dev", "wifi")
        for ln in out.splitlines():
            if ln.startswith("*:"):
                sig = ln.split(":", 1)[1].strip() + "%"
                break
    return radio or "unknown", conn + (" (%s)" % sig if sig else ""), True


BOOTCFGS = ("/boot/firmware/config.txt", "/boot/config.txt")
WIFI_HW_REQ = "/var/lib/apc-nmc/wifi-hw.request"
PROC_STAT = "/proc/stat"


def boot_cfg_pending():
    """True when config.txt was modified after this boot — overlay edits
    only take effect at boot time, so the running radios don't match
    the on-disk intent until a reboot."""
    try:
        with open(PROC_STAT) as f:
            btime = next(int(l.split()[1]) for l in f
                         if l.startswith("btime"))
    except (OSError, StopIteration, ValueError, IndexError):
        return False
    for path in BOOTCFGS:
        try:
            if os.path.getmtime(path) > btime + 5:
                return True
        except OSError:
            continue
    return False


def wifi_hw_status():
    """-> {"wifi": bool, "bt": bool, "cfg": bool} — True means the radio
    is killed at boot by dtoverlay=disable-* in config.txt (what the
    installer's hardware hardening writes). cfg = config.txt readable."""
    txt = ""
    for path in BOOTCFGS:
        try:
            with open(path) as f:
                txt = f.read()
            break
        except OSError:
            continue
    return {
        radio: bool(re.search(r"(?m)^\s*dtoverlay=disable-%s\b" % radio,
                              txt))
        for radio in ("wifi", "bt")} | {"cfg": bool(txt)}


def wifi_scan(ifname=""):
    """-> [{'ssid','signal','security'}] from nmcli dev wifi list."""
    args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"]
    if ifname:
        args += ["ifname", ifname]
    rc, out = _nmcli(*args, timeout=25)
    if rc != 0:
        return []
    nets = {}
    for ln in out.splitlines():
        parts = ln.rsplit(":", 2)          # SSID:SIGNAL:SECURITY
        if len(parts) != 3 or not parts[0]:
            continue
        ssid, sig, secu = parts
        try:
            sig = int(sig)
        except ValueError:
            sig = 0
        if ssid not in nets or sig > nets[ssid]["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": sig,
                          "security": secu}
    return sorted(nets.values(), key=lambda n: -n["signal"])[:20]


_tz_cache = {"list": [], "at": 0}


def timezones():
    """timedatectl's zone list, cached for 5 min (it's ~500 entries)."""
    if not _tz_cache["list"] or time.time() - _tz_cache["at"] > 300:
        _rc, out = _run(["timedatectl", "list-timezones"], 15)
        if out:
            _tz_cache.update(list=out.splitlines(), at=time.time())
    return _tz_cache["list"]


def secure_boot_status():
    """Best-effort Raspberry Pi secure-boot state: Pi 5/CM4 signed boot
    surfaces as a bootloader devicetree property; other platforms can't
    do it and report n/a. Enabling requires OTP/eeprom changes that must
    be done manually — see docs/secure-boot.md."""
    for p in ("/proc/device-tree/chosen/bootloader/secure-boot",
              ("/sys/firmware/devicetree/base/chosen/bootloader/"
               "secure-boot")):
        try:
            with open(p, "rb") as f:
                v = f.read(4).rstrip(b"\x00")
            if v in (b"\x01", b"1"):
                return "enabled"
            return "disabled"
        except OSError:
            continue
    return "not supported on this platform"


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


def new_session(ip="", user="", role="admin"):
    tok = secrets.token_hex(16)
    with _sessions_lock:
        # prune expired while we're here
        dead = [t for t, s in sessions.items() if s["exp"] <= time.time()]
        for t in dead:
            sessions.pop(t, None)
        sessions[tok] = {"exp": time.time() + SESSION_TTL,
                         "csrf": secrets.token_hex(16),
                         "ip": ip, "created": time.time(), "user": user,
                         "role": role}
        _persist_sessions()
    return tok


def get_session(tok):
    now = time.time()
    with _sessions_lock:
        s = sessions.get(tok)
        if s and s["exp"] > now \
                and now - s.get("created", 0) <= SESSION_MAX:
            old = s["exp"]
            # sliding renewal, but never past the absolute cap
            s["exp"] = min(now + SESSION_TTL,
                           s.get("created", now) + SESSION_MAX)
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


USER_ROLES = ("admin", "device", "network", "readonly")

# Capability map: cap -> (label, POST path-prefixes it grants). Every
# logged-in role may VIEW all pages — cards only render the controls a
# capability grants — and always gets the baseline self-service posts.
# admin holds every capability and is not editable in the matrix;
# other roles read their [roles] entry in nmc.conf (ROLE_CAP_DEFAULTS
# when unset — an empty configured value is honoured, that's how a
# role is reduced to baseline).
ROLE_CAPS = {
    "ups":      ("UPS Settings",
                 ("/config/ups", "/config/outlets", "/status/battery")),
    "control":  ("UPS Control & Tests",
                 ("/control/", "/test/diag", "/test/schedule")),
    "notify":   ("Notifications",
                 ("/config/notify", "/config/notify/test")),
    "network":  ("Network Services",
                 ("/config/services", "/config/firewall",
                  "/config/dot1x", "/config/wifi", "/config/datalog")),
    "accounts": ("Accounts & RADIUS",
                 ("/config/users", "/config/radius", "/config/roles")),
    "system":   ("System & Updates",
                 ("/config/sessions", "/config/backup", "/config/restore",
                  "/config/support", "/log/clear", "/about/update/",
                  "/about/sysact", "/config/time")),
}
ROLE_CAP_DEFAULTS = {
    "device": ("ups", "control"),
    "network": ("notify", "network"),
    "readonly": (),
}
_BASE_POSTS = ("/config/security",)      # own-password change, always on


def role_caps(role):
    """Effective capability set for a role."""
    if role == "admin":
        return set(ROLE_CAPS)
    if _cp.has_option("roles", role):
        return set(_cp.get("roles", role).split()) & set(ROLE_CAPS)
    return set(ROLE_CAP_DEFAULTS.get(role, ()))


def _role_posts(role):
    posts = set(_BASE_POSTS)
    for c in role_caps(role):
        posts.update(ROLE_CAPS[c][1])
    return tuple(posts)


def _prefix_ok(path, prefixes):
    for pre in prefixes:
        if path == pre or (pre.endswith("/") and path.startswith(pre)) \
                or path.startswith(pre + "/"):
            return True
    return False


def role_get_ok(role, path):
    # every logged-in role may view every page; the page renders only
    # the controls its capabilities unlock. Sensitive downloads are
    # gated inline in do_GET via role_post_ok instead.
    return True


def role_post_ok(role, path):
    return role == "admin" or _prefix_ok(path, _role_posts(role))


def _parse_user_cred(stored):
    """[users] value -> (role, credential). New format is
    `role:<salt>$<hash>` or `role:plaintext`; a bare value (no valid
    role prefix) is a legacy full-access admin credential."""
    head, sep, rest = stored.partition(":")
    if sep and head.strip().lower() in USER_ROLES and rest:
        return head.strip().lower(), rest
    return "admin", stored


def _cred_ok(stored, pw):
    salt, _, digest = stored.partition("$")
    if digest:                      # hashed form
        try:
            d = hashlib.pbkdf2_hmac(_PBKDF[0], pw.encode(),
                                    bytes.fromhex(salt), _PBKDF[1])
        except ValueError:
            return False
        return secrets.compare_digest(d.hex(), digest)
    return secrets.compare_digest(pw, stored)


# ------------------------------------------------------------- RADIUS
def _radius_hide_pass(pw, secret, authenticator):
    """RFC 2865 User-Password attribute: pad to 16-byte blocks, XOR with
    md5(secret + authenticator) for the first block and md5(secret +
    c_i-1) afterwards."""
    raw = pw.encode()[:128]
    if len(raw) % 16:
        raw += b"\x00" * (16 - len(raw) % 16)
    out = b""
    prev = authenticator
    for i in range(0, len(raw), 16):
        b = hashlib.md5(secret + prev).digest()
        out += bytes(x ^ y for x, y in zip(raw[i:i + 16], b))
        prev = out[i:i + 16]
    return out


def _radius_attr(t, val):
    return bytes((t, len(val) + 2)) + val


def radius_auth(user, pw):
    """PAP Access-Request against [radius] server. Returns the configured
    default role on Access-Accept, None otherwise. Only runs when local
    auth failed — RADIUS never shadows local accounts."""
    if cfg("radius", "enabled", "0") not in ("1", "yes", "true", "on"):
        return None
    server = cfg("radius", "server", "").strip()
    secret = cfg("radius", "secret", "").encode()
    if not server or not secret or not pw:
        return None
    try:
        port = int(cfg("radius", "port", "1812"))
    except ValueError:
        port = 1812
    try:
        timeout = float(cfg("radius", "timeout", "4"))
    except ValueError:
        timeout = 4
    import struct
    ident = secrets.randbelow(256)
    req_auth = secrets.token_bytes(16)
    attrs = (_radius_attr(1, user.encode()[:253])
             + _radius_attr(2, _radius_hide_pass(pw, secret, req_auth)))
    pkt = struct.pack("!BBH", 1, ident, 20 + len(attrs)) + req_auth + attrs
    try:
        fam, _, _, _, sa = socket.getaddrinfo(
            server, port, proto=socket.IPPROTO_UDP)[0]
        s = socket.socket(fam, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(pkt, sa)
        resp, _ = s.recvfrom(4096)
        s.close()
    except Exception:
        log_event("NMC: RADIUS server %s unreachable." % server,
                  notify=False)
        return None
    if len(resp) < 20:
        return None
    code, rid, rlen = struct.unpack("!BBH", resp[:4])
    if rid != ident or rlen > len(resp):
        return None
    expect = hashlib.md5(resp[:4] + req_auth + resp[20:rlen] + secret) \
        .digest()
    if resp[4:20] != expect:
        return None
    if code == 2:                                # Access-Accept
        role = cfg("radius", "role", "readonly").strip().lower()
        return role if role in USER_ROLES else "readonly"
    return None                                  # Access-Reject/other


def user_auth(u, pw):
    """-> role string or None. Primary web user is always admin; extra
    accounts come from [users] as `name = [role:]credential` — value is
    either plaintext (legacy) or `<salt_hex>$<pbkdf2_sha256_hex>`, the
    same format web_pass_hash uses (generate: python3 -c 'import
    hashlib,secrets; s=secrets.token_hex(16); print("admin:" + s + "$" +
    hashlib.pbkdf2_hmac("sha256", b"PASS", bytes.fromhex(s),
    200_000).hexdigest())'). Falls back to RADIUS when enabled."""
    if secrets.compare_digest(u, WEB_USER) and web_password_ok(pw):
        return "admin"
    if _cp.has_section("users"):
        for name in set(_cp.options("users")) - set(_cp.defaults()):
            if not secrets.compare_digest(u, name):
                continue
            role, cred = _parse_user_cred(_cp.get("users", name))
            if _cred_ok(cred, pw):
                return role
            return None            # name matched, password didn't —
                                   # don't leak it to the RADIUS backend
    return radius_auth(u, pw)


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
        "syslog_insecure": cfg_get("notify_syslog_insecure", False),
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
        "trap_dest": cfg_get("notify_trap_dest",
                             cfg("notify", "trap_dest", "")),
        "trap_comm": cfg_get("notify_trap_comm",
                             cfg("notify", "trap_community", "public")),
        "routes": notify_routes(),
    }


# Routing kinds: each STATUS_EVENTS flag edge maps to one; "other"
# catches every non-flag event (logins, config changes, NMC notices).
NOTIFY_CHANNELS = ("syslog", "webhook", "discord", "teams",
                 "email", "trap")

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


def _syslog_target(host):
    """-> (scheme, host, port). 'local'/'/dev/log' = journald; a bare
    host[:port] is UDP/514 (NMC-style); tcp:// and tls:// are the
    RFC 5425 transports (defaults 601 / 6514)."""
    h = host.strip()
    for scheme, dport in (("tls://", 6514), ("tcp://", 601)):
        if h.lower().startswith(scheme):
            h = h[len(scheme):]
            hp = h.rsplit(":", 1)
            if len(hp) == 2 and hp[1].isdigit():
                return scheme[:-3], hp[0].strip("[]"), int(hp[1])
            return scheme[:-3], h.strip("[]"), dport
    hp = h.rsplit(":", 1)
    if len(hp) == 2 and hp[1].isdigit():
        return "udp", hp[0].strip("[]"), int(hp[1])
    return "udp", h.strip("[]"), 514


def _send_syslog(host, text, sev):
    pri = {"crit": 130, "warn": 132, "info": 134}.get(sev, 134)
    pkt = ("<%d>apc-nmc: %s" % (pri, text)).encode()
    if host in ("local", "/dev/log"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect("/dev/log")
        s.send(pkt)
        s.close()
        return
    scheme, addr, port = _syslog_target(host)
    if scheme == "udp":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(pkt, (addr, port))
        s.close()
        return
    s = socket.create_connection((addr, port), 5)
    if scheme == "tls":
        if cfg_get("notify_syslog_insecure", False):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
        s = ctx.wrap_socket(s, server_hostname=addr)
    try:
        s.sendall(str(len(pkt)).encode() + b" " + pkt)  # octet-counted
    finally:
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


def _send_trap(dest, comm, text, sev):
    """SNMPv2c trap to a manager: enterprise-specific under the APC
    PowerNet OID (.318) carrying the event text + severity as a string
    varbind — what an NMC sends to e.g. StruxureWare/Zabbix/snmptrapd."""
    oid = "1.3.6.1.4.1.318.2.4.2.2"
    payload = "[%s] %s" % (sev.upper(), text)
    r = subprocess.run(
        ["snmptrap", "-v", "2c", "-c", comm, dest, "",
         "." + oid, "." + oid + ".0", "s", payload],
        check=False, capture_output=True, text=True, timeout=8)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "snmptrap failed").strip())


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
             lambda: _send_email(n, text, sev)),
            ("trap", n["trap_dest"], lambda: _send_trap(
                n["trap_dest"], n["trap_comm"], text, sev))):
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


# ---- MQTT telemetry ([mqtt]) ------------------------------------------
# Minimal MQTT 3.1.1 QoS-0 publisher in stdlib — paho isn't guaranteed
# installed on the appliance and CONNECT+PUBLISH is a small wire format.

def _vbi(n):
    """MQTT remaining-length variable byte integer."""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _mqs(s):
    """MQTT length-prefixed UTF-8 string."""
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def mqtt_publish(host, port, topic, payload, client_id="apc-nmc",
                 user="", pw="", tls=False, insecure=False, timeout=8):
    """One-shot CONNECT + PUBLISH (QoS 0) + disconnect.
    -> (True, "") on success, (False, error) otherwise."""
    flags = 0x02                                # clean session
    if user:
        flags |= 0x80 | (0x40 if pw else 0)
    vh = (b"\x00\x04MQTT\x04" + bytes([flags])
          + (60).to_bytes(2, "big"))
    pl = _mqs(client_id)
    if user:
        pl += _mqs(user)
    if pw:
        pl += _mqs(pw)
    conn = b"\x10" + _vbi(len(vh) + len(pl)) + vh + pl
    body = _mqs(topic) + payload.encode("utf-8", "replace")
    pub = b"\x30" + _vbi(len(body)) + body
    try:
        s = socket.create_connection((host, port), timeout)
        if tls:
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=host)
        with s:
            s.sendall(conn)
            resp = s.recv(4)
            if len(resp) < 4 or resp[0] != 0x20 or resp[3] != 0:
                return (False, "broker refused connect (rc=%s)"
                        % (resp[3] if len(resp) > 3 else "?"))
            s.sendall(pub)
        return True, ""
    except Exception as e:
        return False, str(e)


_MQTT_VARS = ("ups.status", "battery.charge", "battery.runtime",
              "battery.voltage", "battery.temperature", "ups.load",
              "ups.realpower", "ups.power", "input.voltage",
              "input.frequency", "output.voltage", "output.frequency")
_mqtt_state = {"ok": None, "err": "", "at": 0, "n": 0}


def mqtt_cfg():
    on = lambda v: str(v).lower() in ("1", "yes", "true", "on")
    return {"enabled": on(cfg("mqtt", "enabled", "0")),
            "host": cfg("mqtt", "host", ""),
            "port": cfg("mqtt", "port", "1883"),
            "tls": on(cfg("mqtt", "tls", "0")),
            "insecure": on(cfg("mqtt", "tls_insecure", "0")),
            "user": cfg("mqtt", "user", ""),
            "pass": cfg("mqtt", "password", ""),
            "topic": (cfg("mqtt", "topic", "apc-nmc/myups").strip("/")
                      or "apc-nmc/myups"),
            "interval": cfg("mqtt", "interval", "60")}


def mqtt_send(payload):
    m = mqtt_cfg()
    try:
        port = int(m["port"] or 1883)
    except ValueError:
        return False, "invalid port"
    return mqtt_publish(m["host"], port, m["topic"] + "/state", payload,
                        user=m["user"], pw=m["pass"], tls=m["tls"],
                        insecure=m["insecure"])


def mqtt_publisher():
    """Publish key UPS vars as JSON to <topic>/state every interval
    seconds while enabled; last result is shown on the services card."""
    while True:
        try:
            iv = max(15, int(mqtt_cfg()["interval"] or 60))
        except (ValueError, TypeError):
            iv = 60
        try:
            m = mqtt_cfg()
            if m["enabled"] and m["host"]:
                d = ups_vars()
                pl = json.dumps({"ts": int(time.time()), **{
                    k: d[k] for k in _MQTT_VARS if k in d}})
                ok, err = mqtt_send(pl)
                try:   # HA-friendly plain-text companions of the JSON
                    port = int(m["port"] or 1883)
                    mqtt_publish(m["host"], port,
                                 m["topic"] + "/status",
                                 d.get("ups.status", ""),
                                 user=m["user"], pw=m["pass"],
                                 tls=m["tls"], insecure=m["insecure"])
                    mqtt_publish(m["host"], port,
                                 m["topic"] + "/availability",
                                 "online" if ups_live(d) else "offline",
                                 user=m["user"], pw=m["pass"],
                                 tls=m["tls"], insecure=m["insecure"])
                except Exception:
                    pass
                _mqtt_state.update(ok=ok, err=err,
                                   n=_mqtt_state["n"] + 1,
                                   at=int(time.time()) if ok
                                   else _mqtt_state["at"])
        except Exception:
            pass
        time.sleep(iv)


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


def _maybe_scheduled_shutdown(ok):
    """Calendar UPS power-off ([schedule] shutdown_weekday/hour/mode).
    Fires shutdown.return (power back when utility returns) or
    shutdown.stayoff at most once per matching day."""
    wd, hr = (cfg("schedule", "shutdown_weekday", "").strip(),
              cfg("schedule", "shutdown_hour", "").strip())
    if not wd or not hr or not ok:
        return
    try:
        wd_i, hr_i = int(wd), int(hr)
    except ValueError:
        return
    t = time.localtime()
    today = time.strftime("%Y-%m-%d")
    if t.tm_wday == wd_i and t.tm_hour == hr_i and \
            cfg_get("shutdown_sched_last", "") != today:
        cfg_set("shutdown_sched_last", today)
        mode = cfg("schedule", "shutdown_mode", "return").strip()
        cmd = ("shutdown.stayoff" if mode == "stayoff"
               else "shutdown.return")
        log_event("UPS: Scheduled shutdown started (%s)." % cmd, "warn")
        ups_cmd(cmd)


def _maybe_scheduled_reboot(ok):
    """Calendar UPS reboot ([schedule] reboot_weekday/hour) — fires
    shutdown.reboot at most once per matching day. Unlike power-off,
    the UPS always restores load after its configured reboot delay."""
    wd, hr = (cfg("schedule", "reboot_weekday", "").strip(),
              cfg("schedule", "reboot_hour", "").strip())
    if not wd or not hr or not ok:
        return
    try:
        wd_i, hr_i = int(wd), int(hr)
    except ValueError:
        return
    t = time.localtime()
    today = time.strftime("%Y-%m-%d")
    if t.tm_wday == wd_i and t.tm_hour == hr_i and \
            cfg_get("reboot_sched_last", "") != today:
        cfg_set("reboot_sched_last", today)
        log_event("UPS: Scheduled reboot started.", "warn")
        ups_cmd("shutdown.reboot")


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
            ok = ups_live(d)   # upsd answering isn't enough — the
                               # driver must have polled the UPS too
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
        _maybe_scheduled_shutdown(ok)
        _maybe_scheduled_reboot(ok)
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
        _ob_since(d, ok)
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
            if not ups_live(d):
                time.sleep(60)
                continue     # UPS gone/stale — don't log dead rows
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


def datalog_page(data, off, lim):
    """Newest-first slice of the datalog for lazy loading the table."""
    off = max(0, off)
    return {"total": len(data),
            "rows": list(reversed(data))[off:off + lim]}


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


_URL_RE = re.compile(r"(https?://[^\s<>'\"]+)")


def linkify(text):
    # esc() + wrap bare URLs in anchors — used for release notes, which
    # arrive as plain text (release body markdown) over update.json
    out = []
    for part in _URL_RE.split(str(text)):
        if _URL_RE.fullmatch(part):
            url = part.rstrip(".,;:!?)]\"'")
            out.append('<a href="%s" target="_blank" rel="noopener '
                       'noreferrer">%s</a>%s'
                       % (esc(url), esc(url), esc(part[len(url):])))
        else:
            out.append(esc(part))
    return "".join(out)


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
        h, m = s // 3600, s % 3600 // 60
        return ("%dh %dm" % (h, m)) if h else "%dm" % m
    except Exception:
        return "unknown"


def battery_health(d):
    """Composite 0-100 battery-health score: install age (5y+ = worst),
    replace flag, last self-test result, and low charge. None when no
    battery data exists at all."""
    if not any(k in d for k in ("battery.charge", "battery.voltage",
                                "battery.test.result")):
        return None
    score = 100
    age = battery_age_years()
    if age is not None:
        score -= min(40, int(age / 5 * 40))
    if "RB" in toks(d):
        score -= 30
    res = str(d.get("battery.test.result", "")).lower()
    if "fail" in res or "abort" in res or "bad" in res:
        score -= 30
    elif "warn" in res:
        score -= 15
    chg = num(d, "battery.charge")
    if chg is not None and chg < 60:
        score -= int((60 - chg) / 2)
    score = max(0, min(100, score))
    label = ("Good" if score >= 80 else
             "Fair" if score >= 50 else "Poor")
    return score, label


def fmt_battery_age():
    """Human battery age: 'new' / 'N months' / 'X.Y years'."""
    a = battery_age_years()
    if a is None:
        return "unknown"
    if a < 0.15:
        return "new"
    if a < 1:
        return "%d months" % max(1, round(a * 12))
    return "%.1f years" % a


def card_health():
    """Pi-side vitals for status.json / support: uptime, load, SoC temp."""
    out = {}
    try:
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        d, rem = divmod(up, 86400)
        h, m = divmod(rem // 60, 60)
        out["sys.uptime"] = ("%dd " % d if d else "") + "%dh %dm" % (h, m)
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["sys.loadavg"] = " ".join(
            "%.2f" % x for x in os.getloadavg())
    except OSError:
        pass
    for tz in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(tz) as f:
                out["sys.cputemp"] = "%.1f" % (int(f.read()) / 1000.0)
            break
        except (OSError, ValueError):
            continue
    return out


def _ob_since(d, ok):
    """Epoch the UPS went on battery (persisted in state.json), or None."""
    st = load_state()
    if "OB" in toks(d):
        if not st.get("ob_since"):
            cfg_set("ob_since", int(time.time()))
            return int(time.time())
        return st.get("ob_since")
    if st.get("ob_since"):
        cfg_set("ob_since", "")
    return None


def sched_next(wd_s, hr_s):
    """Epoch of the next weekly fire time for weekday+hour cfg, or None."""
    try:
        wd, hr = int(wd_s), int(hr_s)
    except (ValueError, TypeError):
        return None
    now = time.localtime()
    days = (wd - now.tm_wday) % 7
    cand = time.mktime((now.tm_year, now.tm_mon, now.tm_mday + days,
                      hr, 0, 0, -1, -1, -1))
    if cand <= time.time():
        cand += 7 * 86400
    return cand


def ups_live(d):
    """Live UPS data = upsd answered AND the driver has polled the UPS —
    ups.status is always published after a successful poll. A var set
    without it (UPS unplugged, driver still starting, data stale) means
    'no UPS', not merely unknown status flags."""
    return bool(d.get("ups.status"))


def power_state(d):
    ts = toks(d)
    if not ups_live(d):
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
    if not ups_live(d):
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
    if not ups_live(d):
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


def msgbox(msg):
    """Render a (css-class, text) flash banner — both parts are escaped;
    `why=` text in post-redirect URLs lands here user-controlled."""
    if not msg:
        return ""
    return '<div class="%s">%s</div>' % (esc(msg[0]), esc(msg[1]))


# ============================================================ page frame
_SVG = {
    "home": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9 21v-6h6v6"/></svg>',
    "status": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 8L9 4l-3 8H2"/></svg>',
    "control": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="9" cy="6" r="2.2" fill="currentColor"/><circle cx="15" cy="12" r="2.2" fill="currentColor"/><circle cx="7" cy="18" r="2.2" fill="currentColor"/></svg>',
    "config": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 8.86 1.7 1.7 0 0 0 4.26 7l-.06-.06A2 2 0 1 1 7.03 4.1l.06.06A1.7 1.7 0 0 0 8.96 4.5 1.7 1.7 0 0 0 10 2.94V3a2 2 0 1 1 4 0v.09c.68.28 1.4.28 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9c.28.68.92 1.03 1.56 1.03H21a2 2 0 1 1 0 4h-.09c-.64 0-1.23.35-1.51.97z"/></svg>',
    "tests": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2v7.5L4.5 20a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 9.5V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/></svg>',
    "logs": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
    "about": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    "help": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.4-3 4"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    "users": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
}

NAV = [
    ("Home", "/home", "home", []),
    ("Status", "/status/ups", "status", [("UPS Status", "/status/ups")]),
    ("Control", "/control/ups", "control",
     [("UPS Control", "/control/ups"),
      ("Outlet Groups", "/control/outlets")]),
    ("Configuration", "/config/ups", "config",
     [("UPS", "/config/ups"), ("System", "/config/system")]),
    ("Users", "/config/users", "users",
     [("User Access", "/config/users")]),
    ("Tests", "/test/diag", "tests", [("Diagnostics", "/test/diag")]),
    ("Logs", "/log/event", "logs",
     [("Event Log", "/log/event"), ("Data Log", "/log/data"),
      ("Statistics", "/log/stats")]),
    ("About", "/about/system", "about",
     [("System", "/about/system"), ("Network", "/about/network"),
      ("Host", "/about/host"), ("UPS Variables", "/about/nut")]),
    ("Help", "/help", "help", [("Documentation", "/help")]),
]


# Help topics: slug -> (title, blurb). Content lives as HTML fragments
# in apc-nmc-web/help/<slug>.html (inlined by _asset).
HELP_TOPICS = [
    ("index", "Getting Around", "Layout, navigation, live data"),
    ("home", "Home Dashboard", "Tiles, alarms, recent events"),
    ("status", "UPS Status", "Every status field explained"),
    ("control", "UPS Control & Outlets", "Safe shutdown, beeper, outlet groups"),
    ("tests", "Diagnostics & Scheduling", "Self-test, calibration, schedules"),
    ("config-ups", "Configuration: UPS", "Thresholds and identity settings"),
    ("config-notify", "Configuration: Notifications", "Every channel, routing matrix"),
    ("config-system", "Configuration: System", "Users, RADIUS, services, firewall, WiFi"),
    ("security", "Security & Backup", "Passwords, sessions, backup/restore"),
    ("logs", "Event & Data Logs", "Filtering, export, energy stats"),
    ("network", "About: Network & Host", "Interface info, Pi health, NUT vars"),
    ("about", "About, Updates & Support", "Versions, updates, API token, bundle"),
    ("api", "API & Integrations", "JSON API, metrics, MQTT, Modbus, SNMP"),
    ("nut-slave", "Connecting Servers (NUT)", "upsd slaves, PowerChute alternatives"),
    ("roles", "User Roles", "The four access tiers"),
    ("troubleshooting", "Troubleshooting", "Common problems and fixes"),
    ("glossary", "Glossary", "Status codes and terms"),
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
        if any(path.startswith(u + "/") for u in [link] + [x for _, x in sub]):
            return name
    return ""


def tip(text):
    """Inline '?' help badge; tooltip text appears on hover/focus."""
    return ('<span class="tip" tabindex="0" title="%s" '
            'aria-label="%s">?</span>' % (esc(text), esc(text)))


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


_WIDE_PATHS = frozenset((
    "/about/system", "/about/network", "/about/host", "/about/nut",
    "/status/ups", "/test/diag", "/home", "/log/stats",
    "/control/outlets", "/config/ups",
    "/config/system", "/config/users"))

# Pages that want the wider column but a SINGLE column of cards — the
# card grid would leave big tables half-width.
_FLAT_PATHS = frozenset(("/log/event", "/log/data", "/help"))


def page(title, body, d, path="", section="", leaf="",
         wide=False):
    alabel, acls = alarms(d)
    wide = (wide or path in _WIDE_PATHS or path in _FLAT_PATHS
            or path.startswith("/help/"))
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
<div id="obbanner" class="obbanner" hidden>&#9888; UPS ON BATTERY
<span id="obb"></span></div>
%s
<div class="shell">
<nav class="side">%s</nav>
<details class="mnav"><summary>Menu</summary>%s</details>
<main><div class="content%s">%s%s</div>
<div class="ftr"><div><a href="/about/system">System</a> &middot;
<a href="/about/network">Network</a> &middot;
<a href="/log/event">Event Log</a></div>
<div>UPS Network Management Pi &middot; %s &middot; Updated: %s</div>
</div></main>
</div>
<script>%s</script>
</body></html>""" % (
        _asset("theme.js"), esc(title), _asset("nmc.css"),
        esc(d.get("ups.model", "Smart-UPS")),
        acls, esc(alabel), SUN, esc(d.get("__uinfo", WEB_USER)),
        updbar, nav, mnav,
        (" wide" if wide else "") +
        (" flat" if (path in _FLAT_PATHS or path.startswith("/help/"))
         else ""),
        crumb, body,
        esc("v" + installed_version()),
        time.strftime("%m/%d/%Y at %H:%M"),
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
<div class="sub">UPS Network Management Pi</div>
<form method="post" action="/login">
<input type="hidden" name="next" value="%s">
<label for="u">User Name</label><input id="u" name="u" autocomplete="username" autofocus>
<label for="p">Password</label><input id="p" name="p" type="password" autocomplete="current-password">
<button class="btn" id="logon">Log On &gt;&gt;</button></form>%s
</div>
<div class="loginfoot">APC-UPS-Pi &mdash; v%s</div>
</div></body></html>""" % (_asset("theme.js"), _asset("nmc.css"),
                              esc(next_url), e,
                              esc(installed_version()))


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
    server_version = "APC-NMC-Pi/" + installed_version()
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
        <token> matching [web] api_token in nmc.conf. Header-only — a
        ?token= URL slot would leak credentials into browser history,
        Referer headers and upstream access logs (SEC-026)."""
        token = cfg("web", "api_token", "")   # live read — settable in UI
        if not token:
            return False
        auth = self.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return bool(tok) and secrets.compare_digest(tok, token)

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
        d["sys.updstate"] = update_status_view(
            _load_json(UPDATE_STATUS)).get("state", "")
        d["sys.ts"] = str(int(time.time()))
        d.update(card_health())
        return d

    def _safe_next(self, f):
        # strict path-charset whitelist: \ could collapse to // (open
        # redirect via browser normalization), : and controls rejected;
        # a leading // is a protocol-relative URL — never a safe next
        nxt = f.get("next", [""])[0]
        if nxt.startswith("/") and not nxt.startswith("//") \
                and re.fullmatch(r"[A-Za-z0-9._~/%?&=+#@!,\-]*",
                                 nxt[1:]):
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
            if p == "/api/v1/events" and self._api_ok():
                return self._send(200, json.dumps(load_events()),
                                  "application/json")
            # machine-readable routes get a real 401, not a login-HTML
            # redirect that scripts would happily follow (ISS-045)
            if p.startswith("/api/") or p.endswith((".json", ".txt")) \
                    or p == "/config/backup":
                return self._send(401, "authentication required\n",
                                  "text/plain")
            return self._redir("/login?next=" + quote(p))
        role = sess.get("role", "admin")
        # pages are view-only for every role; sensitive downloads are
        # capability-gated (backup/restore hold every credential).
        if not role_get_ok(role, p) or \
                (p == "/config/backup"
                 and not role_post_ok(role, "/config/backup")):
            return self._page(403, pg_403(), {}, p)
        if p == "/stats.json":
            q = parse_qs(urlparse(self.path).query)
            try:
                win = int(q.get("win", ["24"])[0])
            except ValueError:
                win = 24
            return self._send(200, json.dumps(stats_series(win)),
                              "application/json")
        if p == "/datalog.json":
            data = load_data()
            q = parse_qs(urlparse(self.path).query)
            if "off" in q or "lim" in q:
                try:
                    off = int(q.get("off", ["0"])[0])
                    lim = min(500, max(1, int(q.get("lim", ["150"])[0])))
                except ValueError:
                    off, lim = 0, 150
                return self._send(200, json.dumps(
                    datalog_page(data, off, lim)), "application/json")
            try:
                hours = float(q.get("hours", ["0"])[0])
            except ValueError:
                hours = 0
            if hours > 0:
                cut = time.time() - hours * 3600
                data = [r for r in data if r.get("ts", 0) > cut]
            return self._send(200, json.dumps(data), "application/json")
        if p == "/config/wifi/scan":
            if not role_post_ok(role, "/config/wifi"):
                return self._send(403, "forbidden\n", "text/plain")
            q = parse_qs(urlparse(self.path).query)
            return self._send(200, json.dumps(
                wifi_scan(q.get("if", [""])[0])), "application/json")
        if p == "/config/support.tgz":
            if not role_post_ok(role, "/config/support"):
                return self._page(403, pg_403(), {}, p)
            return self._send(200, support_bundle(), "application/gzip",
                              {"Content-Disposition":
                               "attachment; filename=apc-nmc-support.tgz",
                               "Cache-Control": "no-store"})
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
                               "attachment; filename=apc-nmc-config.tgz",
                               "Cache-Control": "no-store"})
        if p == "/about/update/progress":
            # streamed log for the apply page — pos is a byte offset so
            # each poll only carries new output. pos=-1 = first poll:
            # start at this run's marker rather than dumping old runs.
            q = parse_qs(urlparse(self.path).query)
            try:
                pos = int(q.get("pos", ["-1"])[0])
            except ValueError:
                pos = -1
            out = update_status_view(_load_json(UPDATE_STATUS))
            try:
                with open(UPDATE_LOG, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    if pos < 0 and out.get("state") == "queued":
                        # a queued stamp knows where the new run's log
                        # begins — start there, not at the previous
                        # run's marker
                        try:
                            pos = min(size, max(0, int(
                                out.get("logpos") or 0)))
                        except (TypeError, ValueError):
                            pos = size
                    elif pos < 0:
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
        if p == "/api/v1/events":
            return self._send(200, json.dumps(load_events()),
                              "application/json")
        d["__csrf"] = sess["csrf"]
        d["__role"] = role
        # plain text — the page template esc()es this, so an HTML
        # entity here would render literally (bug: "&middot;" shown)
        d["__uinfo"] = "%s · %s" % (
            sess.get("user") or WEB_USER, role)
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
                    msg = ("msg-ok", q.get("why", [""])[0]
                           or "Command issued successfully.")
                elif q.get("r", [""])[0] == "err":
                    msg = fmt_cmd_result(q.get("why", [""])[0])
                elif q.get("r", [""])[0] == "cfgerr":
                    msg = ("msg-err", q.get("why", ["Save failed"])[0])
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
            elif p == "/config/system":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", q.get("why", [""])[0]
                           or "Configuration saved.")
                elif q.get("r", [""])[0] == "err":
                    msg = ("msg-err", q.get("why", ["Save failed"])[0])
                b = pg_system(d, msg)
                section, leaf = "Configuration", "System"
            elif p == "/config/users":
                q = parse_qs(urlparse(self.path).query)
                msg = ""
                if q.get("r", [""])[0] == "ok":
                    msg = ("msg-ok", q.get("why", [""])[0]
                           or "Saved.")
                elif q.get("r", [""])[0] == "err":
                    msg = ("msg-err", q.get("why", ["Save failed"])[0])
                b = pg_users(d, msg, self._token() or "")
                section, leaf = "Users", "User Access"
            elif p == "/log/event":
                b = pg_events(d)
                section, leaf = "Logs", "Event Log"
            elif p == "/log/data":
                b = pg_data()
                section, leaf = "Logs", "Data Log"
            elif p == "/log/stats":
                b = pg_stats(d)
                section, leaf = "Logs", "Statistics"
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
            elif p == "/help" or p.startswith("/help/"):
                slug = p.split("/", 2)[2] if p.count("/") >= 2 else ""
                b = pg_help(d, slug)
                section, leaf = "Help", "Documentation"
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
            role = user_auth(u, f.get("p", [""])[0])
            if role:
                tok = new_session(self._peer(), u, role)
                log_event("NMC: User %s logged on from %s."
                          % (u, self._peer()))
                return self._redir(nxt, {
                    "Set-Cookie": "nmc_session=%s; Path=/; HttpOnly; Secure; "
                                  "SameSite=Lax" % tok})
            self._record_fail()
            log_event("NMC: Failed log on attempt from %s (user '%s')."
                    % (self._peer(), user or "?"))
            return self._send(200, login_page(
                "Invalid User Name or Password.", nxt))
        sess = self._sess()
        if not sess:
            return self._redir("/login")
        role = sess.get("role", "admin")
        if p == "/config/restore":
            if not role_post_ok(role, "/config/restore"):
                return self._send(403,
                                  "system capability required")
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
                res = do_restore(self.rfile.read(n),
                                 sess.get("user"))
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
        if not role_post_ok(role, p):
            log_event("NMC: %s role denied POST %s from %s."
                      % (role, p, self._peer()))
            return self._page(403, pg_403(), {}, p)
        d = self._d()
        d["__csrf"] = sess["csrf"]
        d["__role"] = role
        d["__uinfo"] = "%s · %s" % (
            sess.get("user") or WEB_USER, role)
        if p == "/control/ups":
            act = f.get("act", [""])[0]
            if act not in ("reboot", "off", "sleep", "on", "bypass",
                           "mute", "beep_on", "beep_off"):
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
        if p == "/test/schedule":
            try:
                res = do_schedule(f)
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Save failed")
            why = quote(res[1])
            return self._redir("/test/diag?r=%s&why=%s"
                               % ("ok" if res[0] == "msg-ok"
                                  else "cfgerr", why))
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
            res = do_password_change(f, sess.get("user"))
            if res[0] == "msg-ok":
                return self._redir("/config/users?r=ok")
            return self._redir("/config/users?r=err&why="
                               + quote(res[1]))
        # Boot-config radio toggles + reboot are gated on the "system"
        # capability even though they share /config/wifi — they edit
        # /boot and can take the whole card offline.
        if p == "/config/wifi" and \
                not role_post_ok(role, "/config/wifi/hw") and \
                f.get("act", [""])[0] in ("hw_enable", "hw_disable",
                                          "reboot"):
            return self._page(403, pg_403(), {}, p)
        for pp, fn, dest in (("/config/users", do_users, "/config/users"),
                             ("/config/roles", do_roles, "/config/users"),
                             ("/config/radius", do_radius, "/config/users"),
                             ("/config/services", do_services,
                              "/config/system"),
                             ("/config/firewall", do_firewall,
                              "/config/system"),
                             ("/config/dot1x", do_dot1x, "/config/system"),
                             ("/config/datalog", do_datalog,
                              "/config/system"),
                             ("/config/time", do_ntp, "/config/system"),
                             ("/config/wifi", do_wifi, "/config/system")):
            if p == pp:
                try:
                    res = fn(f)
                except Exception:
                    traceback.print_exc()
                    res = ("msg-err", "Save failed")
                if res[0] == "msg-ok":
                    return self._redir(dest + "?r=ok&why="
                                       + quote(res[1]))
                return self._redir(dest + "?r=err&why="
                                   + quote(res[1]))
        if p == "/config/sessions":
            cur = self._token() or ""
            if f.get("act", [""])[0] == "others":
                with _sessions_lock:
                    for t in [t for t in sessions if t != cur]:
                        sessions.pop(t, None)
                    _persist_sessions()
                log_event("NMC: All other sessions logged off by %s."
                          % (sess.get("user") or WEB_USER))
                return self._redir("/config/users?r=ok")
            drop = f.get("drop", [""])[0]
            if drop != cur and re.fullmatch(r"[0-9a-f]{32}", drop):
                drop_session(drop)
                log_event("NMC: A session was logged off by %s."
                          % (sess.get("user") or WEB_USER))
            return self._redir("/config/users?r=ok")
        if p == "/log/clear":
            _atomic_json(EVENTS_FILE, lambda old: [])
            log_event("NMC: Event log cleared by %s."
                      % (sess.get("user") or WEB_USER))
            return self._redir("/log/event")
        if p == "/about/update/check":
            try:
                res = do_update_check()
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Update check failed")
            return self._redir("/about/system?r=%s&why=%s" % (
                "ok" if res[0] == "msg-ok" else "err", quote(res[1])))
        if p == "/about/update/settings":
            try:
                res = do_update_settings(f, sess.get("user"))
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Settings could not be saved")
            return self._redir("/about/system?r=%s&why=%s" % (
                "ok" if res[0] == "msg-ok" else "err", quote(res[1])))
        if p == "/about/update/token":
            try:
                res = do_update_token(f, sess.get("user"))
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Token operation failed")
            return self._redir("/about/system?r=%s&why=%s" % (
                "ok" if res[0] == "msg-ok" else "err", quote(res[1])))
        if p == "/about/sysact":
            try:
                res = do_sysact(f.get("act", [""])[0],
                                sess.get("user"))
            except Exception:
                traceback.print_exc()
                res = ("msg-err", "Action failed")
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
            log_event("NMC: Software update started by %s."
                      % (sess.get("user") or WEB_USER))
            return self._send(200, page("APC UPS", pg_update_apply(), d,
                                        "/about/system", "About", "System"),
                              extra={"Cache-Control": "no-store"})
        if p == "/about/update/rollback":
            if update_busy():
                return self._redir("/about/system?r=err&why=" +
                                   quote("An update is already "
                                         "in progress."))
            queue_update_status("starting rollback")
            try:
                subprocess.run(
                    ["systemctl", "start", "--no-block",
                     "apc-nmc-update-rollback.service"],
                    check=False, capture_output=True, timeout=15)
            except Exception:
                return self._redir("/about/system?r=err&why=" +
                                   quote("Rollback could not be started"))
            log_event("NMC: Software rollback started by %s."
                      % (sess.get("user") or WEB_USER))
            return self._send(200, page("APC UPS", pg_update_apply(), d,
                                        "/about/system", "About", "System"),
                              extra={"Cache-Control": "no-store"})
        self._page(404, pg_404(), d)

    do_PUT = do_DELETE = lambda s: s._send(405, "method not allowed",
                                           "text/plain")

    def log_message(self, format="", *a):
        pass


class RedirectH(BaseHTTPRequestHandler):
    server_version = "APC-NMC-Pi/" + installed_version()
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
    threading.Thread(target=mqtt_publisher, daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", 80), RedirectH)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain("/etc/apc-nmc/nmc.pem")
    httpsd = ThreadingHTTPServer(("0.0.0.0", 443), H)
    httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
    httpsd.serve_forever()


# ============================================== code file split
# Page renderers and POST handlers live in nmc_pg_*.py / nmc_act_*.py
# under the asset directory (deployed with the rest of the web assets).
# exec()ing them into this module's namespace keeps the former single-
# file semantics: shared globals, and tests monkeypatch one module.
_PARTS = ("nmc_pg_status.py", "nmc_pg_config.py", "nmc_pg_misc.py",
          "nmc_act_ups.py", "nmc_act_update.py", "nmc_act_conf.py")


def _load_parts():
    for name in _PARTS:
        path = os.path.join(ASSET_DIR, name)
        with open(path, encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), globals())


_load_parts()

if __name__ == "__main__":
    main()
