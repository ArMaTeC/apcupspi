#!/usr/bin/env bash
#
# firstboot-wizard.sh — interactive first-boot configuration wizard.
# Runs once on the console (via firstboot-wizard.service) on a clean/published
# image, before the login prompt and before sshd starts.
# Can also be re-run manually: sudo firstboot-wizard.sh
#
set -u
# newt breaks on unknown TERM types — fall back to xterm.
export TERM="${TERM:-xterm}"
infocmp "$TERM" >/dev/null 2>&1 || export TERM=xterm
# Always take input from the controlling terminal — safe under `curl | bash`
# where stdin is the pipe. Draw on it too: whiptail's UI goes to stdout, so a
# piped/tee'd run would render dialogs invisibly and look like a hang.
W() { /usr/bin/whiptail "$@" </dev/tty >/dev/tty; }

die() { printf '\nWizard aborted.\n' >&2; exit 130; }
trap 'die' INT TERM
NMC_CONF=/etc/apc-nmc/nmc.conf
NMC_DIR=/etc/apc-nmc
UPS_CONF=/etc/nut/ups.conf
UPSD_USERS=/etc/nut/upsd.users
UPSMON_CONF=/etc/nut/upsmon.conf
SNMP_CONF=/etc/snmp/snmpd.conf
STATE_FILE=/var/lib/apc-nmc/state.json

say()  { W --msgbox "$1" 12 70; }
ask()  { W --inputbox "$1" 10 70 "$2" 3>&1 1>&2 2>&3; }
askpw(){ W --passwordbox "$1" 10 70 3>&1 1>&2 2>&3; }
# yes/no has no Cancel button — Esc is the way out; it must abort, not
# silently mean "no".
yesno() { # $1=text $2=height (optional)
    W --yesno "$1\n\n(Esc = abort wizard)" "${2:-13}" 70
    local rc=$?
    [ $rc -eq 255 ] && die
    return $rc
}

ini_set() { # file section key value — write an ini key via python3
            # (value via env so it never lands in /proc/*/cmdline)
    INI_VAL=$4 python3 - "$1" "$2" "$3" <<'PY'
import configparser, os, sys
f, s, k = sys.argv[1:4]
v = os.environ["INI_VAL"]
cp = configparser.ConfigParser()
cp.optionxform = str
cp.read(f)
if not cp.has_section(s):
    cp.add_section(s)
cp.set(s, k, v)
with open(f, 'w') as fh:
    cp.write(fh)
PY
}

esc_sed() { printf '%s' "$1" | sed 's/[\&/|]/\\&/g'; }

upsd_set_pw() { # $1=user $2=password — upsd.users is not strict INI (bare
                # directives like "upsmon primary" break ConfigParser)
                # password via env so it never lands in /proc/*/cmdline
    UPW=$2 awk -v sec="[$1]" '
        $0 == sec { insec=1; print; next }
        /^\[/   { insec=0 }
        insec && match($0, /^[[:space:]]*password[[:space:]]*=/) {
            print substr($0, 1, RLENGTH) " " ENVIRON["UPW"]; found=1; next
        }
        { print }
        END { if (!found) exit 1 }
    ' "$UPSD_USERS" > "$UPSD_USERS.tmp" \
      && mv "$UPSD_USERS.tmp" "$UPSD_USERS" \
      && chown root:nut "$UPSD_USERS" && chmod 640 "$UPSD_USERS"
}

pw_pair() { # $1=prompt -> echoes confirmed password; Esc/Ctrl+C propagates
    while :; do
        p1=$(askpw "$1") || return 1
        p2=$(askpw "Confirm: $1") || return 1
        if [ -z "$p1" ]; then W --msgbox "Password cannot be empty." 8 50 || return 1; continue; fi
        [ "$p1" = "$p2" ] && { echo "$p1"; return; }
        W --msgbox "Passwords do not match, try again." 8 50 || return 1
    done
}

ask_req() { # $1=prompt $2=default — non-empty answer
    local v
    while :; do
        v=$(ask "$1" "$2") || return 1
        [ -n "$v" ] && { echo "$v"; return; }
        W --msgbox "This field is required." 8 45 || return 1
    done
}

ask_url() { # $1=prompt — non-empty http(s):// URL (matches the web UI's rule)
    local u
    while :; do
        u=$(ask "$1" "") || return 1
        case "$u" in http://*|https://*) echo "$u"; return;; esac
        W --msgbox "Enter a URL starting with http:// or https://" 8 55 \
            || return 1
    done
}

state_set() { # key value — write cfg.<key> into state.json with the same
              # lock + permissions the web app uses (value via env — argv
              # is world-readable in /proc/*/cmdline while this runs)
    ST_VAL=$2 python3 - "$STATE_FILE" "$1" <<'PY'
import fcntl, grp, json, os, sys
path, key = sys.argv[1:3]
val = os.environ["ST_VAL"]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".lock", "a") as lf:
    try:
        os.chown(lf.name, -1, grp.getgrnam("Debian-snmp").gr_gid)
        os.chmod(lf.name, 0o660)
    except Exception:
        pass
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        with open(path) as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    if not isinstance(st.get("cfg"), dict):
        st["cfg"] = {}
    st["cfg"][key] = val
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    fcntl.flock(lf, fcntl.LOCK_UN)
PY
}

# ------------------------------------------------------------------ collect
W --msgbox "Welcome!\n\nThis is the first boot of this image. The wizard will now ask you to configure:\n\n  - hostname and credentials\n  - network\n  - UPS monitoring (NUT) and web interface\n  - SNMP\n  - alert notifications (optional)\n\nPress OK to begin. Esc or Ctrl+C exits at any prompt." 19 70 || die

HN=$(ask "Hostname for this device:" "apc-nmc") || die; HN=${HN:-apc-nmc}
ROOTPW=$(pw_pair "New root password (used for console + SSH):") || die
SSHKEY=$(ask "SSH public key to authorize for root (ssh-ed25519/ssh-rsa/...).\nLeave empty to use password login only:" "") || die
NET=$(W --menu "Network configuration for eth0:" 12 70 2 \
    "dhcp"   "Automatic (DHCP) - recommended" \
    "static" "Static IP address" 3>&1 1>&2 2>&3) || die
CIDR=""; GW=""; DNS=""
if [ "$NET" = "static" ]; then
    CIDR=$(ask "Static address in CIDR notation (e.g. 192.168.1.50/24):" "") || die
    GW=$(ask "Default gateway (e.g. 192.168.1.1):" "") || die
    DNS=$(ask "DNS server(s), space separated (e.g. 192.168.1.1 1.1.1.1):" "") || die
fi

WEBUSER=$(ask "Web interface admin username:" "apc") || die; WEBUSER=${WEBUSER:-apc}
WEBPW=$(pw_pair "Web interface admin password:") || die

UPS=$(ask "UPS name in NUT (must match the [section] in ups.conf):" "myups") || die; UPS=${UPS:-myups}
DRIVER=$(ask "NUT driver for your UPS (e.g. usbhid-ups, apc_modbus, blazer_usb):" "apc_modbus") || die; DRIVER=${DRIVER:-apc_modbus}
UPORT=$(ask "NUT port (e.g. auto, /dev/ttyS0, /dev/usb/hiddev0):" "auto") || die; UPORT=${UPORT:-auto}
NUTPW=$(pw_pair "NUT monitor password (used by upsmon + web interface):") || die
ADMPW=$(askpw "NUT admin password (leave empty = same as monitor password):") || die
[ -z "$ADMPW" ] && ADMPW="$NUTPW"

ROCOM=$(ask "SNMP read community (v1/v2c):" "public") || die; ROCOM=${ROCOM:-public}
RWCOM=$(ask "SNMP write community (v1/v2c):" "private") || die; RWCOM=${RWCOM:-private}
LOC=$(ask "Device location (shown in web UI + SNMP):" "Server Room") || die; LOC=${LOC:-Server Room}
CONTACT=$(ask "Device contact (shown in web UI + SNMP):" "admin@localhost") || die; CONTACT=${CONTACT:-admin@localhost}

# --- alert notifications (optional; the per-event routing matrix stays
# --- in the web UI — every selected channel gets all enabled severities)
NOTIFY=0; NCH=""; NMIN=warn
DURL=""; DMEN=""; TURL=""; WURL=""; SLOG=""
SMTP_HOST=""; SMTP_PORT=""; SMTP_TLS=""; SMTP_USER=""; SMTP_PASS=""; SMTP_FROM=""; SMTP_TO=""
if yesno "Configure alert notifications now?\n\nThe card can push UPS alerts (power loss, low battery,\ncomms loss, self-test results...) to external channels.\nThis can also be done later in the web UI\n(Configuration -> Notifications)."; then
    NCH=$(W --separate-output --checklist \
        "Alert channels to configure (space = toggle):" 15 70 5 \
        discord "Discord webhook" OFF \
        teams   "Microsoft Teams webhook" OFF \
        webhook "Generic HTTP webhook (JSON POST)" OFF \
        email   "Email via SMTP" OFF \
        syslog  "Syslog (local, or host:port via UDP)" OFF \
        3>&1 1>&2 2>&3) || die
    if [ -n "$NCH" ]; then
        NOTIFY=1
        NMIN=$(W --default-item warn --menu \
            "Minimum severity to send:" 13 70 3 \
            info "Everything, including informational notices" \
            warn "Warnings and critical only (recommended)" \
            crit "Critical alerts only" 3>&1 1>&2 2>&3) || die
        for c in $NCH; do case $c in
            discord)
                DURL=$(ask_url "Discord webhook URL:\n\n(channel settings -> Integrations -> Webhooks)") || die
                DMEN=$(ask "Mention on critical alerts (optional):\n@here, @everyone, <@&roleID> or <@userID>.\nLeave empty for none." "") || die
                MEN_RE='^[[:alnum:]_@<>&!:#.-]{1,64}$'
                if [ -n "$DMEN" ] && ! [[ "$DMEN" =~ $MEN_RE ]]; then
                    W --msgbox "Invalid mention format — ignoring it." 8 55 || die
                    DMEN=""
                fi
                ;;
            teams)
                TURL=$(ask_url "Teams Incoming Webhook URL:\n\n(channel -> Connectors -> Incoming Webhook)") || die
                ;;
            webhook)
                WURL=$(ask_url "Generic webhook URL (receives a JSON POST):") || die
                ;;
            syslog)
                SLOG=$(ask "Syslog target:\n\n'local' for this device's syslog, or host[:port] for a\nremote collector (UDP)." "local") || die
                SLOG=${SLOG:-local}
                ;;
            email)
                SMTP_HOST=$(ask_req "SMTP server hostname:" "") || die
                SMTP_PORT=$(ask "SMTP port:" "587") || die
                case "$SMTP_PORT" in *[!0-9]*) SMTP_PORT=587;; esac
                { [ "$SMTP_PORT" -ge 1 ] && [ "$SMTP_PORT" -le 65535 ]; } 2>/dev/null || SMTP_PORT=587
                SMTP_TLS=$(W --default-item starttls --menu \
                    "SMTP security:" 13 70 3 \
                    starttls "STARTTLS — port 587 (most providers)" \
                    ssl      "SSL/TLS — port 465" \
                    none     "None — port 25 (local relay only)" \
                    3>&1 1>&2 2>&3) || die
                SMTP_USER=$(ask "SMTP username (leave empty for no auth):" "") || die
                if [ -n "$SMTP_USER" ]; then
                    SMTP_PASS=$(askpw "SMTP password:") || die
                fi
                SMTP_FROM=$(ask "From address:" "apc-nmc@$HN") || die
                SMTP_FROM=${SMTP_FROM:-apc-nmc@$HN}
                SMTP_TO=$(ask_req "Recipient address(es), comma-separated:" "") || die
                ;;
        esac; done
    fi
fi

yesno "Generate a fresh self-signed HTTPS certificate for the web interface?\n(Recommended: the image ships a placeholder cert.)" && REGEN_CERT=1 || REGEN_CERT=0

SUMMARY="Hostname:      $HN
Root password: (set)
SSH key:       $([ -n "$SSHKEY" ] && echo provided || echo none)
Network:       $NET $CIDR
Web admin:     $WEBUSER
UPS:           $UPS ($DRIVER, port $UPORT)
SNMP:          ro=$ROCOM rw=$RWCOM
Location:      $LOC / $CONTACT
Alerts:        $([ $NOTIFY -eq 1 ] && echo "$(echo $NCH | tr '\n' ' ') (min: $NMIN)" || echo "not configured")
New TLS cert:  $([ $REGEN_CERT -eq 1 ] && echo yes || echo no)

Apply this configuration?"
yesno "$SUMMARY" 24 || { say "Cancelled. The wizard will run again on next boot,\nor re-run: sudo firstboot-wizard.sh"; exit 0; }

# -------------------------------------------------------------------- apply
echo ">> Applying configuration..."
OLD_HN=$(hostname)
hostnamectl set-hostname "$HN"
if grep -qw "$OLD_HN" /etc/hosts; then
    sed -i "s/\b$OLD_HN\b/$(esc_sed "$HN")/g" /etc/hosts
else
    echo "127.0.1.1	$HN" >> /etc/hosts
fi

echo "root:$ROOTPW" | chpasswd

if [ -n "$SSHKEY" ]; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    echo "$SSHKEY" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
fi

if [ "$NET" = "static" ] && [ -n "$CIDR" ]; then
    CON=$(nmcli -t -f NAME con show --active | grep -v '^lo$' | head -n1)
    [ -n "$CON" ] && nmcli con mod "$CON" ipv4.method manual ipv4.addresses "$CIDR" \
        ipv4.gateway "$GW" ipv4.dns "$DNS" ipv4.ignore-auto-dns yes \
        && nmcli con up "$CON" || true
fi

# --- web/NMC config
ini_set "$NMC_CONF" web      user     "$WEBUSER"
ini_set "$NMC_CONF" web      password "$WEBPW"
ini_set "$NMC_CONF" upsd     ups      "$UPS"
ini_set "$NMC_CONF" upsd     user     "upsmon"
ini_set "$NMC_CONF" upsd     password "$NUTPW"
ini_set "$NMC_CONF" device   location "$LOC"
ini_set "$NMC_CONF" device   contact  "$CONTACT"

# --- NUT
CUR_UPS=$(grep -oE '^\[[^]]+\]' "$UPS_CONF" | head -1 | tr -d '[]')
[ -n "$CUR_UPS" ] && [ "$CUR_UPS" != "$UPS" ] && sed -i -E "0,/^\[$CUR_UPS\]/s//[$(esc_sed "$UPS")]/" "$UPS_CONF"
sed -i -E "s|^([[:space:]]*driver[[:space:]]*=[[:space:]]*).*|\1$(esc_sed "$DRIVER")|" "$UPS_CONF"
sed -i -E "s|^([[:space:]]*port[[:space:]]*=[[:space:]]*).*|\1$(esc_sed "$UPORT")|" "$UPS_CONF"
upsd_set_pw upsmon "$NUTPW" || echo "!! warning: could not set upsmon password in upsd.users" >&2
upsd_set_pw admin  "$ADMPW" || echo "!! warning: could not set admin password in upsd.users" >&2
# NUTPW travels via env, not argv — sed/python cmdlines are world-readable
MONLINE="MONITOR ${UPS}@localhost 1 upsmon ${NUTPW} primary" \
python3 - "$UPSMON_CONF" <<'PY'
import os, re, sys
p = sys.argv[1]
with open(p) as f:
    s = f.read()
s = re.sub(r"(?m)^MONITOR[ \t].*", lambda m: os.environ["MONLINE"], s)
with open(p, "w") as f:
    f.write(s)
PY
if [ -n "$CUR_UPS" ] && [ "$CUR_UPS" != "$UPS" ]; then
    systemctl disable "nut-driver@$CUR_UPS" 2>/dev/null || true
    systemctl enable  "nut-driver@$UPS"     2>/dev/null || true
fi

# --- SNMP
sed -i -E "s/^(rocommunity6?)[[:space:]]+\S+/\1 $(esc_sed "$ROCOM")/" "$SNMP_CONF"
sed -i -E "s/^(rwcommunity6?)[[:space:]]+\S+/\1 $(esc_sed "$RWCOM")/" "$SNMP_CONF"
sed -i -E "s/^(sysLocation)[[:space:]].*/\1 $(esc_sed "$LOC")/" "$SNMP_CONF"
sed -i -E "s/^(sysContact)[[:space:]].*/\1 $(esc_sed "$CONTACT")/" "$SNMP_CONF"
SHA_P=$(openssl rand -hex 16); AES_P=$(openssl rand -hex 16)
sed -i -E "s/(createUser[[:space:]]+\S+[[:space:]]+SHA[[:space:]]+)\"[^\"]*\"([[:space:]]+AES[[:space:]]+)\"[^\"]*\"/\1\"$SHA_P\"\2\"$AES_P\"/" "$SNMP_CONF"

# --- alert notifications -> state.json cfg (same store as the web UI)
if [ $NOTIFY -eq 1 ]; then
    echo "   alerts: saving channel config"
    state_set notify_min_sev "$NMIN"
    [ -n "$DURL" ] && state_set notify_discord         "$DURL"
    [ -n "$DMEN" ] && state_set notify_discord_mention "$DMEN"
    [ -n "$TURL" ] && state_set notify_teams           "$TURL"
    [ -n "$WURL" ] && state_set notify_webhook         "$WURL"
    [ -n "$SLOG" ] && state_set notify_syslog          "$SLOG"
    if [ -n "$SMTP_HOST" ]; then
        state_set notify_smtp_host "$SMTP_HOST"
        state_set notify_smtp_port "$SMTP_PORT"
        state_set notify_smtp_tls  "$SMTP_TLS"
        state_set notify_smtp_user "$SMTP_USER"
        state_set notify_smtp_pass "$SMTP_PASS"
        state_set notify_smtp_from "$SMTP_FROM"
        state_set notify_smtp_to   "$SMTP_TO"
    fi
fi

# --- TLS cert
if [ $REGEN_CERT -eq 1 ]; then
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$NMC_DIR/nmc.key" -out "$NMC_DIR/nmc.crt" -days 3650 \
        -subj "/CN=$HN" -addext "subjectAltName=DNS:$HN" 2>/dev/null \
        && cat "$NMC_DIR/nmc.crt" "$NMC_DIR/nmc.key" > "$NMC_DIR/nmc.pem" \
        && chmod 600 "$NMC_DIR/nmc.key" "$NMC_DIR/nmc.pem" || true
fi

# --- restart affected services
for s in "nut-driver@$UPS" nut-server nut-monitor snmpd apc-nmc-web; do
    echo "   restarting: $s"
    systemctl restart "$s" 2>&1 || echo "   !! could not restart $s"
done

# --- optional live test of the channels just configured
if [ $NOTIFY -eq 1 ] && yesno "Send a test alert on the configured channel(s) now?\n\nNeeds working network/DNS — may not be up yet on first boot\n(failures are harmless; test again from the web UI later)."; then
    TEST_RES=""
    ntest() { # $1=channel $2=url $3=json — echoes "name: sent | FAILED ..."
              # webhook URL via -K- config on stdin — it embeds a secret
              # token, and argv is world-readable in /proc/*/cmdline
        local e
        if e=$(printf 'url = "%s"\n' "$2" | curl -fsS -o /dev/null -m 8 \
                 -X POST -K- \
                 -H 'Content-Type: application/json' \
                 -H 'User-Agent: apc-nmc/test (UPS monitor)' \
                 -d "$3" 2>&1); then
            echo "$1: sent"
        else
            echo "$1: FAILED — $(echo "$e" | tail -1)"
        fi
    }
    [ -n "$DURL" ] && TEST_RES+=$(ntest discord "$DURL" \
        '{"username":"APC UPS","content":"apc-nmc: test alert — notifications are configured."}')$'\n'
    [ -n "$TURL" ] && TEST_RES+=$(ntest teams "$TURL" \
        '{"@type":"MessageCard","@context":"http://schema.org/extensions","summary":"apc-nmc test","text":"apc-nmc: test alert — notifications are configured."}')$'\n'
    [ -n "$WURL" ] && TEST_RES+=$(ntest webhook "$WURL" \
        '{"text":"apc-nmc: test alert — notifications are configured.","sev":"info","source":"apc-nmc"}')$'\n'
    if [ -n "$SLOG" ]; then
        if [ "$SLOG" = local ] || [ "$SLOG" = /dev/log ]; then
            logger -p daemon.info -t apc-nmc "apc-nmc: test alert" \
                && TEST_RES+=$'syslog: sent to local syslog\n' \
                || TEST_RES+=$'syslog: FAILED\n'
        else
            _h=${SLOG%%:*}; _p=514; case $SLOG in *:*) _p=${SLOG##*:};; esac
            logger -n "$_h" -P "$_p" -d -p daemon.info -t apc-nmc \
                "apc-nmc: test alert" 2>/dev/null \
                && TEST_RES+="syslog: sent to $SLOG (UDP — verify on the collector)\n" \
                || TEST_RES+=$'syslog: FAILED\n'
        fi
    fi
    if [ -n "$SMTP_HOST" ]; then
        if SMTPPW=$SMTP_PASS python3 - "$SMTP_HOST" "$SMTP_PORT" \
                  "$SMTP_TLS" "$SMTP_USER" "$SMTP_FROM" "$SMTP_TO" \
                  <<'PY' 2>/tmp/.ntest_smtp
import os, smtplib, sys
from email.message import EmailMessage
host, port, tls, user, frm, to = sys.argv[1:7]
pw = os.environ["SMTPPW"]
m = EmailMessage()
m["Subject"] = "[APC UPS] test alert"
m["From"] = frm; m["To"] = to
m.set_content("apc-nmc: test alert - notifications are configured.\n")
s = smtplib.SMTP_SSL(host, int(port), timeout=10) if tls == "ssl" \
    else smtplib.SMTP(host, int(port), timeout=10)
if tls == "starttls":
    s.starttls(); s.ehlo()
if user:
    s.login(user, pw)
s.send_message(m); s.quit()
PY
        then
            TEST_RES+=$'email:   sent\n'
        else
            TEST_RES+="email:   FAILED — $(tail -1 /tmp/.ntest_smtp)\n"
        fi
        rm -f /tmp/.ntest_smtp
    fi
    say "Test alert results:\n\n$TEST_RES"
fi

say "Configuration applied!\n\nWeb interface:  https://$HN  (or this device's IP)\nUPS monitoring: nut-server on port 3493\nSNMP:           port 161\n\nYou can re-run this wizard anytime: sudo firstboot-wizard.sh"
yesno "Reboot now to apply everything cleanly?" && reboot || true
