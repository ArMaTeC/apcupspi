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
# where stdin is the pipe.
W() { /usr/bin/whiptail "$@" </dev/tty; }

die() { printf '\nWizard aborted.\n' >&2; exit 130; }
trap 'die' INT TERM
NMC_CONF=/etc/apc-nmc/nmc.conf
NMC_DIR=/etc/apc-nmc
UPS_CONF=/etc/nut/ups.conf
UPSD_USERS=/etc/nut/upsd.users
UPSMON_CONF=/etc/nut/upsmon.conf
SNMP_CONF=/etc/snmp/snmpd.conf

say()  { W --msgbox "$1" 12 70; }
ask()  { W --inputbox "$1" 10 70 "$2" 3>&1 1>&2 2>&3; }
askpw(){ W --passwordbox "$1" 10 70 3>&1 1>&2 2>&3; }

ini_set() { # file section key value — write an ini key via python3
    python3 - "$1" "$2" "$3" "$4" <<'PY'
import configparser, sys
f, s, k, v = sys.argv[1:5]
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
    awk -v sec="[$1]" -v pw="$2" '
        $0 == sec { insec=1; print; next }
        /^\[/   { insec=0 }
        insec && match($0, /^[[:space:]]*password[[:space:]]*=/) {
            print substr($0, 1, RLENGTH) " " pw; found=1; next
        }
        { print }
        END { if (!found) exit 1 }
    ' "$UPSD_USERS" > "$UPSD_USERS.tmp" \
      && mv "$UPSD_USERS.tmp" "$UPSD_USERS" && chmod 640 "$UPSD_USERS"
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

# ------------------------------------------------------------------ collect
W --msgbox "Welcome!\n\nThis is the first boot of this image. The wizard will now ask you to configure:\n\n  - hostname and credentials\n  - network\n  - UPS monitoring (NUT) and web interface\n  - SNMP\n\nPress OK to begin. Esc or Ctrl+C exits at any prompt." 18 70 || die

HN=$(ask "Hostname for this device:" "apc-nmc") || die; HN=${HN:-apc-nmc}
ROOTPW=$(pw_pair "New root password (used for console + SSH):") || die
SSHKEY=$(ask "SSH public key to authorize for root (ssh-ed25519/ssh-rsa/...).\nLeave empty to use password login only:" "") || die
NET=$(W --menu "Network configuration for eth0:" 12 70 2 \
    "dhcp"   "Automatic (DHCP) - recommended" \
    "static" "Static IP address" 3>&1 1>&2 2>&3 || echo dhcp)
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

W --yesno "Generate a fresh self-signed HTTPS certificate for the web interface?\n(Recommended: the image ships a placeholder cert.)" 10 70 && REGEN_CERT=1 || REGEN_CERT=0

SUMMARY="Hostname:      $HN
Root password: (set)
SSH key:       $([ -n "$SSHKEY" ] && echo provided || echo none)
Network:       $NET $CIDR
Web admin:     $WEBUSER
UPS:           $UPS ($DRIVER, port $UPORT)
SNMP:          ro=$ROCOM rw=$RWCOM
Location:      $LOC / $CONTACT
New TLS cert:  $([ $REGEN_CERT -eq 1 ] && echo yes || echo no)

Apply this configuration?"
W --yesno "$SUMMARY" 22 70 || { say "Cancelled. The wizard will run again on next boot,\nor re-run: sudo firstboot-wizard.sh"; exit 0; }

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
sed -i -E "s|^MONITOR[[:space:]].*|$(esc_sed "MONITOR ${UPS}@localhost 1 upsmon ${NUTPW} primary")|" "$UPSMON_CONF"
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

say "Configuration applied!\n\nWeb interface:  https://$HN  (or this device's IP)\nUPS monitoring: nut-server on port 3493\nSNMP:           port 161\n\nYou can re-run this wizard anytime: sudo firstboot-wizard.sh"
W --yesno "Reboot now to apply everything cleanly?" 8 60 && reboot || true
