# Kali lab setup

This is the host-side setup for Kestrel's SSH wrapper. Run it on the Kali VM,
not on Windows.

## 1. Create the account and install the tools

Two transports work, and the rest of this guide has to match the one you pick.

**Model A — host-only adapter (recommended).** The VM gets a real address on a
private subnet and Kestrel reaches it directly: `host` is that address, `port`
is 22.

**Model B — NAT with a host port forward.** The VM keeps only its NAT interface
(on QEMU and VirtualBox user-net that is `10.0.2.15`, gateway `10.0.2.2`) and
Windows forwards a loopback port into it, so `ssh -p 2222 kestrel@127.0.0.1`
reaches the VM's port 22. Kestrel then uses `host` `127.0.0.1` and `port`
`2222`. QEMU: `-netdev user,id=n0,hostfwd=tcp::2222-:22`. VirtualBox:
`VBoxManage modifyvm "<vm>" --natpf1 "ssh,tcp,127.0.0.1,2222,,22"`.

Tell which you are on before you write the scope. Boot the VM and read its
addresses:

```bash
ip -brief addr
```

A private-range address besides `lo` (below, `192.168.56.20`) means Model A is
already up. Only `lo` plus a `10.0.2.x` address means Model B. Then check what
the Windows host itself owns, because that subnet may not be free:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,InterfaceAlias
```

**Do not claim a subnet the host already has.** `192.168.56.1` is VirtualBox's
default host-only address and, on a Windows host running WSL2 or Hyper-V, it is
commonly already taken by `vEthernet (WSL (Hyper-V firewall))`. Two adapters on
one subnet means Windows routing decides which machine an address reaches, so a
target the scope allows can resolve to the wrong host. Check with the command
above: if it lists `192.168.56.1` on a Hyper-V or WSL adapter, build the
host-only network on a different range instead (VirtualBox: File > Tools >
Network Manager > Host-only Networks, then set that adapter's IPv4 address to
`192.168.120.1/24`) and use that range everywhere below, including
`/etc/kestrel/scope.json`.

Model A only: attach a host-only adapter to the VM first, on the
`192.168.56.0/24` subnet
(VirtualBox: Settings > Network > Adapter 2 > Host-only Adapter; VMware: add a
host-only network on the same subnet). Keep the NAT adapter as well so apt can
reach the mirrors; a fresh Kali VM has no host-only interface until you attach
one. The address you just read is the one to use everywhere below.

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

Copy the whole `kali/` directory from the Kestrel checkout to the VM — the
three files the installer uses (`kestrel_run.py`, `scope.example.json`,
`logrotate.kestrel`) sit beside it, so copying only the script fails. A host
folder shared into the VM works; so does `scp -r kali kestrel@<vm-ip>:~`, but
Kestrel's key carries a forced command, so use an ordinary login key or the
VM console for that copy. If the copy came through a Windows editor or a
`shared folder` that rewrites line endings, the script arrives with CRLF and
`bash` fails with `$'\r': command not found`; fix it with
`sed -i 's/\r$//' kali/*.sh`. Check the script parses before running it:

```bash
bash -n kali/install_kestrel_run.sh
```

Then from inside that directory:

```bash
sudo bash install_kestrel_run.sh
```

The installer is idempotent: it re-applies owners and modes, keeps an existing
`/etc/kestrel/scope.json` and never truncates the audit log. It refuses to run
if `/tmp/kestrel` or `/var/log/kestrel/audit.jsonl` is a symlink, because it
runs as root and must not chown/chmod through a link planted in world-writable
`/tmp`. If it refuses, remove the link (`sudo rm /tmp/kestrel`, no trailing
slash; for the audit file, `sudo rm /var/log/kestrel/audit.jsonl`) and re-run.
The wrapper also refuses a symlinked `/tmp/kestrel`, and on Linux one that is
not owned by `kestrel` or not mode `0700`, at run time.

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

**Scope targets are resolved inside the VM**, by the wrapper, against
`networks` — never on Windows. Hold that in mind for both models:

- Model A: `192.168.56.20` in a task is the VM, because that is its own address
  on the host-only link.
- Model B: `127.0.0.1` in a task is the **VM's** loopback, not your Windows
  machine. The SSH transport being `127.0.0.1:2222` too is an accident of the
  port forward; a scan of `127.0.0.1` never leaves the VM and cannot reach
  Windows. To scan the Windows host from the VM, name the host's own address on
  the link between them (`192.168.56.1` under Model A; under Model B the NAT
  gateway, usually `10.0.2.2`).

Under Model B the addresses the VM can really see are its loopback and the NAT
range, so the example subnet alone allows nothing reachable. Set the networks
to the ranges you find, in this shape:

```json
"networks": ["127.0.0.0/8", "10.0.2.0/24"]
```

`127.0.0.0/8` lets the wrapper scan the box it runs on; `10.0.2.0/24` covers the
VM at `10.0.2.15` and its gateway at `10.0.2.2`. Confirm with `ip -brief addr`
and `ip route` on the VM first, and list only what you actually find there.

Do not set `allow_public` to `true` to make a refusal go away. That flag is the
boundary between your lab and the internet, and `nmap -sV 8.8.8.8` is refused
by design. When a target you own is refused, add that target's own network to
`networks` — nothing else. The wrapper re-reads the file on the next request,
so there is no restart step.

## 5. Record and verify the host key on Windows

`ssh-keyscan` trusts whatever answers, so on its own this step is
trust-on-first-use, not authentication. Verify the fingerprint against the VM
console before you rely on it.

On the VM console (not over SSH), print the host key fingerprint:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Under Model A, record the host key using the VM's host-only IP (`ip addr` on
Kali), then on Windows:

```powershell
ssh-keyscan -p 22 192.168.56.20 | Out-File -Encoding ascii "$env:APPDATA\Kestrel\kali\known_hosts"
ssh-keygen -lf "$env:APPDATA\Kestrel\kali\known_hosts"
```

Under Model B, scan the forward instead, with the same host and port
`kali.json` will use:

```powershell
ssh-keyscan -p 2222 127.0.0.1 | Out-File -Encoding ascii "$env:APPDATA\Kestrel\kali\known_hosts"
ssh-keygen -lf "$env:APPDATA\Kestrel\kali\known_hosts"
```

`known_hosts` entries are keyed by host and port, and OpenSSH writes a
non-default port in brackets: that scan produces
`[127.0.0.1]:2222 ssh-ed25519 ...`. An entry recorded for some other host or
port names a key OpenSSH will never look up, so every call dies with
`Host key verification failed` — which reads as a broken Kali tool, and is not
one. If you switch transports later, re-record this file.

Use `Out-File -Encoding ascii`, not `>`: Windows PowerShell 5.1 redirects as
UTF-16, and OpenSSH rejects such a file ("is not a public key file").

The `SHA256:` fingerprint of the ED25519 key must match the console's
character for character. If it does not, stop: something else answered the
scan. Delete `known_hosts` and investigate before connecting.

```powershell
ssh -i "$env:APPDATA\Kestrel\kali\id_ed25519" -o UserKnownHostsFile="$env:APPDATA\Kestrel\kali\known_hosts" kestrel@192.168.56.20
```

Under Model B the same check goes through the forward:

```powershell
ssh -i "$env:APPDATA\Kestrel\kali\id_ed25519" -p 2222 -o UserKnownHostsFile="$env:APPDATA\Kestrel\kali\known_hosts" kestrel@127.0.0.1
```

Type this JSON and press Enter:

```json
{"version":1,"mode":"scope"}
```

Expected: `{"ok": true, "scope": {...}}`.

## 6. Kestrel config

Create `%APPDATA%\Kestrel\kali.json`. Model A, a host-only address and the
default port:

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

Model B, the forwarded loopback and the forward's own port. Keep these two
fields matching step 5, or the host key will not resolve:

```json
{
  "host": "127.0.0.1",
  "port": 2222,
  "user": "kestrel",
  "key_path": "%APPDATA%\\Kestrel\\kali\\id_ed25519",
  "known_hosts": "%APPDATA%\\Kestrel\\kali\\known_hosts",
  "connect_timeout": 10
}
```

`%APPDATA%` inside `key_path` and `known_hosts` is expanded by the bridge, so
both forms work. `host` is only the SSH transport: the address you scan is a
separate thing, chosen inside the scope (step 4).

## 7. Live checks

From Kestrel, using the address your model actually has — under Model A that is
the VM's host-only address `192.168.56.20`; under Model B the VM has no such
address, so use its loopback, which needs `127.0.0.0/8` in `networks` from
step 4 first:

- `kali_scope` returns the scope.
- `kali_task` with `nmap -sV 192.168.56.20` shows the approval dialog.
- `kali_task` with `nmap -sV 127.0.0.1` is the Model B equivalent, and it proves
  the target resolves inside the VM: the answer is the Kali box's own open
  ports, not Windows'.
- Approve and verify the summary.
- Deny a second task and verify nothing runs.
- `kali_task` with `masscan -p80 --rate 100 127.0.0.1` exercises the one
  capability the installer grants. The wrapper tiers masscan as `intrusive`, so
  this asks you twice: a second dialog, with the commands repeated, only
  appears for `intrusive`, `credential` and `exploitation` work, and either
  answer of No stops the task before it runs. Approve it: masscan runs as
  `kestrel`, never as root. If it reports a raw-socket permission error, check
  `getcap "$(command -v masscan)"` and re-run the installer; only `cap_net_raw`
  is granted, so a masscan build that also needs `cap_net_admin` will fail here.
- Try `nmap -sV 8.8.8.8` and `bash -c "id"`; both are refused before execution.
- If every target is refused as outside scope, that is step 4, not a broken
  bridge: the transport working and the scope being wrong are independent.
