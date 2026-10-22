#!/usr/bin/env bash
# apc-nmc-dot1x.sh — wired 802.1X (EAPoL) supplicant for the card.
#
#   genconf   render /run/apc-nmc/wired-<iface>.conf from [dot1x]
#   run       genconf + exec wpa_supplicant (service ExecStart)
#
# [dot1x] keys: iface (default eth0), eap (PEAP|TLS), identity,
# password, ca_cert, client_cert, private_key, private_key_pass,
# phase2 (default auth=MSCHAPV2, PEAP only).
set -u
CONF=/etc/apc-nmc/nmc.conf

conf() {  # conf <key>
    awk -v k="$1" '
        /^\[/ { sec = ($0 ~ /^\[dot1x\]/) }
        sec && $0 ~ "^[[:space:]]*" k "[[:space:]]*=" {
            sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, "")
            print; exit
        }' "$CONF" 2>/dev/null
}

iface() { local i; i=$(conf iface); echo "${i:-eth0}"; }

genconf() {
    local i f eap id pw ca cc pk pkp ph2
    i=$(iface)
    f=/run/apc-nmc/wired-$i.conf
    eap=$(conf eap); eap=${eap:-PEAP}
    id=$(conf identity); pw=$(conf password)
    ca=$(conf ca_cert); cc=$(conf client_cert); pk=$(conf private_key)
    pkp=$(conf private_key_pass); ph2=$(conf phase2)
    ph2=${ph2:-auth=MSCHAPV2}
    mkdir -p /run/apc-nmc
    {
        echo "ctrl_interface=/run/wpa_supplicant"
        echo "ap_scan=0"
        echo "network={"
        echo "    key_mgmt=IEEE8021X"
        echo "    eap=$eap"
        [ -n "$id" ]  && echo "    identity=\"$id\""
        [ -n "$ca" ]  && echo "    ca_cert=\"$ca\""
        case "${eap^^}" in
            PEAP|TTLS|FAST)
                [ -n "$pw" ]  && echo "    password=\"$pw\""
                echo "    phase2=\"$ph2\"" ;;
            TLS)
                [ -n "$cc" ]  && echo "    client_cert=\"$cc\""
                [ -n "$pk" ]  && echo "    private_key=\"$pk\""
                [ -n "$pkp" ] && echo "    private_key_passwd=\"$pkp\"" ;;
        esac
        echo "}"
    } > "$f"
    chmod 600 "$f"
    echo "$f"
}

case "${1:-run}" in
    genconf) genconf ;;
    run)
        i=$(iface)
        f=$(genconf)
        exec /sbin/wpa_supplicant -s -c "$f" -i "$i" -D wired
        ;;
    *) echo "usage: $0 {genconf|run}" >&2; exit 2 ;;
esac
