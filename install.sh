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
PI_MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
grep -qi 'raspberry pi' <<<"$PI_MODEL" \
    || echo "!! Warning: /proc/device-tree/model does not look like a Raspberry Pi"

echo "============================================================"
echo " APC NMC appliance installer   (repo: $REPO @ $BRANCH)"
echo "============================================================"

# ---------------------------------------------------------------- packages
export DEBIAN_FRONTEND=noninteractive
echo ">> Updating package lists..."
apt-get update
echo ">> Installing packages (this can take a few minutes)..."
apt-get install -y \
    nut nut-server nut-client \
    snmpd snmp \
    whiptail openssl rsync parted dosfstools cloud-guest-utils \
    curl ca-certificates iputils-ping git ethtool \
    network-manager avahi-daemon python3 \
    build-essential libusb-1.0-0-dev

# newt breaks on unknown TERM types (PuTTY variants, serial) — keys leak
# as raw escapes like ^[[C. Fall back to xterm when TERM is unusable.
export TERM="${TERM:-xterm}"
infocmp "$TERM" >/dev/null 2>&1 || export TERM=xterm
# When run as `curl | bash` or piped/tee'd, stdin and stdout may not be the
# terminal — whiptail draws on stdout, so the dialog would be invisible and
# look like a hang. Always read AND draw on the controlling terminal.
W() { /usr/bin/whiptail "$@" </dev/tty >/dev/tty; }
aborted() { printf '\nInstall aborted.\n' >&2; exit 130; }
trap 'aborted' INT TERM
# yes/no prompts: Yes skips nothing (No = skip feature); Esc = abort install.
confirm() {
    W --yesno "$1\n\n(Esc = abort install)" 13 68
    local rc=$?
    [ $rc -eq 255 ] && aborted
    return $rc
}

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
put usr/local/sbin/apc-nmc-update.sh    0755
put usr/local/sbin/firstboot-wizard.sh  0755
put usr/local/sbin/net-watchdog.sh      0755
put usr/local/sbin/nut-usb-detect.sh    0755
put usr/local/sbin/pi-system-backup.sh  0755

# Run a long build step quietly (full output -> $log) while showing a
# spinner, elapsed time and the latest log line so builds don't look hung.
_run_step() {
    local label="$1" log="$2" pid rc=0 i=0 spin='|/-\' start=$SECONDS
    shift 2
    if [ -t 1 ]; then
        "$@" >>"$log" 2>&1 & pid=$!
        while kill -0 "$pid" 2>/dev/null; do
            printf '\r   %s %-30s %4ds  %-56.56s' "${spin:i++%4:1}" \
                "$label" $((SECONDS-start)) \
                "$(tail -n1 "$log" 2>/dev/null | tr -d '\r')"
            sleep 0.3
        done
        wait "$pid" || rc=$?
        printf '\r   %-3s %-30s %4ds%57s\n' \
            "$([ $rc -eq 0 ] && echo OK || echo FAIL)" \
            "$label" $((SECONDS-start)) ""
    else
        "$@" >>"$log" 2>&1 || rc=$?
        echo "   $label: $([ $rc -eq 0 ] && echo OK || echo FAIL)"
    fi
    return $rc
}

# apc_modbus is not in Debian's nut package — it is built from the vendored
# NUT 2.8.5 source (with the NUT libmodbus fork for USB support). Falls back
# to the shipped prebuilt binary + library if no source is present.
build_nut_driver() {
    local work=/tmp/nutbuild.$$ J; J=$(nproc 2>/dev/null || echo 1)
    mkdir -p "$work" || return 1
    echo ">> Building libmodbus (NUT fork, USB transport) from source..."
    tar -xzf "$SRC"/vendor/libmodbus-*.tar.gz -C "$work" || return 1
    (cd "$work"/libmodbus-* \
        && _run_step "libmodbus: configure" "$work/build.log" \
            ./configure --prefix=/usr/local \
        && _run_step "libmodbus: make -j$J" "$work/build.log" make -j"$J" \
        && _run_step "libmodbus: make install" "$work/build.log" \
            make install) || {
        echo "!! libmodbus build failed — see $work/build.log"; return 1; }
    ldconfig
    echo ">> Building apc_modbus from NUT 2.8.5 source (a few minutes)..."
    tar -xzf "$SRC"/vendor/nut-2.8.5-src.tar.gz -C "$work" || return 1
    # The vendored snapshot inverts the APC_VF_UNAVAILABLE test in
    # _apc_modbus_setvar(), so every SET VAR is rejected as "not available"
    # while upsd still ACKs it (battery.date writes silently do nothing).
    # Re-apply the fix if the tarball is ever regenerated unpatched —
    # see vendor/patches/apc_modbus-setvar-unavailable.patch.
    if grep -q '!(apc_value->value_flags & APC_VF_UNAVAILABLE)' \
            "$work"/nut-2.8.5/drivers/apc_modbus.c 2>/dev/null; then
        sed -i 's/!(apc_value->value_flags & APC_VF_UNAVAILABLE)/(apc_value->value_flags \& APC_VF_UNAVAILABLE)/' \
            "$work"/nut-2.8.5/drivers/apc_modbus.c || return 1
    fi
    (cd "$work"/nut-2.8.5 \
        && _run_step "NUT: configure" "$work/build.log" \
            ./configure --with-drivers=apc_modbus --with-modbus+usb=yes \
                --with-doc=no --without-asciidoc --without-pkg-config \
        && _run_step "NUT: make -j$J" "$work/build.log" make -j"$J" \
        && _run_step "NUT: install driver" "$work/build.log" \
            install -s -m 0755 drivers/apc_modbus /usr/lib/nut/) || {
        echo "!! NUT build failed — see $work/build.log"; return 1; }
    echo ">> apc_modbus built and installed to /usr/lib/nut/"
    rm -rf "$work"
}
NUT_BUILT=0
if ls "$SRC"/vendor/nut-2.8.5-src.tar.gz >/dev/null 2>&1 \
   && ls "$SRC"/vendor/libmodbus-*.tar.gz >/dev/null 2>&1; then
    build_nut_driver && NUT_BUILT=1 || echo "!! falling back to shipped binaries"
fi
if [ $NUT_BUILT -eq 0 ]; then
    # The shipped binaries are aarch64 ELF — installing them on a 32-bit
    # (armv7l) or non-ARM OS would just produce a driver that can't exec.
    # Those platforms must build from source, handled above.
    if [ "$(uname -m)" != "aarch64" ]; then
        echo "!! shipped apc_modbus/libmodbus are aarch64-only — this system"
        echo "   is $(uname -m); install build tools and the vendor sources"
        echo "   so the driver can be compiled instead"
    elif [ -f "$SRC/usr/lib/nut/apc_modbus" ]; then
        put usr/lib/nut/apc_modbus 0755
        if ls "$SRC"/usr/local/lib/libmodbus.so.5* >/dev/null 2>&1; then
            put usr/local/lib/libmodbus.so.5 0644
            put usr/local/lib/libmodbus.so.5.1.0 0644
            ln -sf libmodbus.so.5.1.0 /usr/local/lib/libmodbus.so.5
            ln -sf libmodbus.so.5 /usr/local/lib/libmodbus.so
        fi
        ldconfig 2>/dev/null || true
    else
        echo "!! no apc_modbus driver available (no source, no binary)"
    fi
fi
# The driver is built with custom paths — bridge them to the Debian layout:
#   sysconfdir=/usr/local/ups/etc  and  statepath=/var/state/ups
mkdir -p /usr/local/ups /var/state
ln -sfn /etc/nut /usr/local/ups/etc
ln -sfn /run/nut /var/state/ups

# --------------------------------------------------------------- deploy conf
echo ">> Installing configuration files..."
put etc/apc-nmc/nmc.conf.default 0644
# update-signing.pub is the maintainer's release-signing public key —
# ship it only if present; the updater signature-checks payloads then
put etc/apc-nmc/update-signing.pub 0644 2>/dev/null || true
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
put etc/systemd/system/apc-nmc-update.service 0644
put etc/systemd/system/apc-nmc-update-rollback.service 0644
put etc/systemd/system/apc-nmc-update-check.service 0644
put etc/systemd/system/apc-nmc-update-check.timer 0644
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
# NUT daemons run as the 'nut' user — they must be able to read these files
chgrp -R nut /etc/nut

# sync vendorid/productid with whatever UPS is attached right now
/usr/local/sbin/nut-usb-detect.sh 2>/dev/null || true
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
# the pass_persist agent runs as Debian-snmp and reads [upsd]/[snmp] creds
# from this file — without group-read, SNMP SETs and traps silently break
chgrp Debian-snmp /etc/apc-nmc/nmc.conf 2>/dev/null || true

# software version stamp, used by the update check (About > System)
install -D -m 0644 "$SRC/VERSION" /etc/apc-nmc/version 2>/dev/null || true

# self-signed HTTPS cert (wizard can regenerate with the real hostname)
# The SNMP agent runs as Debian-snmp and writes state/events/agent.log here;
# the setgid bit keeps new files in its group so root-created lock files
# stay writable by the agent.
install -d -o Debian-snmp -g Debian-snmp -m 2750 /var/lib/apc-nmc 2>/dev/null \
    || mkdir -p /var/lib/apc-nmc
if [ ! -s /etc/apc-nmc/nmc.key ]; then
    echo ">> Generating self-signed TLS certificate..."
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

if [[ "$PI_MODEL" == *"Pi 4"* ]]; then
    if confirm "Apply Pi 4 mild overclock? (arm_freq=1750, over_voltage=2, gpu_freq=600)"; then
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
else
    echo ">> Skipping Pi 4 overclock option (detected: ${PI_MODEL:-unknown})"
fi

# kernel watchdog -> systemd ping + panic reboot (sysctl.d conf deployed above)
install -D -m 0644 /dev/stdin /etc/systemd/system.conf.d/60-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=14
EOF

# dedicated swapfile (UPS shutdown workloads)
if confirm "Create a dedicated 10G /var/swap swapfile?"; then
    if [ ! -f /var/swap ]; then
        echo ">> Creating /var/swap (10G)..."
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
systemctl daemon-reload && echo "   systemd reloaded"
systemctl unmask ssh.service 2>/dev/null || true
for s in nut-server nut-monitor nut-driver-enumerator.path \
         nut-driver-enumerator.service \
         snmpd apc-nmc-web net-watchdog.timer apc-nmc-update-check.timer \
         ssh serial-getty@ttyS0; do
    echo "   enabling: $s"
    systemctl enable "$s" 2>&1 || echo "   !! could not enable $s"
done
systemctl mask userconfig.service 2>/dev/null || true
echo ">> Services enabled."

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
