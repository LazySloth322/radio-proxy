#!/usr/bin/env bash
# firewall_example.sh - nftables ruleset example for private-radio-proxy.
#
# REVIEW EVERY LINE BEFORE RUNNING. Wrong rules can lock you out of SSH.
# Keep an active SSH session open while applying, and verify you can still
# log in from a second terminal.
#
# What it does:
#   * allow loopback
#   * allow established/related connections
#   * allow SSH on $SSH_PORT (set yours - the owner uses a non-standard port)
#   * allow the app port $APP_PORT (config/app.json -> server.port)
#   * allow basic ICMP (ping)
#   * drop everything else inbound
#
# Install persistently on Debian 12/13:
#   sudo cp this ruleset to /etc/nftables.conf  (or write your own)
#   sudo systemctl enable --now nftables
#
# ufw alternative (simpler):
#   sudo ufw default deny incoming
#   sudo ufw default allow outgoing
#   sudo ufw allow 22/tcp            # your SSH port
#   sudo ufw allow 54391/tcp         # app port
#   sudo ufw enable

set -euo pipefail

SSH_PORT="${SSH_PORT:-22}"
APP_PORT="${APP_PORT:-54391}"

cat <<EOF | nft -f -
flush ruleset

table inet filter {
    chain input {
        type filter hook input priority 0; policy drop;

        # loopback
        iif "lo" accept

        # established/related
        ct state established,related accept

        # invalid packets
        ct state invalid drop

        # SSH (owner's custom port - CHANGE ME)
        tcp dport ${SSH_PORT} accept

        # private-radio-proxy
        tcp dport ${APP_PORT} accept

        # ping (optional; comment out to be stealthier)
        ip protocol icmp icmp type echo-request limit rate 4/second accept
        ip6 nexthdr ipv6-icmp accept

        # everything else: dropped by policy
    }

    chain forward {
        type filter hook forward priority 0; policy drop;
    }

    chain output {
        type filter hook output priority 0; policy accept;
    }
}
EOF

echo "Firewall rules applied: SSH on ${SSH_PORT}/tcp, app on ${APP_PORT}/tcp, rest dropped."
echo "Remember to make this persistent (see comments at the top)."
