# Kali lab setup

This is the host-side setup for Kestrel's SSH wrapper. Run it on the Kali VM,
not on Windows.

## 1. Create the account and install the tools

```bash
sudo apt update
sudo apt install -y openssh-server nmap whatweb dnsutils whois curl \
  ffuf gobuster feroxbuster nuclei nikto masscan netcat-traditional \
  exploitdb libcap2-bin
sudo useradd -m -s /bin/bash kestrel
sudo passwd -l kestrel
# Root-owned home with no user-writable shell startup files: sshd runs the
# forced command through this account's shell.
sudo rm -f /home/kestrel/.bashrc /home/kestrel/.profile \
  /home/kestrel/.bash_logout
sudo chown -R root:root /home/kestrel
sudo chmod 0755 /home/kestrel
sudo install -d -o root -g root -m 0755 /home/kestrel/.ssh
```

Masscan needs raw sockets; `install_kestrel_run.sh` grants only
`cap_net_raw`. Nmap runs unprivileged, so phase 1 uses `-sT`/`-sV`; `-sS`
and `-O` need root and are not enabled.

## 2. Install the wrapper

Copy `kali/` to the VM, then:

```bash
sudo bash install_kestrel_run.sh
```

## 3. Add the forced command

On Windows:

```powershell
mkdir "$env:APPDATA\Kestrel\kali"
ssh-keygen -t ed25519 -f "$env:APPDATA\Kestrel\kali\id_ed25519" -N ""
Get-Content "$env:APPDATA\Kestrel\kali\id_ed25519.pub"
```

On Kali, put this single line in `/home/kestrel/.ssh/authorized_keys`
(replace the key text):

```text
command="/usr/local/bin/kestrel-run",no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA... kestrel@windows
```

```bash
sudo chown root:root /home/kestrel/.ssh/authorized_keys
sudo chmod 0644 /home/kestrel/.ssh/authorized_keys
sudo sshd -t
sudo systemctl restart ssh
```

Root ownership is deliberate: `kestrel` can read the public key but cannot
replace the forced command, even if an allowlisted tool writes a file.
Tools run with `HOME` and the working directory set to `/tmp/kestrel`, and
output paths are restricted to that directory.

## 4. Set the scope

Edit `/etc/kestrel/scope.json`. It is root-owned and Kestrel cannot change it.
The example allows only `192.168.56.0/24` and `*.lab.local` until
`2027-12-31`.

## 5. Pin the host key on Windows

Find the VM's host-only IP (`ip addr` on Kali), then:

```powershell
ssh-keyscan -p 22 192.168.56.20 > "$env:APPDATA\Kestrel\kali\known_hosts"
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
- Try `nmap -sV 8.8.8.8` and `bash -c "id"`; both are refused before execution.
