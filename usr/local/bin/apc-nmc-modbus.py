#!/usr/bin/env python3
"""
apc-nmc-modbus.py - Modbus TCP server exposing live NUT UPS data.

Bridges upsd to a documented register map (docs/modbus-registers.md) so
building-management systems can poll the UPS like they would a real
management card. Read-only by default; NUT instant commands through the
command register/coils need [modbus] allow_write = 1.
"""

# Daemon style: broad excepts keep the service alive through UPS or
# network hiccups.
# ruff: noqa: BLE001, S110, UP031
import configparser
import socket
import socketserver
import struct
import threading
import time

CONF = "/etc/apc-nmc/nmc.conf"

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

BIND = cfg("modbus", "bind", "0.0.0.0")
PORT = _cp.getint("modbus", "port", fallback=502)
UNIT = _cp.getint("modbus", "unit", fallback=1)
ALLOW_WRITE = cfg("modbus", "allow_write", "0") in ("1", "yes", "true", "on")


# ============================================================ NUT client
# Mirrors the Nut client in apc-nmc-web.py / apc-nmc-agent.py.
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

    def instcmd(self, ups, cmd):
        self._auth()
        return self._req("INSTCMD %s %s" % (ups, cmd))


nut = Nut()
_nut_lock = threading.Lock()
_cache = {"ts": 0.0, "vars": {}}
CACHE_TTL = 2.0


def ups_vars():
    now = time.time()
    with _nut_lock:
        if now - _cache["ts"] < CACHE_TTL and _cache["vars"]:
            return _cache["vars"]
        d = nut.list_vars(UPSNAME)
        _cache.update(ts=now, vars=d)
        return d


def ups_cmd(cmd):
    with _nut_lock:
        return nut.instcmd(UPSNAME, cmd)


# ============================================================ register map
# ups.status flags, bit position == discrete-input address.
STATUS_BITS = ("OL", "OB", "LB", "OVER", "BYPASS", "CHRG", "DISCHRG",
               "RB", "CAL", "OFF", "TRIM", "BOOST", "FSD", "ALARM",
               "SD", "HB")

STATUS_REG = 0            # bitmap register (bit n = STATUS_BITS[n])
CMD_REG = 100             # write-only command register (see CMDS)
CMD_COIL_BASE = 0         # same commands as coils 0..n-1 when writable

# NUT instant commands reachable through CMD_REG / coils (allow_write=1).
CMDS = {
    1: "test.battery.start",
    2: "calibrate.start",
    3: "calibrate.stop",
    4: "beeper.mute",
    5: "beeper.enable",
    6: "beeper.disable",
    7: "load.on",
    8: "shutdown.return",
    9: "shutdown.stayoff",
    10: "load.off",
}

# (nut var, scale) — scaled int cast to u16; missing var -> register
# absent from the map (reads of it raise illegal-data-address).
NUMERIC_REGS = (
    (1,  "battery.charge", 1),
    (2,  "battery.runtime", 1),          # low word; see reg 3
    (4,  "input.voltage", 10),
    (5,  "output.voltage", 10),
    (6,  "input.frequency", 100),
    (7,  "output.frequency", 100),
    (8,  "ups.load", 1),
    (9,  "ups.realpower", 1),
    (10, "ups.power", 1),
    (11, "battery.voltage", 100),
    (12, "battery.temperature", 10),
    (13, "output.current", 10),
    (14, "input.current", 10),
    (15, "battery.charge.low", 1),
    (16, "ups.temperature", 10),
    (17, "input.voltage.nominal", 10),
    (18, "output.voltage.nominal", 10),
)

# (base reg, nut var, char count) — ASCII, 2 chars/register, space pad.
STRING_REGS = (
    (20, "ups.model", 20),
    (30, "ups.serial", 10),
    (35, "ups.firmware", 10),
    (40, "ups.mfr", 10),
)


def tokens(d):
    return set((d.get("ups.status") or "").split())


def _f(d, key):
    try:
        return float(d.get(key))
    except (TypeError, ValueError):
        return None


def _regs_for_string(text, nregs):
    b = (text or "").encode()[:nregs * 2].ljust(nregs * 2, b" ")
    return [b[i] << 8 | b[i + 1] for i in range(0, len(b), 2)]


def read_registers(d=None):
    """Live dense register map 0..MAX (unmapped -> omitted)."""
    if d is None:
        d = ups_vars()
    regs = {STATUS_REG: 0}
    st = tokens(d)
    for i, flag in enumerate(STATUS_BITS):
        if flag in st:
            regs[STATUS_REG] |= 1 << i
    rt = _f(d, "battery.runtime")
    for addr, var, scale in NUMERIC_REGS:
        v = _f(d, var)
        if v is not None:
            regs[addr] = int(v * scale) & 0xFFFF
    if rt is not None:                     # runtime as u32 across 2-3
        regs[2] = int(rt) & 0xFFFF
        regs[3] = (int(rt) >> 16) & 0xFFFF
    for base, var, nchars in STRING_REGS:
        for i, r in enumerate(_regs_for_string(d.get(var), nchars // 2)):
            regs[base + i] = r
    return regs


def read_discretes(d=None):
    if d is None:
        d = ups_vars()
    st = tokens(d)
    return [f in st for f in STATUS_BITS]


# ============================================================ PDU handling
def _exc(fc, code):
    return bytes((fc | 0x80, code))


def _run_cmd(n):
    cmd = CMDS.get(n)
    if cmd is None:
        return None
    try:
        return ups_cmd(cmd).startswith(("OK", "PENDING"))
    except Exception:
        return False


def handle_pdu(pdu):
    """-> response PDU bytes, or None to drop the request."""
    if len(pdu) < 1:
        return None
    fc = pdu[0]
    if fc in (0x03, 0x04):                   # holding / input registers
        if len(pdu) < 5:
            return _exc(fc, 3)
        start, count = struct.unpack("!HH", pdu[1:5])
        if not 1 <= count <= 125:
            return _exc(fc, 3)
        regs = read_registers()
        vals = []
        for a in range(start, start + count):
            if a not in regs:
                return _exc(fc, 2)           # illegal data address
            vals.append(regs[a])
        return bytes((fc, count * 2)) + struct.pack("!%dH" % count, *vals)
    if fc == 0x02:                           # discrete inputs (status)
        if len(pdu) < 5:
            return _exc(fc, 3)
        start, count = struct.unpack("!HH", pdu[1:5])
        if not 1 <= count <= 16 or start + count > len(STATUS_BITS):
            return _exc(fc, 2)
        bits = read_discretes()[start:start + count]
        out = bytearray((count + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                out[i // 8] |= 1 << (i % 8)
        return bytes((fc, len(out))) + bytes(out)
    if fc in (0x01, 0x05):                   # coils: command surface
        if not ALLOW_WRITE:
            return _exc(fc, 1)
        if len(pdu) < 5:
            return _exc(fc, 3)
        addr, val = struct.unpack("!HH", pdu[1:5])
        if fc == 0x01:                       # read coil states
            if not 1 <= val <= 16 or addr + val > max(CMDS):
                return _exc(fc, 2)
            return bytes((fc, 1, 0))         # commands are momentary
        # FC5: addr = coil number, val = 0xFF00 (on) / 0x0000 (off)
        if val not in (0xFF00, 0x0000):
            return _exc(fc, 3)
        if val == 0xFF00:
            ok = _run_cmd(addr + 1)          # coils map to CMDS 1..n
            if ok is None:
                return _exc(fc, 2)
            if not ok:
                return _exc(fc, 4)
        return pdu[:5]
    if fc == 0x06:                           # write single register
        if not ALLOW_WRITE:
            return _exc(fc, 1)
        if len(pdu) < 5:
            return _exc(fc, 3)
        addr, val = struct.unpack("!HH", pdu[1:5])
        if addr != CMD_REG:
            return _exc(fc, 2)
        ok = _run_cmd(val)
        if ok is None:
            return _exc(fc, 3)               # unknown command value
        if not ok:
            return _exc(fc, 4)               # UPS refused
        return pdu[:5]
    if fc == 0x10:                           # write multiple registers
        if not ALLOW_WRITE:
            return _exc(fc, 1)
        if len(pdu) < 6:
            return _exc(fc, 3)
        addr, count, nbytes = struct.unpack("!HHB", pdu[1:6])
        if addr != CMD_REG or count != 1 or nbytes != 2 or len(pdu) < 7:
            return _exc(fc, 2)
        val = struct.unpack("!H", pdu[6:8])[0]
        ok = _run_cmd(val)
        if ok is None:
            return _exc(fc, 3)
        if not ok:
            return _exc(fc, 4)
        return bytes((fc,)) + struct.pack("!HH", addr, count)
    return _exc(fc, 1)


def handle_mbap(frame):
    """-> response frame bytes, or None."""
    if len(frame) < 8:
        return None
    tid, pid, ln, uid = struct.unpack("!HHHB", frame[:7])
    if pid != 0:
        return None
    pdu = frame[7:7 + ln - 1]
    if uid not in (UNIT, 0, 255):
        return None
    resp = handle_pdu(pdu)
    if resp is None:
        return None
    return struct.pack("!HHHB", tid, 0, len(resp) + 1, uid) + resp


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class _H(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        try:
            while True:
                hdr = _recv_exact(self.request, 7)
                if hdr is None:
                    return
                ln = struct.unpack("!H", hdr[4:6])[0]
                if ln < 2 or ln > 260:
                    return
                pdu = _recv_exact(self.request, ln - 1)
                if pdu is None:
                    return
                resp = handle_mbap(hdr + pdu)
                if resp:
                    self.request.sendall(resp)
        except (OSError, NutError):
            return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    srv = Server((BIND, PORT), _H)
    print("apc-nmc-modbus: listening on %s:%d (unit %d, write %s)"
          % (BIND, PORT, UNIT, "on" if ALLOW_WRITE else "off"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
