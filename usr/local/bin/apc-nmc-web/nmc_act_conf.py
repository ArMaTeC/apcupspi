"""Config POST handlers: UPS settings, password, notifications,
users/roles/RADIUS, services, firewall, 802.1X and WiFi.
Loaded into the apc-nmc-web.py namespace at startup (see the
_load_parts() call near the bottom of that file). Names defined in
other parts or the main file resolve through the shared module
globals — identical semantics to the former single file."""
# Part of apc-nmc-web.py — see that file for the noqa rationale.
# ruff: noqa: BLE001, UP031
# pyright: reportUndefinedVariable=false

def do_config(form):
    def v(name):
        return form.get(name, [""])[0].strip()

    before = dict(load_state().get("cfg") or {})
    try:
        rw_vars = ups_rw()
    except Exception:
        rw_vars = None   # upsd unreachable: attempt all, errors surface
    msgs = []
    for key, skey in (("ups_name", "ups_name"), ("location", "location"),
                      ("contact", "contact"), ("ratedv", "rated_output_voltage"),
                      ("sens", "sensitivity")):
        val = v(key)
        if val:
            if key in ("ratedv", "sens"):
                try:
                    val = int(val)
                except ValueError:
                    msgs.append("%s: invalid number" % key)
                    continue
            cfg_set(skey, val)

    # string SET VAR
    if v("ups_id"):
        if not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,16}", v("ups_id")):
            msgs.append("ups.id: invalid characters "
                        "(a-z 0-9 _ . - space, max 16)")
        else:
            try:
                r = ups_setvar("ups.id", v("ups_id"))
                if not r.startswith("OK"):
                    msgs.append("ups.id: %s" % r)
            except Exception as e:
                msgs.append("ups.id: %s" % e)

    # numeric SET VARs
    for key, var in (("lowrt", "battery.runtime.low"),
                     ("sddel", "ups.delay.shutdown"),
                     ("rdel", "ups.delay.start"),
                     ("rbdel", "ups.delay.reboot"),
                     ("lowchg", "battery.charge.low"),
                     ("trlow", "input.transfer.low"),
                     ("trhigh", "input.transfer.high")):
        val = v(key)
        if val:
            if not re.fullmatch(r"\d+", val):
                msgs.append("%s: not a number" % var)
                continue
            if key == "lowrt":
                cfg_set("low_battery_runtime", int(val) * 100)
            elif key == "sddel":
                cfg_set("shutoff_delay", int(val) * 100)   # timeticks
            elif key == "lowchg":
                cfg_set("min_return_capacity", int(val))
            if rw_vars is not None and var not in rw_vars:
                continue   # card keeps it; UPS can't store it
            try:
                r = ups_setvar(var, val)
                if not r.startswith("OK"):
                    msgs.append("%s: %s" % (var, r))
            except Exception as e:
                msgs.append("%s: %s" % (var, e))

    alarm = v("alarm")
    if alarm and re.fullmatch(r"[1-4]", alarm):
        try:
            # same fallback pg_config renders (status var absent → 1)
            cur = {"enabled": 1, "disabled": 3, "muted": 4}.get(
                ups_vars().get("ups.beeper.status", ""), 1)
            sup = ups_cmds()
        except Exception:
            cur, sup = 1, None   # upsd down: attempt, let error surface
        cmd = {1: "beeper.enable", 2: "beeper.enable",
               3: "beeper.disable", 4: "beeper.mute"}[int(alarm)]
        if int(alarm) != cur:
            if sup is not None and cmd not in sup:
                msgs.append("alarm setting: not supported by this UPS")
            else:
                r = ups_cmd(cmd)
                if not r.startswith("OK"):
                    msgs.append("alarm setting: " + r)
    elif alarm:
        msgs.append("alarm: invalid value")

    if msgs:
        return ("msg-err", "Saved card settings; UPS reported: "
                + "; ".join(msgs))
    diff = _cfg_diff(before, load_state().get("cfg") or {})
    log_event("UPS: Configuration changed via the network management card."
              + (" (" + "; ".join(diff)[:240] + ")" if diff else ""))
    return ("msg-ok", "Configuration saved.")


def do_password_change(form, user=None):
    """Self-service password change for the *logged-in* user — validates
    the current password against that user's own credential, then writes
    web_pass_hash for the primary apc account or the user's [users]
    entry (role preserved) for extra accounts. RADIUS-only users get an
    error; their password lives on the authentication server."""
    cur = form.get("curp", [""])[0]
    new = form.get("newp", [""])[0]
    cnf = form.get("cfmp", [""])[0]
    user = user or WEB_USER
    # A name with no local credential at all is a RADIUS-only account —
    # its password lives on the authentication server.
    if user != WEB_USER and not (
            _cp.has_section("users")
            and user in set(_cp.options("users")) - set(_cp.defaults())):
        return ("msg-err", ("This account is managed by the "
                            "authentication server, not this card."))
    # user_auth checks the session user's own credential: web_pass for
    # apc, the [users] entry for extra accounts
    if not user_auth(user, cur):
        return ("msg-err", "Current password is incorrect.")
    if len(new) < 6:
        return ("msg-err", "New password must be at least 6 characters.")
    if new != cnf:
        return ("msg-err", "New passwords do not match.")
    salt = secrets.token_hex(16)
    hashed = salt + "$" + hashlib.pbkdf2_hmac(
        _PBKDF[0], new.encode(), bytes.fromhex(salt),
        _PBKDF[1]).hex()
    if user == WEB_USER:
        cfg_set("web_pass_hash", hashed)
        cfg_set("web_pass", "")
        log_event("NMC: Web interface password changed.")
    else:
        if _cp.has_section("users"):
            for name in set(_cp.options("users")) - set(_cp.defaults()):
                if not secrets.compare_digest(user, name):
                    continue
                role, _ = _parse_user_cred(_cp.get("users", name))
                conf_set("users", name, "%s:%s" % (role, hashed))
                log_event("NMC: Password changed for user %s." % user)
                return ("msg-ok",
                        "Password changed. Use it at next log on.")
        return ("msg-err", ("This account is managed by the "
                            "authentication server, not this card."))
    return ("msg-ok", "Password changed. Use it at next log on.")


_NOTIFY_TEST_TEXT = ("Test notification from the APC UPS network "
                     "management card.")


def do_notify_config(form):
    def v(name):
        return form.get(name, [""])[0].strip()

    before = dict(load_state().get("cfg") or {})
    errs = []
    sev = v("nsev")
    if sev in _SEV_ORDER:
        cfg_set("notify_min_sev", sev)
    elif sev:
        errs.append("minimum severity: invalid value")
    # Non-secret fields: only written when present in the form — a
    # partial POST (API caller, stale page) must not clear config.
    if "nsyslog" in form:
        cfg_set("notify_syslog", v("nsyslog")[:64])
        cfg_set("notify_syslog_insecure", bool(form.get("nsysinsecure")))

    for name, key in (("nwebhook", "notify_webhook"),
                      ("ndiscord", "notify_discord"),
                      ("nteams", "notify_teams")):
        if form.get("clr_" + name):
            cfg_set(key, "")
            continue
        val = v(name)
        if val:
            if not re.match(r"^https?://", val):
                errs.append("%s: must start with http(s)://" % key[7:])
                continue
            cfg_set(key, val[:256])

    if "ndismention" in form:
        men = v("ndismention")
        if men and not re.fullmatch(r"[\w@<>&!:#.-]{1,64}", men):
            errs.append("discord mention: invalid value")
        else:
            cfg_set("notify_discord_mention", men)

    if "nsmtphost" in form:
        cfg_set("notify_smtp_host", v("nsmtphost")[:64])
    if "nsmtpport" in form:
        port = v("nsmtpport")
        if port:
            if not re.fullmatch(r"\d{1,5}", port) or \
                    not 1 <= int(port) <= 65535:
                errs.append("smtp_port: invalid")
            else:
                cfg_set("notify_smtp_port", port)
        else:
            cfg_set("notify_smtp_port", "")
    tls = v("nsmtptls")
    if tls in ("none", "starttls", "ssl"):
        cfg_set("notify_smtp_tls", tls)
    elif tls:
        errs.append("smtp security: invalid value")
    if "nsmtpuser" in form:
        cfg_set("notify_smtp_user", v("nsmtpuser")[:64])
    if form.get("clr_nsmtppass"):
        cfg_set("notify_smtp_pass", "")
    elif v("nsmtppass"):
        cfg_set("notify_smtp_pass", v("nsmtppass")[:128])
    if "nsmtpfrom" in form:
        cfg_set("notify_smtp_from", v("nsmtpfrom")[:64])
    if "nsmtpto" in form:
        cfg_set("notify_smtp_to", v("nsmtpto")[:128])

    # Routing matrix: only when rt_* fields are present — a partial POST
    # (API caller, stale page) must not silently clear all routing.
    if any(k.startswith("rt_") for k in form):
        for k, _lbl in NOTIFY_KINDS:
            chans = [c for c in NOTIFY_CHANNELS
                     if form.get("rt_%s_%s" % (k, c))]
            cfg_set("notify_route_" + k, ",".join(chans))

    if errs:
        return ("msg-err", "Saved what was valid; " + "; ".join(errs))
    diff = _cfg_diff(before, load_state().get("cfg") or {})
    log_event("NMC: Notification settings changed via the network "
              "management card." +
              (" (" + "; ".join(diff)[:240] + ")" if diff else ""))
    return ("msg-ok", "Notification settings saved.")


def do_notify_test(ch):
    n = notify_cfg()
    send = {
        "syslog": (n["syslog"],
                   lambda: _send_syslog(n["syslog"],
                                        _NOTIFY_TEST_TEXT, "info")),
        "webhook": (n["webhook"],
                    lambda: _send_webhook(n["webhook"],
                                          _NOTIFY_TEST_TEXT, "info")),
        "discord": (n["discord"],
                    lambda: _send_discord(n["discord"],
                                          _NOTIFY_TEST_TEXT, "info",
                                          "other",
                                          cfg_get("ups_name", ""),
                                          n.get("discord_mention")
                                          or None)),
        "teams": (n["teams"],
                  lambda: _send_teams(n["teams"],
                                      _NOTIFY_TEST_TEXT, "info")),
        "email": (n["smtp_host"] and n["smtp_to"],
                  lambda: _send_email(n, _NOTIFY_TEST_TEXT, "info")),
    }.get(ch)
    if not send:
        return ("msg-err", "Unknown channel.")
    enabled, fn = send
    if not enabled:
        return ("msg-err",
                "%s is not configured — save settings first." % ch)
    try:
        fn()
    except Exception as e:
        return ("msg-err", "%s test failed: %s" % (ch, e))
    log_event("NMC: Test notification sent to %s." % ch,
              notify=False)   # don't re-notify the test itself
    return ("msg-ok", "Test notification sent to %s." % ch)


# ============================================================ system config
def do_users(form):
    act = form.get("act", [""])[0]
    name = form.get("uname", [""])[0].strip().lower()
    if act == "del":
        if not _cp.has_section("users") or \
                name not in _cp.options("users"):
            return ("msg-err", "No such user.")
        conf_del("users", name)
        log_event("NMC: User account %s removed." % name)
        return ("msg-ok", "User %s removed." % name)
    if act != "add":
        return ("msg-err", "Unknown action.")
    if not re.fullmatch(r"[a-z0-9_.\-]{1,32}", name):
        return ("msg-err", "User name: a-z 0-9 _ . - only, max 32.")
    if secrets.compare_digest(name, WEB_USER.lower()):
        return ("msg-err", "That name is the primary account.")
    pw = form.get("upass", [""])[0]
    if len(pw) < 6:
        return ("msg-err", "Password must be at least 6 characters.")
    role = form.get("urole", [""])[0].lower()
    if role not in USER_ROLES:
        return ("msg-err", "Invalid role.")
    salt = secrets.token_hex(16)
    cred = "%s:%s$%s" % (role, salt, hashlib.pbkdf2_hmac(
        _PBKDF[0], pw.encode(), bytes.fromhex(salt), _PBKDF[1]).hex())
    conf_set("users", name, cred)
    log_event("NMC: User account %s added with role %s." % (name, role))
    return ("msg-ok", "User %s added (%s)." % (name, role))


def do_roles(form):
    """Persist the Access Permissions matrix — one [roles] key per
    non-admin role holding its space-separated capability list."""
    for role in USER_ROLES[1:]:          # admin is fixed to all caps
        caps = sorted(c for c in ROLE_CAPS
                      if form.get("cap_%s_%s" % (role, c)))
        conf_set("roles", role, " ".join(caps))
    log_event("NMC: Role permissions updated.")
    return ("msg-ok", "Role permissions saved.")


def do_radius(form):
    def v(k):
        return form.get(k, [""])[0].strip()
    errs = []
    srv = v("r_srv")
    if srv and not re.fullmatch(r"[A-Za-z0-9.\-]{1,64}", srv):
        errs.append("server: invalid hostname")
    port = v("r_port") or "1812"
    if not re.fullmatch(r"\d{1,5}", port) or not 1 <= int(port) <= 65535:
        errs.append("port: invalid")
    role = v("r_role")
    if role not in USER_ROLES:
        errs.append("role: invalid")
    try:
        to = float(v("r_to") or 4)
        if not 0.5 <= to <= 30:
            errs.append("timeout: out of range")
    except ValueError:
        errs.append("timeout: invalid")
    if errs:
        return ("msg-err", "; ".join(errs))
    conf_set("radius", "enabled", "1" if form.get("r_en") else "0")
    conf_set("radius", "server", srv)
    conf_set("radius", "port", port)
    conf_set("radius", "timeout", v("r_to") or "4")
    conf_set("radius", "role", role)
    if form.get("clr_r_secret"):
        conf_set("radius", "secret", "")
    elif v("r_secret"):
        conf_set("radius", "secret", v("r_secret"))
    log_event("NMC: RADIUS settings changed.")
    return ("msg-ok", "RADIUS settings saved.")


def do_services(form):
    def v(k):
        return form.get(k, [""])[0].strip()
    svc = v("svc")
    if svc == "modbus":
        bind = v("mb_bind") or "0.0.0.0"
        if not re.fullmatch(r"[0-9a-fA-F.:]{1,45}", bind):
            return ("msg-err", "bind address: invalid")
        port = v("mb_port") or "502"
        if not re.fullmatch(r"\d{1,5}", port) \
                or not 1 <= int(port) <= 65535:
            return ("msg-err", "port: invalid")
        unit = v("mb_unit") or "1"
        if not re.fullmatch(r"\d{1,3}", unit) or not 1 <= int(unit) <= 247:
            return ("msg-err", "unit id: 1-247")
        conf_set("modbus", "bind", bind)
        conf_set("modbus", "port", port)
        conf_set("modbus", "unit", unit)
        conf_set("modbus", "allow_write",
                 "1" if form.get("mb_write") else "0")
        ok, out = svc_apply("apc-nmc-modbus.service",
                            bool(form.get("mb_en")))
        log_event("NMC: Modbus TCP %s." %
                  ("enabled" if form.get("mb_en") else "disabled"))
        if not ok:
            return ("msg-err", "Saved; service change failed: %s" % out)
        return ("msg-ok", "Modbus TCP settings saved.")
    if svc == "ftp":
        ok, out = svc_apply("vsftpd.service", bool(form.get("ftp_en")))
        log_event("NMC: FTP server %s." %
                  ("enabled" if form.get("ftp_en") else "disabled"))
        if not ok:
            return ("msg-err",
                    "Service change failed: %s "
                    "(vsftpd installed?)" % out)
        return ("msg-ok", "FTP server %s."
                          % ("enabled" if form.get("ftp_en")
                             else "disabled"))
    if svc == "mqtt":
        host = v("mq_host")
        if host and not re.fullmatch(r"[A-Za-z0-9._\-]{1,128}", host):
            return ("msg-err", "broker host: invalid")
        port = v("mq_port") or "1883"
        if not re.fullmatch(r"\d{1,5}", port) \
                or not 1 <= int(port) <= 65535:
            return ("msg-err", "port: invalid")
        topic = (v("mq_topic").strip("/") or "apc-nmc/myups")
        if not re.fullmatch(r"[A-Za-z0-9_\-/]{1,96}", topic):
            return ("msg-err",
                    "topic prefix: letters, digits, _-/ only")
        iv = v("mq_interval") or "60"
        if not re.fullmatch(r"\d{1,5}", iv) \
                or not 15 <= int(iv) <= 86400:
            return ("msg-err", "interval: 15-86400")
        conf_set("mqtt", "enabled", "1" if form.get("mq_en") else "0")
        conf_set("mqtt", "host", host)
        conf_set("mqtt", "port", port)
        conf_set("mqtt", "tls", "1" if form.get("mq_tls") else "0")
        conf_set("mqtt", "tls_insecure",
                 "1" if form.get("mq_insecure") else "0")
        conf_set("mqtt", "user", v("mq_user"))
        if v("mq_pass"):
            conf_set("mqtt", "password", v("mq_pass"))
        elif form.get("clr_mq_pass"):
            conf_set("mqtt", "password", "")
        conf_set("mqtt", "topic", topic)
        conf_set("mqtt", "interval", iv)
        log_event("NMC: MQTT telemetry %s."
                  % ("enabled" if form.get("mq_en") else "disabled"))
        if form.get("mq_test"):
            if not host:
                return ("msg-err",
                        "Saved; test skipped - no broker host set.")
            ok, err = mqtt_send(json.dumps(
                {"ts": int(time.time()), "test": True,
                 "source": "apc-nmc"}))
            if not ok:
                return ("msg-err", "Saved; test publish failed: %s"
                                   % esc(err))
            return ("msg-ok", "Saved; test publish succeeded.")
        return ("msg-ok", "MQTT settings saved.")
    return ("msg-err", "Unknown service.")


def do_firewall(form):
    def v(k):
        return form.get(k, [""])[0].strip()
    for k, key in (("fw_tcp", "allow_tcp"), ("fw_udp", "allow_udp")):
        for p in v(k).split():
            if not re.fullmatch(r"\d{1,5}", p) \
                    or not 1 <= int(p) <= 65535:
                return ("msg-err", "%s: '%s' is not a port" % (k, p))
        conf_set("firewall", key, v(k)[:128])
    for c in v("fw_from").split():
        if not re.fullmatch(r"[0-9a-fA-F.:]+/\d{1,3}", c):
            return ("msg-err", "allowed sources: '%s' is not a CIDR" % c)
    conf_set("firewall", "allow_from", v("fw_from")[:256])
    ok, out = svc_apply("apc-nmc-firewall.service",
                        bool(form.get("fw_en")))
    log_event("NMC: Firewall %s." %
              ("enabled" if form.get("fw_en") else "disabled"))
    if not ok:
        return ("msg-err", "Saved; apply failed: %s" % out)
    return ("msg-ok", "Firewall settings saved.")


def do_dot1x(form):
    def v(k):
        return form.get(k, [""])[0].strip()
    iface = v("x_if") or "eth0"
    if not re.fullmatch(r"[a-z0-9]{1,15}", iface):
        return ("msg-err", "interface: invalid name")
    eap = v("x_eap").upper()
    if eap not in ("PEAP", "TTLS", "TLS", "FAST"):
        return ("msg-err", "EAP method: invalid")
    for key in ("x_ca", "x_cc", "x_pk"):
        val = v(key)
        if val and not re.fullmatch(r"[A-Za-z0-9_/.\-]{1,128}", val):
            return ("msg-err", "%s: invalid path" % key)
    ph2 = v("x_ph2")
    if ph2 and not re.fullmatch(r"[a-zA-Z0-9= _\-]{1,64}", ph2):
        return ("msg-err", "phase2: invalid")
    conf_set("dot1x", "iface", iface)
    conf_set("dot1x", "eap", eap)
    conf_set("dot1x", "identity", v("x_id")[:64])
    conf_set("dot1x", "phase2", ph2)
    conf_set("dot1x", "ca_cert", v("x_ca"))
    conf_set("dot1x", "client_cert", v("x_cc"))
    conf_set("dot1x", "private_key", v("x_pk"))
    if form.get("clr_x_pass"):
        conf_set("dot1x", "password", "")
    elif v("x_pass"):
        conf_set("dot1x", "password", v("x_pass"))
    if form.get("clr_x_pkpass"):
        conf_set("dot1x", "private_key_pass", "")
    elif v("x_pkpass"):
        conf_set("dot1x", "private_key_pass", v("x_pkpass"))
    ok, out = svc_apply("apc-nmc-dot1x.service", bool(form.get("x_en")))
    log_event("NMC: 802.1X %s." % ("enabled" if form.get("x_en")
                                   else "disabled"))
    if not ok:
        return ("msg-err", "Saved; service change failed: %s "
                           "(wpasupplicant installed?)" % out)
    return ("msg-ok", "802.1X settings saved.")


def _reboot_later():
    rc, _ = _sysctl("reboot")
    if rc != 0:
        _run(["reboot", "-f"], 5)


def do_wifi(form):
    """WiFi setup actions: radio on/off/connect/disconnect via
    NetworkManager, plus boot-config radio toggles (dtoverlay in
    config.txt — admin-only, needs a reboot) and reboot itself."""
    act = form.get("act", [""])[0]
    # Boot-config actions need no NetworkManager — they matter most when
    # the radio was disabled at install time and no wlan iface exists.
    # (Admin gating happens in do_POST before dispatch.)
    if act in ("hw_enable", "hw_disable", "reboot"):
        if act == "reboot":
            log_event("NMC: Reboot requested via the network management "
                      "card.")
            threading.Timer(1.5, _reboot_later).start()
            return ("msg-ok", "Rebooting - reconnect in a minute.")
        radio = form.get("hw", [""])[0]
        if radio not in ("wifi", "bt"):
            return ("msg-err", "Unknown radio.")
        try:
            with open(WIFI_HW_REQ, "a") as f:
                f.write("%s=%s\n" % (radio, act[3:]))
        except OSError as e:
            return ("msg-err", "Request failed: %s" % e)
        rc, out = _sysctl("start", "--no-block", "apc-nmc-wifi-hw.service")
        if rc != 0:
            return ("msg-err", "Boot-config service failed: %s" % esc(out))
        log_event("NMC: Onboard %s %sd in boot config; reboot required."
                  % (radio, act[3:]))
        return ("msg-ok", "Onboard %s %sd - reboot the card to apply."
                        % (radio, act[3:]))
    _radio, _conn, has_nm = wifi_status()
    if not has_nm:
        return ("msg-err", "NetworkManager (nmcli) not detected.")
    if act == "on":
        _run(["rfkill", "unblock", "wifi"], 8)
        rc, out = _nmcli("radio", "wifi", "on")
        log_event("NMC: WiFi setup activated on %s." %
                  (net_ifaces(("wl",)) or ["wlan?"])[0])
        if rc == 0:
            return ("msg-ok", ("WiFi radio enabled - scan and connect "
                              "below."))
        return ("msg-err", "WiFi enable failed: %s" % esc(out))
    if act == "off":
        rc, out = _nmcli("radio", "wifi", "off")
        log_event("NMC: WiFi radio switched off.")
        if rc == 0:
            return ("msg-ok", "WiFi radio switched off.")
        return ("msg-err", "WiFi disable failed: %s" % esc(out))
    if act == "disconnect":
        ifn = form.get("w_if", [""])[0].strip()
        if ifn not in net_ifaces(("wl",)):
            return ("msg-err", "Unknown wireless interface.")
        rc, out = _nmcli("dev", "disconnect", ifn, timeout=20)
        log_event("NMC: WiFi disconnected on %s." % ifn)
        if rc == 0:
            return ("msg-ok", "Disconnected from WiFi.")
        return ("msg-err", "Disconnect failed: %s" % esc(out))
    if act != "connect":
        return ("msg-err", "Unknown action.")
    ssid = form.get("w_ssid", [""])[0].strip()
    ifn = form.get("w_if", [""])[0].strip()
    psk = form.get("w_psk", [""])[0]
    if not 1 <= len(ssid.encode("utf-8", "replace")) <= 32:
        return ("msg-err", "SSID must be 1-32 bytes.")
    if ifn and ifn not in net_ifaces(("wl",)):
        return ("msg-err", "Unknown wireless interface.")
    if psk and not (re.fullmatch(r"[\x20-\x7e]{8,63}", psk)
                    or re.fullmatch(r"[0-9a-fA-F]{64}", psk)):
        return ("msg-err", ("Passphrase must be 8-63 printable "
                           "characters or a 64-digit hex PSK."))
    args = ["dev", "wifi", "connect", ssid]
    if ifn:
        args += ["ifname", ifn]
    if psk:
        args += ["password", psk]
    rc, out = _nmcli(*args, timeout=45)
    if rc == 0:
        log_event("NMC: WiFi connected to \"%s\" on %s."
                  % (ssid, ifn or "wlan"))
        return ("msg-ok", "Connected to %s." % esc(ssid))
    log_event("NMC: WiFi connection to \"%s\" failed." % ssid)
    return ("msg-err", "Connect failed: %s" % esc(out))


