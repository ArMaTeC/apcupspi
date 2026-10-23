#!/bin/bash
# apc-nmc-wifi-hw: apply onboard WiFi/Bluetooth boot-config changes.
#
# The installer offers hardware hardening that writes
#   dtoverlay=disable-wifi / dtoverlay=disable-bt
# into config.txt (inside the "# >>> apc-nmc" block). Those overlays are
# applied by the firmware at boot, so re-enabling the radio means editing
# config.txt and rebooting — the web service is sandboxed away from /boot,
# so it writes a request file and starts this unit instead.
#
# Request file: /var/lib/apc-nmc/wifi-hw.request — one "wifi|bt=enable|disable"
# per line. Consumed (deleted) after processing.
set -euo pipefail

TAG="apc-nmc-wifi-hw"
REQ="${APCNMC_WIFIHW_REQ:-/var/lib/apc-nmc/wifi-hw.request}"
BOOTCFG="${APCNMC_BOOTCFG:-/boot/firmware/config.txt}"
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt

[ -f "$REQ" ] || exit 0                      # nothing requested
[ -f "$BOOTCFG" ] || { logger -t "$TAG" "no config.txt found"; exit 1; }

rm -f "$REQ.work"                            # stale run leftover
mv "$REQ" "$REQ.work"
changed=0

while IFS='=' read -r target state; do
    case "$target" in wifi|bt) ;; *) continue;; esac
    case "$state" in enable|disable) ;; *) continue;; esac
    ov="disable-$target"

    if [ "$state" = "enable" ]; then
        # un-disable = remove every active occurrence of the overlay
        grep -q "^[[:space:]]*dtoverlay=$ov" "$BOOTCFG" || continue
        sed -i "/^[[:space:]]*dtoverlay=$ov\\b/d" "$BOOTCFG"
        changed=1
        logger -t "$TAG" "removed dtoverlay=$ov from $BOOTCFG"
        continue
    fi

    # disable: already present -> nothing to do
    grep -q "^[[:space:]]*dtoverlay=$ov" "$BOOTCFG" && continue
    if grep -q '# >>> apc-nmc' "$BOOTCFG" \
            && sed -n '/# >>> apc-nmc/,/# <<< apc-nmc/p' "$BOOTCFG" \
               | grep -q '^\[all\]'; then
        # inside the managed block, right after its [all] header so the
        # overlay lands in the unconditional section, not a [piN] one
        sed -i '/# >>> apc-nmc/,/# <<< apc-nmc/ {
            /^\[all\]/a dtoverlay='"$ov"'
        }' "$BOOTCFG"
    elif grep -q '# <<< apc-nmc' "$BOOTCFG"; then
        # managed block exists but has no [all] — re-open one before the
        # end marker, then drop the overlay inside it (two inserts keep
        # the order without \n-escaping tricks)
        sed -i "/# <<< apc-nmc/i [all]" "$BOOTCFG"
        sed -i "/# <<< apc-nmc/i dtoverlay=$ov" "$BOOTCFG"
    else
        printf '\n# >>> apc-nmc\n[all]\ndtoverlay=%s\n# <<< apc-nmc\n' \
            "$ov" >> "$BOOTCFG"
    fi
    changed=1
    logger -t "$TAG" "added dtoverlay=$ov to $BOOTCFG"
done < "$REQ.work"

rm -f "$REQ.work"
[ "$changed" = 1 ] && logger -t "$TAG" "boot config updated — reboot required"
exit 0
