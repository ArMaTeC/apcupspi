"""Thin unit layer for apc-nmc-web pure functions and the updater's
version comparison — no NUT/UPS hardware needed."""
import hashlib
import importlib.util
import json
import os
import struct
import subprocess

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


web = _load(WEB, "nmcweb")
mod = _load(MOD, "nmcmodbus")


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
    assert web.role_get_ok("readonly", "/config/ups")
    assert not web.role_get_ok("device", "/config/ups")
    assert web.role_get_ok("device", "/control/ups")
    assert web.role_get_ok("network", "/config/system")
    assert not web.role_get_ok("network", "/control/ups")


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


def test_wifi_scan_parse():
    web._nmcli = lambda *a, timeout=15: \
        (0, "CorpNet:80:WPA2\nCorpNet:99:WPA2\nHome:50:\n:10:")
    nets = web.wifi_scan("wlan0")
    assert [n["ssid"] for n in nets] == ["CorpNet", "Home"]
    assert nets[0]["signal"] == 99            # strongest dup wins
    web._nmcli = lambda *a, timeout=15: (1, "err")
    assert web.wifi_scan() == []              # nmcli failure -> []
