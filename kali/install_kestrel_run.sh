#!/usr/bin/env bash
set -euo pipefail

# Every step below applies root-owned attributes, so a half-privileged run cannot
# be "mostly fine": it would install a wrapper the account cannot execute and
# leave the marker files that mean enforcement pointing at nothing.
if [ "$(id -u)" -ne 0 ]; then
  echo "refusing: this installer must run as root" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -o root -g root -m 0755 "$HERE/kestrel_run.py" /usr/local/bin/kestrel-run
install -d -o root -g root -m 0755 /etc/kestrel

if [ ! -e /etc/kestrel/scope.json ]; then
  install -o root -g root -m 0644 "$HERE/scope.example.json" /etc/kestrel/scope.json
fi

# T8: an egress rule is the only control that can confine a child which dials
# hosts argv never names (theHarvester queries its providers itself), so the
# wrapper refuses those binaries until /etc/kestrel/egress.conf exists. The rule
# is generated from the scope file, resolved now, and loaded now: writing the
# marker without loading the rule would claim a control that is not in place.
if ! command -v nft >/dev/null 2>&1; then
  echo "refusing: nft is required to confine the kestrel account's egress" >&2
  exit 1
fi
if [ -L /etc/kestrel/egress.conf ]; then
  echo "refusing: /etc/kestrel/egress.conf is a symlink" >&2
  exit 1
fi
python3 "$HERE/egress_rules.py" /etc/kestrel/scope.json \
  "$(id -u kestrel)" > /etc/kestrel/egress.conf.tmp
mv /etc/kestrel/egress.conf.tmp /etc/kestrel/egress.conf
chmod 0644 /etc/kestrel/egress.conf
nft -f /etc/kestrel/egress.conf

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

echo "Installed /usr/local/bin/kestrel-run and loaded /etc/kestrel/egress.conf"
echo "Edit /etc/kestrel/scope.json, then re-run this installer to apply an"
echo "edited egress list, and install the forced command in"
echo "~kestrel/.ssh/authorized_keys. See kali/README.md."
