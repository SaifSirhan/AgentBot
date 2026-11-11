# Kali SSH Wrapper — Design

**Date:** 2026-11-11
**Status:** Approved architecture; implementation not started
**Scope:** Kali general-toolkit track. The phone track is separate.

## 1. Goal

Let Kestrel run authorized security tooling in a Kali VM from one natural-language
request. Kestrel plans the commands, the user approves the task, and a policy
wrapper inside Kali executes the commands without a shell and returns structured
results.

The first deployment is a Kali VM on the Windows machine. The same image and
configuration move to the old laptop later.

## 2. Non-goals

- No second AI or separate Kali assistant.
- No public targets by default.
- No autonomous exploitation, credential attacks, evasion, or persistence.
- No GUI tools (Burp Suite, Metasploit GUI) in phase 1.
- No phone emulator or phone automation.
- No mounting Kestrel memory, API keys, or personal files in Kali.

## 3. Topology

```
Windows host
  Kestrel (PySide6)
    └─ kali_task tool
         └─ ssh.exe  ── host-only network ──>  Kali VM
                                                 └─ /usr/local/bin/kestrel-run
                                                      ├─ scope check
                                                      ├─ binary/risk check
                                                      ├─ argv execution
                                                      └─ JSONL audit log
```

**Phase 1 host:** Kali VM on the Windows machine.
**Later host:** the same VM image on the old laptop, reached over an isolated
LAN. Only the SSH host in Kestrel's config changes.

**VM defaults:** 4 vCPU, 8 GB RAM, 80 GB disk, Kali rolling. Use VMware
Workstation Pro if it is already installed; otherwise VirtualBox 7.x. If the
Windows host has 16 GB RAM or less, use 2 vCPU and 4 GB and run the target VM
on the old laptop later instead of on the same host. One host-only adapter on
`192.168.56.0/24` for targets, plus NAT for updates. Take a `clean` snapshot
before installing tools.

## 4. Components

### 4.1 Kali VM

- Dedicated `kestrel` user. No password login. SSH key only.
- `openssh-server` bound to the host-only interface, not the public network.
- `authorized_keys` entry uses a forced command:

  ```
  command="/usr/local/bin/kestrel-run",no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA...
  ```

- `/home/kestrel` and `~kestrel/.ssh` are root-owned (`0755`), and
  `authorized_keys` is root-owned (`0644`). The `kestrel` account can read the
  public key but cannot rewrite its own SSH state, so an allowlisted tool that
  writes a file cannot replace the forced command.
- The home directory has no user-writable shell startup files
  (`~/.bashrc`, `~/.profile`, `~/.bash_logout`). SSHD runs the forced command
  through the account's shell, so a writable startup file would execute code
  before the wrapper.
- Tool steps run with `HOME` and the working directory set to `/tmp/kestrel`,
  with a minimal environment (no proxy variables). Output-path flags are
  restricted to `/tmp/kestrel`.
- No Kestrel memory, API keys, or personal accounts on this VM.
- A `lab` snapshot is taken before intrusive work; the user can roll back.

### 4.2 Policy wrapper

`/usr/local/bin/kestrel-run`, Python 3 standard library only, root-owned and not
writable by `kestrel`.

Responsibilities:

1. Read one JSON request from stdin.
2. Dispatch on `mode`: `execute` (default), `scope`, or `check`.
3. Load and validate `/etc/kestrel/scope.json`.
4. Validate every command argument against the binary allowlist and scope.
5. Classify risk and refuse destructive flags.
6. Execute each step with `shell=False`, a timeout, a process-group kill, and
   an output cap.
7. Write a JSONL audit record.
8. Return one JSON response on stdout.

`mode: "scope"` is read-only: it returns the current scope so Kestrel can plan
without editing it. `mode: "check"` runs the same policy validation as
execution and returns the risk tier and targets without running anything.
Neither mode executes a command. The SSH forced command passes no client
arguments, so both use the stdin JSON rather than a CLI flag.

Wrapper language is Python because Kali ships it and the policy logic needs
`ipaddress`, `json`, `subprocess`, and `signal`. There are no pip dependencies.

### 4.3 Kestrel client

`kali_bridge.py` on Windows:

- Reads `%APPDATA%\Kestrel\kali.json`.
- Calls the built-in Windows OpenSSH client with `BatchMode=yes`,
  `StrictHostKeyChecking=yes`, and a dedicated key.
- Sends the request JSON on stdin; reads the response JSON from stdout.
- Never prints the SSH key or full environment.
- Returns honest errors for unreachable host, policy denial, timeout, and
  malformed responses.

### 4.4 Approval gate

`kali_task` is a `system_control` tool. Before execution it requests approval
through a Qt relay, mirroring the existing sprite relay:

- The tool runs on a worker thread.
- `kali_bus.request_approval(plan)` emits a signal to the GUI thread.
- The GUI shows a `QMessageBox` with the objective, every command, the targets,
  and the highest risk tier.
- Default button is **No**.
- If the GUI is unavailable (headless or Telegram), the request is denied.

The dialog is the enforceable "approve the task" gate. The model can propose a
plan, but it cannot execute it without a human pressing Yes.

**Decision:** one tool, one gate. The earlier plan/run split is collapsed into
`kali_task`; the dialog shows the exact plan, so a plan-id store adds state
without adding protection.

## 5. Request flow

1. User: "quick service scan on 192.168.56.10."
2. Kestrel reads the scope with a `mode: "scope"` request.
3. Kestrel builds one `kali_task` call:

   ```json
   {
     "objective": "Service scan on 192.168.56.10",
     "steps": [
       {"argv": ["nmap", "-sV", "-T4", "192.168.56.10"], "timeout": 120}
     ],
     "total_timeout": 180
   }
   ```

4. Before the dialog, Kestrel sends `mode: "check"`; the wrapper returns the
   validated risk tier and targets without executing.
5. The GUI dialog lists the commands, targets, and risk. User presses Yes.
6. The wrapper re-checks scope, risk, allowlist, and destructive flags.
7. The wrapper runs the command and returns per-step results.
8. Kestrel summarizes open ports and versions; raw output stays in the tool
   result or becomes an artifact when it is large.
9. The audit log records the approved plan, argv, target checks, risk, exit
   code, duration, and stdout hash.

## 6. Protocol (version 1)

Execute request, sent on stdin:

```json
{
  "version": 1,
  "mode": "execute",
  "objective": "string",
  "steps": [
    {
      "argv": ["nmap", "-sV", "192.168.56.10"],
      "timeout": 120
    }
  ],
  "total_timeout": 300
}
```

Check request:

```json
{
  "version": 1,
  "mode": "check",
  "objective": "string",
  "steps": [
    {"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 120}
  ],
  "total_timeout": 300
}
```

Check response:

```json
{
  "ok": true,
  "risk": "recon",
  "targets": ["192.168.56.10"],
  "steps": [["nmap", "-sV", "192.168.56.10"]]
}
```

Scope request:

```json
{
  "version": 1,
  "mode": "scope"
}
```

Scope response:

```json
{
  "ok": true,
  "scope": {
    "version": 1,
    "expires": "2026-12-31T23:59:59Z",
    "allow_public": false,
    "networks": ["192.168.56.0/24"],
    "domains": ["lab.local", "*.lab.local"]
  }
}
```

Response, written to stdout:

```json
{
  "ok": true,
  "results": [
    {
      "argv": ["nmap", "-sV", "192.168.56.10"],
      "exit_code": 0,
      "stdout": "…",
      "stderr": "…",
      "duration_ms": 4210,
      "timed_out": false,
      "truncated": false
    }
  ],
  "risk": "recon",
  "targets": ["192.168.56.10"]
}
```

On policy denial:

```json
{
  "ok": false,
  "denied": true,
  "reason": "target 8.8.8.8 is outside scope",
  "risk": "recon"
}
```

Exit codes: `0` completed (even if a step returned nonzero), `2` policy denial,
`3` protocol/configuration error. The JSON response is authoritative.

Limits:

- Maximum 8 steps per task.
- Per-step timeout: 1–600 seconds; default 120.
- Total timeout: 1–1800 seconds; default 300.
- Output cap: 256 KB per stream per step; 1 MB total. Truncated output is
  marked.
- Steps run sequentially and stop after the first failed step.

## 7. Policy

### 7.1 Scope file

Authoritative file: `/etc/kestrel/scope.json`, owned by root, mode `0644`.
Kestrel cannot write it. The user edits it on Kali for phase 1.

```json
{
  "version": 1,
  "expires": "2026-12-31T23:59:59Z",
  "allow_public": false,
  "networks": ["192.168.56.0/24"],
  "domains": ["lab.local", "*.lab.local"]
}
```

- Expired scope denies every target.
- Missing or malformed scope denies every target.
- `allow_public` defaults to false. If false, public IPs and domains are denied
  even if they appear in the lists.
- Localhost and link-local addresses are denied unless explicitly listed.
- Target extraction is flag-aware and binary-aware. Known file-argument flags
  (`-w`, `--wordlist`, `-in`, `-CAfile`, and the output flags in §7.3) are
  treated as paths; `-T` is timing for nmap and upload for curl. Every other
  IP, CIDR, URL host, `host:port` value, `@resolver` token, bracketed IPv6
  address, and hostname-like token is validated as a target even if a file
  with the same name exists. A command with no target is allowed only for a
  binary in the local-safe list (`searchsploit`, `openssl`; openssl `s_client`
  still needs a validated `-connect` target).
- Target-list and request-file flags are denied in phase 1 because they can
  hide out-of-scope targets: `nmap -iL`, `nuclei -l`, `ffuf -request`, and
  `curl -K`. The wrapper only accepts targets written directly in the argv.

### 7.2 Binary allowlist and risk tiers

Phase 1 allowlist:

| Tier | Binaries |
|---|---|
| `recon` | `nmap`, `whatweb`, `dig`, `host`, `whois`, `curl`, `openssl`, `searchsploit` |
| `intrusive` | `ffuf`, `gobuster`, `feroxbuster`, `nuclei`, `nikto`, `masscan`, `nc` |

`searchsploit` is local-safe and needs no target. `curl` is `recon` for GET and
HEAD; POST, data upload, and file output are `intrusive`.

The following are not allowed in phase 1: `bash`, `sh`, `zsh`, `python3`,
`perl`, `ruby`, `rm`, `dd`, `mkfs`, `mount`, `systemctl`, `crontab`, and any
binary not in the table. They can bypass policy or modify the host.

Phase 5 may add `sqlmap`, `msfconsole`, and `hydra` behind the `exploitation`
and `credential` tiers. Those tiers are not enabled in phase 1 even if the
binary is installed.

### 7.3 Destructive flags

Denied outright in phase 1:

- `sqlmap`: `--os-shell`, `--os-pwn`, `--os-cmd`, `--file-write`,
  `--file-dest`, `--sql-shell`
- `nmap`: `--script` with exploitation or brute categories; `--script-args`
  carrying shell/file writes
- `curl`: `-o` / `--output` outside `/tmp/kestrel`, `-T` / `--upload-file`
- `nc`: `-e` / `--exec`, `-c`
- Any argv containing `;`, `|`, `&`, `$`, backticks, newline, or redirection
  tokens where they would imply a shell
- Output-path flags (`nmap -oN/-oX/-oG/-oA/-oS`, the ffuf, gobuster,
  feroxbuster, nuclei, nikto and masscan output flags, `curl -o`, whatweb
  `--log-*`, and openssl `-out`/`-keylogfile`/`-writerand`) must resolve under
  `/tmp/kestrel`; relative paths resolve there. `~` and `..` escapes are
  denied.
- `curl -O`/`--remote-name` and proxy flags (`-x`, `--proxy`, SOCKS variants,
  `--preproxy`) are denied. Proxy flags are also denied for ffuf, gobuster,
  feroxbuster, nikto, nuclei, whatweb, and openssl's `s_client`.
- `nmap --script` accepts only comma-separated built-in names or categories;
  `exploit`, `brute`, `dos`, `malware`, `fuzzer`, `external`, and `broadcast`
  are denied. `--datadir`, `--script-args-file`, and `--script-updatedb` are
  denied because they load or write arbitrary NSE code.
- Nuclei custom templates, workflows, code templates, and headless mode are
  denied (`-t`, `-templates`, `-w`, `-workflow`, `-code`, `-headless`).
  Whatweb custom plugins are denied.
- `openssl` is limited to `s_client` and `x509`; `-engine` and `-provider`
  flags are denied because they load native code.

The wrapper never invokes a shell, so shell metacharacters are inert; they are
still rejected to keep plans honest and prevent confusion.

### 7.4 Risk result

The wrapper returns the highest tier in the task. The dialog shows it. The
manager does not rely on the model's risk claim for enforcement.

## 8. Kestrel integration

### 8.1 Tool

`kali_task` — `system_control`, timeout 1800 seconds. It first sends
`mode: "check"`, then requests approval with the returned risk/targets, then
sends `mode: "execute"` only after Yes.

Input schema (kept compact for the prompt budget):

```json
{
  "type": "object",
  "properties": {
    "objective": {"type": "string"},
    "steps": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "argv": {"type": "array", "items": {"type": "string"}},
          "timeout": {"type": "integer"}
        },
        "required": ["argv"]
      }
    },
    "total_timeout": {"type": "integer"}
  },
  "required": ["objective", "steps"]
}
```

`kali_scope` — `read_only`, no input. Sends a `mode: "scope"` request and
returns the scope summary, or an honest error if Kali is unreachable.

The exact command formats and allowed binaries live in
`skills/core_tools/SKILL.md`, not in the prompt schema.

### 8.2 Routing

Add a `security` rule group with patterns for `kali`, `nmap`, `scan`,
`recon`, `pentest`, `security test`, and `vulnerability`. Add both tools to
`prompt_catalog` descriptions and keywords.

### 8.3 Configuration

`%APPDATA%\Kestrel\kali.json`:

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

The key is generated with `ssh-keygen -t ed25519`. Its ACL grants only the
Windows user. The host key is pinned; `StrictHostKeyChecking=yes` and
`UserKnownHostsFile` point at the Kestrel-specific known-hosts file.

## 9. Audit and artifacts

- Audit log: `/var/log/kestrel/audit.jsonl`, owned by `kestrel`, mode `0600`,
  rotated by logrotate. The directory is owned by `kestrel`, mode `0700`.
- Each record includes timestamp, objective hash, argv, target checks, risk,
  approval result, exit code, duration, and stdout SHA-256.
- Full stdout/stderr are not written to the audit log. Phase 1 returns them in
  the tool response with caps. Phase 3 writes large outputs under
  `/var/log/kestrel/runs/<id>/` with mode `0600` and adds `scp` retrieval.
- Kestrel stores the summarized report and the raw artifact path in its own
  artifact store.

## 10. Failure handling

| Failure | Behavior |
|---|---|
| SSH unreachable | `kali_task` returns "Kali VM is not reachable at host:port. Start it and retry." |
| Host key mismatch | Refuse; tell the user to verify the VM and update `known_hosts`. |
| Scope missing/expired | Refuse; tell the user which file to update. |
| Out-of-scope target | Refuse with the exact target and the allowed ranges. |
| Binary not allowed | Refuse and list the phase-1 binaries. |
| Destructive flag | Refuse and name the flag. |
| Dialog denied/unavailable | Do not execute; return "not approved". |
| Step timeout | Kill the process group; return partial output with `timed_out: true`. |
| Nonzero exit | Return the result; Kestrel reports the failure and does not claim success. |
| Malformed response | Treat as failure; do not retry blindly. |

## 11. Security and threat model

**Prompt injection in tool output.** Target banners, HTTP pages, and scan output
are untrusted data. Kestrel's prompt already treats tool output as data. The
wrapper never follows instructions from output. The model cannot add targets or
commands after approval without a new dialog.

**Model proposes an out-of-scope target.** The wrapper re-checks every target
against the Kali-side scope file. The Windows copy is only for planning.

**Compromised Kali VM.** It holds no personal accounts, keys, or Kestrel memory.
The SSH key on Windows is dedicated to this VM and can be revoked without
affecting anything else. The VM can be rolled back to a snapshot.

**Compromised Windows Kestrel.** It can run allowed tools against in-scope lab
targets only. It cannot write the scope file, invoke a shell, or use a
non-allowlisted binary. It cannot reach public targets while
`allow_public` is false.

**Approval spoofing.** The Qt dialog is the only approval surface. A tool result
or model message cannot press Yes. Headless runs are denied.

**Noise and side effects.** Intrusive commands are visibly marked in the dialog.
The user takes a lab snapshot before intrusive work. Phase 1 relies on per-step
and total timeouts for remote termination; the GUI Stop button stops the local
turn. A remote kill channel is phase 3.

## 12. Testing

### Wrapper tests (Kali)

- Accepts an in-scope IPv4, CIDR, hostname, and URL.
- Denies public IPs with `allow_public: false`.
- Denies an out-of-scope private IP.
- Denies an expired or malformed scope.
- Denies a non-allowlisted binary.
- Denies `bash`, `sh`, `python3`, `rm`, and shell metacharacters.
- Denies destructive flags.
- Classifies `nmap` as recon, `ffuf` as intrusive, and returns the highest tier.
- Runs two steps in one request and stops after a failed step.
- Kills a process group on timeout and marks `timed_out`.
- Truncates output at the cap and marks `truncated`.
- Writes an audit record with hashes, not raw stdout.
- Scope mode returns the scope without executing anything.
- Check mode returns risk and targets without executing anything.

### Kestrel tests (Windows)

- Fake `ssh` executable returns a valid response; the client parses it.
- Unreachable host, policy denial, timeout, malformed JSON, and nonzero exit
  each produce distinct honest errors.
- Approval relay: worker thread emits, GUI thread shows, default No.
- Headless run is denied.
- `kali_task` sends check, blocks on approval, then sends execute only after
  Yes; a No never sends execute.
- `kali_scope` is read-only and never sends a command.
- Prompt budget still passes with the new tool schemas. Shrink the schemas or
  move detail into the manual; do not raise the budget.
- Tool registration: `VALID_TOOLS`, `OBJECT_INPUT_TOOLS`, registry, native
  schema, prompt catalog, core-tools manual.

### Live smoke

Kali VM on the host-only network, a second local VM as the target:

1. `nmap -sV <target>` through `kali_task`; approve; verify dialog, summary,
   audit record, and target re-check.
2. Try `nmap -sV 8.8.8.8`; verify refusal before execution.
3. Try `bash -c "id"`; verify refusal.
4. Deny the dialog; verify nothing ran.

No public targets are used in any test.

## 13. Phases

1. **Kali base:** VM, SSH, forced command, scope file, wrapper, audit log,
   manual `nmap` smoke. No Kestrel changes.
2. **Kestrel tool:** `kali_bridge.py`, `kali_task`, `kali_scope`, Qt approval
   relay, routing, manual, tests.
3. **Recipes and artifacts:** named recipes for repeated recon/web flows, large
   output retrieval, report generation.
4. **Old laptop:** move the same image/config; change the SSH host.
5. **Advanced tiers:** add exploitation and credential binaries with dedicated
   dialogs and live tests.
6. **Phone track:** separate project, not part of this spec.

## 14. Decisions

- SSH forced-command wrapper over an in-VM HTTP agent. Fewer moving parts and
  the same policy works on both hosts.
- One `kali_task` tool with a Qt approval dialog, not a separate plan/run pair.
- Kali owns the authoritative scope; Kestrel's view is read-only.
- CLI-first. Burp and other GUI tools are out of scope for this spec.
- No second AI. Kestrel plans; Kali executes.
- The phone is a separate subsystem with physical hardware, not an emulator.
