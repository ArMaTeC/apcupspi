"""UPS action handlers: control/outlet commands, self-test,
schedule, battery-replacement marker and config restore.
Loaded into the apc-nmc-web.py namespace at startup (see the
_load_parts() call near the bottom of that file). Names defined in
other parts or the main file resolve through the shared module
globals — identical semantics to the former single file."""
# Part of apc-nmc-web.py — see that file for the noqa rationale.
# ruff: noqa: BLE001, S110, UP031

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
    if act in ("beep_on", "beep_off"):
        r = ups_cmd("beeper.%s" % ("enable" if act == "beep_on"
                                   else "disable"))
        if r.startswith("OK"):
            log_event("UPS: Audible alarm turned %s via the network "
                      "management card."
                      % ("on" if act == "beep_on" else "off"))
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


def do_restore(blob, user=None):
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
        # the snapshot holds the pre-restore secrets — match nmc.conf's
        # owner/mode so the agent can read conf but the world cannot
        try:
            st = os.stat(CONF)
            os.chown(CONF + ".pre-restore", -1, st.st_gid)
            os.chmod(CONF + ".pre-restore", st.st_mode & 0o777)
        except OSError:
            os.chmod(CONF + ".pre-restore", 0o640)
    except OSError:
        pass
    with open(CONF, "wb") as f:
        f.write(data["nmc.conf"])
    if "state.json" in data:
        _atomic_json(STATE_FILE, lambda o: json.loads(data["state.json"]))
    if "events.json" in data:
        _atomic_json(EVENTS_FILE, lambda o: json.loads(data["events.json"]))
    log_event("NMC: Card configuration restored from backup by %s."
              % (user or WEB_USER), "warn")
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


def do_schedule(form):
    """Save weekly self-test + UPS power-off schedules
    ([schedule] in nmc.conf; empty weekday/hour = disabled)."""
    for fkey, ckey in (("st_wd", "selftest_weekday"),
                       ("sd_wd", "shutdown_weekday")):
        v = form.get(fkey, [""])[0].strip()
        if v and not re.fullmatch(r"[0-6]", v):
            return ("msg-err", "weekday: invalid")
        conf_set("schedule", ckey, v)
    for fkey, ckey in (("st_hr", "selftest_hour"),
                       ("sd_hr", "shutdown_hour")):
        v = form.get(fkey, [""])[0].strip()
        if v and not re.fullmatch(r"([01]?\d|2[0-3])", v):
            return ("msg-err", "hour: invalid")
        conf_set("schedule", ckey, v)
    mode = form.get("sd_mode", ["return"])[0]
    conf_set("schedule", "shutdown_mode",
             "stayoff" if mode == "stayoff" else "return")
    log_event("NMC: Schedule settings saved.")
    return ("msg-ok", "Schedules saved.")


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
        return ("msg-ok", ("Battery marked as replaced — install "
                "date written to the UPS, statistics reset."))
    if pushed is False:
        log_event("UPS: Battery marked as replaced; UPS did not update "
                  "its install date, battery statistics reset.", "warn")
        return ("msg-ok", ("Battery marked as replaced — the UPS "
                "did not update its install date; recorded locally, "
                "statistics reset."))
    log_event("UPS: Battery marked as replaced; install date recorded "
              "locally, battery statistics reset.")
    return ("msg-ok", ("Battery marked as replaced — install date "
            "recorded locally, statistics reset."))


