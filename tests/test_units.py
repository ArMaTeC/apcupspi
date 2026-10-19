"""Thin unit layer for apc-nmc-web pure functions and the updater's
version comparison — no NUT/UPS hardware needed."""
import importlib.util
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "usr/local/bin/apc-nmc-web.py")
UPD = os.path.join(ROOT, "usr/local/sbin/apc-nmc-update.sh")


def _web():
    spec = importlib.util.spec_from_file_location("nmcweb", WEB)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % WEB)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


web = _web()


def _ver_newer(installed, latest):
    """Run the real bash function extracted from the updater."""
    fn = subprocess.run(
        ["sed", "-n", "/^ver_newer/,/^}/p", UPD],
        capture_output=True, text=True, check=True).stdout
    r = subprocess.run(
        ["bash", "-c", fn + f'\nver_newer "{installed}" "{latest}"'],
        capture_output=True)
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
    setattr(web, "CONF", str(conf))
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
