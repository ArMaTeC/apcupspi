#!/usr/bin/env bash
# apc-nmc-firewall.sh — apply/remove the card's nftables input ruleset.
#
#   apply   build table `inet apc-nmc` from [firewall] in nmc.conf
#   flush   drop the table entirely (service stop)
#   status  list the live table
#
# [firewall] keys:
#   allow_tcp   extra TCP ports, space separated (22 80 443 always open)
#   allow_udp   extra UDP ports (161 + 5353 always open)
#   allow_from  optional CIDR list — when set, only those sources may
#               reach any service port (established replies still pass)
set -u
CONF=/etc/apc-nmc/nmc.conf
TABLE=apc-nmc

conf() {  # conf <key> — print value of [firewall] <key>
    awk -v k="$1" '
        /^\[/ { sec = ($0 ~ /^\[firewall\]/) }
        sec && $0 ~ "^[[:space:]]*" k "[[:space:]]*=" {
            sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, "")
            print; exit
        }' "$CONF" 2>/dev/null
}

ports() {  # ports <defaults> <extra> — merge, validate, emit nft set
    local defaults="$1" extra="$2" p out=""
    for p in $defaults $extra; do
        [[ "$p" =~ ^[0-9]+$ ]] && ((p >= 1 && p <= 65535)) && out+="$p, "
    done
    echo "{ ${out%, } }"
}

src_rules() {  # emit per-family saddr match lines for valid CIDRs
    local from="$1" c v4="" v6=""
    for c in $from; do
        [[ "$c" =~ ^[0-9a-fA-F.:]+/[0-9]+$ ]] || continue
        if [[ "$c" == *:* ]]; then v6+="$c, "; else v4+="$c, "; fi
    done
    [ -n "$v4" ] && echo "ip saddr { ${v4%, } }"
    [ -n "$v6" ] && echo "ip6 saddr { ${v6%, } }"
}

apply() {
    local tcp udp rules="" r srclines
    tcp=$(ports "22 80 443" "$(conf allow_tcp)")
    udp=$(ports "161 5353" "$(conf allow_udp)")
    srclines=$(src_rules "$(conf allow_from)")
    if [ -z "$srclines" ]; then
        # no source restriction: the service sets are open to all
        rules="        tcp dport $tcp accept
        udp dport $udp accept
"
    else
        # source-limited: each allowed CIDR may reach the service sets
        while IFS= read -r r; do
            rules+="        $r tcp dport $tcp accept
        $r udp dport $udp accept
"
        done <<< "$srclines"
    fi

    nft -f - <<EOF
table inet $TABLE
delete table inet $TABLE
table inet $TABLE {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        ct state invalid counter drop
        iif "lo" accept
        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept
        udp dport 68 accept
$rules        counter drop
    }
}
EOF
}

flush() {
    nft delete table inet $TABLE 2>/dev/null || true
}

case "${1:-apply}" in
    apply)  apply ;;
    flush|stop) flush ;;
    status) nft list table inet $TABLE 2>/dev/null || echo "not applied" ;;
    *) echo "usage: $0 {apply|flush|status}" >&2; exit 2 ;;
esac
