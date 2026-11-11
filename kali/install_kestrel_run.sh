#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -o root -g root -m 0755 "$HERE/kestrel_run.py" /usr/local/bin/kestrel-run
install -d -o root -g root -m 0755 /etc/kestrel

if [ ! -e /etc/kestrel/scope.json ]; then
  install -o root -g root -m 0644 "$HERE/scope.example.json" /etc/kestrel/scope.json
fi

install -d -o kestrel -g kestrel -m 0700 /var/log/kestrel
touch /var/log/kestrel/audit.jsonl
chown kestrel:kestrel /var/log/kestrel/audit.jsonl
chmod 0600 /var/log/kestrel/audit.jsonl

install -d -o kestrel -g kestrel -m 0700 /tmp/kestrel

# The kestrel account must not rewrite its own SSH state or home; tool
# output is confined to /tmp/kestrel. No user-writable shell startup files,
# because sshd runs the forced command through the account's shell.
rm -f /home/kestrel/.bashrc /home/kestrel/.profile \
  /home/kestrel/.bash_logout
mkdir -p /home/kestrel/.ssh
chown -R root:root /home/kestrel
chmod 0755 /home/kestrel /home/kestrel/.ssh
if [ -f /home/kestrel/.ssh/authorized_keys ]; then
  chmod 0644 /home/kestrel/.ssh/authorized_keys
fi

if command -v masscan >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$(command -v masscan)"
fi

install -o root -g root -m 0644 "$HERE/logrotate.kestrel" \
  /etc/logrotate.d/kestrel

echo "Installed /usr/local/bin/kestrel-run"
echo "Edit /etc/kestrel/scope.json, then install the forced command in"
echo "~kestrel/.ssh/authorized_keys. See kali/README.md."
