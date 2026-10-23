"""Thin unit layer for apc-nmc-web pure functions and the updater's
version comparison — no NUT/UPS hardware needed."""
import configparser
import hashlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import tarfile
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "usr/local/bin/apc-nmc-web.py")
MOD = os.path.join(ROOT, "usr/local/bin/apc-nmc-modbus.py")
UPD = os.path.join(ROOT, "usr/local/sbin/apc-nmc-update.sh")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Loaded dynamically and monkey-patched by tests — Any silences
# "attribute unknown" diagnostics on ModuleType assignments.
web: Any = _load(WEB, "nmcweb")
mod: Any = _load(MOD, "nmcmodbus")


def _ver_newer(installed, latest):
    """Run the real bash function extracted from the updater."""
    fn = subprocess.run(
        ["sed", "-n", "/^ver_newer/,/^}/p", UPD],
        capture_output=True, text=True, check=True).stdout
    r = subprocess.run(
        ["bash", "-c", fn + f'\nver_newer "{installed}" "{latest}"'],
        check=False, capture_output=True)
    return r.returncode == 0


def test_esc():
    assert web.esc('<a href="x">&') == '&lt;a href=&quot;x&quot;&gt;&amp;'


def test_msgbox_escapes_why():
    """The ?why= query param is user-controlled and must not reach HTML raw."""
    out = web.msgbox(("msg-err", '<script>alert(1)</script>'))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert out.startswith('<div class="msg-err">')
    assert web.msgbox("") == ""


def test_num():
    assert web.num({"a": "1.5"}, "a") == 1.5
    assert web.num({"a": "x"}, "a", 9) == 9
    assert web.num({}, "a") is None


def test_humanize():
    assert web.humanize("AcceptableInput") == "Acceptable Input"
    assert web.humanize("load_too_low") == "Load too low"
    assert web.humanize("") == ""


def test_ver_newer():
    assert _ver_newer("1.0.3", "1.0.4")
    assert _ver_newer("1.0.3", "1.1.0")
    assert not _ver_newer("1.0.4", "1.0.4")
    assert not _ver_newer("1.0.5", "1.0.4")
    assert not _ver_newer("1.0.4", "")
    assert _ver_newer("unknown", "1.0.4")


def test_update_backups(tmp_path):
    b = tmp_path / "update-backup-20260101-000000"
    b.mkdir()
    (b / "meta").write_text("1.0.2\n1.0.3\n1700000000\n")
    (tmp_path / "update-backup-empty").mkdir()
    baks = web.update_backups(str(tmp_path))
    assert baks[0][1] == "1.0.2"          # version the backup restores
    assert baks[0][2] == 1700000000
    assert len(baks) == 2


def test_config_new_keys(tmp_path):
    new = tmp_path / "nmc.conf.default"
    live = tmp_path / "nmc.conf"
    new.write_text("[web]\napi_token =\nuser = apc\n"
                   "[update]\nverify = checksum\n")
    live.write_text("[web]\nuser = apc\n#api_token = x\n")
    assert web.config_new_keys(str(new), str(live)) == [
        "web.api_token", "update.verify"]


def test_conf_set(tmp_path):
    conf = tmp_path / "nmc.conf"
    conf.write_text("[update]\nrepo = a/b\n#verify = off\n\n[web]\nuser = apc\n")
    web.CONF = str(conf)
    web.conf_set("update", "verify", "strict")     # replaces commented key
    web.conf_set("update", "channel", "branch")    # appends into section
    web.conf_set("web", "api_token", "tok123")     # appends into section
    web.conf_set("newsec", "k", "v")               # creates new section
    t = conf.read_text()
    assert "verify = strict" in t and "#verify" not in t
    assert "channel = branch" in t
    assert "[web]\nuser = apc\napi_token = tok123" in t
    assert "[newsec]\nk = v" in t
    assert "repo = a/b" in t          # untouched


def test_jstr_roundtrip(tmp_path):
    """The bash jstr() must produce valid JSON string content."""
    fn = subprocess.run(
        ["sed", "-n", "/^jstr/,/^}/p", UPD],
        capture_output=True, text=True, check=True).stdout
    raw = 'line one\nline "two"\ttab\\back'
    r = subprocess.run(
        ["bash", "-c", fn + '\njstr "$0"', raw],
        capture_output=True, text=True, check=True)
    assert json.loads('"' + r.stdout + '"') == raw


# ------------------------------------------------------------- roles
def test_parse_user_cred():
    assert web._parse_user_cred("readonly:salt$hash") == \
        ("readonly", "salt$hash")
    assert web._parse_user_cred("device:plainpw") == ("device", "plainpw")
    assert web._parse_user_cred("plainpw") == ("admin", "plainpw")
    # a colon value that doesn't start with a known role stays whole
    assert web._parse_user_cred("we:ird:pw") == ("admin", "we:ird:pw")


def test_cred_ok_hashed_and_plain():
    salt = "00" * 16
    digest = hashlib.pbkdf2_hmac("sha256", b"secret",
                                 bytes.fromhex(salt), 200_000).hex()
    assert web._cred_ok(salt + "$" + digest, "secret")
    assert not web._cred_ok(salt + "$" + digest, "wrong")
    assert web._cred_ok("plaintext", "plaintext")
    assert not web._cred_ok("plaintext", "other")


def test_role_gates():
    assert web.role_post_ok("admin", "/config/users")
    assert not web.role_post_ok("readonly", "/control/ups")
    assert web.role_post_ok("device", "/control/ups/exec")
    assert not web.role_post_ok("device", "/config/notify")
    assert web.role_post_ok("network", "/config/firewall")
    assert not web.role_post_ok("network", "/config/users")
    assert not web.role_post_ok("network", "/config/radius")
    # every logged-in role may view every page; caps gate the actions
    for r in ("device", "network", "readonly"):
        assert web.role_get_ok(r, "/config/system"), r
        assert web.role_get_ok(r, "/control/ups"), r
    # self-service password change is open to every authenticated role
    for r in ("device", "network", "readonly"):
        assert web.role_post_ok(r, "/config/security"), r
    assert not web.role_post_ok("readonly", "/config/users")


def test_role_caps(monkeypatch):
    cp = configparser.RawConfigParser()
    monkeypatch.setattr(web, "_cp", cp)
    # defaults apply while [roles] is unset — matches the shipped model
    assert web.role_caps("device") == {"ups", "control"}
    assert web.role_caps("network") == {"notify", "network"}
    assert web.role_caps("readonly") == set()
    # a configured list overrides the defaults
    cp.add_section("roles")
    cp.set("roles", "device", "ups accounts")
    assert web.role_caps("device") == {"ups", "accounts"}
    # unknown names are dropped
    cp.set("roles", "device", "ups bogus")
    assert web.role_caps("device") == {"ups"}
    # an explicitly empty value is honoured (view-only role)
    cp.set("roles", "device", "")
    assert web.role_caps("device") == set()
    # admin is always full and ignores the section
    cp.set("roles", "admin", "")
    assert web.role_caps("admin") == set(web.ROLE_CAPS)
    # posts follow the capability list
    cp.set("roles", "network", "accounts")
    assert web.role_post_ok("network", "/config/users")
    assert not web.role_post_ok("network", "/config/firewall")


def test_do_roles(tmp_path, monkeypatch):
    conf = tmp_path / "nmc.conf"
    conf.write_text("[web]\nuser = apc\n")
    monkeypatch.setattr(web, "CONF", str(conf))
    form = {"cap_device_ups": ["1"], "cap_device_accounts": ["1"],
            "cap_readonly_notify": ["1"]}
    res = web.do_roles(form)
    assert res[0] == "msg-ok"
    assert web.cfg("roles", "device") == "accounts ups"
    assert web.cfg("roles", "network") == ""       # cleared, not default
    assert web.cfg("roles", "readonly") == "notify"
    assert "admin" not in web.cfg("roles", "admin")  # admin untouched


def test_pg_users_page():
    h = web.pg_users({"__role": "admin", "__csrf": "x"}, "", "tok")
    assert "User Accounts" in h and "Access Permissions" in h
    assert "RADIUS" in h and "Change Password" in h
    # admin sees an editable matrix (device has ups+control checked)
    assert 'name="cap_device_ups" checked>' in h
    assert 'name="cap_device_notify">' in h
    # admin row is all-checked but disabled (not editable)
    assert "Administrator always has full access" in h
    # readonly: matrix renders with every checkbox disabled, no add form
    ro = web.pg_users({"__role": "readonly", "__csrf": "x"}, "", "tok")
    assert "Access Permissions" in ro
    assert 'name="cap_device_ups" disabled>' in ro or \
        'name="cap_device_ups" checked disabled' in ro
    assert "Add User" not in ro
    # but the self-service password form still renders for readonly
    assert 'action="/config/security"' in ro


def test_syslog_target():
    assert web._syslog_target("local") == ("udp", "local", 514)
    assert web._syslog_target("logs.local") == ("udp", "logs.local", 514)
    assert web._syslog_target("10.0.0.2:1514") == ("udp", "10.0.0.2", 1514)
    assert web._syslog_target("tcp://10.0.0.2") == ("tcp", "10.0.0.2", 601)
    assert web._syslog_target("tls://siem.corp") == ("tls", "siem.corp", 6514)
    assert web._syslog_target("tls://siem.corp:6515") == \
        ("tls", "siem.corp", 6515)


def test_radius_hide_pass_roundtrip():
    """Encrypt then decrypt with the RFC 2865 scheme -> original back."""
    secret, auth, pw = b"s3cret", bytes(range(16)), "hunter2"
    enc = web._radius_hide_pass(pw, secret, auth)
    b = hashlib.md5(secret + auth).digest()
    dec = bytes(x ^ y for x, y in zip(enc[:16], b)).rstrip(b"\x00")
    assert dec.decode() == pw


def test_conf_del(tmp_path):
    conf = tmp_path / "nmc.conf"
    conf.write_text("[users]\nbob = x\nalice = y\n\n[web]\nuser = apc\n")
    web.CONF = str(conf)
    web.conf_del("users", "bob")
    t = conf.read_text()
    assert "bob" not in t and "alice" in t and "[web]" in t


# ------------------------------------------------------------- modbus
def _mod_with(d):
    mod.ups_vars = lambda: d
    return mod


def _mbap(tid, uid, pdu):
    return struct.pack("!HHHB", tid, 0, len(pdu) + 1, uid) + pdu


VARS = {"ups.status": "OL CHRG", "battery.charge": "97",
        "battery.runtime": "1800", "input.voltage": "231.4",
        "ups.load": "42", "ups.model": "Smart-UPS 750"}


def test_modbus_read_regs():
    m = _mod_with(VARS)
    pdu = struct.pack("!BHH", 0x04, 0, 5)          # FC4, addr 0, 5 regs
    r = m.handle_mbap(_mbap(7, 1, pdu))
    assert r[7] == 0x04 and r[8] == 10             # 5 regs x 2 bytes
    vals = struct.unpack("!5H", r[9:19])
    assert vals[0] == 0b0000000000100001           # OL|CHRG bitmap
    assert vals[1] == 97 and vals[2] == 1800
    assert vals[4] == 2314                          # 231.4 x10


def test_modbus_discretes_and_strings():
    m = _mod_with(VARS)
    r = m.handle_mbap(_mbap(1, 1, struct.pack("!BHH", 0x02, 0, 16)))
    assert r[7] == 0x02 and r[8] == 2               # 2 bytes of bits
    assert r[9] == 0b00100001                       # OL(0) + CHRG(5)
    r = m.handle_mbap(_mbap(1, 1, struct.pack("!BHH", 0x04, 20, 5)))
    assert b"Smart-UPS " in r


def test_modbus_errors_and_write_gate():
    m = _mod_with(VARS)
    r = m.handle_mbap(_mbap(1, 1, struct.pack("!B", 0x63)))
    assert r[7] == (0x63 | 0x80) and r[8] == 1      # illegal function
    r = m.handle_mbap(_mbap(1, 1, struct.pack("!BHH", 0x04, 90, 3)))
    assert r[7] == 0x84 and r[8] == 2               # illegal address
    r = m.handle_mbap(_mbap(1, 1, struct.pack("!BHH", 0x06, 100, 1)))
    assert r[7] == 0x86 and r[8] == 1               # writes gated off
    assert m.handle_mbap(_mbap(1, 9, struct.pack("!B", 0x04))) is None  # unit


def test_modbus_write_path():
    m = _mod_with(VARS)
    m.ALLOW_WRITE = True
    got = []
    m.ups_cmd = lambda c: got.append(c) or "OK"
    r = m.handle_mbap(_mbap(1, 1, struct.pack("!BHH", 0x06, 100, 1)))
    assert r[7] == 0x06 and got == ["test.battery.start"]


def test_beeper_control_actions():
    got = []
    web.ups_cmd = lambda c: got.append(c) or "OK"
    web.log_event = lambda *a, **k: None   # keep tests off the real
    web.save_state = lambda *a, **k: None  # event log + notify channels
    assert web.do_control("beep_on", "", "").startswith("OK")
    assert web.do_control("beep_off", "", "").startswith("OK")
    assert web.do_control("mute", "", "").startswith("OK")
    assert got == ["beeper.enable", "beeper.disable", "beeper.mute"]
    assert web.do_control("nonsense", "", "") == "ERR INVALID"


def test_beeper_radio_options():
    html = web.pg_control({"__csrf": "x"}, cmds=["beeper.enable",
                                               "beeper.disable",
                                               "beeper.mute"])
    for act in ("beep_on", "beep_off", "mute"):
        assert f'value="{act}"' in html
    html = web.pg_control({"__csrf": "x"}, cmds=["load.on"])
    assert "beep_on" not in html and "beep_off" not in html


def test_config_merge_defaults(tmp_path):
    dflt = tmp_path / "nmc.conf.default"
    dflt.write_text("[modbus]\n# daemon bind\nbind = 0.0.0.0\n"
                    "port = 502\n\n[web]\nuser = apc\n")
    conf = tmp_path / "nmc.conf"
    conf.write_text("[web]\nuser = admin\n")
    web.CONF = str(conf)
    merged = web.config_merge_defaults(str(dflt))
    t = conf.read_text()
    assert merged == ["modbus.bind", "modbus.port"]
    assert "[modbus]" in t and "bind = 0.0.0.0" in t
    assert "# daemon bind" in t                # doc comment copied
    assert "user = admin" in t                 # existing value untouched
    assert web.config_merge_defaults(str(dflt)) == []   # idempotent


def test_do_wifi_validation():
    web.wifi_status = lambda: ("enabled", "", True)
    web.net_ifaces = lambda p: ["wlan0"]
    calls = []
    web._nmcli = lambda *a, timeout=15: calls.append(a) or (0, "ok")
    web._run = lambda c, t=20: (0, "")
    web.log_event = lambda *a, **k: None
    assert web.do_wifi({"act": ["on"]})[0] == "msg-ok"
    assert web.do_wifi({"act": ["connect"], "w_ssid": ["Net"],
                        "w_if": ["wlan0"], "w_psk": ["12345678"]})[0] \
        == "msg-ok"
    assert web.do_wifi({"act": ["connect"], "w_ssid": ["Net"],
                        "w_if": ["wlan0"], "w_psk": ["x"]})[0] \
        == "msg-err"                          # too-short passphrase
    assert web.do_wifi({"act": ["connect"], "w_ssid": ["Net"],
                        "w_if": ["eth0"], "w_psk": [""]})[0] \
        == "msg-err"                          # non-wireless ifname
    assert calls[-1] == ("dev", "wifi", "connect", "Net",
                         "ifname", "wlan0", "password", "12345678")


def test_wifi_hw_status(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("[all]\ndtoverlay=disable-wifi\ndtparam=watchdog=on\n")
    web.BOOTCFGS = (str(cfg),)
    st = web.wifi_hw_status()
    assert st["wifi"] is True and st["bt"] is False and st["cfg"]
    cfg.write_text("[all]\ndtparam=watchdog=on\n")
    assert web.wifi_hw_status()["wifi"] is False
    web.BOOTCFGS = (str(tmp_path / "missing"),)
    assert web.wifi_hw_status()["cfg"] is False


def test_boot_cfg_pending(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("x\n")
    st = tmp_path / "stat"
    web.BOOTCFGS = (str(cfg),)
    web.PROC_STAT = str(st)
    # boot an hour ago, config touched now -> pending
    st.write_text(f"btime {int(time.time()) - 3600}\n")
    assert web.boot_cfg_pending() is True
    # boot in the future (config older than boot) -> not pending
    st.write_text(f"btime {int(time.time()) + 3600}\n")
    assert web.boot_cfg_pending() is False
    st.write_text("garbage\n")
    assert web.boot_cfg_pending() is False
    web.PROC_STAT = str(tmp_path / "gone")
    assert web.boot_cfg_pending() is False


def test_pg_system_wifi_hw_section(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("# >>> apc-nmc\n[all]\ndtoverlay=disable-wifi\n"
                   "dtoverlay=disable-bt\n# <<< apc-nmc\n")
    web.BOOTCFGS = (str(cfg),)
    web.wifi_status = lambda: ("enabled", "", False)   # no NM
    web.net_ifaces = lambda p: []
    web.boot_cfg_pending = lambda: True
    a = web.pg_system({"__role": "admin", "__csrf": "X", "__uinfo": ""})
    assert 'value="hw_enable"' in a and 'value="reboot"' in a
    assert "Reboot pending" in a
    assert "disabled at boot" in a
    r = web.pg_system({"__role": "readonly", "__csrf": "X",
                       "__uinfo": ""})
    assert "disabled at boot" in r                    # status visible
    assert 'value="hw_enable"' not in r
    assert 'value="reboot"' not in r                  # but no buttons
    # enabled-in-config but no live radio + pending -> pending label
    cfg.write_text("[all]\n")
    a = web.pg_system({"__role": "admin", "__csrf": "X", "__uinfo": ""})
    assert "pending reboot" in a


def test_do_wifi_hw_actions(tmp_path, monkeypatch):
    req = tmp_path / "wifi-hw.request"
    web.WIFI_HW_REQ = str(req)
    starts = []
    web._sysctl = lambda *a: starts.append(a) or (0, "")
    web.log_event = lambda *a, **k: None
    web.wifi_status = lambda: ("enabled", "", False)  # no NM — must not
    # block boot-config actions
    assert web.do_wifi({"act": ["hw_enable"], "hw": ["wifi"]})[0] \
        == "msg-ok"
    assert req.read_text() == "wifi=enable\n"
    assert starts == [("start", "--no-block", "apc-nmc-wifi-hw.service")]
    assert web.do_wifi({"act": ["hw_disable"], "hw": ["bt"]})[0] \
        == "msg-ok"
    assert req.read_text().endswith("bt=disable\n")
    assert web.do_wifi({"act": ["hw_enable"], "hw": ["gpu"]})[0] \
        == "msg-err"                          # unknown radio
    web._sysctl = lambda *a: (1, "denied")
    req.unlink()
    assert web.do_wifi({"act": ["hw_disable"], "hw": ["wifi"]})[0] \
        == "msg-err"                          # service start failed
    timers = []

    class _T:
        def __init__(self, d, fn):
            timers.append(fn)

        def start(self):
            pass

    monkeypatch.setattr(web.threading, "Timer", _T)
    assert web.do_wifi({"act": ["reboot"]})[0] == "msg-ok"
    assert len(timers) == 1                   # deferred reboot scheduled


def test_wifi_hw_script(tmp_path):
    """Run the real helper against temp config.txt fixtures."""
    script = os.path.join(ROOT, "usr/local/sbin/apc-nmc-wifi-hw.sh")
    cfg = tmp_path / "config.txt"
    req = tmp_path / "wifi-hw.request"

    def run(cfgtxt, reqtxt):
        cfg.write_text(cfgtxt)
        req.write_text(reqtxt)
        subprocess.run(
            ["bash", str(script)], check=True,
            env={**os.environ, "APCNMC_BOOTCFG": str(cfg),
                 "APCNMC_WIFIHW_REQ": str(req)})
        assert not req.exists()              # request consumed
        return cfg.read_text()

    base = ("# >>> apc-nmc\n[all]\nenable_uart=1\n"
            "dtoverlay=disable-wifi\ndtoverlay=disable-bt\n"
            "dtparam=watchdog=on\n[pi5]\ndtoverlay=nospi10\n"
            "# <<< apc-nmc\n")
    # enable wifi: only that overlay goes, everything else preserved
    out = run(base, "wifi=enable\n")
    assert "disable-wifi" not in out
    for keep in ("disable-bt", "enable_uart=1", "watchdog=on",
                 "[pi5]", "nospi10"):
        assert keep in out
    # disable wifi into a block whose [all] exists
    out = run(base, "wifi=disable\n")
    assert "dtoverlay=disable-wifi" in out
    assert out.count("[all]") == 1
    # disable wifi into a block without [all]
    out = run("# >>> apc-nmc\n[pi4]\narm_freq=1750\n# <<< apc-nmc\n",
              "wifi=disable\n")
    assert "[all]\ndtoverlay=disable-wifi\n# <<< apc-nmc" in out
    # no managed block at all -> append a fresh one
    out = run("arm_freq=1500\n", "wifi=disable\n")
    assert out.startswith("arm_freq=1500\n")
    assert "# >>> apc-nmc\n[all]\ndtoverlay=disable-wifi" in out
    # idempotent: second identical request changes nothing
    out2 = run(out, "wifi=disable\n")
    assert out2 == out
    # garbage lines are ignored, file untouched
    out3 = run(out, "gpu=explode\nwifi=maybe\n")
    assert out3 == out


def test_mqtt_wire():
    """The stdlib client speaks enough MQTT for a broker to accept."""
    import socket as _s
    import threading as _t
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def acc():
        c, _ = srv.accept()
        c.settimeout(4)
        got.append(c.recv(4096))                     # CONNECT
        c.sendall(b"\x20\x02\x00\x00")               # CONNACK accept
        got.append(c.recv(4096))                     # PUBLISH
        c.close()

    th = _t.Thread(target=acc, daemon=True)
    th.start()
    port = srv.getsockname()[1]
    ok, err = web.mqtt_publish("127.0.0.1", port, "a/b", '{"x":1}')
    th.join(5)
    srv.close()
    assert ok, err
    assert got[0][0] == 0x10 and b"MQTT" in got[0]   # CONNECT
    assert got[1][0] >> 4 == 3                       # PUBLISH packet
    assert b"a/b" in got[1] and b'{"x":1}' in got[1]


def test_do_services_mqtt(monkeypatch):
    conf = {}
    monkeypatch.setattr(web, "conf_set",
                        lambda s, k, v: conf.__setitem__((s, k), v))
    monkeypatch.setattr(web, "log_event", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(web, "mqtt_send",
                        lambda p: sent.append(p) or (True, ""))
    f = {"svc": ["mqtt"], "mq_en": ["on"], "mq_host": ["broker.local"],
         "mq_port": ["1883"], "mq_topic": ["apc-nmc/ups"],
         "mq_interval": ["30"], "mq_test": ["1"]}
    assert web.do_services(f)[0] == "msg-ok"
    assert conf[("mqtt", "enabled")] == "1"
    assert conf[("mqtt", "host")] == "broker.local"
    assert conf[("mqtt", "topic")] == "apc-nmc/ups"
    assert sent                                     # test publish ran
    f["mq_host"] = ["bad host!"]
    assert web.do_services(f)[0] == "msg-err"       # host charset
    f["mq_host"] = ["broker.local"]
    f["mq_interval"] = ["5"]
    assert web.do_services(f)[0] == "msg-err"       # interval < 15


def test_do_schedule(monkeypatch):
    conf = {}
    monkeypatch.setattr(web, "conf_set",
                        lambda s, k, v: conf.__setitem__((s, k), v))
    monkeypatch.setattr(web, "log_event", lambda *a, **k: None)
    assert web.do_schedule({"st_wd": ["2"], "st_hr": ["3"],
                            "sd_wd": ["5"], "sd_hr": ["22"],
                            "sd_mode": ["stayoff"]})[0] == "msg-ok"
    assert conf[("schedule", "selftest_weekday")] == "2"
    assert conf[("schedule", "shutdown_weekday")] == "5"
    assert conf[("schedule", "shutdown_mode")] == "stayoff"
    assert web.do_schedule({"st_wd": ["9"]})[0] == "msg-err"
    assert web.do_schedule({"sd_hr": ["25"]})[0] == "msg-err"


def test_maybe_scheduled_shutdown(monkeypatch):
    now = time.localtime()
    conf = {("schedule", "shutdown_weekday"): str(now.tm_wday),
            ("schedule", "shutdown_hour"): str(now.tm_hour),
            ("schedule", "shutdown_mode"): "stayoff"}
    st, calls = {}, []
    monkeypatch.setattr(web, "cfg",
                        lambda s, k, d="": conf.get((s, k), d))
    monkeypatch.setattr(web, "cfg_get", lambda k, d="": st.get(k, d))
    monkeypatch.setattr(web, "cfg_set",
                        lambda k, v: st.__setitem__(k, v))
    monkeypatch.setattr(web, "ups_cmd",
                        lambda c: calls.append(c) or "OK")
    monkeypatch.setattr(web, "log_event", lambda *a, **k: None)
    web._maybe_scheduled_shutdown(True)
    assert calls == ["shutdown.stayoff"]
    web._maybe_scheduled_shutdown(True)             # once per day
    assert calls == ["shutdown.stayoff"]
    web._maybe_scheduled_shutdown(False)            # UPS offline
    assert calls == ["shutdown.stayoff"]


def test_support_bundle(tmp_path, monkeypatch):
    conf = tmp_path / "nmc.conf"
    conf.write_text("[web]\napi_token = secret123\n"
                    "[mqtt]\npassword = hunter2\nhost = m.local\n")
    monkeypatch.setattr(web, "CONF", str(conf))
    data = web.support_bundle()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = tf.getnames()
        assert "meta.txt" in names and "nmc.conf" in names
        f = tf.extractfile("nmc.conf")
        assert f is not None
        c = f.read().decode()
    assert "secret123" not in c and "hunter2" not in c
    assert "<redacted>" in c and "host = m.local" in c


def test_wifi_scan_parse():
    web._nmcli = lambda *a, timeout=15: \
        (0, "CorpNet:80:WPA2\nCorpNet:99:WPA2\nHome:50:\n:10:")
    nets = web.wifi_scan("wlan0")
    assert [n["ssid"] for n in nets] == ["CorpNet", "Home"]
    assert nets[0]["signal"] == 99            # strongest dup wins
    web._nmcli = lambda *a, timeout=15: (1, "err")
    assert web.wifi_scan() == []              # nmcli failure -> []


def test_password_change_per_user(tmp_path):
    conf = tmp_path / "nmc.conf"
    conf.write_text("[web]\nx = 1\n\n[users]\nbob = device:pw12345\n")
    web.CONF = str(conf)
    web._cp.read(str(conf))
    good = {"curp": ["pw12345"], "newp": ["newsecret1"],
            "cfmp": ["newsecret1"]}
    r = web.do_password_change(good, user="bob")
    assert r[0] == "msg-ok", r
    role, cred = web._parse_user_cred(web._cp.get("users", "bob"))
    assert role == "device"                 # role preserved
    assert web._cred_ok(cred, "newsecret1")  # new hash verifies
    # wrong current password -> error, stored hash untouched
    r = web.do_password_change(
        {"curp": ["nope"], "newp": ["x123456"], "cfmp": ["x123456"]},
        user="bob")
    assert r[0] == "msg-err"
    # no local account -> treated as RADIUS-managed, no phantom entry
    r = web.do_password_change(good, user="ghost")
    assert r[0] == "msg-err" and "authentication server" in r[1]
    assert "ghost" not in web._cp.options("users")


def test_datalog_page():
    data = [{"ts": i} for i in range(10)]     # stored oldest -> newest
    p = web.datalog_page(data, 0, 3)
    assert p["total"] == 10
    assert [r["ts"] for r in p["rows"]] == [9, 8, 7]   # newest first
    p = web.datalog_page(data, 8, 3)
    assert [r["ts"] for r in p["rows"]] == [1, 0]      # partial last page
    p = web.datalog_page(data, 20, 3)
    assert p["rows"] == []                     # past end -> empty
    p = web.datalog_page(data, -5, 3)
    assert [r["ts"] for r in p["rows"]] == [9, 8, 7]   # neg off clamps


def test_code_split_parts():
    # every _load_parts file ships and the loaded module exposes all
    # page renderers + POST handlers that used to live in one file
    for name in web._PARTS:
        p = os.path.join(web.ASSET_DIR, name)
        assert os.path.isfile(p), name
        with open(p, encoding="utf-8") as f:
            compile(f.read(), p, "exec")            # syntax check
    for fn in ("pg_home", "pg_status", "pg_control", "pg_outlets",
               "pg_tests", "pg_config", "pg_users", "pg_system",
               "pg_events", "pg_data", "pg_about", "pg_network",
               "pg_nut", "pg_host", "pg_help", "pg_404",
               "do_config", "do_users", "do_roles", "do_notify_config",
               "do_update_apply", "do_wifi", "do_control", "do_test"):
        assert callable(getattr(web, fn)), fn


def test_do_config_alarm(monkeypatch):
    # beeper instcmd only fires on a real change and only when the UPS
    # supports it — the mute-only driver must not error on plain saves
    fired = []
    st = {"cfg": {}}
    monkeypatch.setattr(web, "load_state", lambda: st)
    monkeypatch.setattr(web, "cfg_set", lambda k, v: st["cfg"].__setitem__(k, v))
    monkeypatch.setattr(web, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(web, "ups_rw", lambda: set())
    monkeypatch.setattr(web, "ups_setvar", lambda v, x: "OK")
    monkeypatch.setattr(web, "ups_cmd", lambda c: fired.append(c) or "OK")

    def run(alarm, status="", cmds=("beeper.mute",)):
        fired.clear()
        monkeypatch.setattr(web, "ups_vars",
                            lambda: ({"ups.beeper.status": status}
                                     if status else {}))
        monkeypatch.setattr(web, "ups_cmds", lambda: list(cmds))
        return web.do_config({"alarm": [alarm]})

    # mute-only UPS, untouched default (1) → no cmd, no error
    assert run("1") == ("msg-ok", "Configuration saved.")
    assert not fired
    # picking "never" on a UPS lacking beeper.disable → clear warning
    cls, txt = run("3")
    assert cls == "msg-err" and "not supported" in txt and not fired
    # supported command fires when the selection changes
    cls, txt = run("3", cmds=["beeper.disable", "beeper.mute"])
    assert cls == "msg-ok" and fired == ["beeper.disable"]
    # same-as-current selection is a no-op even when supported
    assert run("4", status="muted",
               cmds=["beeper.mute"]) == ("msg-ok", "Configuration saved.")
    assert not fired
    # mute works on the mute-only UPS when explicitly picked
    assert run("4") == ("msg-ok", "Configuration saved.")
    assert fired == ["beeper.mute"]


def test_help_pages():
    # every registered topic renders real content and has a TOC entry
    for slug, title, _ in web.HELP_TOPICS:
        h = web.pg_help({}, slug)
        assert 'helptoc' in h and 'class="here"' in h
        assert web.esc(title) in h
        assert 'missing or not yet' not in h, slug
    # unknown slugs fall back to index
    assert 'Getting Around' in web.pg_help({}, 'nope')
    # kv() tooltips appear for known labels only
    assert 'class="tip"' in web.kv('Battery Charge', 'x')
    assert 'class="tip"' not in web.kv('Some Other Label', 'x')
