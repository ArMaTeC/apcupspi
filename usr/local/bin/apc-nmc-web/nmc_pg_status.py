"""Dashboard/status/control pages: pg_home, pg_status, pg_control,
outlet pages and Diagnostics — plus the tile/kv render helpers.
Loaded into the apc-nmc-web.py namespace at startup (see the
_load_parts() call near the bottom of that file). Names defined in
other parts or the main file resolve through the shared module
globals — identical semantics to the former single file."""
# Part of apc-nmc-web.py — see that file for the noqa rationale.
# ruff: noqa: UP031
# pyright: reportUndefinedVariable=false

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

    live = ups_live(d)
    charge = num(d, "battery.charge", 0) or 0
    charge_cls = ("crit" if charge < 20 else "warn" if charge < 50
                  else "") if live else ""
    loadp = num(d, "ups.load")
    watts = num(d, "ups.realpower")
    rt_sec = int(num(d, "battery.runtime", 0) or 0)
    rt_cls = "warn" if rt_sec and rt_sec < 1800 else ""
    tiles = (
        tile("Battery Charge", "%g %%" % charge if live else "?",
             "capacity" if live else "no data",
             bar=charge if live else None, cls=charge_cls,
             vid="battery.charge",
             tip="Remaining battery capacity")
        + tile("Runtime", "%dh %dm" % divmod(rt_sec // 60, 60)
               if live else "?", "remaining" if live else "no data",
               cls=rt_cls, vid="battery.runtime",
               tip="Estimated runtime at current load")
        + (tile("Load", "%g %%" % loadp, "of capacity", bar=loadp,
                vid="ups.load",
                tip="Share of the UPS rated capacity being drawn")
           if live and loadp is not None else "")
        + (tile("Output", "%g W" % watts,
                "%s VA" % d.get("ups.power", "?"), vid="ups.realpower",
                tip="Real power drawn now (W); apparent power in VA")
           if live and watts is not None else "")
        + (tile("Input", "%g V" % num(d, "input.voltage", 0),
                "%s Hz" % d.get("input.frequency", "?"),
                vid="input.voltage",
                tip="Utility mains voltage/frequency at the UPS input")
           if live and "input.voltage" in d else "")
        + (tile("Output V", "%g V" % num(d, "output.voltage", 0),
                "%s Hz" % d.get("output.frequency", "?"),
                vid="output.voltage",
                tip="Voltage/frequency the UPS feeds to the load")
           if live and "output.voltage" in d else "")
        + (tile("Battery Temp", "%g \u00b0C"
                % num(d, "battery.temperature", 0),
                "internal", vid="battery.temperature",
                tip="Internal UPS temperature — high values shorten battery life")
           if live and "battery.temperature" in d else "")
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


# Tooltips for kv() display rows, keyed by the label text. Applies to
# status/about/log tables without touching each call site.
_KV_TIPS = {
    "Last Battery Transfer": "Timestamp of the most recent switch to battery or bypass",
    "Battery Charge": "Remaining battery capacity as reported by the UPS",
    "Runtime Remaining": "Estimated battery runtime at the current load",
    "Battery Voltage": "Present battery terminal voltage",
    "Battery Temperature": "Internal battery temperature — heat shortens battery life",
    "Nominal Battery Voltage": "Rated battery pack voltage",
    "Battery Replacement": "Whether the UPS flags the battery for replacement (RB)",
    "Battery Install Date": "Recorded install/replace date — reset with 'Battery Replaced'",
    "Next Battery Maintenance": "Suggested next battery service date",
    "Battery Age": "Age since the recorded install/replace date",
    "Battery Health": "Composite 0-100 score from battery age, "
                      "replace flag, last self-test and charge",
    "Time On Battery": "How long the UPS has been on battery this "
                       "transfer",
    "UPS Alarm": "Active alarm conditions reported by the UPS",
    "Audible Alarm": "State of the UPS beeper",
    "Transfers to Battery": "How many times the UPS has switched to battery",
    "Total Time on Battery": "Cumulative time spent running on battery",
    "Average Outage": "Mean duration of recorded outages",
    "Longest Outage": "Longest single recorded outage",
    "Input Voltage": "Utility mains voltage at the UPS input",
    "Input Frequency": "Utility mains frequency in Hz",
    "Transfer Range": "Input voltage band; outside it the UPS goes on battery",
    "Last Transfer Reason": "Why the UPS last switched to battery",
    "Output Voltage": "Voltage the UPS feeds to the load",
    "Output Frequency": "Output frequency in Hz",
    "Output Load": "Percent of rated capacity being drawn",
    "Output VA": "Apparent power drawn (volt-amperes)",
    "Output Watts": "Real power drawn (watts)",
    "Load Current": "Output current in amps",
    "Output Efficiency": "Share of input power delivered to the load",
    "Output Energy Usage": "Cumulative energy delivered by the UPS",
    "Internal Temperature": "UPS internal temperature",
    "Last Test Result": "Result of the most recent battery self-test",
    "Last Test Date": "When the last self-test ran",
    "Diagnostic Schedule": "Configured weekly self-test schedule",
    "Last Calibration Date": "When the last runtime calibration ran",
    "Calibration Status": "Whether runtime calibration is currently running",
}


_KV_CPY = frozenset(("Serial Number", "UPS Identifier", "Firmware",
                     "Model", "MAC Address"))


def kv(label, val):
    t = _KV_TIPS.get(label)
    badge = ('<span class="tip" title="%s">?</span>' % esc(t)) if t else ""
    cpy = ""
    if label in _KV_CPY:
        cpy = (' <button type="button" onclick="nmcCopy(this)" '
               'data-tok="%s" title="Copy" style="background:none;'
               'border:none;cursor:pointer;padding:0 4px;font-size:12px">'
               '&#x2398;</button>' % esc(re.sub(r"<[^>]+>", "", str(val))))
    return ('<tr><td class="lbl">%s%s</td><td>%s%s</td></tr>'
            % (esc(label), badge, val, cpy))


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
    bh = battery_health(d)
    if bh is not None:
        sc, bl = bh
        col = "#3dcd58" if sc >= 80 else "#e0a800" if sc >= 50 \
            else "#b00020"
        top += kv("Battery Health",
                  '<b style="color:%s">%s</b> (%d/100)' % (col, bl, sc))
    obs = load_state().get("ob_since")
    if "OB" in toks(d) and obs:
        el = int(time.time() - obs)
        top += kv("Time On Battery", "%dh %dm" % divmod(el // 60, 60))
    if d.get("ups.alarm"):
        top += kv("UPS Alarm",
                  '<span style="color:#b00020">%s</span>'
                  % esc(humanize(d["ups.alarm"])))
    beep = (d.get("ups.beeper.status") or "").strip().lower()
    if beep == "muted":
        top += kv("Audible Alarm",
                  "Muted" + (" &mdash; re-arms when the alarm clears"
                             if st.get("beeper_muted") else ""))
    elif beep == "disabled":
        top += kv("Audible Alarm",
                  '<span style="color:#b00020">Off</span>')
    elif beep:
        top += kv("Audible Alarm", "On")
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
    m = msgbox(msg)
    batbtn = ""
    if role_post_ok(d.get("__role", "admin"), "/status/battery"):
        batbtn = ('<form method="post" action="/status/battery" '
                  'style="margin-top:14px" onsubmit="return confirm('
                  "'Mark the battery as replaced? The battery install "
                  "date will be set to today and the on-battery "
                  "statistics reset.')\">%s"
                  '<button class="btn sec" type="submit">Battery '
                  'Replaced</button></form>' % csrf_input(d))
    return """<h1 class="page">UPS Status</h1>%s
<div class="card"><h3>%s</h3><div class="pad"><table class="kv"><tbody id="stTop">%s</tbody></table>
%s</div></div>
<div class="card"><h3>UPS Input</h3><div class="pad"><table class="kv"><tbody id="stInp">%s</tbody></table></div></div>
<div class="card"><h3>UPS Output</h3><div class="pad"><table class="kv"><tbody id="stOut">%s</tbody></table></div></div>
<div id="stTimers">%s</div><div id="stEnv">%s</div>
<script>%s</script>""" % (m, esc(d.get("ups.model", "UPS")), fr["stTop"],
                batbtn, fr["stInp"], fr["stOut"], fr["stTimers"],
                fr["stEnv"], _asset("status.js"))


def pg_control(d, msg: "str | tuple" = "", cmds=None):
    m = msgbox(msg)
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
    if "beeper.enable" in can:
        opts.append(("beep_on", "Turn Audible Alarm On", 0))
    if "beeper.disable" in can:
        opts.append(("beep_off", "Turn Audible Alarm Off", 0))
    radios = "".join(
        '<label class="f"><input type="radio" name="act" value="%s"%s> '
        '%s</label>' % (v, " checked" if first else "", esc(t))
        for v, t, first in opts)
    sdel = num(d, "ups.delay.shutdown", 0) or 0
    delnote = ('<p class="note" style="margin-top:6px">Configured shutdown '
               'delay: %d s.</p>' % int(sdel)) if sdel else ""
    if not role_post_ok(d.get("__role", "admin"), "/control/ups"):
        return """<h1 class="page">UPS Control</h1>%s
<div class="card"><h3>Control the UPS</h3><div class="pad">
<p class="note">Read-only access &mdash; UPS control actions are not
available for your user role.</p>
</div></div>""" % m
    return """<h1 class="page">UPS Control</h1>%s
<div class="card"><h3>Control the UPS</h3><div class="pad">
<form method="post" action="/control/ups">
%s
%s
<label class="f" style="color:#666"><input type="checkbox" name="pcns" value="1"> Signal PowerChute Network Shutdown Clients<span class="tip" title="Tell connected PCNS clients to shut their hosts down before the UPS powers off">?</span></label>
<label class="f"><input type="checkbox" name="skipdel" value="1"> Skip outlet off delays<span class="tip" title="Ignore configured outlet shutdown delays — cuts power sooner; use only when needed">?</span></label>%s
<div style="margin-top:14px">
<button class="btn" type="submit">Next &#187;</button>
<a class="btn sec" href="/home">Cancel</a></div>
</form></div></div>""" % (m, csrf_input(d), radios, delnote)


def pg_control_confirm(d, act, skipdel, pcns):
    desc = {"reboot": "Reboot UPS", "off": "Turn UPS Off",
            "sleep": "Put UPS to Sleep", "on": "Turn UPS On",
            "bypass": "Put UPS in Bypass",
            "mute": "Mute the UPS beeper",
            "beep_on": "Turn the audible alarm on",
            "beep_off": "Turn the audible alarm off"}.get(act, act)
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
        "beep_on": "The UPS beeper will sound on alarm conditions.",
        "beep_off": "The UPS beeper will stay silent even during "
                    "alarm conditions.",
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
    m = msgbox(msg)
    groups = outlet_groups(d)
    can_control = role_post_ok(d.get("__role", "admin"), "/control/outlets")
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
        if can_control:
            ctrl = ("""<form method="post" action="/control/outlets" style="display:inline;margin-right:10px">
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
</form>""" % (
                csrf_input(d), esc(g["idx"]), csrf_input(d), esc(g["idx"]),
                "off" if on else "on", " checked" if on else "",
                "On" if on else "Off"))
        else:
            ctrl = '<span class="note">read-only</span>'
        rows += ("""<tr data-gidx="%s">
<td>%s</td><td>%s</td>
<td><span class="dot %s"></span><span class="gstat">%s</span>
<div class="note" style="font-size:12px">%s</div></td>
<td style="text-align:right;white-space:nowrap">%s</td></tr>""" % (
            esc(g["idx"]), esc(g["id"]), esc(g["name"]),
            "on" if on else "off", esc(g["status"]), esc(sub), ctrl))
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
    if cfg_rows and can_control:
        cfgcard = ("""<div class="card"><h3>Outlet Group Configuration</h3>
<div class="pad">
<form method="post" action="/config/outlets">%s
<table class="ev"><tr><th class="lbl" style="width:180px">Group</th><th>Name</th>
<th>Shutdown delay s<span class="tip" title="Wait before cutting this group's power on shutdown">?</span></th><th>Reboot delay s<span class="tip" title="Off-time when this group is cycled">?</span></th><th>Power-on delay s<span class="tip" title="Wait before restoring power — staggers inrush across groups">?</span></th></tr>
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
    m = msgbox(msg)
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
    if role_post_ok(d.get("__role", "admin"), "/test/diag"):
        selfform = ('<form method="post" action="/test/diag" '
                    'style="margin-top:14px">%s'
                    '<input type="hidden" name="test" value="self">'
                    '<button class="btn"%s>%s</button></form>' % (
                        csrf_input(d), " disabled" if testing else "",
                        "Self Test In Progress..." if testing
                        else "Initiate UPS Self Test"))
        calforms = ('<form method="post" action="/test/diag" '
                    'style="margin-top:14px;display:inline">%s'
                    '<input type="hidden" name="test" value="calstart">'
                    '<button class="btn"%s>Start Runtime Calibration'
                    '</button></form>'
                    '<form method="post" action="/test/diag" '
                    'style="display:inline">%s'
                    '<input type="hidden" name="test" value="calstop">'
                    '<button class="btn sec"%s>Stop Runtime Calibration'
                    '</button></form>' % (
                        csrf_input(d), " disabled" if cal else "",
                        csrf_input(d), "" if cal else " disabled"))
    else:
        selfform = ('<p class="note" style="margin-top:10px">Read-only '
                    'access &mdash; test actions require the device '
                    'role.</p>')
        calforms = ""
    sched = ""
    if role_post_ok(d.get("__role", "admin"), "/test/schedule"):
        wdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        def wdsel(name, cur):
            return ('<select name="%s"><option value="">disabled'
                    '</option>%s</select>' % (name, "".join(
                        '<option value="%d"%s>%s</option>' % (
                            i, " selected" if str(i) == cur else "",
                            wdays[i]) for i in range(7))))

        def hrsel(name, cur):
            return ('<select name="%s"><option value="">&mdash;</option>'
                    '%s</select>' % (name, "".join(
                        '<option value="%d"%s>%02d:00</option>' % (
                            i, " selected" if str(i) == cur else "", i)
                        for i in range(24))))

        smode = cfg("schedule", "shutdown_mode", "return")
        sched = """<div class="card"><h3>Scheduling</h3><div class="pad">
<form method="post" action="/test/schedule"><table class="kv">
<tr><td class="lbl">Weekly Self-Test<span class="tip" title="Run a battery self-test automatically on this day/hour (server local time)">?</span></td><td>%s at %s</td></tr>
<tr><td class="lbl">Next Self-Test<span class="tip" title="When the weekly self-test will next run">?</span></td><td>%s</td></tr>
</table><h4 style="margin:18px 0 8px">Scheduled UPS Power-Off</h4>
<table class="kv">
<tr><td class="lbl">When<span class="tip" title="Day and hour of the scheduled UPS power-off — once per week">?</span></td><td>%s at %s</td></tr>
<tr><td class="lbl">After Power-Off<span class="tip" title="Return when power is restored vs. stay off until manually restarted">?</span></td><td><select name="sd_mode">%s</select></td></tr>
<tr><td class="lbl">Next Run<span class="tip" title="When the scheduled power-off will next fire">?</span></td><td>%s</td></tr>
</table><p class="note" style="margin-top:8px">Fires once per matching
day. "Return when power is restored" sends shutdown.return; "stay off"
sends shutdown.stayoff &mdash; the UPS then needs a manual restart.</p>
<div style="margin-top:12px">%s
<button class="btn">Save Schedules</button></div></form></div></div>""" % (
            wdsel("st_wd", cfg("schedule", "selftest_weekday", "")),
            hrsel("st_hr", cfg("schedule", "selftest_hour", "")),
            (time.strftime("%a %d %b %H:%M", time.localtime(nsx))
             if (nsx := sched_next(
                 cfg("schedule", "selftest_weekday", ""),
                 cfg("schedule", "selftest_hour", ""))) else "&mdash;"),
            wdsel("sd_wd", cfg("schedule", "shutdown_weekday", "")),
            hrsel("sd_hr", cfg("schedule", "shutdown_hour", "")),
            (time.strftime("%a %d %b %H:%M", time.localtime(nx))
             if (nx := sched_next(
                 cfg("schedule", "shutdown_weekday", ""),
                 cfg("schedule", "shutdown_hour", ""))) else "&mdash;"),
            "".join('<option value="%s"%s>%s</option>' % (
                v, " selected" if v == smode else "", lbl)
                for v, lbl in (("return",
                                "return when power is restored"),
                               ("stayoff", "stay off"))),
            csrf_input(d))
    return """<h1 class="page">Diagnostics</h1>
<div id="pagebody">%s
<div class="card"><h3>UPS Self Test</h3><div class="pad"><table class="kv">
%s%s%s</table>
%s</div></div>
<div class="card"><h3>Runtime Calibration</h3><div class="pad"><table class="kv">
%s%s</table>%s
%s</div></div>
%s</div>
<script>var NMC_INIT={status:%s};</script><script>%s</script>""" % (
        m,
        kv("Last Test Result", esc(test_result_str(result))),
        kv("Last Test Date", esc(testdate)),
        kv("Diagnostic Schedule", esc(diag_schedule_str())),
        selfform,
        kv("Last Calibration Date", st.get("cal_date", "UNKNOWN")),
        kv("Calibration Status", "Running" if cal else "Not running"),
        req,
        calforms,
        sched,
        json.dumps(d.get("ups.status", "")), _asset("tests.js"))


