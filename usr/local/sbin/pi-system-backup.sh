#!/usr/bin/env bash
#
# pi-system-backup.sh — build a flashable .img of this running system.
#
#   Normal mode : byte-identical partitioned image (same PARTUUIDs/UUIDs)
#                 of the live system. Flash it to a new disk -> boots as a clone.
#   --clean mode: same image but stripped of personal data/secrets and with the
#                 first-boot wizard enabled, so the image can be published.
#                 On first boot the wizard asks for hostname, root password,
#                 SSH key, network, web credentials, UPS/NUT and SNMP config.
#
# Usage:
#   pi-system-backup.sh [--clean] [--output FILE] [--no-gzip] [--headroom MB]
#
set -euo pipefail

CLEAN=0
GZIP=1
OUT=""
IMGROOT=/mnt/pi-imgbuild
HEADROOM_MB=2048
STAMP="$(date +%F)"

usage() { grep -E '^#' "$0" | head -14 | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --clean)     CLEAN=1 ;;
        --no-gzip)   GZIP=0 ;;
        --output)    OUT="$2"; shift ;;
        --headroom)  HEADROOM_MB="$2"; shift ;;
        -h|--help)   usage ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

[ "$EUID" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ -n "$OUT" ] || OUT="/root/pi4-backup-${STAMP}$([ $CLEAN -eq 1 ] && echo '-clean').img"
OUT_DIR=$(dirname "$OUT"); mkdir -p "$OUT_DIR"
echo ">> Output: $OUT  (clean=$CLEAN gzip=$GZIP)"

# ---------------------------------------------------------------- autodetect
ROOT_PART=$(findmnt -nro SOURCE /)
BOOT_PART=$(findmnt -nro SOURCE /boot/firmware)
DISK=/dev/$(lsblk -nro PKNAME "$ROOT_PART")
DISK_ID=$(sfdisk --disk-id "$DISK")
ROOT_UUID=$(blkid -s UUID -o value "$ROOT_PART")
ROOT_LABEL=$(blkid -s LABEL -o value "$ROOT_PART" || true)
BOOT_UUID=$(blkid -s UUID -o value "$BOOT_PART")
BOOT_VOLID=${BOOT_UUID//-/}
BOOT_LABEL=$(blkid -s LABEL -o value "$BOOT_PART" || true)
BOOT_MNT=$(findmnt -nro TARGET "$BOOT_PART")

mapfile -t PART_LINES < <(sfdisk -d "$DISK" | grep -E '^/dev/')
NPARTS=${#PART_LINES[@]}
ROOT_NUM=$(echo "$ROOT_PART" | grep -oE '[0-9]+$')
[ "$ROOT_NUM" = "$NPARTS" ] || { echo "Root partition is not the last one; unsupported layout." >&2; exit 1; }
echo ">> Source: $DISK ($NPARTS partitions, id $DISK_ID)"

# ------------------------------------------------------------------- sizing
EXCL_KB=0
for p in /var/swap; do
    [ -e "$p" ] && EXCL_KB=$((EXCL_KB + $(du -sk "$p" | cut -f1)))
done
CLEAN_DIRS=(/root/.codeium /root/.devin-server /root/audit-run /.git /var/lib/apc-nmc /var/lib/cloud)
if [ $CLEAN -eq 1 ]; then
    for p in "${CLEAN_DIRS[@]}"; do
        [ -e "$p" ] && EXCL_KB=$((EXCL_KB + $(du -sk "$p" | cut -f1)))
    done
fi
USED_KB=$(df -k / | awk 'NR==2{print $3}')
ROOT_START=$(echo "${PART_LINES[-1]}" | sed -E 's/.*start=[[:space:]]*([0-9]+).*/\1/')
IMG_MB=$(( (USED_KB - EXCL_KB) / 1024 + HEADROOM_MB + ROOT_START * 512 / 1048576 + 64 ))
echo ">> Image size: ${IMG_MB}M"

# ------------------------------------------------------------ partition table
truncate -s "${IMG_MB}M" "$OUT"
TABLE=$(printf 'label: dos\nlabel-id: %s\nunit: sectors' "$DISK_ID")
for i in "${!PART_LINES[@]}"; do
    line="${PART_LINES[$i]}"
    start=$(echo "$line" | sed -E 's/.*start=[[:space:]]*([0-9]+).*/\1/')
    type=$(echo  "$line" | sed -E 's/.*type=[[:space:]]*([0-9a-fA-F]+).*/\1/')
    if [ "$i" -eq $((NPARTS - 1)) ]; then
        TABLE+=$(printf '\nstart=%s, type=%s' "$start" "$type")
    else
        size=$(echo "$line" | sed -E 's/.*size=[[:space:]]*([0-9]+).*/\1/')
        TABLE+=$(printf '\nstart=%s, size=%s, type=%s' "$start" "$size" "$type")
    fi
done
printf '%b\n' "$TABLE" | sfdisk "$OUT" >/dev/null
echo ">> Partition table written (id $DISK_ID)"

# ------------------------------------------------------------ format + mount
LOOP=$(losetup -fP --show "$OUT")
cleanup() {
    set +e
    mountpoint -q "$IMGROOT$BOOT_MNT" && umount "$IMGROOT$BOOT_MNT"
    mountpoint -q "$IMGROOT" && umount "$IMGROOT"
    losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

mkfs.vfat -F 32 ${BOOT_LABEL:+-n "$BOOT_LABEL"} -i "$BOOT_VOLID" "${LOOP}p1" >/dev/null
mkfs.ext4 -q -U "$ROOT_UUID" ${ROOT_LABEL:+-L "$ROOT_LABEL"} \
    -E lazy_itable_init=1,lazy_journal_init=1 "${LOOP}p${ROOT_NUM}"
mkdir -p "$IMGROOT"
mount "${LOOP}p${ROOT_NUM}" "$IMGROOT"
mkdir -p "$IMGROOT$BOOT_MNT"
mount "${LOOP}p1" "$IMGROOT$BOOT_MNT"
echo ">> Image formatted and loop-mounted at $LOOP"

# -------------------------------------------------------------------- rsync
EXCLUDES=(
    "/var/swap"
    "/tmp/*" "/var/tmp/*"
    "/root/pi4-backup-*.img*"
    "${OUT#/}" "${OUT#/}.gz"
)
if [ $CLEAN -eq 1 ]; then
    EXCLUDES+=(
        # requested personal paths
        "/root/.codeium" "/root/.devin-server" "/root/audit-run" "/.git"
        "/root/.ssh/authorized_keys" "/root/.bash_history"
        # additional privacy/data scrub
        "/root/.gitconfig" "/root/.ssh/known_hosts"
        "/var/lib/apc-nmc/*" "/var/lib/cloud/*"
        "/var/log/journal/*" "/var/log/*.gz" "/var/log/*.[0-9]"
        "/var/log/syslog*" "/var/log/messages*" "/var/log/auth.log*"
        "/var/log/daemon.log*" "/var/log/kern.log*" "/var/log/user.log*"
        "/var/log/wtmp*" "/var/log/btmp*" "/var/log/lastlog*" "/var/log/faillog*"
        "/var/lib/NetworkManager/*.lease*"
        "/etc/ssh/ssh_host_*"
        "/root/.wget-hsts" "/root/.viminfo" "/root/.lesshst" "/root/.python_history"
    )
fi
RSYNC_EXCL=(); for e in "${EXCLUDES[@]}"; do RSYNC_EXCL+=(--exclude="$e"); done

echo ">> Syncing filesystem (this takes a few minutes)..."
rsync -aAXHSx --delete "${RSYNC_EXCL[@]}" / "$IMGROOT/"
rsync -aAX "$BOOT_MNT/" "$IMGROOT$BOOT_MNT/"

# ------------------------------------------------------- clean-mode scrubbing
if [ $CLEAN -eq 1 ]; then
    echo ">> Sanitizing image for publication..."
    cp -L /etc/skel/.bashrc "$IMGROOT/root/.bashrc"
    : > "$IMGROOT/root/.bash_history"
    mkdir -p "$IMGROOT/var/lib/apc-nmc" "$IMGROOT/var/log/journal"
    : > "$IMGROOT/etc/machine-id"
    rm -f "$IMGROOT"/etc/ssh/ssh_host_*

    sed -i -E 's/(password[[:space:]]*=[[:space:]]*).*/\1CHANGE_ME_ON_FIRST_BOOT/i' \
        "$IMGROOT/etc/apc-nmc/nmc.conf" "$IMGROOT/etc/nut/upsd.users" 2>/dev/null || true
    sed -i -E 's/^(MONITOR[[:space:]]+\S+[[:space:]]+[0-9]+[[:space:]]+\S+[[:space:]]+)\S+/\1CHANGE_ME_ON_FIRST_BOOT/' \
        "$IMGROOT/etc/nut/upsmon.conf" 2>/dev/null || true
    sed -i -E 's/(rocommunity6?|rwcommunity6?)[[:space:]]+\S+/\1 CHANGE_ME_ON_FIRST_BOOT/;
               s/(createUser[[:space:]]+\S+[[:space:]]+SHA[[:space:]]+)"[^"]*"([[:space:]]+AES[[:space:]]+)"[^"]*"/\1"CHANGE_ME_ON_FIRST_BOOT"\2"CHANGE_ME_ON_FIRST_BOOT"/' \
        "$IMGROOT/etc/snmp/snmpd.conf" 2>/dev/null || true

    mkdir -p "$IMGROOT/etc/systemd/system/multi-user.target.wants"
    ln -sf /etc/systemd/system/firstboot-wizard.service \
        "$IMGROOT/etc/systemd/system/multi-user.target.wants/firstboot-wizard.service"
fi

sync
mountpoint -q "$IMGROOT$BOOT_MNT" && umount "$IMGROOT$BOOT_MNT"
umount "$IMGROOT"
losetup -d "$LOOP"; LOOP=""
trap - EXIT
echo ">> Image complete: $OUT"

if [ $GZIP -eq 1 ]; then
    echo ">> Compressing..."
    gzip -k "$OUT"
    echo ">> Done: $OUT.gz"
fi
