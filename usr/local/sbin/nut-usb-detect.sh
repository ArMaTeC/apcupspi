#!/usr/bin/env bash
#
# nut-usb-detect.sh — detect the attached UPS on USB and sync vendorid/
# productid in ups.conf before the driver starts. Runs as ExecStartPre of
# nut-driver@.service (arg $1 = UPS section name, e.g. "myups").
#
# Exit is always 0: detection failure must never block driver start.

set -u
CONF=/etc/nut/ups.conf
[ -f "$CONF" ] || exit 0

UPS=${1:-}
[ -n "$UPS" ] || UPS=$(grep -oE '^\[[^]]+\]' "$CONF" | head -1 | tr -d '[]')
[ -n "$UPS" ] || exit 0

sec_val() { # $1=key -> value inside the [$UPS] section
    awk -v s="[$UPS]" -v k="$1" '
        $0==s { f=1; next } /^\[/ { f=0 }
        f && $0 ~ "^[[:space:]]*" k "[[:space:]]*=" {
            sub(/[^=]*=/, ""); gsub(/[ \t]/, ""); print; exit
        }' "$CONF"
}

sec_has() { # $1=key -> rc 0 if key exists inside the [$UPS] section
    awk -v s="[$UPS]" -v k="$1" '
        $0==s { f=1; next } /^\[/ { f=0 }
        f && $0 ~ "^[[:space:]]*" k "[[:space:]]*=" { e=1 }
        END { exit !e }' "$CONF"
}

DRIVER=$(sec_val driver)

# Which USB vendors count as a UPS. apc_modbus only speaks APC MODBUS, so it
# is locked to 051d; other drivers get the common UPS vendor list.
VENDORS="051d"
[ "$DRIVER" = "apc_modbus" ] || VENDORS="051d 0463 0592 06da 0764 09ae 050d 0f03"

FV=""; FP=""
for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] || continue
    v=$(cat "$d/idVendor")
    case " $VENDORS " in *" $v "*)
        FV=$v; FP=$(cat "$d/idProduct"); break ;;
    esac
done
if [ -z "$FV" ]; then
    echo "nut-usb-detect: no UPS-class USB device found — leaving $CONF unchanged" >&2
    exit 0
fi

CV=$(sec_val vendorid); CP=$(sec_val productid)
if [ "$CV" = "$FV" ] && [ "$CP" = "$FP" ]; then
    echo "nut-usb-detect: [$UPS] already matches $FV:$FP"
    exit 0
fi

for kv in "vendorid=$FV" "productid=$FP"; do
    k=${kv%%=*}; val=${kv#*=}
    if sec_has "$k"; then
        sed -i -E "/^\\[$UPS\\]/,/^\\[/ s|^([[:space:]]*$k[[:space:]]*=[[:space:]]*).*|\\1$val|" "$CONF"
    else
        sed -i -E "0,/^\\[$UPS\\]/ s//&\n    $k = $val/" "$CONF"
    fi
done

echo "nut-usb-detect: [$UPS] updated ${CV:-none}:${CP:-none} -> $FV:$FP"
