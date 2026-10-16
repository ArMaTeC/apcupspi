#!/usr/bin/env bash
#
# firstboot-wizard.sh — interactive first-boot configuration wizard.
# Runs once on the console (via firstboot-wizard.service) on a clean/published
# image, before the login prompt and before sshd starts.
# Can also be re-run manually: sudo firstboot-wizard.sh
#
set -u
W=/usr/bin/whiptail
NMC_CONF=/etc/apc-nmc/nmc.conf
NMC_DIR=/etc/apc-nmc
UPS_CONF=/etc/nut/ups.conf
UPSD_USERS=/etc/nut/upsd.users
UPSMON_CONF=/etc/nut/upsmon.conf
SNMP_CONF=/etc/snmp/snmpd.conf

say()  { $W --msgbox "$1" 12 70; }
ask()  { $W --inputbox "$1" 10 70 "$2" 3>&1 1>&2 2>&3; }
askpw(){ $W --passwordbox "$1" 10 70 3>&1 1>&2 2>&3; }

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

pw_pair() { # $1=prompt -> echoes confirmed password
    while :; do
        p1=$(askpw "$1"); p2=$(askpw "Confirm: $1")
        if [ -z "$p1" ]; then $W --msgbox "Password cannot be empty." 8 50; continue; fi
        [ "$p1" = "$p2" ] && { echo "$p1"; return; }
        $W --msgbox "Passwords do not match, try again." 8 50
    done
}

# ------------------------------------------------------------------ collect
$W --msgbox "Welcome!\n\nThis is the first boot of this image. The wizard will now ask you to configure:\n\n  - hostname and credentials\n  - network\n  - UPS monitoring (NUT) and web interface\n  - SNMP\n\nPress OK to begin." 18 70

HN=$(ask "Hostname for this device:" "apc-nmc"); HN=${HN:-apc-nmc}
ROOTPW=$(pw_pair "New root password (used for console + SSH):")
SSHKEY=$(ask "SSH public key to authorize for root (ssh-ed25519/ssh-rsa/...).\nLeave empty to use password login only:" "")
NET=$($W --menu "Network configuration for eth0:" 12 70 2 \
    "dhcp"   "Automatic (DHCP) - recommended" \
    "static" "Static IP address" 3>&1 1>&2 2>&3 || echo dhcp)
CIDR=""; GW=""; DNS=""
if [ "$NET" = "static" ]; then
    CIDR=$(ask "Static address in CIDR notation (e.g. 192.168.1.50/24):" "")
    GW=$(ask "Default gateway (e.g. 192.168.1.1):" "")
    DNS=$(ask "DNS server(s), space separated (e.g. 192.168.1.1 1.1.1.1):" "")
fi

WEBUSER=$(ask "Web interface admin username:" "apc"); WEBUSER=${WEBUSER:-apc}
WEBPW=$(pw_pair "Web interface admin password:")

UPS=$(ask "UPS name in NUT (must match the [section] in ups.conf):" "myups"); UPS=${UPS:-myups}
DRIVER=$(ask "NUT driver for your UPS (e.g. usbhid-ups, apc_modbus, blazer_usb):" "apc_modbus"); DRIVER=${DRIVER:-apc_modbus}
UPORT=$(ask "NUT port (e.g. auto, /dev/ttyS0, /dev/usb/hiddev0):" "auto"); UPORT=${UPORT:-auto}
NUTPW=$(pw_pair "NUT monitor password (used by upsmon + web interface):")
ADMPW=$(askpw "NUT admin password (leave empty = same as monitor password):")
[ -z "$ADMPW" ] && ADMPW="$NUTPW"

ROCOM=$(ask "SNMP read community (v1/v2c):" "public"); ROCOM=${ROCOM:-public}
RWCOM=$(ask "SNMP write community (v1/v2c):" "private"); RWCOM=${RWCOM:-private}
LOC=$(ask "Device location (shown in web UI + SNMP):" "Server Room"); LOC=${LOC:-Server Room}
CONTACT=$(ask "Device contact (shown in web UI + SNMP):" "admin@localhost"); CONTACT=${CONTACT:-admin@localhost}

$W --yesno "Generate a fresh self-signed HTTPS certificate for the web interface?\n(Recommended: the image ships a placeholder cert.)" 10 70 && REGEN_CERT=1 || REGEN_CERT=0

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
$W --yesno "$SUMMARY" 22 70 || { say "Cancelled. The wizard will run again on next boot,\nor re-run: sudo firstboot-wizard.sh"; exit 0; }

# -------------------------------------------------------------------- apply
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
ini_set "$UPSD_USERS" upsmon password "$NUTPW"
ini_set "$UPSD_USERS" admin  password "$ADMPW"
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
    systemctl restart "$s" 2>/dev/null || true
done

say "Configuration applied!\n\nWeb interface:  https://$HN  (or this device's IP)\nUPS monitoring: nut-server on port 3493\nSNMP:           port 161\n\nYou can re-run this wizard anytime: sudo firstboot-wizard.sh"
$W --yesno "Reboot now to apply everything cleanly?" 8 60 && reboot || true
