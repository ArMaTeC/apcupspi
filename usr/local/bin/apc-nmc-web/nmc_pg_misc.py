"""Misc pages: help browser, event/data logs, About & updates,
network/NUT/host views, error pages and text/metrics renderers.
Loaded into the apc-nmc-web.py namespace at startup (see the
_load_parts() call near the bottom of that file). Names defined in
other parts or the main file resolve through the shared module
globals — identical semantics to the former single file."""
# Part of apc-nmc-web.py — see that file for the noqa rationale.
# ruff: noqa: BLE001, S110, UP031
# pyright: reportUndefinedVariable=false
import itertools


def pg_help(d, slug):
    slugs = [s for s, _, _ in HELP_TOPICS]
    if slug not in slugs:
        slug = "index"
    toc = "".join(
        '<a href="/help/%s"%s>%s<span>%s</span></a>'
        % (s, ' class="here" aria-current="page"' if s == slug else "",
           esc(t), esc(b))
        for s, t, b in HELP_TOPICS)
    body = _asset("help/%s.html" % slug)
    if not body:
        body = ('<p class="note">This help topic is missing or not yet '
                'written.</p>')
    titles = {s: t for s, t, _ in HELP_TOPICS}
    nxt = slugs[slugs.index(slug) + 1] if slug != slugs[-1] else ""
    nxtlink = ('<p class="helplink">Next: <a href="/help/%s">%s</a></p>'
               % (nxt, esc(titles[nxt]))) if nxt else ""
    card = """
<div class="card help">
 <div class="card-head"><h3>Documentation</h3></div>
 <div class="helpwrap">
  <div class="helptoc">%s</div>
  <div class="helpbody"><h2>%s</h2>%s%s</div>
 </div>
</div>
""" % (toc, esc(titles[slug]), body, nxtlink)
    return card


def pg_events(d, msg=""):
    evs = load_events()
    m = msgbox(msg)
    now = time.time()

    def rel(ts):
        el = int(now - ts)
        if el < 60:
            return "just now"
        if el < 3600:
            return "%dm ago" % (el // 60)
        if el < 86400:
            return "%dh ago" % (el // 3600)
        return "%dd ago" % (el // 86400)

    sevs = {"crit": 0, "warn": 0, "info": 0}
    for e in evs:
        sevs[e.get("sev", "info")] = sevs.get(e.get("sev", "info"), 0) + 1
    counts = ("<span class='note'>%d critical &middot; %d warning "
              "&middot; %d info</span>"
              % (sevs.get("crit", 0), sevs.get("warn", 0),
                 sevs.get("info", 0)))
    rows = "".join(
        "<tr data-sev='%s'><td><span class='dot %s'></span></td>"
        "<td>%s</td><td>%s <span class='mut' style='font-size:11px'>"
        "%s</span></td><td>%s</td></tr>" % (
            esc(e.get("sev", "info")), esc(e.get("sev", "info")),
            time.strftime("%m/%d/%Y", time.localtime(e["ts"])),
            time.strftime("%H:%M:%S", time.localtime(e["ts"])),
            rel(e["ts"]), esc(e["text"]))
        for e in reversed(evs)) or \
        '<tr><td colspan="4" class="note">No events.</td></tr>'
    clearform = ""
    if role_post_ok(d.get("__role", "admin"), "/log/clear"):
        clearform = ('<form method="post" action="/log/clear" '
                     'style="display:inline" onsubmit="return confirm('
                     "'Clear the entire event log?')\">%s"
                     '<button class="btn sec">Clear Log</button></form>'
                     % csrf_input(d))
    return """<h1 class="page">Event Log</h1>%s
<div class="card"><div class="pad">
<div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center;
 margin-bottom:12px;font-size:13px">
<label class="f" style="padding:0"><input type="checkbox" class="evf" value="crit" checked> critical</label>
<label class="f" style="padding:0"><input type="checkbox" class="evf" value="warn" checked> warning</label>
<label class="f" style="padding:0"><input type="checkbox" class="evf" value="info" checked> info</label>
<input type="text" id="evq" placeholder="Filter events&hellip;"
 style="width:220px" title="Filter the table by event text — client-side, instant">
%s
<label class="f" style="padding:0;margin-left:auto"
 title="Reload the page every 60 seconds"><input type="checkbox"
 id="evrefresh"> auto-refresh</label></div>
<table class="ev" id="evtable" data-pg-global="#evq" data-pg-sev>
<tr><th style="width:20px"></th>
<th style="width:105px">Date</th><th style="width:85px">Time</th>
<th>Event</th></tr>%s</table>
<div style="margin-top:14px;display:flex;gap:10px">
<a class="btn sec" href="/events.txt">Download events.txt</a>
<a class="btn sec" href="/data.txt" title="Download the power data log">Download data.txt</a>
%s</div>
</div></div>""" % (m, counts, rows, clearform)


def pg_data():
    # Rows are lazy-loaded by datalog.js via /datalog.json?off=&lim= —
    # the full log can be ~1500 samples, far too much markup to inline.
    total = len(load_data())
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
<div id="dlranges" style="margin-bottom:8px">
<button class="btn sec dlrange act" data-h="24" title="Last 24 hours">24 h</button>
<button class="btn sec dlrange" data-h="168"
 style="margin-left:6px" title="Last 7 days">7 days</button>
<button class="btn sec dlrange" data-h="0"
 style="margin-left:6px" title="Full retained history">All</button></div>
<canvas id="dlc" width="1000" height="260"
 style="width:100%%;height:auto"></canvas>
<div class="note" id="dllbl" style="margin-top:6px"></div>
<div class="note">Refreshes every 60 s.</div>
</div></div>
<div class="card"><div class="pad">
<table class="ev"><thead><tr><th>Date</th><th>Time</th><th>Charge %%</th>
<th>Load %%</th><th>Watts</th><th>In V</th><th>Out V</th><th>Temp &deg;C</th>
<th>Status</th></tr></thead>
<tbody id="dlrows"><tr><td colspan="9" class="note">Loading&hellip;</td></tr></tbody></table>
<div id="dlsent" style="height:1px"></div>
<div style="margin-top:14px"><a class="btn sec" href="/data.txt">Download data.txt</a>
<a class="btn sec" href="/data.txt?fmt=csv" style="margin-left:8px">Download CSV</a>
<a class="btn sec" id="dlmore" style="margin-left:8px;display:none">Load more</a>
<span class="note" id="dlnote" style="margin-left:10px">Sampled every 60 s, newest first &mdash; %s samples.</span></div>
</div></div>
<script>%s</script>""" % (ecard, total, _asset("datalog.js"))


def support_bundle():
    """tar.gz bytes of a diagnostics snapshot for support: versions,
    sanitized config/events/state, UPS vars, service + journal output.
    Secret-looking config values (passwords, tokens, keys, communities)
    are redacted before packing."""
    secret_re = re.compile(
        r"(?im)^([ \t]*[\w.\[\]-]*(?:pass|secret|token|key|community|"
        r"private)[\w.\[\]-]*[ \t]*[=:]).*$")

    def sanitize(txt):
        return secret_re.sub(r"\1 <redacted>", txt)

    def put(tf, name, data):
        b = data.encode("utf-8", "replace")
        ti = tarfile.TarInfo(name)
        ti.size = len(b)
        tf.addfile(ti, io.BytesIO(b))

    def sh(cmd, timeout=15):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, check=False)
            return "$ %s\n%s%s" % (" ".join(cmd), r.stdout, r.stderr)
        except Exception as e:
            return "$ %s\n<%s>\n" % (" ".join(cmd), e)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        put(tf, "meta.txt",
            "apc-nmc support bundle\nversion: %s\ngenerated: %s\n"
            % (installed_version(), time.strftime("%Y-%m-%d %H:%M:%S %z")))
        try:
            with open(CONF) as f:
                put(tf, "nmc.conf", sanitize(f.read()))
        except OSError:
            pass
        for src, arc in ((STATE_FILE, "state.json"),
                         (EVENTS_FILE, "events.json")):
            try:
                with open(src) as f:
                    put(tf, arc, sanitize(f.read()))
            except OSError:
                pass
        try:
            put(tf, "ups-vars.txt", "\n".join(
                "%s: %s" % kv for kv in sorted(ups_vars().items())))
        except Exception as e:
            put(tf, "ups-vars.txt", "<%s>" % e)
        try:
            put(tf, "ups-cmds.txt", "\n".join(
                "%s: %s" % kv for kv in sorted(ups_cmds().items())))
        except Exception as e:
            put(tf, "ups-cmds.txt", "<%s>" % e)
        try:
            put(tf, "ups-rw.txt", "\n".join(
                "%s: %s" % kv for kv in sorted(ups_rw().items())))
        except Exception as e:
            put(tf, "ups-rw.txt", "<%s>" % e)
        put(tf, "nmcli.txt", sh(["nmcli", "-t", "dev", "status"]) +
            sh(["nmcli", "dev", "show"]))
        put(tf, "services.txt", sh(
            ["systemctl", "--no-pager", "--full", "status",
             "apc-nmc-web.service", "nut-server.service",
             "nut-monitor.service", "nut-driver@myups.service",
             "apc-nmc-modbus.service", "net-watchdog.service"]))
        put(tf, "failed-units.txt",
            sh(["systemctl", "--no-pager", "--failed"]))
        put(tf, "journal.txt", sh(
            ["journalctl", "--no-pager", "-n", "400",
             "-u", "apc-nmc-web.service", "-u", "nut-server.service",
             "-u", "nut-driver@myups.service",
             "-u", "apc-nmc-modbus.service"]))
        put(tf, "update-log.txt", sh(["tail", "-200", UPDATE_LOG]))
        put(tf, "network.txt",
            sh(["ip", "addr"]) + sh(["ip", "route"]))
        put(tf, "host.txt",
            sh(["free", "-m"]) + sh(["df", "-h"]) + sh(["uptime"]) +
            sh(["vcgencmd", "get_throttled"]))
    return buf.getvalue()


def pg_about(d, msg: "str | tuple" = ""):
    m = msgbox(msg)
    try:
        with open("/sys/class/net/eth0/address") as f:
            mac = f.read().strip().upper()
    except Exception:
        mac = "?"
    up = "unknown"
    try:
        with open("/proc/uptime") as f:
            s = float(f.read().split()[0])
        up = "%d day%s %02d:%02d" % (s // 86400,
                    "s" if s // 86400 != 1 else "", s % 86400 // 3600,
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
        ("Application Module",
         "apcupspi v%s" % installed_version()),
        ("APC OS (AOS)", NMC_FW + " (emulated AP9631 identity)"),
        ("Boot Monitor", "v1.0.9"),
        ("NMC Uptime", up),
        ("Host", "Raspberry Pi &mdash; Linux %s" % kernel)])
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
    uss = update_status_view(_load_json(UPDATE_STATUS))
    try:
        fin = float(uss.get("finished", 0) or 0)
    except (TypeError, ValueError):
        fin = 0
    if uss.get("state") == "ok" and fin > float(
            cfg_get("update_announced", 0) or 0):
        cfg_set("update_announced", fin)
        log_event("NMC: Software updated: %s › %s."
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
    repo = cfg("update", "repo", "ArMaTeC/apcupspi")
    relurl = "https://github.com/%s/releases" % repo
    upd = "".join(kv(a, b) for a, b in [
        ("Installed Version", esc(installed_version())),
        ("Latest Version", esc(us.get("latest") or "?")
         + ' &nbsp;<a class="btn sec" style="padding:1px 8px;'
           'font-size:11px" href="%s" target="_blank" rel="noopener" '
           'title="Release notes and history on GitHub">Releases</a>'
           % esc(relurl)),
        ("Last Check", checked),
        ("Update Available", avail)])
    if us.get("available") and us.get("notes"):
        upd += kv("Release Notes",
                  '<pre class="updnotes">%s</pre>'
                  % linkify("\n".join(us["notes"].splitlines()[:24])))
    if uss.get("state") == "queued":
        upd += kv("Update Status",
                  '<a href="/about/update/apply"><span class="st-warn">'
                  'starting&hellip;</span></a>')
    elif uss.get("state") == "running":
        upd += kv("Update Status",
                  '<a href="/about/update/apply"><span class="st-warn">'
                  'in progress: %s</span></a>'
                  % esc(uss.get("msg", "")))
    elif uss.get("state") == "interrupted":
        upd += kv("Update Status", '<span class="st-warn">interrupted '
                  '(reboot?) &mdash; see update.log</span>')
    elif uss.get("state") == "failed":
        upd += kv("Last Update", '<span class="st-warn">failed: %s</span>'
                  % esc(uss.get("msg", "")))
    elif uss.get("state") == "ok":
        upd += kv("Last Update", esc("%s (to %s)"
                  % (time.strftime("%m/%d/%Y %H:%M",
                                   time.localtime(fin)) if fin else "done",
                     uss.get("to", "?"))))
    # the update action buttons stay off while a run is queued/live —
    # the POSTs are refused too, this just keeps the UI honest
    dis = (' disabled title="an update is in progress"'
           if uss.get("state") in ("queued", "running") else "")
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
                   '<button class="btn sec updbtn" type="submit"%s>'
                   'Roll Back</button></form>' % dis)
    newkeys = config_new_keys()
    if newkeys:
        upd += kv("New Config Options",
                  '<span class="st-warn">%s</span> &mdash; unset in '
                  'nmc.conf; defaults are adopted automatically on the '
                  'next update' % esc(", ".join(newkeys)))
    def sel(cur, *opts):
        return "".join('<option value="%s"%s>%s</option>'
                       % (o, " selected" if o == cur else "", esc(o))
                       for o in opts)
    # Update controls and the Update Settings card (which displays the
    # API token in full — readable bearer credentials) are admin-only;
    # other roles keep the read-only version/status table.
    is_admin = d.get("__role", "admin") == "admin"
    updbtns = updcfg = ""
    if is_admin:
        updbtns = ("""<form method="post" action="/about/update/check" style="display:inline"
 onsubmit="var b=this.querySelector('button');b.disabled=true;b.textContent='Checking&#8230;';">%s
<button class="btn sec updbtn" type="submit"%s>Check for Updates</button></form>
<form method="post" action="/about/update/apply" style="display:inline"
 onsubmit="return confirm('Download and install the latest version now? Services (including this interface) restart during the update.')">
%s<button class="btn sec updbtn" type="submit"%s>Update Now</button></form>
%s
<form method="post" action="/about/sysact" style="display:inline"
 onsubmit="return confirm('Restart the web interface? You will be logged out for a few seconds.')">
%s<input type="hidden" name="act" value="restart-web">
<button class="btn sec" type="submit">Restart Interface</button></form>
<form method="post" action="/about/sysact" style="display:inline"
 onsubmit="return confirm('Reboot this management card? All services (including this interface) stop until it comes back.')">
%s<input type="hidden" name="act" value="reboot">
<button class="btn dan" type="submit">Reboot Card</button></form>
""" % (csrf_input(d), dis, csrf_input(d), dis, rollbtn,
       csrf_input(d), csrf_input(d)))
        utok = cfg("web", "api_token", "")
        tokdisp = ('<code style="word-break:break-all">%s</code>'
                   '<button class="btn sec" type="button" '
                   'onclick="nmcCopy(this)" data-tok="%s" '
                   'style="padding:2px 10px;font-size:12px;'
                   'margin-left:8px">Copy</button>'
                   % (esc(utok), esc(utok)) if utok else
                   '<span style="color:var(--mut)">not set</span>')
        tokclear = ("""<form method="post" action="/about/update/token"
 style="display:inline"
 onsubmit="return confirm('Clear the API token? Clients using it lose access.')">"""
                    + csrf_input(d)
                    + '<input type="hidden" name="act" value="clear">'
                      '<button class="btn sec" type="submit">Clear</button></form>'
                    if utok else "")
        updcfg = """<div class="card"><h3>Update Settings</h3><div class="pad">
<form method="post" action="/about/update/settings"><table class="kv">
<tr><td class="lbl">Repository<span class="tip" title="GitHub repo checked for releases, in owner/name form">?</span></td><td><input name="urepo" maxlength="64"
 value="%s" placeholder="owner/name"></td></tr>
<tr><td class="lbl">Branch<span class="tip" title="Branch used when the channel tracks a branch instead of tags">?</span></td><td><input name="ubranch" maxlength="64"
 value="%s"></td></tr>
<tr><td class="lbl">Channel<span class="tip" title="Which release stream to track (stable tags vs. branch builds)">?</span></td><td><select name="uchannel">%s</select></td></tr>
<tr><td class="lbl">Verify<span class="tip" title="Require sha256 (and signature if available) before installing an update">?</span></td><td><select name="uverify">%s</select></td></tr>
<tr><td class="lbl">Daily auto-check<span class="tip" title="Check GitHub once a day and show the update banner when a new release exists">?</span></td><td>
 <input type="checkbox" name="uautocheck" value="1"%s></td></tr>
</table>
<div style="margin-top:14px">%s
<button class="btn sec updbtn" type="submit"%s>Save Settings</button></div>
</form>
<h3 style="margin:20px 0 8px;font-size:14px">API Token</h3>
<p style="margin:0 0 10px;color:var(--mut)">Read-only bearer token for
/metrics and /api/v1/* — send as <code>Authorization: Bearer</code>.</p>
<table class="kv"><tr><td class="lbl">Token<span class="tip" title="Full API access — keep it secret; Generate invalidates the old one">?</span></td><td>%s</td></tr></table>
<div style="margin-top:12px">
<form method="post" action="/about/update/token" style="display:inline"
 onsubmit="return confirm('%s')">%s
<input type="hidden" name="act" value="generate">
<button class="btn sec" type="submit">%s</button></form>
%s
<h3 style="margin:20px 0 8px;font-size:14px">Support Bundle</h3>
<p style="margin:0 0 10px;color:var(--mut)">Diagnostic snapshot —
sanitized config, events, service states and recent journals packed
into one download for support requests.</p>
<div><a class="btn sec" href="/config/support.tgz">Download Bundle</a>
</div></div></div>""" % (
            esc(cfg("update", "repo", "ArMaTeC/apcupspi")),
            esc(cfg("update", "branch", "main")),
            sel(cfg("update", "channel", "releases"), "releases", "branch"),
            sel(cfg("update", "verify", "checksum"),
                "checksum", "strict", "0"),
            " checked" if cfg("update", "autocheck", "1") != "0" else "",
            csrf_input(d), dis,
            tokdisp,
            ("Replace the existing API token? Clients using it lose "
             "access." if utok else "Generate a new API token?"),
            csrf_input(d),
            "Regenerate" if utok else "Generate",
            tokclear)
    return """<h1 class="page">About</h1>%s
<div class="card"><h3>Software Update</h3><div class="pad">
<table class="kv">%s</table>
%s</div></div>
%s
<div class="card"><h3>Network Management Card</h3><div class="pad">
<table class="kv">%s</table>
<p class="note">Model, serial, AOS and boot-monitor fields are the
emulated AP9631 identity this card presents to SNMP/clients;
they are not physical hardware values.</p></div></div>
<div class="card"><h3>Attached UPS</h3><div class="pad">
<table class="kv">%s</table></div></div>""" % (
        m, upd, updbtns, updcfg, rows, dev)


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
   }else if(s.state==='interrupted'){
     ph.textContent='Update interrupted';
     dn.innerHTML='<span class="st-warn">The updater stopped mid-run '+
       '(e.g. a reboot) &mdash; check <b>update.log</b> before '+
       'retrying or rolling back.</span> <a href="/about/system">'+
       'Back to About &#8250; System</a>';
   }else if(s.state==='queued'){
     /* the web side stamped this when it started the unit — the
        updater itself writes 'running' once it takes the lock */
     ph.textContent='Starting update';
     setTimeout(tick,1500);
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
              "nut-driver@myups", "nut-monitor", "apc-nmc-modbus",
              "apc-nmc-firewall", "apc-nmc-dot1x", "vsftpd"):
        try:
            r = subprocess.run(["systemctl", "is-active", s],
                               capture_output=True, text=True,
                               timeout=5, check=False).stdout.strip() or "?"
            en = subprocess.run(["systemctl", "is-enabled", s],
                                capture_output=True, text=True,
                                timeout=5, check=False).stdout.strip()
        except Exception:
            r, en = "?", ""
        if r != "active" and en in ("disabled", "masked", ""):
            # opt-in service deliberately off — not a fault
            svcs += ("<tr><td class='lbl'>%s</td><td>"
                     "<span class='note'>%s</span></td></tr>"
                     % (s, esc(r if r != "?" else "not installed")))
            continue
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


def pg_403():
    return """<h1 class="page">Access Denied</h1>
<div class="card"><div class="pad">
<p>Your account's role does not allow access to this page.</p>
<p><a class="btn sec" href="/home">Return to Home</a></p></div></div>"""


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


def _txt_banner(what):
    return ("# %s — APC Network Management PI v%s\n"
            "# generated %s\n\n" % (
                what, installed_version(),
                time.strftime("%Y-%m-%d %H:%M:%S")))


def events_txt():
    lines = [_txt_banner("Event Log")]
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
    lines = ([] if csv else [_txt_banner("Data Log")])
    lines.append(sep.join(["ts", "date", "time"] + list(keys)))
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


