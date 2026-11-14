#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -o root -g root -m 0755 "$HERE/kestrel_run.py" /usr/local/bin/kestrel-run
install -d -o root -g root -m 0755 /etc/kestrel

if [ ! -e /etc/kestrel/scope.json ]; then
  install -o root -g root -m 0644 "$HERE/scope.example.json" /etc/kestrel/scope.json
fi

install -d -o kestrel -g kestrel -m 0700 /var/log/kestrel
# T2-R1: touch, chown and chmod all follow a symlink, so a planted link would
# hand some other file to kestrel. Refuse before touching the path.
if [ -L /var/log/kestrel/audit.jsonl ]; then
  echo "refusing: /var/log/kestrel/audit.jsonl is a symlink" >&2
  exit 1
fi
touch /var/log/kestrel/audit.jsonl
chown kestrel:kestrel /var/log/kestrel/audit.jsonl
chmod 0600 /var/log/kestrel/audit.jsonl

# T2-R1: /tmp is world-writable (1777) and `install -d` tolerates an existing
# entry, applying attributes by path. A pre-planted symlink would make this
# root-run installer chown/chmod an arbitrary directory (e.g. /etc) to
# kestrel:kestrel 0700. The wrapper's runtime symlink refusal cannot undo that
# install-time damage, so refuse here, before any attribute is applied.
if [ -L /tmp/kestrel ]; then
  echo "refusing: /tmp/kestrel is a symlink" >&2
  exit 1
fi
# `mkdir` refuses an existing entry, so the create and the refusal are one step,
# not a check-then-use pair a local user could win by swapping a link in between.
if [ ! -e /tmp/kestrel ]; then
  mkdir -m 0700 /tmp/kestrel
fi
chown kestrel:kestrel /tmp/kestrel
chmod 0700 /tmp/kestrel

# The kestrel account must not rewrite its own SSH state or home; tool
# output is confined to /tmp/kestrel. No user-writable shell startup files,
# because sshd runs the forced command through the account's shell.
rm -f /home/kestrel/.bashrc /home/kestrel/.profile \
  /home/kestrel/.bash_logout
# T2-R3: PermitUserRC defaults to yes, so sshd runs ~/.ssh/rc as the login user
# BEFORE the forced command. chown -R root:root below would make a planted rc
# root-owned but would not remove it; the key's no-user-rc option is the second
# layer, not a replacement for deleting the file.
rm -f /home/kestrel/.ssh/rc
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
