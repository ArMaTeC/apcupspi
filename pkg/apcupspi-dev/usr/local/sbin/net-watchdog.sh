#!/bin/bash
# net-watchdog: recover network interfaces that lose connectivity.
# Escalation per interface: NM re-activate -> (wlan0 only) brcmfmac reload -> reboot (last resort, once per boot).

TAG="net-watchdog"
STATE_DIR="/run/net-watchdog"
REBOOTED_FLAG="$STATE_DIR/rebooted"
mkdir -p "$STATE_DIR"

log() { logger -t "$TAG" -- "$*"; }

GW="$(ip -4 route show default scope global | awk 'NR==1{print $3}')"
GW="${GW:-192.168.1.1}"

# Discover the NetworkManager connections dynamically (fallbacks below also
# try a bare `nmcli device connect` so this stays usable without one).
ETH_CON="$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: '$2=="eth0"{print $1;exit}')"
[ -n "$ETH_CON" ] || ETH_CON="$(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2 ~ /ethernet|802-3/{print $1;exit}')"
WLAN_CON="$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: '$2=="wlan0"{print $1;exit}')"
[ -n "$WLAN_CON" ] || WLAN_CON="$(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2 ~ /wifi|wireless/{print $1;exit}')"

# Ping the interface's OWN default gateway: with eth0+wlan0 on different
# subnets, the first global default GW may be unreachable via the other
# interface even though that link is healthy (false failure -> bounce).
gw_for() { ip -4 route show default dev "$1" scope global 2>/dev/null | awk 'NR==1{print $3;exit}'; }

ping_ok() {
    PING_GW="$(gw_for "$1")"; PING_GW="${PING_GW:-$GW}"
    ping -I "$1" -c 2 -W 3 -q "$PING_GW" >/dev/null 2>&1
}

iface_has_ip() { ip -4 addr show dev "$1" 2>/dev/null | grep -q "inet "; }

# returns 0 if interface recovered, 1 otherwise
recover_iface() {
    local ifname="$1" con="$2"
    iface_has_ip "$ifname" || { log "$ifname: no IPv4 address, activating $con"; }
    nmcli -w 30 con up "$con" >/dev/null 2>&1 || nmcli -w 30 device connect "$ifname" >/dev/null 2>&1
    sleep 5
    ping_ok "$ifname"
}

if ! ping_ok eth0; then
    log "eth0: cannot reach gateway $PING_GW - recovering"
    if recover_iface eth0 "${ETH_CON:-eth0}"; then
        log "eth0: recovered after connection bounce"
    else
        log "eth0: still unreachable after bounce"
    fi
fi

# wlan0: only manage it if wifi is enabled and it is meant to be up
if [ "$(nmcli -t -f WIFI radio)" = "enabled" ]; then
    if ping_ok wlan0; then
        rm -f "$STATE_DIR/wlan0.reloaded"
    else
        log "wlan0: cannot reach gateway $PING_GW - bouncing connection"
        if recover_iface wlan0 "${WLAN_CON:-wlan0}"; then
            log "wlan0: recovered after connection bounce"
            rm -f "$STATE_DIR/wlan0.reloaded"
        elif [ ! -f "$STATE_DIR/wlan0.reloaded" ]; then
            # brcmfmac firmware wedge (RXHEADER -110) looks connected but passes no
            # traffic; only a module reload (or reboot) clears it.
            log "wlan0: bounce failed - reloading brcmfmac (firmware wedge recovery)"
            nmcli device disconnect wlan0 >/dev/null 2>&1
            modprobe -r brcmfmac >/dev/null 2>&1
            sleep 3
            modprobe brcmfmac
            touch "$STATE_DIR/wlan0.reloaded"
            sleep 15
            nmcli -w 45 device connect wlan0 >/dev/null 2>&1 || \
                nmcli -w 45 con up "${WLAN_CON:-wlan0}" >/dev/null 2>&1
            sleep 5
            if ping_ok wlan0; then
                log "wlan0: recovered after brcmfmac reload"
            else
                log "wlan0: still unreachable after brcmfmac reload"
            fi
        else
            log "wlan0: still unreachable (brcmfmac already reloaded this boot)"
        fi
    fi
fi

# Last resort: if NO interface can reach the gateway at all, count the strikes.
# Reboot once per boot after ~10 minutes (5 consecutive runs at 2-min interval)
# of total connectivity loss. Skipped entirely if already rebooted by us.
if ping_ok eth0 || { [ "$(nmcli -t -f WIFI radio)" = "enabled" ] && ping_ok wlan0; }; then
    rm -f "$STATE_DIR/all.fail"
else
    n=0; [ -f "$STATE_DIR/all.fail" ] && n=$(cat "$STATE_DIR/all.fail")
    n=$((n + 1)); echo "$n" > "$STATE_DIR/all.fail"
    log "total connectivity loss ($n consecutive checks)"
    if [ "$n" -ge 5 ] && [ ! -f "$REBOOTED_FLAG" ] && [ "$(cut -d. -f1 /proc/uptime)" -gt 600 ]; then
        touch "$REBOOTED_FLAG"
        log "no connectivity for ~10 min despite recovery attempts - rebooting"
        systemctl reboot
    fi
fi

exit 0
