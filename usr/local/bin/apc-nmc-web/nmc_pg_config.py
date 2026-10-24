"""Configuration pages: UPS settings (pg_config), User Access
(pg_users) and System (pg_system) — plus their form helpers.
Loaded into the apc-nmc-web.py namespace at startup (see the
_load_parts() call near the bottom of that file). Names defined in
other parts or the main file resolve through the shared module
globals — identical semantics to the former single file."""
# Part of apc-nmc-web.py — see that file for the noqa rationale.
# ruff: noqa: BLE001, UP031
# pyright: reportUndefinedVariable=false

def _sessions_rows(d, cur_tok, can_drop=True):
    """Active-session table for the config page (N5). Log-off buttons
    render only for roles allowed to POST /config/sessions (admin)."""
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
        exp = time.strftime("%m/%d %H:%M", time.localtime(s["exp"]))
        ip = esc(s.get("ip") or "") or "&mdash;"
        if s.get("user"):
            ip = esc(s["user"]) + "@" + ip
        if t == cur_tok:
            ip += " <b>(this session)</b>"
            btn = ""
        elif not can_drop:
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
    m = msgbox(msg)
    lowrt_tt = int(cfg_get("low_battery_runtime",
                           int(num(d, "battery.runtime.low", 120)
                               or 120) * 100))
    sddel_tt = int(cfg_get("shutoff_delay",
                           int(num(d, "ups.delay.shutdown", 0) or 0) * 100))
    alarm = {"enabled": 1, "disabled": 3, "muted": 4}.get(
        d.get("ups.beeper.status", "enabled"), 1)
    try:
        _sup = ups_cmds()
    except Exception:
        _sup = None
    if _sup is not None and "beeper.enable" not in _sup \
            and "beeper.disable" not in _sup:
        # driver only exposes the momentary mute (or nothing): render a
        # one-click Mute button instead of a mode select. The button
        # posts its own hidden form (forms can't nest inside the card
        # form) via the HTML5 form= attribute.
        if "beeper.mute" in _sup:
            beeper_ui = ('<button form="mutebeeper" class="btn sec">'
                         'Mute Beeper</button> '
                         '<span class="note">only momentary muting is '
                         'supported by this UPS</span>')
            muteform = ('<form id="mutebeeper" method="post" '
                        'action="/config/ups" style="display:none">%s'
                        '<input type="hidden" name="alarm" value="4">'
                        '</form>' % csrf_input(d))
        else:
            beeper_ui = ('<span class="note">no beeper control on '
                         'this UPS</span>')
            muteform = ""
    else:
        beeper_ui = '<select name="alarm">%s</select>' % "".join(
            '<option value="%d"%s%s>%s%s</option>' % (
                v, " selected" if v == alarm else "",
                "" if _sup is None or c in _sup else " disabled",
                t, "" if _sup is None or c in _sup
                     else " — not supported")
            for v, t, c in [(1, "on battery / timed", "beeper.enable"),
                            (2, "at low battery", "beeper.enable"),
                            (3, "never", "beeper.disable"),
                            (4, "mute current alarm", "beeper.mute")])
        muteform = ""
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
<tr><td class="lbl">Minimum Severity<span class="tip" title="Only events at or above this level are sent out; lower ones are only logged">?</span></td><td><select name="nsev">%s</select></td></tr>
<tr><td class="lbl">Syslog Target<span class="tip" title="'local' logs to this host, host[:514] uses UDP, tcp://host:port, tls://host:port for TLS">?</span></td><td><input name="nsyslog" maxlength="64" value="%s" placeholder="local, host[:514], tcp:// or tls://host"></td></tr>
<tr><td class="lbl"></td><td><label class="f" style="font-weight:normal"><input type="checkbox" name="nsysinsecure"%s> TLS: skip certificate verification<span class="tip" title="Accept invalid/self-signed certificates — for testing only">?</span></label></td></tr>
%s%s%s%s
<tr><td colspan="2"><b>E-mail (SMTP)</b></td></tr>
<tr><td class="lbl">SMTP Host<span class="tip" title="Outgoing mail server, e.g. smtp.gmail.com or your internal relay">?</span></td><td><input name="nsmtphost" maxlength="64" value="%s" placeholder="mail.example.com"></td></tr>
<tr><td class="lbl">SMTP Port<span class="tip" title="Leave empty for auto: 465 with SSL, 587 with STARTTLS, 25 plain">?</span></td><td><input name="nsmtpport" type="number" min="1" max="65535" value="%s" placeholder="auto"></td></tr>
<tr><td class="lbl">SMTP Security<span class="tip" title="None, STARTTLS (upgrade after connect) or SSL/TLS (encrypted from the start)">?</span></td><td><select name="nsmtptls">%s</select></td></tr>
<tr><td class="lbl">SMTP User<span class="tip" title="Login name for authenticated SMTP — often the full email address">?</span></td><td><input name="nsmtpuser" maxlength="64" value="%s" autocomplete="off"></td></tr>
%s
<tr><td class="lbl">Mail From<span class="tip" title="Sender address shown on alert emails">?</span></td><td><input name="nsmtpfrom" maxlength="64" value="%s" placeholder="ups@example.com"></td></tr>
<tr><td class="lbl">Mail To<span class="tip" title="Recipients — separate multiple addresses with commas">?</span></td><td><input name="nsmtpto" maxlength="128" value="%s" placeholder="admin@example.com, oncall@example.com"></td></tr>
<tr><td colspan="2"><b>SNMP Traps</b><br><span class="mut">Send SNMPv2c
traps to a monitoring host on events — what a real NMC sends to
StruxureWare, Zabbix, snmptrapd, etc.</span></td></tr>
<tr><td class="lbl">Destination<span class="tip" title="Manager host — IP or name, optionally host:port (default 162)">?</span></td><td><input name="ntrapdest" maxlength="128" value="%s" placeholder="192.168.1.10 or zabbix.local"></td></tr>
<tr><td class="lbl">Community<span class="tip" title="v2c community string the manager expects">?</span></td><td><input name="ntrapcomm" maxlength="32" value="%s" placeholder="public"></td></tr>
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
        " checked" if nc["syslog_insecure"] else "",
        _secf("Webhook URL", "nwebhook", "webhook",
              "https://hooks.example.com/..."),
        _secf("Discord Webhook", "ndiscord", "discord",
              "https://discord.com/api/webhooks/..."),
        ('<tr><td class="lbl">Discord Ping<span class="tip" '
         'title="Mention sent only with critical alerts — @here, '
         '&lt;@userid&gt; or &lt;@&amp;roleid&gt;">?</span></td><td>'
         '<input name="ndismention" maxlength="64" value="%s" '
         'placeholder="@here or &lt;@&amp;roleid&gt; — crit only">'
         '</td></tr>' % esc(nc["discord_mention"])),
        _secf("Teams Webhook", "nteams", "teams",
              "https://outlook.office.com/webhook/..."),
        esc(nc["smtp_host"]), esc(nc["smtp_port"]), tlsopts,
        esc(nc["smtp_user"]),
        _secf("SMTP Password", "nsmtppass", "smtp_pass", "password"),
        esc(nc["smtp_from"]), esc(nc["smtp_to"]),
        esc(nc["trap_dest"]), esc(nc["trap_comm"]),
        rt_head, rt_rows, tbtns)
    # Role-aware cards: hide actions the session's role can't POST so
    # the page shows controls that actually work (device gets the UPS
    # form, network gets notifications).
    role = d.get("__role", "admin")
    if not role_post_ok(role, "/config/notify"):
        ncard = ""
    upsbtns = ('<div style="margin-top:16px"><button class="btn">Apply'
               '</button> <a class="btn sec" href="/config/ups">Cancel'
               '</a></div>'
               if role_post_ok(role, "/config/ups") else "")
    bkcard = ""
    if role == "admin":
        bkcard = """<div class="card"><h3>Backup / Restore</h3><div class="pad">
<p class="mut" style="margin-top:0">Download a backup of the card
configuration and state, or restore one. Restoring replaces
nmc.conf and card state, then restarts this interface.</p>
<a class="btn sec" href="/config/backup">Download backup</a>
<input type="file" id="bkfile" accept=".tgz,.tar.gz"
 style="margin-left:12px;font-size:13px">
<button class="btn dan" onclick="bkRestore()" style="margin-left:8px">Restore</button>
<div id="bkmsg" class="mut" style="margin-top:8px"></div>
</div></div>"""
    return """<h1 class="page">UPS Configuration</h1>%s
<div class="card"><h3>General</h3><div class="pad">%s
<form method="post" action="/config/ups"><table class="kv">
%s
<tr><td class="lbl">UPS Name<span class="tip" title="Logical UPS name in NUT — must match the entry in ups.conf (usually 'myups')">?</span></td><td><input name="ups_name" maxlength="32" value="%s"></td></tr>
<tr><td class="lbl">UPS Identifier (ups.id)<span class="tip" title="Identifier reported to clients as ups.id — informational">?</span></td><td><input name="ups_id" maxlength="16" value="%s"></td></tr>
<tr><td class="lbl">Location<span class="tip" title="Physical location; shown on the dashboard and included in notifications">?</span></td><td><input name="location" maxlength="64" value="%s"></td></tr>
<tr><td class="lbl">Contact<span class="tip" title="Responsible person; included in notification messages">?</span></td><td><input name="contact" maxlength="64" value="%s"></td></tr>
<tr><td class="lbl">Audible Alarm<span class="tip" title="Enable/disable the UPS beeper that sounds on power events">?</span></td><td>%s</td></tr>
<tr><td class="lbl">Sensitivity<span class="tip" title="How readily the UPS transfers to battery on input fluctuation — higher means more transfers">?</span></td><td><select name="sens">%s</select></td></tr>
<tr><td class="lbl">Rated Output Voltage<span class="tip" title="Nominal output voltage the UPS regulates (e.g. 230 V)">?</span></td><td><input name="ratedv" type="number" min="100" max="300" value="%s"></td></tr>
</table>
<h3 style="margin:18px 0 8px;font-size:14px">Power thresholds</h3>
<table class="kv">
<tr><td class="lbl">Low Battery Runtime (s)<span class="tip" title="When remaining battery runtime drops below this, the UPS raises LB (low battery)">?</span></td><td><input name="lowrt" type="number" min="0" max="36000" value="%s"></td></tr>
<tr><td class="lbl">Shutdown Delay (s)<span class="tip" title="Grace period between the shutdown command and the actual output cut — lets servers power off cleanly">?</span></td><td><input name="sddel" type="number" min="0" max="3600" value="%s"></td></tr>
<tr><td class="lbl">Restart Delay (s)<span class="tip" title="Wait before output returns after mains power is restored">?</span></td><td><input name="rdel" type="number" min="0" max="3600" value="%s"></td></tr>
<tr><td class="lbl">Reboot Delay (s)<span class="tip" title="How long output stays off during a reboot cycle">?</span></td><td><input name="rbdel" type="number" min="0" max="3600" value="%s"></td></tr>
<tr><td class="lbl">Low Battery Charge (%%)<span class="tip" title="Charge level at which the low-battery warning is raised">?</span></td><td><input name="lowchg" type="number" min="0" max="100" value="%s"></td></tr>
<tr><td class="lbl">Transfer Voltage Low (VAC)<span class="tip" title="Below this input voltage the UPS transfers to battery">?</span></td><td><input name="trlow" type="number" min="150" max="300" value="%s"></td></tr>
<tr><td class="lbl">Transfer Voltage High (VAC)<span class="tip" title="Above this input voltage the UPS transfers to battery">?</span></td><td><input name="trhigh" type="number" min="150" max="300" value="%s"></td></tr>
</table>
%s
</form></div></div>
%s
%s
<script>var NMC_INIT={csrf:%s};</script><script>%s</script>
<p class="note">Values the UPS can store are written to it via NUT SET VAR;
the rest are kept on the management card.</p>""" % (
        m, muteform, csrf_input(d),
        g("ups_name", "APC Smart-UPS"),
        esc(d.get("ups.id", "")),
        g("location", LOCATION), g("contact", CONTACT), beeper_ui, sensopts,
        ratedv,
        lowrt_tt // 100, sddel_tt // 100,
        esc(d.get("ups.delay.start", "0")),
        esc(d.get("ups.delay.reboot", "8")),
        g("min_return_capacity", num(d, "battery.charge.low", 10) or 10),
        esc(d.get("input.transfer.low", "")),
        esc(d.get("input.transfer.high", "")),
        upsbtns,
        ncard,
        bkcard,
        json.dumps(d.get("__csrf", "")), _asset("config.js"))


def _sel(name, cur, opts):
    o = "".join('<option value="%s"%s>%s</option>'
                % (v, " selected" if v == cur else "", esc(t))
                for v, t in opts)
    return '<select name="%s">%s</select>' % (name, o)


def _secf(name, cur, ph):
    badge = (' <span class="note">(configured)</span>'
             ' <label class="f" style="font-weight:normal">'
             '<input type="checkbox" name="clr_%s"> clear</label>'
             % name) if cur else ""
    return ('<input name="%s" type="password" autocomplete="off" '
            'maxlength="128" placeholder="%s">%s'
            % (name, esc(ph), badge))


def pg_users(d, msg: "str | tuple" = "", cur_tok=""):
    """User Access page: accounts, per-role permission matrix, RADIUS,
    active sessions and the self-service password form."""
    m = msgbox(msg)
    role = d.get("__role", "admin")
    admin = role == "admin"
    csrf = csrf_input(d)
    can_acc = role_post_ok(role, "/config/users")
    can_rad = role_post_ok(role, "/config/radius")
    can_sess = role_post_ok(role, "/config/sessions")

    # ---- user accounts -------------------------------------------------
    urows = ('<tr><td>%s</td><td>admin</td><td>'
             '<span class="note">primary account &mdash; password '
             'below</span></td></tr>' % esc(WEB_USER))
    if _cp.has_section("users"):
        for name in sorted(set(_cp.options("users"))
                           - set(_cp.defaults())):
            urole, _cred = _parse_user_cred(_cp.get("users", name))
            rm = ""
            if can_acc:
                rm = ('<form method="post" action="/config/users" '
                      'style="display:inline">%s'
                      '<input type="hidden" name="act" value="del">'
                      '<input type="hidden" name="uname" value="%s">'
                      '<button class="btn sec">Remove</button></form>'
                      % (csrf, esc(name)))
            urows += ("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                      % (esc(name), esc(urole), rm))
    addform = ""
    if can_acc:
        addform = """<form method="post" action="/config/users"><table class="kv">
%s<input type="hidden" name="act" value="add">
<tr><td class="lbl">User Name<span class="tip" title="Login name for the web UI — local account, separate from RADIUS">?</span></td><td><input name="uname" maxlength="32" required></td></tr>
<tr><td class="lbl">Password<span class="tip" title="Minimum 6 characters; stored as a PBKDF2 hash">?</span></td><td><input name="upass" type="password" minlength="6" autocomplete="new-password" required></td></tr>
<tr><td class="lbl">Role<span class="tip" title="Account group — its permissions come from the matrix below">?</span></td><td>%s</td></tr>
</table><div style="margin-top:12px"><button class="btn">Add User</button></div>
</form>""" % (csrf, _sel("urole", "readonly",
                       [(r, r.capitalize()) for r in USER_ROLES]))
    ucard = """<div class="card"><h3>User Accounts</h3><div class="pad">
<p class="mut" style="margin-top:0">Each account belongs to a role; the
role's capabilities are set in the Access Permissions matrix below.
The primary <b>%s</b> account is always Administrator.</p>
<table class="kv pgtbl"><tr><th style="text-align:left">User</th>
<th style="text-align:left">Role</th><th></th></tr>%s</table>
%s</div></div>""" % (esc(WEB_USER), urows, addform)

    # ---- access permissions matrix -------------------------------------
    chead = "".join('<th title="%s">%s</th>'
                    % (esc(", ".join(posts)), esc(lab))
                    for _, (lab, posts) in ROLE_CAPS.items())
    mrows = ('<tr><td><b>Administrator</b></td>%s</tr>'
             % "".join('<td><input type="checkbox" checked disabled '
                       'title="Administrator always has full access">'
                       '</td>' for _ in ROLE_CAPS))
    for r in USER_ROLES[1:]:
        cells = "".join(
            '<td><input type="checkbox" name="cap_%s_%s"%s%s></td>'
            % (r, c,
               " checked" if c in role_caps(r) else "",
               "" if admin else " disabled")
            for c in ROLE_CAPS)
        mrows += '<tr><td><b>%s</b></td>%s</tr>' % (r.capitalize(), cells)
    mbtns = ('<div style="margin-top:14px"><button class="btn">Apply'
             '</button></div>' if admin else
             '<p class="note" style="margin-top:10px">Only administrator '
             'accounts can change permissions.</p>')
    mcard = """<div class="card"><h3>Access Permissions</h3><div class="pad">
<p class="mut" style="margin-top:0">Which actions each account group may
run. Every signed-in user can view all pages and change their own
password &mdash; the matrix controls the write actions.<span class="tip" title="Hover a column header to see the exact paths it unlocks">?</span></p>
<form method="post" action="/config/roles"><table class="ev">
%s
<tr><th style="text-align:left">Role</th>%s</tr>
%s</table>
%s</form></div></div>""" % (csrf, chead, mrows, mbtns)

    # ---- RADIUS --------------------------------------------------------
    r_en = cfg("radius", "enabled", "0") in ("1", "yes", "true", "on")
    rform = ""
    if can_rad:
        rform = """<form method="post" action="/config/radius"><table class="kv">
%s
<tr><td class="lbl">Enabled<span class="tip" title="When on, logins are checked against RADIUS first, then local accounts">?</span></td><td><input type="checkbox" name="r_en"%s></td></tr>
<tr><td class="lbl">Server</td><td><input name="r_srv" maxlength="64" value="%s" placeholder="radius.example.com"></td></tr>
<tr><td class="lbl">Port<span class="tip" title="RADIUS authentication port — usually 1812">?</span></td><td><input name="r_port" type="number" min="1" max="65535" value="%s"></td></tr>
<tr><td class="lbl">Shared Secret<span class="tip" title="Secret shared with the RADIUS server — keep it confidential">?</span></td><td>%s</td></tr>
<tr><td class="lbl">Timeout (s)<span class="tip" title="Seconds to wait for a RADIUS reply before falling back to local auth">?</span></td><td><input name="r_to" type="number" min="1" max="30" step="0.5" value="%s"></td></tr>
<tr><td class="lbl">Assigned Role<span class="tip" title="Role granted to users who authenticate via RADIUS">?</span></td><td>%s</td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form>""" % (
            csrf, " checked" if r_en else "",
            esc(cfg("radius", "server", "")),
            esc(cfg("radius", "port", "1812")),
            _secf("r_secret", cfg("radius", "secret", ""),
                  "shared secret"),
            esc(cfg("radius", "timeout", "4")),
            _sel("r_role", cfg("radius", "role", "readonly").lower(),
                 [(r, r.capitalize()) for r in USER_ROLES]))
    else:
        rform = ('<p class="note">Only accounts with the Accounts '
                 'capability can change this section.</p>')
    rcard = """<div class="card"><h3>RADIUS Authentication</h3><div class="pad">
<p class="mut" style="margin-top:0">Fallback authentication for web
logins &mdash; tried only when no local account matches (PAP over
UDP/1812). RADIUS users get the configured role.
Status: <b>%s</b></p>%s</div></div>""" % (
        "enabled" if r_en else "disabled", rform)

    # ---- sessions + self-service password ------------------------------
    othersform = ""
    if can_sess:
        othersform = ('<form method="post" action="/config/sessions" '
                      'style="margin-top:12px">%s'
                      '<input type="hidden" name="act" value="others">'
                      '<button class="btn sec">Log off all other '
                      'sessions</button></form>' % csrf)
    scard = """<div class="card"><h3>Active Sessions</h3><div class="pad">
<table class="kv pgtbl"><tr><th style="text-align:left">Client</th>
<th style="text-align:left">Started</th><th style="text-align:left">Expires</th>
<th></th></tr>
%s
</table>
%s
</div></div>""" % (_sessions_rows(d, cur_tok, can_sess), othersform)
    pcard = """<div class="card"><h3>Change Password</h3><div class="pad">
<form method="post" action="/config/security"><table class="kv">
%s
<tr><td class="lbl">Current Password<span class="tip" title="Your own current password — changes your account only">?</span></td><td><input name="curp" type="password" autocomplete="current-password"></td></tr>
<tr><td class="lbl">New Password<span class="tip" title="Minimum 6 characters; stored as a hash in nmc.conf">?</span></td><td><input name="newp" type="password" minlength="6" autocomplete="new-password"></td></tr>
<tr><td class="lbl">Confirm New Password</td><td><input name="cfmp" type="password" autocomplete="new-password"></td></tr>
</table>
<div style="margin-top:16px"><button class="btn">Change Password</button></div>
</form></div></div>""" % csrf

    return """<h1 class="page">User Access</h1>%s
%s%s%s%s%s""" % (m, ucard, mcard, rcard, scard, pcard)


def pg_system(d, msg: "str | tuple" = ""):
    """System & network configuration: Modbus TCP, FTP, MQTT, firewall,
    802.1X, WiFi and secure-boot status."""
    m = msgbox(msg)
    admin = d.get("__role", "admin") == "admin"
    csrf = csrf_input(d)

    def rof(path):
        """(open, close) of a disabled fieldset for cards whose POST
        target the session role can't reach — the whole card renders
        but every input/button inside is visibly disabled."""
        if role_post_ok(d.get("__role", "admin"), path):
            return "", ""
        return (('<fieldset disabled style="border:0;margin:0;'
                 'padding:0;min-width:0">'), '</fieldset>')

    sel = _sel
    secf = _secf

    # ---- network services ----------------------------------------------
    mb_en, mb_act = svc_state("apc-nmc-modbus.service")
    ftp_en, ftp_act = svc_state("vsftpd.service")
    ftppw = ""
    try:
        with open("/etc/apc-nmc/ftp-password") as f:
            ftppw = f.read().strip()
    except OSError:
        pass
    _h = int(cfg_get("datalog_hours", 25))
    dl_use = (720 if _h % 720 == 0 else 168 if _h % 168 == 0 else
              24 if _h % 24 == 0 else 1)
    dl_ret_disp = _h // dl_use
    dl_unit_sel = "".join(
        '<option value="%d"%s>%s</option>' % (
            mul, " selected" if mul == dl_use else "", lbl)
        for mul, lbl in ((1, "hours"), (24, "days"),
                         (168, "weeks"), (720, "months")))
    so, sc = rof("/config/services")
    scard = """<div class="card"><h3>Network Services</h3><div class="pad">%s
<h4 style="margin:0 0 8px">Modbus TCP (BMS integration)</h4>
<p class="mut" style="margin-top:0">Serves the documented UPS register
map (docs/modbus-registers.md) &mdash; status bits as discrete inputs,
readings as input registers. Status: <b>%s / %s</b></p>
<form method="post" action="/config/services">
%s<input type="hidden" name="svc" value="modbus"><table class="kv">
<tr><td class="lbl">Enabled<span class="tip" title="Serve UPS registers to BMS/SCADA clients over Modbus TCP">?</span></td><td><input type="checkbox" name="mb_en"%s></td></tr>
<tr><td class="lbl">Bind Address<span class="tip" title="Local address to listen on — 0.0.0.0 means all interfaces">?</span></td><td><input name="mb_bind" maxlength="40" value="%s" placeholder="0.0.0.0"></td></tr>
<tr><td class="lbl">Port<span class="tip" title="Modbus TCP port — 502 is the standard; open it in the firewall">?</span></td><td><input name="mb_port" type="number" min="1" max="65535" value="%s"></td></tr>
<tr><td class="lbl">Unit Identifier<span class="tip" title="Modbus unit/slave ID the client must address — 1..247">?</span></td><td><input name="mb_unit" type="number" min="1" max="247" value="%s"></td></tr>
<tr><td class="lbl">Write Access</td><td><label class="f" style="font-weight:normal"><input type="checkbox" name="mb_write"%s> allow UPS commands via the command register/coils<span class="tip" title="Lets Modbus clients send UPS commands (tests, shutdown) — enable only on trusted networks">?</span></label></td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form>
<h4 style="margin:22px 0 8px">FTP Server</h4>
<p class="mut" style="margin-top:0">vsftpd serving a chrooted
<code>nmcftp</code> account at <code>/var/lib/apc-nmc/ftp</code>.
Status: <b>%s / %s</b>%s</p>
<form method="post" action="/config/services">
%s<input type="hidden" name="svc" value="ftp"><table class="kv">
<tr><td class="lbl">Enabled<span class="tip" title="Chrooted vsftpd for file transfer (e.g. pulling config/events) — enable only when needed">?</span></td><td><input type="checkbox" name="ftp_en"%s></td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form>
<h4 style="margin:22px 0 8px">MQTT Telemetry</h4>
<p class="mut" style="margin-top:0">Publishes the key UPS readings as a
JSON document to <code>%s/state</code> every interval &mdash; for
Home Assistant, Node-RED and similar consumers.%s</p>
<form method="post" action="/config/services">
%s<input type="hidden" name="svc" value="mqtt"><table class="kv">
<tr><td class="lbl">Enabled<span class="tip" title="Publish UPS state to an MQTT broker periodically">?</span></td><td><input type="checkbox" name="mq_en"%s></td></tr>
<tr><td class="lbl">Broker Host<span class="tip" title="MQTT broker address, e.g. your Home Assistant or BMS broker">?</span></td><td><input name="mq_host" maxlength="128" value="%s" placeholder="mqtt.local"></td></tr>
<tr><td class="lbl">Port<span class="tip" title="1883 for plain MQTT, 8883 for TLS">?</span></td><td><input name="mq_port" type="number" min="1" max="65535" value="%s"></td></tr>
<tr><td class="lbl">TLS</td><td><label class="f" style="font-weight:normal"><input type="checkbox" name="mq_tls"%s> use TLS (port 8883)<span class="tip" title="Encrypt the broker connection — enable for internet-facing brokers">?</span></label>
 <label class="f" style="font-weight:normal"><input type="checkbox" name="mq_insecure"%s> skip certificate verification</label></td></tr>
<tr><td class="lbl">Username<span class="tip" title="Broker login — leave blank for anonymous brokers">?</span></td><td><input name="mq_user" maxlength="64" value="%s"></td></tr>
<tr><td class="lbl">Password<span class="tip" title="Broker password — blank keeps the stored value">?</span></td><td><input name="mq_pass" type="password" maxlength="128" value="" placeholder="unchanged" autocomplete="off"></td></tr>
<tr><td class="lbl">Topic Prefix<span class="tip" title="State JSON is published to &lt;prefix&gt;/state">?</span></td><td><input name="mq_topic" maxlength="96" value="%s" placeholder="apc-nmc/myups"></td></tr>
<tr><td class="lbl">Interval (s)<span class="tip" title="Seconds between state publishes — 15 to 86400">?</span></td><td><input name="mq_interval" type="number" min="15" max="86400" value="%s"></td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button>
<button class="btn sec" name="mq_test" value="1" style="margin-left:8px">Save &amp; Test Publish</button></div>
</form>%s
<h4 style="margin:22px 0 8px">Data Logging</h4>
<p class="mut" style="margin-top:0">Power-metrics history driving the
Data Log history and the energy stats &mdash; one sample per minute.</p>
<form method="post" action="/config/datalog">
%s<table class="kv">
<tr><td class="lbl">Retention<span class="tip" title="Samples older than this are dropped — pick a value and unit; longer spans increase chart history but SD wear">?</span></td><td><input name="dl_ret" type="number" min="1" max="8760" value="%s" style="width:90px"> <select name="dl_unit">%s</select></td></tr>
<tr><td class="lbl">Write Batch<span class="tip" title="Samples buffered in RAM before each SD flush — higher means fewer card writes">?</span></td><td><input name="dl_batch" type="number" min="1" max="200" value="%s"></td></tr>
<tr><td class="lbl">Energy Rate<span class="tip" title="Cost per kWh — enables the Est. Cost rows in the Data Log energy stats; 0 disables">?</span></td><td><input name="dl_rate" type="number" step="0.0001" min="0" max="100" value="%s"></td></tr>
<tr><td class="lbl">Currency<span class="tip" title="Symbol shown before the estimate, e.g. $ &euro; &pound;">?</span></td><td><input name="dl_cur" maxlength="4" value="%s" placeholder="&euro;"></td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form></div></div>""" % (
        so,
        "enabled" if mb_en else "disabled",
        "active" if mb_act else "stopped",
        csrf, " checked" if mb_en else "",
        esc(cfg("modbus", "bind", "0.0.0.0")),
        esc(cfg("modbus", "port", "502")),
        esc(cfg("modbus", "unit", "1")),
        " checked" if cfg("modbus", "allow_write", "0")
        in ("1", "yes", "true", "on") else "",
        "enabled" if ftp_en else "disabled",
        "active" if ftp_act else "stopped",
        ("<br>Login: <code>nmcftp</code> &mdash; password <code>%s</code>"
         % esc(ftppw)) if ftppw and admin else "",
        csrf, " checked" if ftp_en else "",
        esc(mqtt_cfg()["topic"]),
        ("<br>Last publish: %s"
         % (("<b>ok</b> %ds ago" % (int(time.time())
                                   - _mqtt_state["at"]))
            if _mqtt_state["ok"] else
            "<span class='st-warn'>%s</span>" % esc(
                _mqtt_state["err"] or "never"))),
        csrf,
        " checked" if mqtt_cfg()["enabled"] else "",
        esc(mqtt_cfg()["host"]), esc(mqtt_cfg()["port"]),
        " checked" if mqtt_cfg()["tls"] else "",
        " checked" if mqtt_cfg()["insecure"] else "",
        esc(mqtt_cfg()["user"]), esc(mqtt_cfg()["topic"]),
        esc(str(mqtt_cfg()["interval"])),
        sc,
        csrf,
        esc(str(dl_ret_disp)), dl_unit_sel,
        esc(str(cfg_get("datalog_batch", 15))),
        esc(cfg("device", "energy_rate", "0")),
        esc(cfg("device", "energy_currency", "&euro;")))

    # ---- firewall -------------------------------------------------------
    fw_en, fw_act = svc_state("apc-nmc-firewall.service")
    fo, fc = rof("/config/firewall")
    fwcard = """<div class="card"><h3>Firewall</h3><div class="pad">%s
<p class="mut" style="margin-top:0">nftables input filter. SSH (22),
HTTP/S (80/443), SNMP (161) and mDNS (5353) are always allowed; list
extra ports to open and, optionally, the only CIDRs allowed to reach
any service. Status: <b>%s / %s</b></p>
<form method="post" action="/config/firewall"><table class="kv">
%s
<tr><td class="lbl">Enabled<span class="tip" title="Apply nftables rules — only listed ports stay reachable; be careful not to lock yourself out">?</span></td><td><input type="checkbox" name="fw_en"%s></td></tr>
<tr><td class="lbl">Extra TCP Ports<span class="tip" title="Additional TCP ports to allow, space-separated (SSH/HTTP/HTTPS/NUT are always open)">?</span></td><td><input name="fw_tcp" maxlength="128" value="%s" placeholder="502 3493"></td></tr>
<tr><td class="lbl">Extra UDP Ports<span class="tip" title="Additional UDP ports to allow, space-separated">?</span></td><td><input name="fw_udp" maxlength="128" value="%s" placeholder=""></td></tr>
<tr><td class="lbl">Allowed Sources<span class="tip" title="Restrict access to these CIDR ranges — empty means any source">?</span></td><td><input name="fw_from" maxlength="256" value="%s" placeholder="192.168.1.0/24 10.0.0.5/32 (empty = any)"></td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form>%s</div></div>""" % (
        fo,
        "enabled" if fw_en else "disabled",
        "active" if fw_act else "stopped",
        csrf, " checked" if fw_en else "",
        esc(cfg("firewall", "allow_tcp", "")),
        esc(cfg("firewall", "allow_udp", "")),
        esc(cfg("firewall", "allow_from", "")), fc)

    # ---- 802.1X ---------------------------------------------------------
    dx_en, dx_act = svc_state("apc-nmc-dot1x.service")
    dx_if = cfg("dot1x", "iface", "eth0")
    wifaces = net_ifaces(("e",))
    if dx_if not in wifaces:
        wifaces.append(dx_if)
    xo, xc = rof("/config/dot1x")
    dxcard = """<div class="card"><h3>Wired Port Authentication \
(802.1X)</h3><div class="pad">%s
<p class="mut" style="margin-top:0">Ethernet port authentication for
managed switches that demand EAP credentials before allowing traffic
(not related to WiFi — leave disabled on a normal LAN). Status:
<b>%s / %s</b></p>
<form method="post" action="/config/dot1x"><table class="kv">
%s
<tr><td class="lbl">Enabled<span class="tip" title="Authenticate the Ethernet port on 802.1X-protected switches (not WiFi)">?</span></td><td><input type="checkbox" name="x_en"%s></td></tr>
<tr><td class="lbl">Interface<span class="tip" title="Wired interface that needs port authentication — usually eth0">?</span></td><td>%s</td></tr>
<tr><td class="lbl">EAP Method<span class="tip" title="PEAP/TTLS use a password; TLS uses a client certificate">?</span></td><td>%s</td></tr>
<tr><td class="lbl">Identity<span class="tip" title="Username presented to the authenticator (RADIUS/NAC)">?</span></td><td><input name="x_id" maxlength="64" value="%s"></td></tr>
<tr><td class="lbl">Password (PEAP)<span class="tip" title="EAP password for PEAP/TTLS — not needed for EAP-TLS">?</span></td><td>%s</td></tr>
<tr><td class="lbl">Phase 2<span class="tip" title="Inner method for PEAP/TTLS, e.g. auth=MSCHAPV2">?</span></td><td><input name="x_ph2" maxlength="64" value="%s" placeholder="auth=MSCHAPV2"></td></tr>
<tr><td class="lbl">CA Certificate<span class="tip" title="Path to the CA cert used to verify the authentication server">?</span></td><td><input name="x_ca" maxlength="128" value="%s" placeholder="/etc/apc-nmc/ca/ca.crt"></td></tr>
<tr><td class="lbl">Client Certificate (TLS)<span class="tip" title="Client cert path — required only for EAP-TLS">?</span></td><td><input name="x_cc" maxlength="128" value="%s"></td></tr>
<tr><td class="lbl">Private Key (TLS)<span class="tip" title="Client private key path — required only for EAP-TLS">?</span></td><td><input name="x_pk" maxlength="128" value="%s"></td></tr>
<tr><td class="lbl">Key Password<span class="tip" title="Password protecting the private key, if any">?</span></td><td>%s</td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form>%s</div></div>""" % (
        xo,
        "enabled" if dx_en else "disabled",
        "active" if dx_act else "stopped",
        csrf, " checked" if dx_en else "",
        sel("x_if", dx_if, [(i, i) for i in wifaces]),
        sel("x_eap", cfg("dot1x", "eap", "PEAP").upper(),
            [(e, e) for e in ("PEAP", "TTLS", "TLS", "FAST")]),
        esc(cfg("dot1x", "identity", "")),
        secf("x_pass", cfg("dot1x", "password", ""), "password"),
        esc(cfg("dot1x", "phase2", "")),
        esc(cfg("dot1x", "ca_cert", "")),
        esc(cfg("dot1x", "client_cert", "")),
        esc(cfg("dot1x", "private_key", "")),
        secf("x_pkpass", cfg("dot1x", "private_key_pass", ""),
             "key password"), xc)

    # ---- wifi setup ----------------------------------------------------
    # Boot-config radios (dtoverlay=disable-*) — visible in every branch
    # since a boot-disabled radio is exactly when no wlan iface exists.
    hw = wifi_hw_status()
    pend = boot_cfg_pending()
    wlifaces = net_ifaces(("wl",))
    wradio, wconn, has_nm = wifi_status()
    try:
        bt_live = any(i.startswith("hci")
                      for i in os.listdir("/sys/class/bluetooth"))
    except OSError:
        bt_live = False
    hrows = ""
    for radio, label in (("wifi", "WiFi"), ("bt", "Bluetooth")):
        dis = hw[radio]
        live = bool(wlifaces) if radio == "wifi" else bt_live
        if dis:
            st = ('<span class="st-warn">disabled &mdash; pending '
                  'reboot</span>' if live
                  else '<span class="st-warn">disabled at boot</span>')
        else:
            st = "enabled"
            if not live and pend:
                st = ('<span class="st-warn">enabled &mdash; pending '
                      'reboot</span>')
        btn = ""
        if admin:
            conf = "" if dis else (
                ' onsubmit="return confirm('
                "'Disabling onboard %s takes effect at reboot and can "
                "cut access if this card reaches you over that radio. "
                "Continue?')" % label)
            btn = ('<form method="post" action="/config/wifi" '
                   'style="display:inline"%s>%s'
                   '<input type="hidden" name="act" value="%s">'
                   '<input type="hidden" name="hw" value="%s">'
                   '<button class="btn sec" type="submit">%s</button>'
                   '</form>'
                   % (conf, csrf, "hw_enable" if dis else "hw_disable",
                      radio, "Enable" if dis else "Disable"))
        hrows += ('<tr><td class="lbl">Onboard %s<span class="tip" '
                  'title="Boot-level radio switch (dtoverlay) — a reboot is '
                  'required for changes to take effect">?</span></td>'
                  '<td>%s</td>'
                  '<td style="text-align:right">%s</td></tr>'
                  % (label, st, btn))
    if hw["cfg"]:
        hwsec = ('<h4 style="margin:22px 0 8px">Wireless Hardware '
                 '(boot config)</h4><table class="kv">%s</table>'
                 '<p class="note" style="margin-top:8px">Boot-config '
                 'changes apply at next boot &mdash; the installer '
                 'hardening option writes these overlays.%s</p>'
                 % (hrows,
                    ' <span class="st-warn">Reboot pending.</span>'
                    if pend else ""))
        if admin:
            hwsec += ('<form method="post" action="/config/wifi" '
                      'onsubmit="return confirm('
                      "'Reboot the card now to apply boot-config "
                      "changes?')\">%s"
                      '<input type="hidden" name="act" value="reboot">'
                      '<button class="btn dan" type="submit">Reboot Now'
                      '</button></form>' % csrf)
    else:
        hwsec = ('<p class="note" style="margin-top:18px">Boot config '
                 '(config.txt) not found &mdash; onboard radio toggles '
                 'unavailable.</p>')
    if not has_nm:
        wcard = """<div class="card"><h3>WiFi Setup</h3><div class="pad">
<p class="note">NetworkManager (nmcli) is not installed — onboard WiFi
setup is unavailable.</p>%s</div></div>""" % hwsec
    elif not wlifaces:
        wcard = """<div class="card"><h3>WiFi Setup</h3><div class="pad">
<p class="note">No wireless interface detected.</p>%s</div></div>""" \
            % hwsec
    else:
        wo, wc = rof("/config/wifi")
        wcard = """<div class="card"><h3>WiFi Setup</h3><div class="pad">%s
<p class="mut" style="margin-top:0">Onboard wireless (Pi 3/4/5) via
NetworkManager. Radio: <b>%s</b>%s</p>
<div style="margin-bottom:14px;display:flex;gap:10px;flex-wrap:wrap">
<form method="post" action="/config/wifi">%s
<input type="hidden" name="act" value="%s">
<button class="btn sec" type="submit">%s</button></form>%s</div>
<form method="post" action="/config/wifi"><table class="kv">
%s<input type="hidden" name="act" value="connect">
<tr><td class="lbl">Interface<span class="tip" title="Wireless interface — usually wlan0">?</span></td><td>%s</td></tr>
<tr><td class="lbl">Network (SSID)<span class="tip" title="WiFi network to join — use Scan to pick from detected networks">?</span></td><td>
<input name="w_ssid" id="wssid" list="wlist" maxlength="32" required>
<datalist id="wlist"></datalist>
<button class="btn sec" type="button" id="wscanbtn">Scan</button>
<span class="note" id="wscanmsg"></span></td></tr>
<tr><td class="lbl">Password<span class="tip" title="WPA passphrase — leave blank only for open networks">?</span></td><td><input name="w_psk" type="password" autocomplete="off" maxlength="64" placeholder="blank for open networks"></td></tr>
</table><div style="margin-top:12px"><button class="btn" type="submit">Connect</button></div></form>
<script>
(function(){var b=document.getElementById('wscanbtn');
if(!b)return;
b.onclick=function(){var msg=document.getElementById('wscanmsg');
msg.textContent='scanning\\u2026';
fetch('/config/wifi/scan?if='+encodeURIComponent(
  document.getElementById('w_if').value))
.then(function(r){return r.json()})
.then(function(ns){var l=document.getElementById('wlist');
 l.innerHTML='';ns.forEach(function(n){var o=document.createElement('option');
 o.value=n.ssid;o.label=n.signal+'%% '+(n.security||'open');
 l.appendChild(o)});
 msg.textContent=ns.length?ns.length+' network(s)':'none found';
 document.getElementById('wssid').focus()})
.catch(function(){msg.textContent='scan failed'})}})();
</script>%s%s</div></div>""" % (
            wo,
            esc(wradio),
            (" &mdash; connected to <b>%s</b>" % esc(wconn)) if wconn else "",
            csrf,
            "off" if wradio == "enabled" else "on",
            "Disable WiFi" if wradio == "enabled"
            else "Activate WiFi Setup",
            ('<form method="post" action="/config/wifi">%s'
             '<input type="hidden" name="act" value="disconnect">'
             '<input type="hidden" name="w_if" value="%s">'
             '<button class="btn sec">Disconnect</button></form>'
             % (csrf, esc(wlifaces[0]))) if wconn else "",
            csrf,
            ('<select name="w_if" id="w_if">%s</select>' % "".join(
                '<option value="%s"%s>%s</option>'
                % (i, " selected" if i == wlifaces[0] else "", esc(i))
                for i in wlifaces)),
            hwsec, wc)

    sb = secure_boot_status()
    sbcard = """<div class="card"><h3>Secure Boot</h3><div class="pad">
<table class="kv"><tr><td class="lbl">Status</td><td>%s</td></tr></table>
<p class="note">Signed boot on Raspberry Pi requires one-time eeprom/OTP
provisioning by hand &mdash; see docs/secure-boot.md. It cannot be
enabled from this interface.</p></div></div>""" % esc(sb)

    # ---- time & date ---------------------------------------------------
    _rc, tzs = _run(["timedatectl", "show", "-p", "Timezone", "-p",
                     "NTP", "-p", "NTPSynchronized"], 10)
    tcur = ntp_on = ntp_ok = ""
    for ln in tzs.splitlines():
        if ln.startswith("Timezone="):
            tcur = ln.split("=", 1)[1]
        elif ln.startswith("NTP="):
            ntp_on = ln.split("=", 1)[1] in ("yes", "true")
        elif ln.startswith("NTPSynchronized="):
            ntp_ok = ("synchronized" if ln.endswith("=yes")
                      else "not synchronized")
    tzo, tzc = rof("/config/time")
    tcard = """<div class="card"><h3>Time &amp; Date</h3><div class="pad">%s
<p class="mut" style="margin-top:0">Card clock — drives the logs,
schedules and certificate validity. NTP sync: <b>%s</b></p>
<form method="post" action="/config/time"><table class="kv">
%s
<tr><td class="lbl">Timezone<span class="tip" title="IANA zone name (e.g. Europe/London) — affects log timestamps and weekly schedules">?</span></td><td><input name="tz" list="tzlist" maxlength="64" value="%s">
<datalist id="tzlist">%s</datalist></td></tr>
<tr><td class="lbl">NTP Sync<span class="tip" title="Synchronise the clock from network time servers (systemd-timesyncd)">?</span></td><td><label class="f" style="font-weight:normal"><input type="checkbox" name="ntp" value="1"%s> enabled</label></td></tr>
</table><div style="margin-top:12px"><button class="btn">Apply</button></div>
</form>%s</div></div>""" % (
        tzo, esc(ntp_ok or "?"), csrf_input(d), esc(tcur),
        "".join('<option value="%s">' % esc(z) for z in timezones()),
        " checked" if ntp_on else "", tzc)

    return """<h1 class="page">System Configuration</h1>%s
%s%s%s%s%s%s""" % (m, scard, fwcard, dxcard, wcard, sbcard, tcard)


