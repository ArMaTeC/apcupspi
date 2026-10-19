#!/usr/bin/env bash
#
# apc-nmc-update.sh — check for and apply apc-nmc software updates.
#
#   --check        fetch VERSION from the tracked repo, compare with
#                  /etc/apc-nmc/version, cache the result for the web UI
#   --check-auto   --check for the daily timer (skips when the
#                  [update] autocheck = 0 option is set in nmc.conf)
#   --apply        download the repo tarball and deploy code, assets,
#                  systemd units and the prebuilt apc_modbus driver over
#                  the running system — never user config or secrets —
#                  then restart only what changed
#   --status       print the cached check/apply state
#
# apc-nmc-update-check.timer runs --check-auto daily. --apply runs under
# apc-nmc-update.service (oneshot), started by the web UI or by hand —
# it can also be run directly from SSH. Apply progress lands in
# update-status.json so the UI can report it after the web service
# restarts mid-update. A copy of every replaced file is kept under
# update-backup-<date>/.
#
# Environment overrides (testing / forks):
#   APC_NMC_UPDATE_URL      base URL for VERSION (default: raw github)
#   APC_NMC_UPDATE_TARBALL  tarball URL for --apply (default: github archive)

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

CONF=/etc/apc-nmc/nmc.conf
VERSION_FILE=/etc/apc-nmc/version
STATE_DIR=/var/lib/apc-nmc
STATE=$STATE_DIR/update.json
STATUS=$STATE_DIR/update-status.json
LOG=$STATE_DIR/update.log
LOCK=$STATE_DIR/update.lock
BACKUP_KEEP=3

conf() {  # conf <section> <key> — print value, empty if absent
    awk -v sec="$1" -v key="$2" '
        $0 ~ "^[ \t]*\\[" sec "\\][ \t]*$" { insec = 1; next }
        /^[ \t]*\[/ { insec = 0 }
        insec && $0 ~ "^[ \t]*" key "[ \t]*=" {
            sub(/^[^=]*=[ \t]*/, ""); sub(/[ \t\r]+$/, ""); print; exit
        }' "$CONF" 2>/dev/null
}

REPO=$(conf update repo);       REPO=${REPO:-ArMaTeC/apcupspi}
BRANCH=$(conf update branch);   BRANCH=${BRANCH:-main}
BASE=${APC_NMC_UPDATE_URL:-https://raw.githubusercontent.com/$REPO/$BRANCH}
TARBALL=${APC_NMC_UPDATE_TARBALL:-https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}

jstr() {  # JSON-escape a string's content (no surrounding quotes added)
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/\\t/g'
}

write_json() {  # write_json <path> <content>
    local tmp="$1.tmp.$$"
    printf '%s\n' "$2" > "$tmp" && chmod 0640 "$tmp" \
        && mv -T "$tmp" "$1"
}

say() { echo "$*"; echo "$(date '+%F %T') $*" >> "$LOG"; }

status() {  # status <state> <message>
    local finished=0
    [ "$1" = ok ] || [ "$1" = failed ] && finished=$(date +%s)
    write_json "$STATUS" \
        "{\"state\":\"$(jstr "$1")\",\"msg\":\"$(jstr "$2")\",\"from\":\"$(jstr "$FROM")\",\"to\":\"$(jstr "$TO")\",\"finished\":$finished}"
}

ver_newer() {  # ver_newer <installed> <latest> — latest strictly newer?
    [ -n "$2" ] || return 1
    [ "$1" = unknown ] && return 0
    [ "$1" = "$2" ] && return 1
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$2" ]
}

do_check() {
    exec 9>"$LOCK"; flock -n 9 || exit 0
    local now installed latest avail
    now=$(date +%s)
    installed=$(tr -d '[:space:]' < "$VERSION_FILE" 2>/dev/null)
    installed=${installed:-unknown}
    latest=$(curl -fsSL --max-time 15 "$BASE/VERSION" 2>/dev/null \
             | tr -d '[:space:]')
    if [ -z "$latest" ]; then
        write_json "$STATE" "{\"installed\":\"$(jstr "$installed")\",\"latest\":\"\",\"available\":null,\"checked_at\":$now,\"err\":\"no VERSION from update server\"}"
        say "check failed: no VERSION from $BASE"
        exit 1
    fi
    if ver_newer "$installed" "$latest"; then avail=true; else avail=false; fi
    write_json "$STATE" "{\"installed\":\"$(jstr "$installed")\",\"latest\":\"$(jstr "$latest")\",\"available\":$avail,\"checked_at\":$now,\"err\":\"\"}"
    echo "installed=$installed latest=$latest available=$avail"
}

deploy() {  # deploy <payload-rel-path> <abs-target> <mode>
    local s="$SRC$1" d="$2"
    [ -e "$s" ] || return 0
    cmp -s "$s" "$d" 2>/dev/null && return 0
    [ -e "$d" ] && { mkdir -p "$BAK$(dirname "$d")"; cp -a "$d" "$BAK$d"; }
    install -D -m "$3" "$s" "$d.new.$$" && mv -T "$d.new.$$" "$d" \
        || return 1
    CHANGED="$CHANGED $d"
    say "  updated $d"
}

sync_dir() {  # sync_dir <payload-rel-path> <abs-target>
    local s="$SRC$1"
    [ -d "$s" ] || return 0
    mkdir -p "$2"
    cp -a "$s/." "$2/" && say "  synced $2"
}

do_apply() {
    exec 9>"$LOCK"; flock -n 9 || { say "update already running"; exit 0; }
    local work SRC TO CHANGED DRIVER_CHG
    FROM=$(tr -d '[:space:]' < "$VERSION_FILE" 2>/dev/null); FROM=${FROM:-unknown}
    TO="?"
    status running "downloading update"
    say "=== update started (installed: $FROM) ==="

    work=$(mktemp -d /tmp/apc-nmc-update.XXXXXX) || { status failed "no temp dir"; exit 1; }
    trap 'rm -rf "$work"' EXIT

    if ! curl -fsSL --max-time 120 -o "$work/src.tgz" "$TARBALL" \
            || ! tar -xzf "$work/src.tgz" -C "$work"; then
        status failed "download/extract failed"
        say "FAIL: could not fetch $TARBALL"
        exit 1
    fi
    SRC=$(echo "$work"/*/); [ -d "$SRC" ] || SRC="$work/"
    if [ ! -f "${SRC}VERSION" ]; then
        status failed "payload has no VERSION file"
        say "FAIL: payload missing VERSION"
        exit 1
    fi
    TO=$(tr -d '[:space:]' < "${SRC}VERSION")

    BAK=$STATE_DIR/update-backup-$(date +%Y%m%d-%H%M%S)
    mkdir -p "$BAK"
    CHANGED=""

    status running "deploying files"
    # Executables
    for f in usr/local/bin/apc-nmc-web.py usr/local/bin/apc-nmc-agent.py \
             usr/local/sbin/apc-nmc-update.sh \
             usr/local/sbin/firstboot-wizard.sh \
             usr/local/sbin/net-watchdog.sh \
             usr/local/sbin/nut-usb-detect.sh \
             usr/local/sbin/pi-system-backup.sh; do
        deploy "$f" "/$f" 0755 || say "WARN: failed to update $f"
    done
    # Whole-dir payloads: web assets, systemd units/drop-ins, argon (if used)
    sync_dir usr/local/bin/apc-nmc-web /usr/local/bin/apc-nmc-web
    sync_dir etc/systemd/system /etc/systemd/system
    sync_dir usr/lib/systemd/system /usr/lib/systemd/system
    [ -d /etc/argon ] && sync_dir etc/argon /etc/argon
    # Config templates and static conf (never nmc.conf, keys, CA, or the
    # generated nut/snmpd/ssh secrets)
    for f in etc/apc-nmc/nmc.conf.default etc/nut/nut.conf etc/nut/ups.conf \
             etc/nut/upsd.conf etc/nut/upssched.conf \
             etc/sysctl.d/90-oom-guard.conf etc/sysctl.d/91-apc-nmc-panic.conf \
             etc/logrotate.d/apc-nmc \
             etc/NetworkManager/conf.d/99-wifi-powersave-off.conf \
             etc/avahi/avahi-daemon.conf; do
        deploy "$f" "/$f" 0644 || say "WARN: failed to update $f"
    done
    deploy etc/nut/upsd.users.default /etc/nut/upsd.users.default 0640

    # Driver + its modbus library: deploy the shipped binary only when it
    # differs (strip-compare — install.sh installs it stripped). No source
    # rebuild here; install.sh still builds from vendor/ on fresh installs.
    DRIVER_CHG=0
    if [ -f "${SRC}usr/lib/nut/apc_modbus" ]; then
        install -s -m 0755 "${SRC}usr/lib/nut/apc_modbus" \
            "$work/apc_modbus.strip" 2>/dev/null
        if [ -s "$work/apc_modbus.strip" ] \
                && ! cmp -s "$work/apc_modbus.strip" /usr/lib/nut/apc_modbus; then
            cp -a /usr/lib/nut/apc_modbus "$BAK/usr-lib-nut-apc_modbus" 2>/dev/null
            install -s -m 0755 "${SRC}usr/lib/nut/apc_modbus" \
                /usr/lib/nut/apc_modbus.new.$$ \
                && mv -T /usr/lib/nut/apc_modbus.new.$$ /usr/lib/nut/apc_modbus
            DRIVER_CHG=1; say "  updated /usr/lib/nut/apc_modbus"
        fi
    fi
    for l in "$SRC"usr/local/lib/libmodbus.so.5*; do
        [ -e "$l" ] || continue
        deploy "${l#"$SRC"}" "/${l#"$SRC"}" 0644
    done

    # VERSION stamps the install last, so a failed/partial update still
    # reports its real (older) version
    deploy VERSION /etc/apc-nmc/version 0644

    status running "restarting services"
    chgrp -R nut /etc/nut 2>/dev/null || true
    systemctl daemon-reload
    case "$CHANGED" in
        *apc-nmc-agent*) systemctl restart snmpd \
                         && say "  restarted snmpd" ;;
    esac
    if [ "$DRIVER_CHG" = 1 ]; then
        systemctl restart 'nut-driver@*.service' 2>/dev/null \
            || systemctl restart nut-driver.target 2>/dev/null \
            || true
        say "  restarted UPS driver"
    fi
    ldconfig 2>/dev/null || true

    write_json "$STATE" "{\"installed\":\"$(jstr "$TO")\",\"latest\":\"$(jstr "$TO")\",\"available\":false,\"checked_at\":$(date +%s),\"err\":\"\"}"
    status ok "update applied"
    say "=== update to $TO complete ==="

    # prune old backups
    ls -1dt "$STATE_DIR"/update-backup-* 2>/dev/null | tail -n +$((BACKUP_KEEP+1)) \
        | while read -r d; do rm -rf "$d"; done

    # last: bounce the web UI to pick up new code (we may be running
    # because of it — keep this the final step)
    systemctl restart apc-nmc-web 2>/dev/null || true
}

case "${1:-}" in
    --check)      do_check ;;
    --check-auto) [ "$(conf update autocheck)" = "0" ] && exit 0; do_check ;;
    --apply)
        # run from a temp copy so deploying a new apc-nmc-update.sh can
        # never corrupt the script mid-run
        if [ -z "${_NMC_UPD_REEXEC:-}" ]; then
            _t=$(mktemp /tmp/apc-nmc-update-self.XXXXXX)
            cp "$0" "$_t" && chmod +x "$_t" || exit 1
            _NMC_UPD_REEXEC=1 exec bash "$_t" "$@"
        fi
        do_apply ;;
    --status)
        [ -f "$STATE" ] && cat "$STATE"
        [ -f "$STATUS" ] && cat "$STATUS" ;;
    *)  echo "usage: $0 --check | --check-auto | --apply | --status" >&2
        exit 2 ;;
esac
