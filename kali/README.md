# Kali lab setup

This is the host-side setup for Kestrel's SSH wrapper. Run it on the Kali VM,
not on Windows.

## 1. Create the account and install the tools

Attach a host-only adapter to the VM first, on the `192.168.56.0/24` subnet
(VirtualBox: Settings > Network > Adapter 2 > Host-only Adapter; VMware: add a
host-only network on the same subnet). Keep the NAT adapter as well so apt can
reach the mirrors. A fresh Kali VM has no host-only interface until you attach
one. Boot it and confirm the address:

```bash
ip -brief addr
```

This guide uses `192.168.56.20`. If the VM got a different address inside
`192.168.56.0/24`, use that address everywhere below. If it got an address
outside that range, fix the adapter's subnet or widen the scope in step 4: the
wrapper refuses every target that is not in `/etc/kestrel/scope.json`.

```bash
sudo apt update
sudo apt install -y openssh-server nmap whatweb dnsutils whois curl \
  ffuf gobuster feroxbuster nuclei nikto masscan netcat-traditional \
  exploitdb libcap2-bin seclists wordlists
sudo useradd -m -s /bin/bash kestrel
# No usable password, without depending on UsePAM: `passwd -l` locks the
# account in a way sshd rejects even for public-key auth when `UsePAM no`.
sudo usermod -p '*' kestrel
# Root-owned home with no user-writable shell startup files: sshd runs the
# forced command through this account's shell.
sudo rm -f /home/kestrel/.bashrc /home/kestrel/.profile \
  /home/kestrel/.bash_logout
sudo rm -f /home/kestrel/.ssh/rc
sudo chown -R root:root /home/kestrel
sudo chmod 0755 /home/kestrel
sudo install -d -o root -g root -m 0755 /home/kestrel/.ssh
```

`seclists` and `wordlists` populate `/usr/share/seclists` and
`/usr/share/wordlists`, the two wordlist directories the wrapper lets a read
flag open; without them ffuf, gobuster and feroxbuster have nothing to fuzz
with. `wordlists` ships `rockyou.txt.gz`, which you can decompress as root if
you want it.

Masscan needs raw sockets; `install_kestrel_run.sh` grants only `cap_net_raw`,
not `cap_net_admin`. Nmap runs unprivileged, so phase 1 uses `-sT`/`-sV`;
`-sS` and `-O` pass policy but fail without root.

## 2. Install the wrapper

Copy `kali/` to the VM, then from inside that directory:

```bash
sudo bash install_kestrel_run.sh
```

The installer is idempotent: it re-applies owners and modes, keeps an existing
`/etc/kestrel/scope.json` and never truncates the audit log. It refuses to run
if `/tmp/kestrel` or `/var/log/kestrel/audit.jsonl` is a symlink, because it
runs as root and must not chown/chmod through a link planted in world-writable
`/tmp`. If it refuses, remove the link (`sudo rm /tmp/kestrel`, no trailing
slash) and re-run. The wrapper also refuses a symlinked `/tmp/kestrel`, and on
Linux one that is not owned by `kestrel` or not mode `0700`, at run time.

## 3. Add the forced command

On Windows:

```powershell
mkdir "$env:APPDATA\Kestrel\kali"
ssh-keygen -t ed25519 -f "$env:APPDATA\Kestrel\kali\id_ed25519" -N ""
Get-Content "$env:APPDATA\Kestrel\kali\id_ed25519.pub"
```

On Kali, put this single line in `/home/kestrel/.ssh/authorized_keys` as root
(the directory is root-owned; replace the key text):

```text
command="/usr/local/bin/kestrel-run",no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding,no-user-rc ssh-ed25519 AAAA... kestrel@windows
```

```bash
sudo chown root:root /home/kestrel/.ssh/authorized_keys
sudo chmod 0644 /home/kestrel/.ssh/authorized_keys
sudo sshd -t
sudo sshd -T | grep -E '^(pubkeyauthentication|permituserrc)'
sudo systemctl restart ssh
```

`PubkeyAuthentication yes` is required (the Kali default): the account has no
usable password, so a public key is the only way in. `no-user-rc` matters
because `PermitUserRC` defaults to yes, and sshd runs `/home/kestrel/.ssh/rc`
as `kestrel` BEFORE the forced command. The installer deletes that file; the
key option keeps it disabled even if the file comes back.

Root ownership is deliberate: `kestrel` can read the public key but cannot
replace the forced command, even if an allowlisted tool writes a file.
Tools run with `HOME` and the working directory set to `/tmp/kestrel`, and
output paths are restricted to that directory.

## 4. Set the scope

Edit `/etc/kestrel/scope.json`. It is root-owned and Kestrel cannot change it.
The example allows only `192.168.56.0/24` and `*.lab.local` until
`2027-12-31`. The wrapper reads that path on every request: the scope, audit
and output paths are compiled-in constants, so no environment variable can
redirect them.

## 5. Record and verify the host key on Windows

`ssh-keyscan` trusts whatever answers, so on its own this step is
trust-on-first-use, not authentication. Verify the fingerprint against the VM
console before you rely on it.

On the VM console (not over SSH), print the host key fingerprint:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Find the VM's host-only IP (`ip addr` on Kali), then on Windows:

```powershell
ssh-keyscan -p 22 192.168.56.20 | Out-File -Encoding ascii "$env:APPDATA\Kestrel\kali\known_hosts"
ssh-keygen -lf "$env:APPDATA\Kestrel\kali\known_hosts"
```

Use `Out-File -Encoding ascii`, not `>`: Windows PowerShell 5.1 redirects as
UTF-16, and OpenSSH rejects such a file ("is not a public key file").

The `SHA256:` fingerprint of the ED25519 key must match the console's
character for character. If it does not, stop: something else answered the
scan. Delete `known_hosts` and investigate before connecting.

```powershell
ssh -i "$env:APPDATA\Kestrel\kali\id_ed25519" -o UserKnownHostsFile="$env:APPDATA\Kestrel\kali\known_hosts" kestrel@192.168.56.20
```

Type this JSON and press Enter:

```json
{"version":1,"mode":"scope"}
```

Expected: `{"ok": true, "scope": {...}}`.

## 6. Kestrel config

Create `%APPDATA%\Kestrel\kali.json`:

```json
{
  "host": "192.168.56.20",
  "port": 22,
  "user": "kestrel",
  "key_path": "%APPDATA%\\Kestrel\\kali\\id_ed25519",
  "known_hosts": "%APPDATA%\\Kestrel\\kali\\known_hosts",
  "connect_timeout": 10
}
```

## 7. Live checks

From Kestrel:

- `kali_scope` returns the scope.
- `kali_task` with `nmap -sV 192.168.56.20` shows the approval dialog.
- Approve and verify the summary.
- Deny a second task and verify nothing runs.
- `kali_task` with `masscan -p80 --rate 100 192.168.56.20` exercises the one
  capability the installer grants. Approve it: masscan runs as `kestrel`, never
  as root. If it reports a raw-socket permission error, check
  `getcap "$(command -v masscan)"` and re-run the installer; only `cap_net_raw`
  is granted, so a masscan build that also needs `cap_net_admin` will fail here.
- Try `nmap -sV 8.8.8.8` and `bash -c "id"`; both are refused before execution.
