#!/usr/bin/env python3
"""
apc-nmc-agent.py - net-snmp pass_persist backend emulating an APC NMC
(PowerNet-MIB 'ups' subtree, .1.3.6.1.4.1.318.1.1.1) on top of NUT/upsd.

Reads NUT data from upsd (TCP, localhost) and serves it as PowerNet-MIB
objects.  SET requests are translated into NUT instant commands / SET VAR.

pass_persist protocol (stdin/stdout, line based):
    PING                        -> "PONG"
    get / getnext               -> next line = OID -> "<oid>\n<type>\n<value>" or "NONE"
    set                         -> next lines = OID, type, value -> "DONE" or error token
"""

# Daemon style, intentional (mirrors apc-nmc-web.py): broad excepts keep the
# pass_persist responder alive, try/except/pass tolerates missing files, and
# %-format is used throughout.
# ruff: noqa: BLE001, S110, UP031
import configparser
import fcntl
import json
import os
import re
import socket
import sys
import time
import traceback

BASE = (1, 3, 6, 1, 4, 1, 318)
UPSPFX = BASE + (1, 1, 1)
CONF = os.environ.get("NMC_CONF", "/etc/apc-nmc/nmc.conf")
STATE_FILE = "/var/lib/apc-nmc/state.json"
CACHE_TTL = 2.0


# NOTE: snmpd merges this process's stderr into the pass_persist pipe.
# Never write to stderr/stdout outside the protocol - log to a file instead.
try:
    _logf = open("/var/lib/apc-nmc/agent.log", "a", buffering=1)  # noqa: SIM115
except Exception:
    _logf = open("/dev/null", "a", buffering=1)  # noqa: SIM115


def log(*a):
    print("apc-nmc-agent:", *a, file=_logf, flush=True)


# ---------------------------------------------------------------- config
_cp = configparser.ConfigParser()
_cp.read(CONF)


def cfg(sec, key, default=""):
    try:
        return _cp.get(sec, key)
    except configparser.Error:
        return default


UPSD_HOST = cfg("upsd", "host", "127.0.0.1")
UPSD_PORT = _cp.getint("upsd", "port", fallback=3493)
UPSNAME = cfg("upsd", "ups", "myups")
UPSD_USER = cfg("upsd", "user", "")
UPSD_PASS = cfg("upsd", "password", "")

DEV = {
    "ups_name": cfg("device", "ups_name", "APC UPS"),
    "va_rating": _cp.getint("device", "va_rating", fallback=1000),
    "rated_output_voltage": _cp.getint("device", "rated_output_voltage", fallback=120),
    "high_transfer_volt": _cp.getint("device", "high_transfer_volt", fallback=136),
    "low_transfer_volt": _cp.getint("device", "low_transfer_volt", fallback=103),
    "nominal_input_voltage": _cp.getint("device", "nominal_input_voltage", fallback=120),
    "nominal_frequency": _cp.getint("device", "nominal_frequency", fallback=60),
    "battery_replace_date": cfg("device", "battery_replace_date", "01/01/16"),
}

# ---------------------------------------------------------------- state
state = {}
_state_mtime = 0


def load_state(force=False):
    """(Re)load state.json when it changes on disk."""
    global state, _state_mtime
    try:
        mt = os.path.getmtime(STATE_FILE)
        if force or mt != _state_mtime:
            with open(STATE_FILE) as f:
                disk = json.load(f)
            for k, v in state.items():
                disk.setdefault(k, v)
            state = disk
            _state_mtime = mt
    except Exception:
        if not state:
            state = {}


load_state(force=True)


def _locked(path):
    """Open the shared lock file; returns an fd or None (no write perm)."""
    try:
        lf = open(path + ".lock", "w")  # noqa: SIM115 - caller owns fd
    except OSError:
        return None
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
    except OSError:
        lf.close()
        return None
    return lf


def save_state():
    """Merge in-memory `state` into the on-disk file under the shared
    lock (same model as the web's _atomic_json).  If the lock file is
    not writable by us, fall back to a lock-free atomic write."""
    try:
        lf = _locked(STATE_FILE)
        try:
            try:
                with open(STATE_FILE) as f:
                    old = json.load(f)
            except Exception:
                old = {}
            old.update(state)
            oc = old.get("cfg", {})
            oc.update(state.get("cfg", {}))
            old["cfg"] = oc
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(old, f)
            os.chmod(tmp, 0o640)   # state.cfg can hold secrets
            os.replace(tmp, STATE_FILE)
        finally:
            if lf:
                lf.close()
    except Exception as e:
        log("state save failed:", e)


EVENTS_FILE = "/var/lib/apc-nmc/events.json"


def event_sev(text):
    # mirrors apc-nmc-web.py event_sev - keep in sync
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


def log_event(text, sev=None):
    """Append an event under the same lock+atomic-write the web uses;
    falls back to a lock-free write if the lock file is not ours."""
    try:
        sev = sev or event_sev(text)
        lf = _locked(EVENTS_FILE)
        try:
            try:
                with open(EVENTS_FILE) as f:
                    ev = json.load(f)
            except Exception:
                ev = []
            ev.append({"ts": int(time.time()), "text": text, "sev": sev})
            tmp = EVENTS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(ev[-400:], f)
            os.replace(tmp, EVENTS_FILE)
        finally:
            if lf:
                lf.close()
    except Exception:
        pass


# ---------------------------------------------------------------- NUT client
class NutError(Exception):
    pass


class Nut:
    """Minimal NUT (upsd) TCP client."""

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

    def _write(self, line):
        self._connect()
        assert self.f is not None
        self.f.write(line + "\n")
        self.f.flush()

    def _read(self):
        assert self.f is not None
        line = self.f.readline()
        if line == "":
            raise NutError("connection closed")
        return line.rstrip("\n")

    def _req(self, line):
        """Send a request, read until first response line.  Reconnects once on failure."""
        for attempt in (0, 1):
            try:
                self._write(line)
                return self._read()
            except Exception:
                self._close()
                if attempt:
                    raise NutError("upsd unreachable")
        raise NutError("upsd unreachable")

    def _auth(self):
        if self.authed:
            return
        r = self._req("USERNAME " + UPSD_USER)
        if not r.startswith("OK"):
            raise NutError("USERNAME: " + r)
        r = self._req("PASSWORD " + UPSD_PASS)
        if not r.startswith("OK"):
            raise NutError("PASSWORD: " + r)
        self.authed = True

    def list_vars(self, ups):
        # On ANY mid-read failure the socket is closed: leftover response
        # lines would otherwise be served as fresh answers to later
        # commands (stale-data desync).
        try:
            r = self._req("LIST VAR " + ups)
            if r.startswith("ERR") or not r.startswith("BEGIN LIST VAR"):
                raise NutError(r)
            out = {}
            while True:
                line = self._read()
                if line.startswith("END LIST VAR"):
                    break
                # VAR myups battery.charge "100"
                parts = line.split(None, 3)
                if len(parts) == 4 and parts[0] == "VAR":
                    val = parts[3]
                    if val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                    out[parts[2]] = val
            return out
        except Exception:
            self._close()
            raise

    def instcmd(self, ups, cmd):
        self._auth()
        r = self._req("INSTCMD %s %s" % (ups, cmd))
        # NUT 2.8: "OK" / "OK TRACKING <id>" / "ERR <reason>"
        return r

    def set_var(self, ups, var, value):
        if re.search(r'["\r\n]', value):
            return "ERR INVALID-VALUE"
        self._auth()
        return self._req('SET VAR %s %s "%s"' % (ups, var, value))


nut = Nut()
_last_refresh = 0.0
_vars = {}
_nut_ok = False


def get_vars():
    """NUT vars with short TTL cache; also tracks on-battery time."""
    global _last_refresh, _vars, _nut_ok
    if time.time() - _last_refresh < CACHE_TTL:
        return _vars
    try:
        _vars = nut.list_vars(UPSNAME)
        _nut_ok = True
    except Exception as e:
        _vars = {}
        _nut_ok = False
        log("upsd error:", e)
    _last_refresh = time.time()
    load_state()

    ts = set(_vars.get("ups.status", "").split())
    if "OB" in ts or "DISCHRG" in ts:
        if not state.get("ob_since"):
            state["ob_since"] = int(time.time())
            state["ob_count"] = int(state.get("ob_count", 0)) + 1
            cause = {"highvoltagelinetransfer": 2, "brownout": 3,
                     "blackout": 4, "smallmomentarysag": 5,
                     "deepmomentarysag": 6, "smallmomentaryspike": 7,
                     "largemomentaryspike": 8, "selftest": 9}.get(
                _vars.get("input.transfer.reason", "").lower(), 0)
            if cause:
                state["line_fail_cause"] = cause
            save_state()
    elif _nut_ok and state.get("ob_since"):
        dur = int(time.time()) - state["ob_since"]
        state["total_ob_seconds"] = (
            state.get("total_ob_seconds", 0) + dur)
        state["ob_max"] = max(int(state.get("ob_max", 0) or 0), dur)
        state.pop("ob_since")
        save_state()
    return _vars


# ---------------------------------------------------------------- helpers
def iv(d, key, default=None):
    """numeric NUT var as int"""
    try:
        return int(float(d[key]))
    except Exception:
        return default


def fv(d, key, default=None):
    try:
        return float(d[key])
    except Exception:
        return default


def tt(seconds):
    return int(float(seconds) * 100)


def soc_temp():
    """Pi SoC temperature in 0.1 C units (-1 if unreadable)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(int(f.read().strip()) / 100)
    except Exception:
        return -1


def mfr_date(d):
    # "2016/01/10" or "2016-01-10" -> "01/10/16"
    try:
        y, m, dd = d["ups.mfr.date"].strip().replace("-", "/").split("/")
        return "%s/%s/%s" % (m, dd, y[2:])
    except Exception:
        return DEV["battery_replace_date"]


def tokens(d):
    ts = set(d.get("ups.status", "").split())
    # mirrors apc-nmc-web.py toks() - keep in sync: apc_modbus's CHRG bit
    # flaps; normalize charging as OL && battery.charge < 100.
    c = iv(d, "battery.charge")
    if "OL" not in ts or (c is not None and c >= 100):
        ts.discard("CHRG")
    elif c is not None:
        ts.add("CHRG")
    return ts


def output_status(d):
    """upsBasicOutputStatus / upsBasicSystemStatus enum"""
    ts = tokens(d)
    if "OFF" in ts:
        return 7
    if "OB" in ts:
        return 3
    if "TEST" in ts:
        return 28
    if "CAL" in ts:
        return 15
    if "BYPASS" in ts:
        return 6
    if "BOOST" in ts:
        return 4
    if "TRIM" in ts:
        return 12
    if "ECO" in ts:
        return 13
    if "OL" in ts:
        return 2
    if "SD" in ts or "FSD" in ts:
        return 11
    return 1


def battery_status(d):
    ts = tokens(d)
    if not d:
        return 1
    if "LB" in ts or "DISCHRG" in ts:
        return 3
    if "RB" in ts:
        return 4
    return 2


def alarm_status(d):
    """upsBasicStateAlarmStatus: 1 none, 2 info, 3 warning, 4 critical"""
    ts = tokens(d)
    if "ALARM" in ts or "LB" in ts or "RB" in ts or "OVER" in ts:
        return 4
    if "OB" in ts or "DISCHRG" in ts or "BYPASS" in ts or "SD" in ts or "FSD" in ts:
        return 3
    if "CHRG" in ts or "CAL" in ts or "TEST" in ts or "BOOST" in ts or "TRIM" in ts:
        return 2
    return 1


def state_flags(d):
    """upsBasicStateOutputState - 64 char flag string"""
    ts = tokens(d)
    f = [0] * 64
    abnormal = bool(ts & {"RB", "OVER", "ALARM", "DISCHRG"})
    f[0] = 1 if abnormal else 0
    f[1] = 1 if "OB" in ts else 0
    f[2] = 1 if "LB" in ts else 0
    f[3] = 1 if "OL" in ts else 0
    f[4] = 1 if "RB" in ts else 0
    f[5] = 1 if _nut_ok else 0
    f[6] = 1 if "BOOST" in ts else 0
    f[7] = 1 if "TRIM" in ts else 0
    f[8] = 1 if "OVER" in ts else 0
    f[9] = 1 if "CAL" in ts else 0
    f[10] = 1 if "DISCHRG" in ts else 0
    f[12] = 1 if "BYPASS" in ts else 0          # software bypass
    f[17] = 1 if "FSD" in ts and "SD" in ts else 0
    f[18] = 0 if "OFF" in ts else 1 if d else 0  # On (output powered)
    f[19] = 1 if iv(d, "ups.timer.reboot", -1) >= 0 else 0
    f[21] = 1 if ("FSD" in ts or "SD" in ts or iv(d, "ups.timer.shutdown", -1) >= 0) else 0
    f[28] = 1 if "TEST" in ts else 0
    f[29] = 1 if "OB" in ts and "LB" in ts else 0
    f[39] = 1 if "LB" in ts and "FSD" in ts else 0
    f[50] = 1 if "ECO" in ts else 0
    f[57] = 1 if "OFF" in ts else 0
    return "".join(str(b) for b in f)


def abnormal_flags(d):
    """upsAdvStateAbnormalConditions - 32 char flag string"""
    ts = tokens(d)
    f = [0] * 32
    f[3] = 1 if "RB" in ts else 0   # battery failure
    f[4] = 1 if "OVER" in ts else 0  # load alarm violation
    return "".join(str(b) for b in f)


def smartups_faults(d):
    """upsAdvStateSmartUPSSpecificFaults - 64 flags, left-to-right.
    Only flag 5 (Weak Battery) is determinable from NUT (RB token);
    all other bits are not reported by apc_modbus and stay 0.
    Per the MIB, undeterminable overall state is 'UNKNOWN'."""
    if not d:
        return "UNKNOWN"
    ts = tokens(d)
    f = [0] * 64
    f[4] = 1 if "RB" in ts else 0   # flag 5: weak battery
    return "".join(str(b) for b in f)


def beeper_alarm_enum(d):
    b = d.get("ups.beeper.status", "").strip().lower()
    return {"enabled": 1, "muted": 4, "disabled": 3}.get(b, 1)


def run_instcmd(cmd):
    try:
        r = nut.instcmd(UPSNAME, cmd)
    except NutError as e:
        log("instcmd error:", e)
        return "commit-failed"
    if r.startswith(("OK", "PENDING")):
        if cmd in ("beeper.mute", "beeper.enable", "beeper.disable"):
            state["beeper_muted"] = cmd == "beeper.mute"
            save_state()
        log("instcmd %s -> %s" % (cmd, r))
        log_event("UPS: command '%s' issued via SNMP." % cmd, "warn")
        return "DONE"
    log("instcmd %s -> %s" % (cmd, r))
    if "CMD-NOT-SUPPORTED" in r or "INVALID-ARGUMENT" in r:
        return "wrong-value"
    if "ACCESS-DENIED" in r:
        return "no-access"
    return "commit-failed"


def run_setvar(var, value):
    try:
        r = nut.set_var(UPSNAME, var, value)
    except NutError as e:
        log("setvar error:", e)
        return "commit-failed"
    if r.startswith("OK"):
        return "DONE"
    log("setvar %s=%s -> %s" % (var, value, r))
    if "VAR-NOT-SUPPORTED" in r or "NOT-WRITABLE" in r:
        return "not-writable"
    return "commit-failed"


def cfg_get(key, default):
    return state.get("cfg", {}).get(key, default)


def cfg_set(key, val):
    state.setdefault("cfg", {})[key] = val
    save_state()
    return "DONE"


def int_setter(key, lo=None, hi=None):
    def st(v):
        try:
            n = int(v)
        except ValueError:
            return "wrong-type"
        if lo is not None and n < lo or hi is not None and n > hi:
            return "wrong-value"
        return cfg_set(key, n)
    return st


def str_setter(key, maxlen=32):
    def st(v):
        return cfg_set(key, v[:maxlen])
    return st


def instcmd_setter(mapping):
    """mapping: snmp int value -> nut instant command name (or None=no-op)"""
    def st(v):
        try:
            n = int(v)
        except ValueError:
            return "wrong-type"
        if n not in mapping:
            return "wrong-value"
        cmd = mapping[n]
        if cmd is None:
            return "DONE"
        return run_instcmd(cmd)
    return st


def make_status_rw(get_key, nut_var=None, scale=1):
    """setter writing into state.cfg and optionally a NUT variable"""
    def st(v):
        try:
            n = int(float(v) / scale) if scale != 1 else int(v)
        except ValueError:
            return "wrong-type"
        cfg_set(get_key, n * scale if scale != 1 else n)
        if nut_var:
            return run_setvar(nut_var, str(n))
        return "DONE"
    return st


# ---------------------------------------------------------------- OID table
# entries: oid-tuple -> (snmp_type, getter(d)->value, setter(v)->token or None)
I = "integer"
G = "gauge"
S = "string"
T = "timeticks"
P = "ipaddress"


def build_table(d):
    t = {}

    def leaf(rel, typ, get, set_=None):
        t[rel] = (typ, get, set_)

    # ---------- upsIdent (.1)
    U = (1,)
    leaf(U + (1, 1, 0), S, lambda d: d.get("ups.model", "Unknown"))
    leaf(U + (1, 2, 0), S, lambda d: cfg_get("ups_name", DEV["ups_name"]),
         str_setter("ups_name"))
    leaf(U + (1, 3, 0), S, lambda d: "APC")
    leaf(U + (1, 4, 0), S, lambda d: d.get("ups.model", "Unknown"))
    leaf(U + (2, 1, 0), S, lambda d: d.get("ups.firmware", "N/A"))
    leaf(U + (2, 2, 0), S, mfr_date)
    leaf(U + (2, 3, 0), S, lambda d: d.get("ups.serial", "UNKNOWN"))
    leaf(U + (2, 4, 0), S, lambda d: d.get("driver.version.data", ""))
    leaf(U + (2, 5, 0), S, lambda d: d.get("ups.model", ""))
    leaf(U + (2, 6, 0), S, lambda d: "05")
    leaf(U + (2, 7, 0), I, lambda d: 3)  # onePhaseInOut
    leaf(U + (2, 8, 0), I, lambda d: 2)  # vrlaOrWetCells
    leaf(U + (2, 9, 0), G, lambda d: DEV["va_rating"])

    # ---------- upsBattery (.2)
    B = (2,)
    leaf(B + (1, 1, 0), I, battery_status)
    leaf(B + (1, 2, 0), T,
         lambda d: tt(time.time() - state["ob_since"]) if state.get("ob_since") else 0)
    leaf(B + (1, 3, 0), S,
         lambda d: cfg_get("battery_replace_date", DEV["battery_replace_date"]),
         str_setter("battery_replace_date", 10))
    leaf(B + (2, 1, 0), G, lambda d: iv(d, "battery.charge", -1))
    if "battery.temperature" in d:
        leaf(B + (2, 2, 0), G, lambda d: int(fv(d, "battery.temperature", 0)))
        leaf(B + (3, 2, 0), G, lambda d: int(fv(d, "battery.temperature", 0) * 10))
    leaf(B + (2, 3, 0), T, lambda d: tt(iv(d, "battery.runtime", 0) or 0))
    leaf(B + (2, 4, 0), I, lambda d: 2 if "RB" in tokens(d) else 1)
    leaf(B + (2, 5, 0), I, lambda d: 1)
    leaf(B + (2, 6, 0), I, lambda d: 1 if "RB" in tokens(d) else 0)
    leaf(B + (2, 7, 0), I, lambda d: iv(d, "battery.voltage.nominal", -1))
    leaf(B + (2, 8, 0), I, lambda d: round(fv(d, "battery.voltage", -1)))
    leaf(B + (2, 9, 0), I, lambda d: round(fv(d, "battery.current", -1)))
    leaf(B + (2, 11, 0), G, lambda d: 100)
    leaf(B + (3, 1, 0), G, lambda d: int((fv(d, "battery.charge", -1) or -1) * 10))
    leaf(B + (3, 3, 0), I, lambda d: int((fv(d, "battery.voltage.nominal", -1) or -1) * 10))
    leaf(B + (3, 4, 0), I, lambda d: int((fv(d, "battery.voltage", -1) or -1) * 10))
    if "battery.current" in d:
        leaf(B + (3, 5, 0), I, lambda d: int(fv(d, "battery.current", 0) * 10))

    # ---------- upsInput (.3)
    I3 = (3,)
    leaf(I3 + (1, 1, 0), I, lambda d: 1)
    if "input.voltage" in d:
        leaf(I3 + (2, 1, 0), G, lambda d: int(fv(d, "input.voltage", 0)))
        leaf(I3 + (3, 1, 0), G, lambda d: int(fv(d, "input.voltage", 0) * 10))
    if "input.voltage.maximum" in d:
        leaf(I3 + (2, 2, 0), G, lambda d: int(fv(d, "input.voltage.maximum", 0)))
        leaf(I3 + (3, 2, 0), G, lambda d: int(fv(d, "input.voltage.maximum", 0) * 10))
    if "input.voltage.minimum" in d:
        leaf(I3 + (2, 3, 0), G, lambda d: int(fv(d, "input.voltage.minimum", 0)))
        leaf(I3 + (3, 3, 0), G, lambda d: int(fv(d, "input.voltage.minimum", 0) * 10))
    if "input.frequency" in d:
        leaf(I3 + (2, 4, 0), G, lambda d: int(fv(d, "input.frequency", 0)))
        leaf(I3 + (3, 4, 0), G, lambda d: int(fv(d, "input.frequency", 0) * 10))
    leaf(I3 + (2, 5, 0), I, lambda d: state.get("line_fail_cause", 1))
    leaf(I3 + (2, 6, 0), G, lambda d: DEV["nominal_frequency"])
    leaf(I3 + (2, 7, 0), G, lambda d: DEV["nominal_input_voltage"])

    # ---------- upsOutput (.4)
    O = (4,)
    leaf(O + (1, 1, 0), I, output_status)
    leaf(O + (1, 2, 0), I, lambda d: 1)
    leaf(O + (1, 3, 0), I, output_status)
    if "ups.temperature" in d:
        leaf(O + (1, 4, 0), G, lambda d: int(fv(d, "ups.temperature", 0)))
    if "output.voltage" in d:
        leaf(O + (2, 1, 0), G, lambda d: int(fv(d, "output.voltage", 0)))
        leaf(O + (3, 1, 0), G, lambda d: int(fv(d, "output.voltage", 0) * 10))
    if "output.frequency" in d:
        leaf(O + (2, 2, 0), G, lambda d: int(fv(d, "output.frequency", 0)))
        leaf(O + (3, 2, 0), G, lambda d: int(fv(d, "output.frequency", 0) * 10))
    if "ups.load" in d:
        leaf(O + (2, 3, 0), G, lambda d: int(fv(d, "ups.load", 0)))
        leaf(O + (3, 3, 0), G, lambda d: int(fv(d, "ups.load", 0) * 10))
    if "output.current" in d:
        leaf(O + (2, 4, 0), G, lambda d: int(fv(d, "output.current", 0)))
        leaf(O + (3, 4, 0), G, lambda d: int(fv(d, "output.current", 0) * 10))
    leaf(O + (2, 5, 0), I, lambda d: 0)
    leaf(O + (2, 6, 0), G, lambda d: DEV["va_rating"])
    leaf(O + (2, 7, 0), G, lambda d: DEV["nominal_frequency"])
    if "ups.realpower" in d:
        leaf(O + (2, 8, 0), G, lambda d: int(fv(d, "ups.realpower", 0)))
    if "ups.power" in d:
        leaf(O + (2, 9, 0), G, lambda d: int(fv(d, "ups.power", 0)))

    # ---------- upsConfig (.5)
    C = (5,)
    leaf(C + (2, 1, 0), I,
         lambda d: cfg_get("rated_output_voltage", DEV["rated_output_voltage"]),
         int_setter("rated_output_voltage", 100, 500))
    leaf(C + (2, 2, 0), I,
         lambda d: cfg_get("high_transfer_volt", DEV["high_transfer_volt"]),
         int_setter("high_transfer_volt", 100, 500))
    leaf(C + (2, 3, 0), I,
         lambda d: cfg_get("low_transfer_volt", DEV["low_transfer_volt"]),
         int_setter("low_transfer_volt", 50, 300))
    leaf(C + (2, 4, 0), I, beeper_alarm_enum, _alarm_setter)
    leaf(C + (2, 5, 0), T, lambda d: cfg_get("alarm_timer", 0), int_setter("alarm_timer", 0))
    leaf(C + (2, 6, 0), I,
         lambda d: cfg_get("min_return_capacity", iv(d, "battery.charge.low", 15) or 15),
         make_status_rw("min_return_capacity", "battery.charge.low"))
    leaf(C + (2, 7, 0), I, lambda d: cfg_get("sensitivity", 1), int_setter("sensitivity", 1, 4))
    leaf(C + (2, 8, 0), T,
         lambda d: cfg_get("low_battery_runtime", tt(iv(d, "battery.runtime.low", 120) or 120)),
         make_status_rw("low_battery_runtime", "battery.runtime.low", scale=100))
    leaf(C + (2, 9, 0), T, lambda d: cfg_get("return_delay", 0), int_setter("return_delay", 0))
    leaf(C + (2, 10, 0), T,
         lambda d: cfg_get("shutoff_delay", tt(iv(d, "ups.delay.shutdown", 20) or 20)),
         make_status_rw("shutoff_delay", "ups.delay.shutdown", scale=100))
    leaf(C + (2, 11, 0), T, lambda d: cfg_get("sleep_time", 0), int_setter("sleep_time", 0))
    leaf(C + (2, 14, 0), I, lambda d: cfg_get("batt_exhaust_thresh", 20),
         int_setter("batt_exhaust_thresh", 0, 100))
    leaf(C + (2, 25, 0), I, lambda d: cfg_get("num_external_batteries", 0),
         int_setter("num_external_batteries", 0, 10))
    leaf(C + (2, 55, 0), I, lambda d: cfg_get("auto_restart", 1), int_setter("auto_restart", 1, 2))

    # ---------- upsControl (.6)
    K = (6,)
    leaf(K + (1, 1, 0), I, lambda d: 1, _conserve_battery)
    leaf(K + (2, 1, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "load.off", 3: "load.off.delay"}))
    leaf(K + (2, 2, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "shutdown.reboot", 3: "shutdown.reboot"}))
    leaf(K + (2, 3, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "shutdown.stayoff", 3: "shutdown.stayoff"}))
    leaf(K + (2, 4, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "test.failure.start"}))
    leaf(K + (2, 5, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "beeper.on", 3: "beeper.on", 4: "beeper.on", 5: "beeper.mute"}))
    leaf(K + (2, 6, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "load.on", 4: "load.on"}))
    leaf(K + (2, 7, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "bypass.start", 3: "bypass.stop"}))
    leaf(K + (2, 8, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "shutdown.reboot", 3: "shutdown.reboot"}))

    # ---------- upsTest (.7)
    E = (7,)
    leaf(E + (2, 1, 0), I, lambda d: cfg_get("diag_schedule", 8),
         int_setter("diag_schedule", 1, 12))
    leaf(E + (2, 2, 0), I, lambda d: 1, _diagnostics_setter)
    leaf(E + (2, 3, 0), I, lambda d: int(state.get("diag_result", 1)))
    leaf(E + (2, 4, 0), S, lambda d: state.get("diag_date", "UNKNOWN"))
    leaf(E + (2, 5, 0), I, lambda d: 1, instcmd_setter({
        1: None, 2: "calibrate.start", 3: "calibrate.stop"}))
    leaf(E + (2, 6, 0), I, lambda d: int(state.get("cal_result", 1)))
    leaf(E + (2, 7, 0), S, lambda d: state.get("cal_date", "UNKNOWN"))
    leaf(E + (2, 13, 0), S, lambda d: state.get("diag_date", "UNKNOWN"))

    # ---------- upsComm (.8)
    leaf((8, 1, 0), I, lambda d: 1 if _nut_ok else 2)

    # ---------- upsPhase (.9) - single-phase feed/phase rows ----------
    # Columns unsupported by a Smart-UPS return -1 (the MIB's documented
    # "unsupported" sentinel), matching what an AP9631 serves on an SUA.
    PH = (9,)
    leaf(PH + (2, 1, 0), I, lambda d: 1)  # upsPhaseNumInputs
    leaf(PH + (3, 1, 0), I, lambda d: 1)  # upsPhaseNumOutputs

    def neg1(f):
        def g(d):
            v = f(d)
            return v if v is not None and v >= 0 else -1
        return g

    def prow(base, ncols, isuf, vals, types=None):
        for c in range(1, ncols + 1):
            v = vals.get(c, -1)
            leaf(base + (c,) + isuf, (types or {}).get(c, I),
                 v if callable(v) else (lambda d, k=v: k))

    prow(PH + (2, 2, 1), 6, (1,), {
        1: 1, 2: 1, 3: 2, 5: 2, 6: "Input 1",
        4: lambda d: round(fv(d, "input.frequency", -1)),
    }, types={6: S})
    prow(PH + (2, 3, 1), 14, (1, 1), {
        1: 1, 2: 1,
        3: lambda d: round(fv(d, "input.voltage", -1)),
        6: neg1(lambda d: fv(d, "input.current", -1) * 10),
    })
    prow(PH + (3, 2, 1), 4, (1,), {
        1: 1, 2: 1, 3: 2,
        4: lambda d: round(fv(d, "output.frequency",
                              fv(d, "input.frequency", -1))),
    })
    prow(PH + (3, 3, 1), 23, (1, 1), {
        1: 1, 2: 1,
        3: lambda d: round(fv(d, "output.voltage", -1)),
        4: neg1(lambda d: fv(d, "output.current", -1) * 10),
        10: lambda d: round(fv(d, "ups.load", -1)),
        13: lambda d: round(fv(d, "ups.realpower", -1)),
        20: lambda d: round(fv(d, "ups.power", -1)),
        23: 0,
    }, types={23: G})

    # ---------- upsState (.11)
    leaf((11, 1, 1, 0), S, state_flags)
    leaf((11, 1, 2, 0), I, alarm_status)
    leaf((11, 2, 1, 0), S, abnormal_flags)
    leaf((11, 2, 7, 0), T,
         lambda d: int(state.get("total_ob_seconds", 0) * 100)
         + (tt(time.time() - state["ob_since"]) if state.get("ob_since") else 0))
    leaf((11, 2, 10, 0), I, lambda d: int(state.get("ob_count", 0)))
    # upsAdvStateSystemMessages - the UPS's own alarm text when present
    leaf((11, 2, 6, 0), S,
         lambda d: d.get("ups.alarm", "") or "None")
    leaf((11, 2, 5, 0), S, smartups_faults)
    leaf((11, 2, 13, 0), I,
         lambda d: 1 if "ECO" in tokens(d) else 2)

    # ---------- upsOutletGroups (.12) - PowerNet-MIB real layout -----
    groups = []
    try:
        ngrp = int(d.get("outlet.group.count", 0) or 0)
    except ValueError:
        ngrp = 0
    for gi in range(ngrp):
        groups.append({
            "idx": gi,
            "id": d.get("outlet.group.%d.id" % gi, str(gi)),
            "name": d.get("outlet.group.%d.name" % gi, "Group %d" % gi),
            "status": d.get("outlet.group.%d.status" % gi, ""),
            "main": d.get("outlet.group.%d.id" % gi, "") == "MOG"
                    or d.get("outlet.group.%d.designator" % gi,
                             "").lower() == "main",
            "pending": any(
                d.get("outlet.group.%d.timer.%s" % (gi, k),
                      "NotActive") != "NotActive"
                for k in ("shutdown", "reboot", "start")),
            "delay_shutdown": d.get("outlet.group.%d.delay.shutdown" % gi),
            "delay_reboot": d.get("outlet.group.%d.delay.reboot" % gi),
            "delay_start": d.get("outlet.group.%d.delay.start" % gi),
        })

    def gidx(i):
        return lambda d: groups[i]["idx"] + 1
    def gname(i):
        return lambda d: groups[i]["name"]
    def gstate(i):
        return lambda d: (1 if groups[i]["status"] == "on"
                          else 2 if groups[i]["status"] == "off" else 3)
    def gpend(i):
        return lambda d: 1 if groups[i]["pending"] else 2
    def gtype(i):
        return lambda d: 2 if groups[i]["main"] else 1
    def gfull(i):
        def f(d):
            flags = list("0" * 32)
            if groups[i]["status"] == "on":
                flags[0] = "1"
            elif groups[i]["status"] == "off":
                flags[1] = "1"
            if groups[i]["pending"]:
                flags[8] = flags[9] = "1"  # pending on/off delay
            return "".join(flags)
        return f

    def gvar(i, key, dv=0):
        return lambda d: iv(d, "outlet.group.%d.%s" % (i, key), dv)

    def gvar_rw(i, key, lo=None, hi=None):
        var = "outlet.group.%d.%s" % (i, key)

        def st(v):
            try:
                n = int(v)
            except ValueError:
                return "wrong-type"
            if lo is not None and n < lo or hi is not None and n > hi:
                return "wrong-value"
            return run_setvar(var, str(n))
        return st

    def gname_rw(i):
        var = "outlet.group.%d.name" % i

        def st(v):
            v = str(v)[:16]
            if not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,16}", v):
                return "wrong-value"
            return run_setvar(var, v)
        return st

    def gcmd(i):
        cmds = {1: "load.on", 2: "load.off", 3: "load.cycle",
                4: "load.on.delay", 5: "load.off.delay",
                6: "load.cycle", 9: "shutdown.reboot",
                10: "shutdown.reboot"}

        def st(v):
            try:
                n = int(v)
            except ValueError:
                return "wrong-type"
            if n not in cmds:
                return "wrong-value"   # 7 cancel / 8 unknown unsupported
            return run_instcmd("outlet.group.%d.%s" % (i, cmds[n]))
        return st

    if groups:
        leaf((12, 1, 1, 0), I, lambda d: len(groups))   # statusTableSize
        leaf((12, 2, 1, 0), I, lambda d: len(groups))   # configTableSize
        leaf((12, 3, 1, 0), I, lambda d: len(groups))   # controlTableSize
        for i in range(len(groups)):
            ix = i + 1
            # rows are <column>.<index> per SMIv2 table layout
            leaf((12, 1, 2, 1, 1, ix), I, gidx(i))
            leaf((12, 1, 2, 1, 2, ix), S, gname(i))
            leaf((12, 1, 2, 1, 3, ix), I, gstate(i))
            leaf((12, 1, 2, 1, 4, ix), I, gpend(i))
            leaf((12, 1, 2, 1, 5, ix), I, gtype(i))
            leaf((12, 1, 2, 1, 6, ix), S, gfull(i))
            leaf((12, 2, 2, 1, 1, ix), I, gidx(i))
            leaf((12, 2, 2, 1, 2, ix), S, gname(i), gname_rw(i))
            leaf((12, 2, 2, 1, 3, ix), I,
                 gvar(i, "delay.start"), gvar_rw(i, "delay.start", 0, 3600))
            leaf((12, 2, 2, 1, 4, ix), I,
                 gvar(i, "delay.shutdown"),
                 gvar_rw(i, "delay.shutdown", 0, 3600))
            leaf((12, 2, 2, 1, 5, ix), I,
                 gvar(i, "delay.reboot"),
                 gvar_rw(i, "delay.reboot", 0, 3600))
            leaf((12, 2, 2, 1, 6, ix), I,
                 lambda d, i=i: iv(d, "battery.runtime.low", 0))
            leaf((12, 2, 2, 1, 7, ix), I, gtype(i))
            leaf((12, 3, 2, 1, 1, ix), I, gidx(i))
            leaf((12, 3, 2, 1, 2, ix), S, gname(i))
            leaf((12, 3, 2, 1, 3, ix), I,
                 lambda d, i=i:
                 (1 if groups[i]["status"] == "on"
                  else 2 if groups[i]["status"] == "off" else 8),
                 gcmd(i))
            leaf((12, 3, 2, 1, 4, ix), I, gtype(i))

    return t


def _conf_sinks():
    """(snmp, trapsink / trapcommunity) -> [(host:port, community)]."""
    comm = cfg("snmp", "trapcommunity", "public")
    out = []
    for part in cfg("snmp", "trapsink", "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append((part if ":" in part else part + ":162", comm))
    return out


def _snmp_sink_slots():
    """Four writable receiver slots persisted in state.cfg.snmp_sinks."""
    slots = state.get("cfg", {}).get("snmp_sinks") or []
    return (list(slots) + [None] * 4)[:4]


def _trap_sinks():
    out = _conf_sinks()
    for s in _snmp_sink_slots():
        if s and s.get("ip") not in (None, "", "0.0.0.0"):
            out.append((s["ip"] + ":162", s.get("comm") or "public"))
    return out


def _sink_slot_update(i, fn):
    """Mutate receiver slot i (0-3) in state.cfg.snmp_sinks; persist."""
    slots = state.get("cfg", {}).get("snmp_sinks") or []
    slots = [dict(s) if isinstance(s, dict) else None
             for s in (list(slots) + [None] * 4)[:4]]
    slots[i] = fn(slots[i])
    while slots and slots[-1] is None:
        slots.pop()
    return cfg_set("snmp_sinks", slots)


def _rc_ip(i):
    def st(v):
        v = str(v).strip()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", v) or \
                any(int(o) > 255 for o in v.split(".")):
            return "wrong-value"

        def upd(s):
            if v == "0.0.0.0":
                return None
            s = dict(s or {})
            s["ip"] = v
            s.setdefault("comm", "public")
            return s
        return _sink_slot_update(i, upd)
    return st


def _rc_comm(i):
    def st(v):
        v = str(v).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,32}", v):
            return "wrong-value"

        def upd(s):
            s = dict(s or {"ip": "0.0.0.0"})
            s["comm"] = v
            return s
        return _sink_slot_update(i, upd)
    return st


def _rc_accept(i):
    def st(v):
        try:
            n = int(v)
        except ValueError:
            return "wrong-type"
        if n == 2:                          # rejectReceiver: drop the slot
            return _sink_slot_update(i, lambda s: None)
        if n == 1:                          # acceptReceiver: keep/enable slot
            return _sink_slot_update(
                i, lambda s: dict(s or {"ip": "0.0.0.0"}))
        return "wrong-value"
    return st


def mconfig_table():
    """mconfig subtree (.318.2.1) keyed rel-to-BASE.  Receiver rows 1-4 are
    the RW state.cfg slots (AP9631 parity); conf-file sinks follow RO."""
    conf_sinks = _conf_sinks()
    slots = _snmp_sink_slots()
    t = {}
    t[(2, 1, 1, 0)] = (I, lambda d: 4 + len(conf_sinks), None)
    for i, slot in enumerate(slots, 1):
        ip = (slot or {}).get("ip") or "0.0.0.0"
        comm = (slot or {}).get("comm") or ""
        # rows are <column>.<index> per SMIv2 table layout
        t[(2, 1, 2, 1, 1, i)] = (I, lambda d, i=i: i, None)
        t[(2, 1, 2, 1, 2, i)] = (P, lambda d, h=ip: h, _rc_ip(i - 1))
        t[(2, 1, 2, 1, 3, i)] = (S, lambda d, c=comm: c, _rc_comm(i - 1))
        t[(2, 1, 2, 1, 4, i)] = (I, lambda d: 5, None)   # warning
        t[(2, 1, 2, 1, 5, i)] = (I, lambda d, s=slot: 1 if s else 2,
                               _rc_accept(i - 1))
        t[(2, 1, 2, 1, 6, i)] = (I, lambda d: 1, None)   # allTraps
    for j, (dest, comm) in enumerate(conf_sinks):
        i = 5 + j
        host = dest.rsplit(":", 1)[0]
        t[(2, 1, 2, 1, 1, i)] = (I, lambda d, i=i: i, None)
        t[(2, 1, 2, 1, 2, i)] = (P, lambda d, h=host: h, None)
        t[(2, 1, 2, 1, 3, i)] = (S, lambda d, c=comm: c, None)
        t[(2, 1, 2, 1, 4, i)] = (I, lambda d: 5, None)
        t[(2, 1, 2, 1, 5, i)] = (I, lambda d: 1, None)
        t[(2, 1, 2, 1, 6, i)] = (I, lambda d: 1, None)
    t[(2, 1, 3, 0)] = (I, lambda d: 2, None)   # mconfigBOOTPEnabled=no
    t[(2, 1, 6, 1, 0)] = (S, lambda d: time.strftime("%m/%d/%Y"), None)
    t[(2, 1, 6, 2, 0)] = (S, lambda d: time.strftime("%H:%M:%S"), None)
    return t


def env_table():
    """environmentalMonitor subtree (.318.1.10) keyed rel-to-BASE, like
    mconfig_table.  One 'integrated' probe reports the Pi SoC temperature;
    humidity is unsupported (-1) since there is no sensor."""
    t = {}
    EM = (1, 10, 2)   # hardware.environmentalMonitor.integrated
    t[EM + (2, 1, 0)] = (I, lambda d: 1, None)   # iemConfigProbesNumProbes
    t[EM + (3, 1, 0)] = (I, lambda d: 1, None)   # iemStatusProbesNumProbes
    probe = {1: 1, 3: 2, 5: 1, 6: -1,
             2: "Integrated", 15: cfg_get("location", "NMC"),
             4: lambda d: soc_temp()}
    for c, v in probe.items():
        t[EM + (3, 2, 1, c, 1)] = (
            S if c in (2, 15) else I,
            v if callable(v) else (lambda d, k=v: k), None)
    return t


# ---------- special setters (defined after build_table refs use them lazily)
def _alarm_setter(v):
    try:
        n = int(v)
    except ValueError:
        return "wrong-type"
    if n == 4:
        return run_instcmd("beeper.mute")
    if n == 3:
        return run_instcmd("beeper.disable")
    if n in (1, 2):
        return run_instcmd("beeper.enable")
    return "wrong-value"


def _conserve_battery(v):
    try:
        n = int(v)
    except ValueError:
        return "wrong-type"
    if n == 1:
        return "DONE"
    if n != 2:
        return "wrong-value"
    d = get_vars()
    if "OB" not in tokens(d):
        return "wrong-value"   # per MIB: badValue when not on battery
    return run_instcmd("shutdown.stayoff")


def _diagnostics_setter(v):
    try:
        n = int(v)
    except ValueError:
        return "wrong-type"
    if n == 1:
        return "DONE"
    if n != 2:
        return "wrong-value"
    r = run_instcmd("test.battery.start")
    if r == "DONE":
        state["diag_result"] = 4
        state["diag_date"] = time.strftime("%m/%d/%Y")
        save_state()
    return r


# ---------------------------------------------------------------- protocol
def parse_oid(line):
    line = line.strip()
    if line.startswith("iso."):
        line = "1." + line.removeprefix("iso.")
    line = line.removeprefix(".")
    try:
        return tuple(int(p) for p in line.split(".") if p != "")
    except ValueError:
        return None


def fmt_val(typ, val):
    if typ in (S, P):
        # pass_persist replies are line-oriented: a control char in a
        # NUT string would corrupt the protocol stream for snmpd.
        return re.sub(r"[\x00-\x1f\x7f]", " ", str(val))
    try:
        n = int(val)
    except Exception:
        return "0"
    # Gauge32/TimeTicks are unsigned on the wire; a -1 sentinel would make
    # snmpd drop the response.
    return str(n if n >= 0 or typ == I else 0)


def respond_get(oid):
    d = get_vars()
    table = build_table(d)
    rel = oid[len(UPSPFX):]
    if oid[: len(UPSPFX)] == UPSPFX and rel in table:
        typ, get, _ = table[rel]
        return (".".join(str(x) for x in oid), typ, fmt_val(typ, get(d)))
    rel = oid[len(BASE):]
    entry = mconfig_table().get(rel) or env_table().get(rel)
    if entry:
        typ, get, _ = entry
        return (".".join(str(x) for x in oid), typ, fmt_val(typ, get(d)))
    return None


def respond_getnext(oid):
    d = get_vars()
    table = build_table(d)
    cands = [(UPSPFX + k, table[k]) for k in table]
    mconf = mconfig_table()
    cands += [(BASE + k, mconf[k]) for k in mconf]
    env = env_table()
    cands += [(BASE + k, env[k]) for k in env]
    for full, (typ, get, _) in sorted(cands):
        if full > oid:
            return (".".join(str(x) for x in full), typ,
                    fmt_val(typ, get(d)))
    return None


def respond_set(oid, typ, val):
    d = get_vars()
    if oid[: len(UPSPFX)] == UPSPFX:
        entry = build_table(d).get(oid[len(UPSPFX):])
    else:
        rel = oid[len(BASE):]
        entry = mconfig_table().get(rel) or env_table().get(rel)
    if entry is None:
        return "not-writable"
    styp, _, setter = entry
    if setter is None:
        return "not-writable"
    if typ not in ("integer",) and styp == I:
        return "wrong-type"
    if typ not in ("string", "octet") and styp == S:
        return "wrong-type"
    if typ not in ("ipaddress", "string") and styp == P:
        return "wrong-type"
    return setter(val)


def main():
    log("starting, upsd=%s:%s ups=%s" % (UPSD_HOST, UPSD_PORT, UPSNAME))
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if line == "":
            break
        line = line.strip()
        try:
            if line == "PING":
                print("PONG")
            elif line in ("get", "getnext"):
                oid = parse_oid(stdin.readline())
                r = None
                if oid is not None:
                    if line == "get":
                        if oid[: len(BASE)] == BASE:
                            r = respond_get(oid)
                    else:
                        r = respond_getnext(oid)
                if r is None:
                    print("NONE")
                else:
                    print(r[0])
                    print(r[1])
                    print(r[2])
            elif line == "set":
                # wire format: "set\n<oid>\n<type> <value>\n"  (strings quoted)
                oid = parse_oid(stdin.readline())
                tv = stdin.readline().rstrip("\n")
                typ, _, val = tv.partition(" ")
                if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                if oid is None:
                    print("wrong-encoding")
                else:
                    print(respond_set(oid, typ, val))
            elif line == "":
                continue
            else:
                print("NONE")
        except Exception as e:
            log("handler error:", e)
            if line == "set":
                print("commit-failed")
            else:
                print("NONE")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # stderr merges into the pass_persist pipe - log, don't traceback.
        log("fatal:\n" + traceback.format_exc())
        sys.exit(1)
