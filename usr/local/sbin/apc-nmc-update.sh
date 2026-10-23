#!/usr/bin/env bash
#
# apc-nmc-update.sh — check for and apply apc-nmc software updates.
#
#   --check        fetch VERSION from the tracked repo, compare with
#                  /etc/apc-nmc/version, cache the result for the web UI
#   --check-auto   --check for the daily timer (skips when the
#                  [update] autocheck = 0 option is set in nmc.conf)
#   --apply        download the repo tarball and deploy code, assets,
#                  systemd units and the apc_modbus driver (rebuilt from
#                  the vendored NUT sources like install.sh, falling back
#                  to the shipped binary) over the running system — never
#                  user config or secrets — then restart only what changed
#   --rollback     restore the newest update-backup-* over the running
#                  system (files, driver, units — never secrets), then
#                  restart the affected services
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
CHANNEL=$(conf update channel); CHANNEL=${CHANNEL:-releases}
VERIFY=$(conf update verify);   VERIFY=${VERIFY:-checksum}
BASE=${APC_NMC_UPDATE_URL:-https://raw.githubusercontent.com/$REPO/$BRANCH}
TARBALL=${APC_NMC_UPDATE_TARBALL:-https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}
RELEASES_API="https://api.github.com/repos/$REPO/releases/latest"
PUBKEY=/etc/apc-nmc/update-signing.pub

jstr() {  # JSON-escape $1's content (no surrounding quotes added)
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read())[1:-1], end="")'
}

rel_json() {  # print the latest-release JSON, empty on failure
    curl -fsSL --max-time 15 -H "Accept: application/vnd.github+json" \
        "$RELEASES_API" 2>/dev/null
}

rel_field() {  # rel_field <json-file> <key>
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2], "")
    print(v if isinstance(v, str) else "")
except Exception:
    pass
PY
}

rel_assets() {  # rel_assets <json-file> — "name url" per asset
    python3 - "$1" <<'PY'
import json, sys
try:
    for a in json.load(open(sys.argv[1])).get("assets", []):
        print(a.get("name", ""), a.get("browser_download_url", ""))
except Exception:
    pass
PY
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
        "{\"state\":\"$(jstr "$1")\",\"msg\":\"$(jstr "$2")\",\"from\":\"$(jstr "${FROM:-}")\",\"to\":\"$(jstr "${TO:-}")\",\"finished\":$finished}"
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
    # an apply can't be running (we hold the lock) — a status left at
    # "running" means it was killed mid-run (e.g. reboot)
    if grep -q '"state":"running"' "$STATUS" 2>/dev/null; then
        status failed "update interrupted"
    fi
    now=$(date +%s)
    installed=$(tr -d '[:space:]' < "$VERSION_FILE" 2>/dev/null)
    installed=${installed:-unknown}
    latest=""; tag=""; notes=""
    if [ "$CHANNEL" = releases ]; then
        rel=$(mktemp /tmp/apc-nmc-rel.XXXXXX) || rel=""
        if [ -n "$rel" ] && rel_json > "$rel" && [ -s "$rel" ]; then
            tag=$(rel_field "$rel" tag_name | tr -d '[:space:]')
            notes=$(rel_field "$rel" body | head -c 4096)
            latest=${tag#v}
        fi
        [ -n "$rel" ] && rm -f "$rel"
    fi
    if [ -z "$latest" ]; then     # no releases yet / channel=branch
        latest=$(curl -fsSL --max-time 15 "$BASE/VERSION" 2>/dev/null \
                 | tr -d '[:space:]')
    fi
    if [ -z "$latest" ]; then
        write_json "$STATE" "{\"installed\":\"$(jstr "$installed")\",\"latest\":\"\",\"available\":null,\"checked_at\":$now,\"err\":\"no VERSION from update server\"}"
        say "check failed: no VERSION from update server"
        exit 1
    fi
    if ver_newer "$installed" "$latest"; then avail=true; else avail=false; fi
    write_json "$STATE" "{\"installed\":\"$(jstr "$installed")\",\"latest\":\"$(jstr "$latest")\",\"tag\":\"$(jstr "$tag")\",\"notes\":\"$(jstr "$notes")\",\"available\":$avail,\"checked_at\":$now,\"err\":\"\"}"
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
    # keep a copy of the pre-sync tree so --rollback can restore it
    if [ -n "$(ls -A "$2" 2>/dev/null)" ]; then
        mkdir -p "$BAK$2" && cp -a "$2/." "$BAK$2/"
    fi
    cp -a "$s/." "$2/" && say "  synced $2"
}

do_apply() {
    exec 9>"$LOCK"; flock -n 9 || { say "update already running"; exit 0; }
    local work SRC TO CHANGED DRIVER_CHG
    FROM=$(tr -d '[:space:]' < "$VERSION_FILE" 2>/dev/null); FROM=${FROM:-unknown}
    TO="?"
    # marker before status: the web console anchors on it, and a first
    # poll that sees "running" must already find this run's marker
    say "=== update started (installed: $FROM) ==="
    status running "downloading update"

    work=$(mktemp -d /tmp/apc-nmc-update.XXXXXX) || { status failed "no temp dir"; exit 1; }
    # deliberate early expansion: a local var would be unbound by the time
    # the EXIT trap fires under set -u
    # shellcheck disable=SC2064
    trap "rm -rf '$work' ${_NMC_UPD_SELF:-}" EXIT

    # Resolve the payload URL: an explicit env override wins; otherwise a
    # release asset when the tracked repo publishes one, then the tag
    # archive, then the tracked-branch archive (forks without releases).
    TARBALL_URL=$TARBALL; SHA_URL=""; SIG_URL=""
    if [ -z "${APC_NMC_UPDATE_TARBALL:-}" ] && [ "$CHANNEL" = releases ]; then
        rel=$(mktemp /tmp/apc-nmc-rel.XXXXXX); rel_json > "$rel" || true
        rtag=$(rel_field "$rel" tag_name | tr -d '[:space:]')
        if [ -n "$rtag" ]; then
            a_tar=""; a_sha=""; a_sig=""
            while read -r an au; do
                case "$an" in
                    *.tar.gz.sha256|SHA256SUMS*) a_sha=$au ;;
                    *.sig)                       a_sig=$au ;;
                    *.tar.gz)                    a_tar=$au ;;
                esac
            done <<EOF
$(rel_assets "$rel")
EOF
            if [ -n "$a_tar" ]; then
                TARBALL_URL=$a_tar; SHA_URL=$a_sha; SIG_URL=$a_sig
            else
                TARBALL_URL="https://github.com/$REPO/archive/refs/tags/$rtag.tar.gz"
            fi
        fi
        rm -f "$rel"
    fi

    status running "downloading update"
    if ! curl -fsSL --max-time 120 -o "$work/src.tgz" "$TARBALL_URL"; then
        status failed "download failed"
        say "FAIL: could not fetch $TARBALL_URL"
        exit 1
    fi
    # integrity: verify the payload against the release's sha256 asset
    # when one is published. verify=strict refuses to run unverified.
    if [ -n "$SHA_URL" ] \
            && curl -fsSL --max-time 30 -o "$work/src.sha256" "$SHA_URL" \
            && [ -s "$work/src.sha256" ]; then
        exp=$(awk '{print $1; exit}' "$work/src.sha256")
        act=$(sha256sum "$work/src.tgz" | awk '{print $1}')
        if [ "$exp" != "$act" ]; then
            status failed "checksum mismatch"
            say "FAIL: payload checksum mismatch"
            exit 1
        fi
        say "  verified payload sha256"
    elif [ "$VERIFY" = strict ]; then
        status failed "no checksum available"
        say "FAIL: release provides no checksum (verify=strict)"
        exit 1
    else
        say "WARN: payload not checksum-verified"
    fi
    # signature: checked whenever a pubkey is installed and the release
    # carries a .sig — a bad signature always fails hard
    if [ -f "$PUBKEY" ] && [ -n "$SIG_URL" ]; then
        if ! curl -fsSL --max-time 30 -o "$work/src.sig" "$SIG_URL" \
                || ! openssl dgst -sha256 -verify "$PUBKEY" \
                    -signature "$work/src.sig" "$work/src.tgz" \
                    >/dev/null 2>&1; then
            status failed "signature check failed"
            say "FAIL: payload signature verification failed"
            exit 1
        fi
        say "  verified payload signature"
    elif [ "$VERIFY" = strict ] && [ -f "$PUBKEY" ]; then
        status failed "no signature available"
        say "FAIL: release is unsigned (verify=strict, signing key installed)"
        exit 1
    fi
    if ! tar -xzf "$work/src.tgz" -C "$work"; then
        status failed "extract failed"
        say "FAIL: could not extract payload"
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
    printf '%s\n%s\n%s\n' "$FROM" "$TO" "$(date +%s)" > "$BAK/meta"
    CHANGED=""

    status running "deploying files"
    # Executables
    for f in usr/local/bin/apc-nmc-web.py usr/local/bin/apc-nmc-agent.py \
             usr/local/bin/apc-nmc-modbus.py \
             usr/local/sbin/apc-nmc-update.sh \
             usr/local/sbin/apc-nmc-firewall.sh \
             usr/local/sbin/apc-nmc-dot1x.sh \
             usr/local/sbin/apc-nmc-wifi-hw.sh \
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
    # vsftpd.conf only lands where the admin opted into the FTP server
    [ -f /etc/vsftpd.conf ] \
        && deploy etc/vsftpd.conf /etc/vsftpd.conf 0644 \
        || true

    # Adopt newly shipped config options: write the default value for any
    # section.key the live nmc.conf doesn't set yet, so new features are
    # visible/editable instead of merely listed as unset on the About
    # page. Existing admin values are never touched. Runs through the
    # just-deployed web module so the merge semantics stay identical.
    NEWKEYS=$(python3 - <<'PYEOF'
import importlib.util
try:
    s = importlib.util.spec_from_file_location(
        "nmcweb", "/usr/local/bin/apc-nmc-web.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    for k in m.config_merge_defaults():
        print(k)
except Exception:
    pass
PYEOF
)
    [ -n "$NEWKEYS" ] \
        && say "  adopted new config options:" \
        && echo "$NEWKEYS" | sed 's/^/    /'

    # Driver + modbus library: rebuild from the vendored sources when
    # they and a toolchain are present — same result install.sh produces.
    # The shipped prebuilt binary is the fallback (no sources, no
    # toolchain, or a failed build). Either way the binary lands only
    # when it strip-compares different, so a rebuild that produces the
    # same driver does not restart it needlessly.
    DRIVER_CHG=0
    DRV_NEW=""
    LIBMB_BUILT=0
    if ls "${SRC}"vendor/nut-2.8.5-src.tar.gz \
            "${SRC}"vendor/libmodbus-*.tar.gz >/dev/null 2>&1 \
            && command -v gcc >/dev/null && command -v make >/dev/null; then
        status running "building NUT driver"
        J=$(nproc 2>/dev/null || echo 1)
        if tar -xzf "$SRC"vendor/libmodbus-*.tar.gz -C "$work" \
                && (cd "$work"/libmodbus-* \
                    && ./configure --prefix=/usr/local >>"$LOG" 2>&1 \
                    && make -j"$J" >>"$LOG" 2>&1 \
                    && make install >>"$LOG" 2>&1); then
            ldconfig
            LIBMB_BUILT=1
            say "  rebuilt libmodbus"
        else
            say "WARN: libmodbus build failed — keeping installed copy"
        fi
        if tar -xzf "$SRC"vendor/nut-2.8.5-src.tar.gz -C "$work"; then
            # same setvar fix as install.sh — survives an unpatched
            # regenerated tarball (vendor/patches/…unavailable.patch)
            if grep -q '!(apc_value->value_flags & APC_VF_UNAVAILABLE)' \
                    "$work"/nut-2.8.5/drivers/apc_modbus.c 2>/dev/null; then
                sed -i 's/!(apc_value->value_flags & APC_VF_UNAVAILABLE)/(apc_value->value_flags \& APC_VF_UNAVAILABLE)/' \
                    "$work"/nut-2.8.5/drivers/apc_modbus.c
            fi
            if (cd "$work"/nut-2.8.5 \
                    && ./configure --with-drivers=apc_modbus \
                        --with-modbus+usb=yes --with-doc=no \
                        --without-asciidoc --without-pkg-config >>"$LOG" 2>&1 \
                    && make -j"$J" >>"$LOG" 2>&1); then
                DRV_NEW="$work/nut-2.8.5/drivers/apc_modbus"
                say "  rebuilt apc_modbus from source"
            else
                say "WARN: NUT build failed — falling back to shipped binary"
            fi
        fi
    fi
    # shipped fallback binaries are aarch64-only — a rebuild is the only
    # correct source on other architectures
    if [ -z "$DRV_NEW" ]; then
        if [ "$(uname -m)" = "aarch64" ]; then
            DRV_NEW="${SRC}usr/lib/nut/apc_modbus"
        else
            say "WARN: shipped apc_modbus is aarch64-only — keeping installed driver"
        fi
    fi
    if [ -n "$DRV_NEW" ]; then
        install -s -m 0755 "$DRV_NEW" "$work/apc_modbus.strip" 2>/dev/null
        if [ -s "$work/apc_modbus.strip" ] \
                && ! cmp -s "$work/apc_modbus.strip" /usr/lib/nut/apc_modbus; then
            cp -a /usr/lib/nut/apc_modbus "$BAK/usr-lib-nut-apc_modbus" \
                2>/dev/null
            install -s -m 0755 "$DRV_NEW" /usr/lib/nut/apc_modbus.new.$$ \
                && mv -T /usr/lib/nut/apc_modbus.new.$$ \
                    /usr/lib/nut/apc_modbus
            DRIVER_CHG=1; say "  updated /usr/lib/nut/apc_modbus"
        fi
    fi
    # shipped libmodbus is only the fallback — a source build already
    # installed the fresh library above. cp -a keeps .so symlinks as
    # links (install would dereference them into duplicate files).
    if [ "$LIBMB_BUILT" = 0 ] && [ "$(uname -m)" = "aarch64" ]; then
        for l in "$SRC"usr/local/lib/libmodbus.so*; do
            [ -e "$l" ] || continue
            t="/${l#"$SRC"}"
            cmp -s "$l" "$t" 2>/dev/null && continue
            [ -e "$t" ] && { mkdir -p "$BAK$(dirname "$t")"; cp -a "$t" "$BAK$t"; }
            cp -a "$l" "$t" && CHANGED="$CHANGED $t" && say "  updated $t"
        done
    fi
    # custom paths the driver was built with (same bridge install.sh makes)
    mkdir -p /usr/local/ups /var/state
    ln -sfn /etc/nut /usr/local/ups/etc
    ln -sfn /run/nut /var/state/ups

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
    # upsd only reads LISTEN at startup — a changed upsd.conf needs a
    # full restart or the new bindings silently never take effect
    case "$CHANGED" in
        */etc/nut/upsd.conf*) systemctl restart nut-server \
                              && say "  restarted nut-server" ;;
    esac
    # modbus daemon runs only when the admin enabled it — refresh it
    # after a code update without auto-enabling it on fresh installs
    case "$CHANGED" in
        *apc-nmc-modbus*) systemctl try-restart apc-nmc-modbus \
                          2>/dev/null || true ;;
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

do_rollback() {
    exec 9>"$LOCK"; flock -n 9 || { say "update/rollback already running"; exit 0; }
    local BAK rel
    BAK=$(ls -1dt "$STATE_DIR"/update-backup-* 2>/dev/null | head -1)
    if [ -z "$BAK" ] || [ ! -d "$BAK" ]; then
        FROM=""; TO=""
        status failed "no backup to roll back to"
        say "rollback: no update backups found"
        exit 1
    fi
    FROM=$(tr -d '[:space:]' < "$VERSION_FILE" 2>/dev/null); FROM=${FROM:-unknown}
    TO=$(sed -n '1p' "$BAK/meta" 2>/dev/null)
    [ -z "$TO" ] && TO=$(tr -d '[:space:]' < "$BAK/etc/apc-nmc/version" 2>/dev/null)
    TO=${TO:-?}
    say "=== rollback started (installed: $FROM, backup: $(basename "$BAK")) ==="
    status running "rolling back to $TO"

    # every file under the backup mirror maps back to its absolute path;
    # the driver is stored under its flattened name. Restore via temp +
    # rename — cp over the running driver binary fails ETXTBSY
    rc=0
    while read -r f; do
        rel=${f#"$BAK"}
        [ "$rel" = "/usr-lib-nut-apc_modbus" ] && rel=/usr/lib/nut/apc_modbus
        mkdir -p "$(dirname "$rel")"
        if cp -a "$f" "$rel.nmc-rb.$$" \
                && mv -T "$rel.nmc-rb.$$" "$rel"; then
            say "  restored $rel"
        else
            rm -f "$rel.nmc-rb.$$"; rc=1
            say "  WARN: could not restore $rel"
        fi
    done < <(find "$BAK" -type f ! -name meta | sort)

    chgrp -R nut /etc/nut 2>/dev/null || true
    ldconfig 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart snmpd 2>/dev/null || true
    systemctl restart nut-server 2>/dev/null || true
    systemctl restart 'nut-driver@*.service' 2>/dev/null \
        || systemctl restart nut-driver.target 2>/dev/null || true

    write_json "$STATE" "{\"installed\":\"$(jstr "$TO")\",\"latest\":\"$(jstr "$TO")\",\"available\":false,\"checked_at\":$(date +%s),\"err\":\"\"}"
    if [ $rc -eq 0 ]; then
        status ok "rolled back to $TO"
        say "=== rollback to $TO complete ==="
    else
        status failed "rollback incomplete — see update.log"
        say "=== rollback to $TO incomplete ==="
    fi
    systemctl restart apc-nmc-web 2>/dev/null || true
    [ $rc -eq 0 ]
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
            _NMC_UPD_REEXEC=1 _NMC_UPD_SELF="$_t" exec bash "$_t" "$@"
        fi
        do_apply ;;
    --rollback)   do_rollback ;;
    --status)
        [ -f "$STATE" ] && cat "$STATE"
        [ -f "$STATUS" ] && cat "$STATUS" ;;
    *)  echo "usage: $0 --check | --check-auto | --apply | --rollback | --status" >&2
        exit 2 ;;
esac
