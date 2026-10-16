#!/usr/bin/env bash
#
# install.sh — APC NMC emulation appliance installer
#
# Transforms a fresh Raspberry Pi OS (or Debian+rpi-kernel) install on a
# Pi 4 / Pi 5 into the full appliance: NUT upsd/upsmon, apc_modbus driver,
# net-snmp with PowerNet-MIB pass_persist agent, and the apc-nmc-web
# HTTPS management interface — plus watchdogs, sysctl and service tuning.
#
# Usage:
#   sudo ./install.sh                 (from a repo checkout, or standalone)
#
# When run standalone it downloads the payload files from GitHub:
#   APCUPS_REPO=owner/repo APCUPS_BRANCH=main ./install.sh
#
# At the end it offers to run the interactive firstboot wizard, which
# collects hostname, root password, SSH key, network, web credentials,
# UPS/NUT passwords, SNMP communities and device info.
#
set -euo pipefail

REPO="${APCUPS_REPO:-ArMaTeC/apcupspi}"
BRANCH="${APCUPS_BRANCH:-main}"

[ "$EUID" -eq 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "This installer targets Debian/Raspberry Pi OS (apt)." >&2; exit 1; }
grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null \
    || echo "!! Warning: /proc/device-tree/model does not look like a Raspberry Pi"

echo "============================================================"
echo " APC NMC appliance installer   (repo: $REPO @ $BRANCH)"
echo "============================================================"

# ---------------------------------------------------------------- packages
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    nut nut-server nut-client \
    snmpd snmp \
    whiptail openssl rsync parted dosfstools cloud-guest-utils \
    curl ca-certificates iputils-ping git \
    network-manager avahi-daemon python3

W=/usr/bin/whiptail
confirm() { $W --yesno "$1" 10 68; }

# ------------------------------------------------------- payload resolution
SRC=""
if [ -f "$(dirname "$0")/usr/local/bin/apc-nmc-web.py" ]; then
    SRC=$(cd "$(dirname "$0")" && pwd)
elif [ -f "$PWD/usr/local/bin/apc-nmc-web.py" ]; then
    SRC=$PWD
else
    echo ">> Fetching payload from github.com/$REPO ($BRANCH)..."
    SRC=$(mktemp -d /tmp/apcups-src.XXXXXX)
    curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" \
        | tar -xz --strip-components=1 -C "$SRC"
fi
echo ">> Payload source: $SRC"

put() {  # put <repo-relative-path> [dest-mode]  — file or dir, parents created
    local rel="$1" mode="${2:-}"
    [ -e "$SRC/$rel" ] || { echo "!! missing payload: $rel"; return 1; }
    if [ -d "$SRC/$rel" ]; then
        mkdir -p "/$rel"
        cp -a "$SRC/$rel/." "/$rel/"
    else
        install -D ${mode:+-m "$mode"} "$SRC/$rel" "/$rel"
    fi
}

# -------------------------------------------------------------- deploy code
echo ">> Installing application files..."
put usr/local/bin/apc-nmc-web.py   0755
put usr/local/bin/apc-nmc-agent.py 0755
put usr/local/bin/apc-nmc-web              # web UI assets (js/css)
put usr/local/sbin/firstboot-wizard.sh  0755
put usr/local/sbin/pi-system-backup.sh  0755
put usr/local/sbin/net-watchdog.sh      0755

# apc_modbus is not in Debian's nut package — it was built locally.
if [ -f "$SRC/usr/lib/nut/apc_modbus" ]; then
    put usr/lib/nut/apc_modbus 0755
else
    echo "!! usr/lib/nut/apc_modbus not in payload — the apc_modbus driver"
    echo "   must be copied from a working system or built from nut>=2.8.1 source."
fi
if ls "$SRC"/usr/local/lib/libmodbus.so.5* >/dev/null 2>&1; then
    put usr/local/lib/libmodbus.so.5 0644
    put usr/local/lib/libmodbus.so.5.1.0 0644
    ln -sf libmodbus.so.5.1.0 /usr/local/lib/libmodbus.so.5
    ln -sf libmodbus.so.5 /usr/local/lib/libmodbus.so
else
    apt-get install -y -qq libmodbus5 || true
fi
ldconfig 2>/dev/null || true

# --------------------------------------------------------------- deploy conf
echo ">> Installing configuration files..."
put etc/apc-nmc/nmc.conf.default 0644
put etc/apc-nmc/ca 2>/dev/null || true
put etc/nut/nut.conf 0644
put etc/nut/ups.conf 0644
put etc/nut/upsd.conf 0644
put etc/nut/upsd.users.default 0640
put etc/nut/upssched.conf 0644
put etc/sysctl.d/90-oom-guard.conf 0644
put etc/sysctl.d/91-apc-nmc-panic.conf 0644
put etc/logrotate.d/apc-nmc 0644
put etc/NetworkManager/conf.d/99-wifi-powersave-off.conf 0644
put etc/avahi/avahi-daemon.conf 0644
put etc/systemd/system/apc-nmc-web.service 0644
put etc/systemd/system/apc-nmc-web.service.d 2>/dev/null || true
put etc/systemd/system/net-watchdog.service 0644
put etc/systemd/system/net-watchdog.timer 0644
put etc/systemd/system/firstboot-wizard.service 0644
put etc/systemd/system/nut-server.service.d 2>/dev/null || true
put etc/systemd/system/nut-driver@.service.d 2>/dev/null || true
put etc/systemd/system/snmpd.service.d 2>/dev/null || true
put etc/systemd/system/ssh.service.d 2>/dev/null || true

# --- secret-bearing configs are generated fresh (placeholders; the wizard
# --- fills real values):
install -D -m 0640 /dev/stdin /etc/nut/upsd.users <<'EOF'
[upsmon]
    password = CHANGE_ME
    upsmon master
    actions = SET
    instcmds = ALL
[admin]
    password = CHANGE_ME
    actions = SET
    instcmds = ALL
EOF
install -D -m 0640 /dev/stdin /etc/nut/upsmon.conf <<'EOF'
MONITOR myups@localhost 1 upsmon CHANGE_ME primary
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
POLLFREQ 5
POLLFREQALERT 5
HOSTSYNC 15
DEADTIME 15
POWERDOWNFLAG "/etc/killpower"
OFFDURATION 30
RBWARNTIME 43200
NOCOMMWARNTIME 300
FINALDELAY 5
EOF
install -D -m 0640 /dev/stdin /etc/snmp/snmpd.conf <<'EOF'
# Emulate an APC Network Management Card 2 (AP9631)
# backed by apc-nmc-agent.py + NUT/upsd

agentaddress udp:161,udp6:161
agentuser Debian-snmp
agentgroup Debian-snmp

# --- identity: look like an AP9631 running AOS 7.x ---
override .1.3.6.1.2.1.1.1.0 octet_str "APC Web/SNMP Management Card (MB:v4.1.0 PF:v7.1.2 PN:apc_hw05_aos_712.bin AF1:v7.1.2 AN1:apc_hw05_sumx_712.bin MN:AP9631 HR:05 SN: 5A2436E12345 MD:04/17/2024)"
override .1.3.6.1.2.1.1.2.0 object_id .1.3.6.1.4.1.318.1.3.27
override .1.3.6.1.2.1.1.5.0 octet_str "apc-nmc"
sysLocation  Server Room
sysContact   admin@localhost
sysServices  72

# --- only serve trees a real NMC would have (no HOST-RESOURCES/UCD etc) ---
view nmc included .1.3.6.1.2.1.1
view nmc included .1.3.6.1.2.1.2
view nmc included .1.3.6.1.2.1.11
view nmc included .1.3.6.1.4.1.318

rocommunity CHANGE_ME  default -V nmc
rwcommunity CHANGE_ME 127.0.0.1 -V nmc

# --- APC PowerNet-MIB handled by the pass_persist backend ---
pass_persist .1.3.6.1.4.1.318 /usr/local/bin/apc-nmc-agent.py

dontLogTCPWrappersConnects yes

# --- SNMPv3 read-only user (AP9631 parity; RW stays v2c localhost) ---
createUser nmcro SHA "CHANGE_ME" AES "CHANGE_ME"
rouser nmcro priv -V nmc
EOF

install -D -m 0644 /dev/stdin /etc/ssh/sshd_config.d/99-apc-nmc.conf <<'EOF'
PermitRootLogin yes
PasswordAuthentication yes
EOF

# nmc.conf seeded from the sanitized template; the wizard patches it.
[ -f /etc/apc-nmc/nmc.conf ] || cp /etc/apc-nmc/nmc.conf.default /etc/apc-nmc/nmc.conf
chmod 640 /etc/apc-nmc/nmc.conf 2>/dev/null || true

# self-signed HTTPS cert (wizard can regenerate with the real hostname)
mkdir -p /var/lib/apc-nmc
if [ ! -s /etc/apc-nmc/nmc.key ]; then
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout /etc/apc-nmc/nmc.key -out /etc/apc-nmc/nmc.crt -days 3650 \
        -subj "/CN=apc-nmc" -addext "subjectAltName=DNS:apc-nmc" 2>/dev/null \
        && cat /etc/apc-nmc/nmc.crt /etc/apc-nmc/nmc.key > /etc/apc-nmc/nmc.pem \
        && chmod 600 /etc/apc-nmc/nmc.key /etc/apc-nmc/nmc.pem || true
fi

# -------------------------------------------------- optional system tweaks
BOOTCFG=/boot/firmware/config.txt; [ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
CFG_BLOCK() {
    sed -i '/# >>> apc-nmc/,/# <<< apc-nmc/d' "$BOOTCFG" 2>/dev/null || true
    cat >> "$BOOTCFG"
}

if confirm "Apply hardware hardening to config.txt?\n\n- enable UART + hardware watchdog\n- disable onboard WiFi and Bluetooth\n(Choose No to keep WiFi/BT enabled.)"; then
    CFG_BLOCK <<'EOF'
# >>> apc-nmc
[all]
enable_uart=1
disable_splash=1
dtparam=watchdog=on
dtoverlay=disable-wifi
dtoverlay=disable-bt
[pi5]
dtoverlay=nospi10
# <<< apc-nmc
EOF
    echo ">> config.txt hardening applied"
fi

if confirm "Apply Pi 4 mild overclock? (arm_freq=1750, over_voltage=2, gpu_freq=600)\n\nOnly affects Pi 4 hardware; Pi 5 ignores it."; then
    if grep -q '# <<< apc-nmc' "$BOOTCFG"; then
        sed -i '/# <<< apc-nmc/i [pi4]\nover_voltage=2\narm_freq=1750\ngpu_freq=600\ngpu_mem=16' "$BOOTCFG"
    else
        CFG_BLOCK <<'EOF'
# >>> apc-nmc
[pi4]
over_voltage=2
arm_freq=1750
gpu_freq=600
gpu_mem=16
# <<< apc-nmc
EOF
    fi
    echo ">> Pi4 overclock block applied"
fi

# kernel watchdog -> systemd ping + panic reboot (sysctl.d conf deployed above)
install -D -m 0644 /dev/stdin /etc/systemd/system.conf.d/60-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=14
EOF

# dedicated swapfile (UPS shutdown workloads)
if confirm "Create a dedicated 10G /var/swap swapfile?"; then
    if [ ! -f /var/swap ]; then
        fallocate -l 10G /var/swap && chmod 600 /var/swap && mkswap /var/swap >/dev/null
    fi
    grep -q '^/var/swap' /etc/fstab || echo '/var/swap none swap sw,pri=10 0 0' >> /etc/fstab
    swapon /var/swap 2>/dev/null || true
    systemctl mask rpi-resize-swap-file.service 2>/dev/null || true
fi

# Argon One case (optional hardware)
if confirm "Argon One case? Install fan/button daemon + i2c support."; then
    apt-get install -y -qq i2c-tools || true
    put etc/argon 2>/dev/null || true
    put etc/argononed.conf 0644 2>/dev/null || true
    put etc/argonunits.conf 0644 2>/dev/null || true
    put usr/lib/systemd/system/argononed.service 0644 2>/dev/null || true
    grep -q '^i2c-dev' /etc/modules || echo i2c-dev >> /etc/modules
    if grep -q '# <<< apc-nmc' "$BOOTCFG" && ! grep -q 'i2c_arm=on' "$BOOTCFG"; then
        sed -i '/# <<< apc-nmc/i dtparam=i2c_arm=on' "$BOOTCFG"
    fi
    ln -sf /etc/argon/argon-config       /usr/bin/argon-config
    ln -sf /etc/argon/argon-config       /usr/bin/argonone-config
    ln -sf /etc/argon/argonone-ir        /usr/bin/argonone-ir
    ln -sf /etc/argon/argon-uninstall.sh /usr/bin/argonone-uninstall
    systemctl enable argononed.service 2>/dev/null || true
fi

# ------------------------------------------------------------- enable stack
echo ">> Enabling services..."
systemctl daemon-reload
systemctl unmask ssh.service 2>/dev/null || true
for s in nut-server nut-monitor nut-driver-enumerator.path \
         snmpd apc-nmc-web net-watchdog.timer ssh serial-getty@ttyS0; do
    systemctl enable "$s" 2>/dev/null || true
done
systemctl enable nut-driver-enumerator.service 2>/dev/null || true
systemctl mask userconfig.service 2>/dev/null || true

echo
echo "============================================================"
echo " Base install complete."
echo " Next: run the configuration wizard to set hostname, root"
echo " password, network, web credentials, UPS and SNMP settings."
echo "============================================================"
if confirm "Run the configuration wizard now?"; then
    exec /usr/local/sbin/firstboot-wizard.sh
else
    echo "Run it later with: sudo firstboot-wizard.sh"
    echo "Reboot recommended to apply config.txt changes."
fi
