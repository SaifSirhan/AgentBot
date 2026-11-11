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
2. Dispatch on `mode`: `check` (default), `scope`, or `execute`. A request
   that does not explicitly ask to execute never runs a command.
3. Load and validate `/etc/kestrel/scope.json`.
4. Validate every command argument against the binary allowlist and scope.
5. Classify risk and refuse destructive flags.
6. Write a JSONL audit *intent* record before executing; if that write fails,
   refuse to execute.
7. Execute each step with `shell=False`, a timeout, a bounded process-group
   kill, and an output cap, then write the JSONL result record.
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
- Output cap: 256 KB per stream per step; 1 MB total, shared by both streams
  and by every step and counted in bytes. Truncated output is marked, and
  `stdout_sha256`/`stderr_sha256` are computed after truncation so they hash
  exactly the delivered text. Pipes are read as bytes and decoded once with
  replacement characters, so non-UTF-8 output is preserved, never dropped.
- Steps run sequentially and stop after the first failed step or once the
  total deadline passes; no step is started after it.

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
- Target extraction is a fail-closed, flag-aware, binary-aware walk: every
  token must be accounted for. A bare token must parse as a target — IP,
  CIDR, URL host, `host:port` value, `@resolver` token, bracketed IPv6
  address, or hostname — or be an explicitly licensed positional
  (`searchsploit` search terms, a `gobuster` mode word, an `nc` port, a
  `dig`/`host` record type, a `dig +option`); otherwise the request is refused.
  Nothing is silently dropped, even when a file with the same name exists.
  Every flag value ends in exactly one of three outcomes: a validated in-scope
  target, a path confined per §7.3, or a non-destination licensed by an
  explicit value class (ports, record types, headers, methods, timeouts,
  script names, aggression levels). A host-shaped value — one that matches
  `[A-Za-z0-9._:*@+-]+` and carries `.`, `:` or `@` — must parse as a target or
  be refused; it can never be licensed away, and a value that parses as a
  target is always scope-checked. The only values decided before target
  parsing are the three narrow §7.3 licences that cannot name a destination:
  a curl `@file` reference (confined as a path), a credential pair, and a
  decimal number for a numeric flag. Comma-separated values are split and each
  part gets the same treatment, so `8.8.8.8,1.1.1.1` is refused part by part.
  Userinfo forms (`user@host`, `user:pass@host:port`) are stripped to the host,
  which is then scope-checked. Unknown flags stay permitted, but their values
  must pass the same rules and carry no non-destination licence.
  `--flag=value` forms and bundled short flags (`-so`, `-sO`, `-vL`) are
  expanded and checked the same way, and a single-dash long flag
  (`nuclei -output`) is matched whole before any short-flag expansion; `-T` is
  timing for nmap and upload for curl. A bare token containing `,`, `*`, or
  `_` is not a valid single target and is refused. One trailing dot is
  stripped from an FQDN before matching, so `example.com.` is refused as
  public rather than dropped. Only `http` and `https` URLs with a hostname are
  accepted as destinations, scheme-only forms included (`http:host` is a URL,
  `ftp:host` is refused). A command with no target is allowed only for a
  binary in the local-safe list (`searchsploit`, `openssl`; openssl `s_client`
  still needs a validated `-connect` target).
- Destination flags always parse as targets and can never be licensed as
  non-destinations: `curl --url`, `whatweb -u`/`--url`, `nuclei -u`, `ffuf -u`,
  `dig -q`, `masscan --range`. `--range` takes `A-B` and both ends must be
  in-scope IPs in the same network, so a range that crosses out of scope is
  refused. The licensed positionals are narrow: `dig`/`host` accept the record
  types A, AAAA, MX, TXT, NS, SOA, CNAME, PTR, SRV, ANY and CAA (never the
  zone transfers AXFR/IXFR), and a `dig +option` is never a destination — an
  attached value such as `+subnet=8.8.8.8` is still scope-checked.
- Target-list, request-file, and random-target flags are denied in phase 1
  because they can hide out-of-scope targets: `nmap -iL`, `-iR`,
  `--excludefile`, and `-D` (decoys); `nuclei -l`; `ffuf -request`;
  `curl -K`/`--config`; `dig -f`; `masscan -iL` and `--excludefile`. The
  wrapper only accepts targets written directly in the argv.
- The scope file, audit destination, and output root are policy constants
  (`/etc/kestrel/scope.json`, `/var/log/kestrel/audit.jsonl`, `/tmp/kestrel`).
  No `KESTREL_*` environment variable is read, so an SSH client cannot
  redirect policy through `AcceptEnv`.

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
- `nmap`: `--script` outside the allowlist below; `--script-args` carrying
  shell/file writes or `newtargets`
- `curl`: `-o` / `--output` outside `/tmp/kestrel`, `-T` / `--upload-file`
- `nc`: `-e` / `--exec`, `-c`
- Any argv containing `;`, `|`, `&`, `$`, backticks, newline, or redirection
  tokens where they would imply a shell, and any NUL or other C0 control
  character in any token (an embedded NUL would make `Popen` raise)
- Path confinement is structural, not a per-flag denylist: any value that
  looks like a filesystem path (starts with `/`, `./`, `../`, `~`, or
  contains `/` or `\`) must resolve under `/tmp/kestrel` after symlink
  resolution — for the output-path flags (`nmap -oN/-oX/-oG/-oA/-oS`, the
  ffuf, gobuster, feroxbuster, nuclei, nikto and masscan output flags,
  `curl -o`, whatweb `--log-*`, openssl `-out`/`-keylogfile`/`-writerand`,
  plus curl `-D`/`--dump-header`, `-c`/`--cookie-jar`, `--trace`,
  `--trace-ascii`, `--stderr` and openssl `-sess_out`) and for any unknown
  flag alike. Relative paths resolve under the root; `~` and `..` escapes
  and a path equal to the root itself are denied. The known read-only flags
  (ffuf/gobuster/feroxbuster `-w`/`--wordlist`, openssl
  `-in`/`-CAfile`/`-cert`/`-key`/`-untrusted`) may additionally resolve under
  `/usr/share/wordlists`, `/usr/share/seclists`, `/usr/share/nmap`,
  `/usr/share/whatweb`, `/etc/ssl/certs` — nothing else.
- A file-reference sigil is not part of a path name: a leading `@` is stripped
  before confinement for every path-like value, so `@/etc/passwd` can never be
  resolved as a relative name under the root. For the curl flags that really
  read files (`-d`, `--data`, `--data-raw`, `--data-binary`, `--data-ascii`,
  `--data-urlencode`, `-F`, `--form`, `--form-string`) the value is a file
  reference when it carries `@file`, `name=@file`, `name=<file` or (for
  `--data-urlencode`, with no `=`) `name@file`; the referenced path is confined
  to `/tmp/kestrel` and is not target-parsed. A plain `-d name=value` stays
  allowed. Elsewhere a leading `@` keeps the §7.1 target-or-path rule.
- Two value classes are licensed before target parsing because the host-shaped
  rule would otherwise refuse legitimate values, and neither can name a
  destination: curl `-u`/`--user` accepts a credential pair unless it contains
  `@`, `/` or a URL scheme; a flag licensed as a number (timeouts, delays,
  rates, retries, thread counts, aggression levels) accepts `^\d+(\.\d+)?$` or
  a comma list of those. A four-octet IP never matches that pattern, so
  `curl -m 8.8.8.8` is still refused, and an unlicensed flag such as
  `nmap --spoof-mac` gains nothing.
- The wrapper refuses to run if `/tmp/kestrel` is a symlink or, on POSIX,
  exists with a different owner or a mode other than `0700`.
- `curl -O`/`--remote-name` and proxy flags (`-x`, `--proxy`, SOCKS variants,
  `--preproxy`) are denied. Proxy flags are also denied for ffuf, gobuster,
  feroxbuster, nikto, nuclei, whatweb, and openssl's `s_client`.
- Redirect following is denied because a redirect leaves the validated
  destination: `curl -L`/`--location`/`--location-trusted`, `ffuf -r`,
  `gobuster -r`/`--followredirect`, `feroxbuster -r`/`--redirects`,
  `nuclei -follow-redirects`, and `whatweb --follow-redirect`.
- Relay, source-routing and protocol-control flags are denied outright: they
  are attack primitives with no lab need, and licensing their values is what
  let an out-of-scope destination hide in a flag value. `nmap -b` (FTP bounce
  relay), `--dns-servers` (out-of-scope resolvers), `-S` (source spoofing) and
  `--ip-options` (loose source routing); `dig -b`; `curl --proto`,
  `--proto-default`, `--proto-redir`, `--max-redirs`, `-Q`/`--quote`,
  `-P`/`--ftp-port`, `-t`/`--telnet-option` and `-U`/`--proxy-user`;
  `whatweb --max-redirects`.
- `nmap --script` accepts only a comma-separated list of built-in names: the
  categories `default`, `safe`, `version`, `discovery`, or a fixed allowlist
  of named safe scripts (`banner`, `http-title`, `http-headers`,
  `http-server-header`, `http-robots.txt`, `http-favicon`, `ssl-cert`,
  `ssl-enum-ciphers`, `ssh-hostkey`, `ssh2-enum-algos`, `ftp-anon`,
  `smb-os-discovery`, `smb-protocols`, `dns-service-discovery`, `mysql-info`).
  Wildcards (`*`), paths, and expressions are refused; the `exploit`, `brute`,
  `dos`, `malware`, `fuzzer`, `external`, and `broadcast` denial stays as a
  second layer. `--datadir`, `--script-args-file`, and `--script-updatedb` are
  denied because they load or write arbitrary NSE code.
- Nuclei custom templates, workflows, code templates, and headless mode are
  denied (`-t`, `-templates`, `-workflow`, `-code`, `-headless`).
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
  approval result, exit code, duration, and the stdout and stderr SHA-256 of
  the delivered (post-truncation) text.
- An `execute` request writes two distinguishable records: `execute_intent`
  (the approved plan) **before** any step runs, then `execute` (the per-step
  results) afterwards. `check` writes its single `check` record and `scope`
  its single `scope` record, unchanged.
- If the intent write raises `OSError`, the wrapper refuses to execute and
  returns a JSON error: no audit, no run. If the result write fails after
  execution, the response carries the results and an explicit error instead of
  a traceback.
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
| Audit log unwritable | Refuse to execute; return a JSON error. Nothing runs without an intent record. |
| Unexpected internal error | Return a JSON error object and a non-zero exit; never a traceback. |
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
- Runs two steps in one request and stops after a failed step or a spent total
  deadline.
- Kills a process group on timeout, marks `timed_out`, and proves the child is
  dead.
- Truncates output at the cap and marks `truncated`; the total byte cap holds
  across both streams and steps, and the hashes match the delivered text.
- Preserves non-UTF-8 child output as replacement characters, with no reader
  thread traceback.
- Writes an audit record with hashes, not raw stdout; the execute intent is
  written before running, and an unwritable audit log blocks execution.
- Scope mode returns the scope without executing anything.
- Check mode returns risk and targets without executing anything, and is the
  default when the request omits `mode`.
- Refuses malformed JSON, a non-integer or wrong protocol version, a missing
  objective, too many steps, and out-of-range timeouts, each as JSON.
- Confines curl `@file` references and licenses credential pairs and decimal
  numbers without licensing a destination.

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
