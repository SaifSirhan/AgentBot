# Kali Toolkit Widening — Design

**Date:** 2026-11-15
**Status:** Approved by the operator; supersedes §2 non-goals of the 2026-11-11 wrapper spec
**Scope:** Kali general-toolkit track only. The phone track is separate.
**Builds on:** `docs/superpowers/specs/2026-11-11-kali-ssh-wrapper-design.md`

## 1. Goal

Let Kestrel run a much wider set of Kali tooling in the lab without weakening the
three properties that make unattended LLM-driven execution acceptable:

1. every destination the wrapper can see must be inside `scope.json`;
2. every executing step is approved by a human, twice at intrusive and above;
3. the tier is computed by the wrapper from the argv, never declared by the model.

Widening the allowlist is cheap. Widening it *while keeping 3 true* is the work,
because the current model is a deny-list: an unknown flag is permitted and only
its value is constrained (`_unknown_item`, `kestrel_run.py:641`). Twelve audited
binaries survived three fix rounds under that posture because every round found
the one flag nobody thought of — `--proxies`, `--script-args newtargets`, curl
`-O`, ffuf `-input-cmd`, nmap `--datadir`. This design replaces that posture with
a per-binary permit list and then adds tools onto it.

## 2. Revised non-goal

The 2026-11-11 spec listed "no autonomous exploitation, credential attacks,
evasion, or persistence" as a phase-1 non-goal. **Credential-tier tooling against
scope-confined lab targets is now in scope**, deliberately, at tier 2 with double
approval. The compensating controls are §4–§6 below and the fact that Meta's
addresses — or anybody else's outside the lab — are not in `scope.networks`, so
such a step fails at policy rather than at intent.

Nothing in this document authorises work against a target the operator does not
own or is not engaged to test.

## 3. Out of scope permanently

| Item | Why it cannot be governed here |
|---|---|
| Social-Engineer Toolkit (`setoolkit`) | Interactive curses menu, not argv: `classify()` has no flags to inspect. Wants `sudo`. Its effective target is whoever clicks, which is not an address in `scope.json`, so property 1 stops applying. |
| `ettercap -M arp:remote`, `arpspoof`, any ARP/DHCP spoofing | Operates on the broadcast domain, not a target. Every host on the segment is intercepted, including the router and anything physically attached that is not in scope. Requires root and raw sockets. If ever run, it is an operator step on a verified isolated segment, never an LLM-invocable binary. |
| Anything needing `sudo`, a shell, or an interpreter | `DENIED_BINARIES` already refuses `bash sh zsh dash python3 python perl ruby`; a permit-list binary must not be able to reach those. |

## 4. Strict per-binary flag permission

New table, derived rather than duplicated:

```
PERMITTED = VALUE_FLAGS[b] ∪ BOOLEAN_FLAGS[b] ∪ NO_VALUE_FLAGS[b]
```

`BOOLEAN_FLAGS` is the one new per-binary list: flags that take no value
(`nmap -sV`, `curl -s`, `hydra -q`). A binary listed in `STRICT_BINARIES` refuses
any flag name outside `PERMITTED`, with the refusal naming the flag so the model
can self-correct:

```
PolicyError("flag %s is not permitted for %s")
```

The existing deny tables stay as a second net for binaries not yet strict.
Migration is staged: new binaries are born strict, then the twelve existing
binaries are converted one at a time, each proven by a live `check` of a
representative scan plus a full local test pass. A binary that is not in
`STRICT_BINARIES` behaves exactly as it does today, so no scan that works now can
break because of a later task.

## 5. Tiers

`TIERS` keeps four levels; `classify()` gains a credential mapping so level 2
becomes reachable for the first time, and `exploitation` (3) stays unreachable
because no permitted binary is assigned to it.

```
CREDENTIAL = frozenset({"hydra", "medusa", "sqlmap"})
```

`kali_bus.RISK_TIERS` already mirrors 0/1/2/3 with `SECOND_CONFIRMATION_TIER = 1`,
so the double approval covers credential work without change. The existing
tier-guard and drift tests are extended to assert the mirror and that every tier
named in a prompt is a tier the wrapper can actually return.

## 6. Binaries added

| Binary | Tier | Purpose | Specific refusals |
|---|---|---|---|
| `httpx` | 0 recon | probe/listen for HTTP services, tech detect | `-l` list input (target smuggling), `-listener` (binds a socket), `-proxy`, `-res-x` |
| `arp-scan` | 0 recon | own-LAN host discovery | `-r` retry-forever, `-t` read timeout, file flags |
| `traceroute`, `ping` | 0 recon | reachability inside scope | `-g`/`-F` loose source route, `-f` fragmentation flood |
| `dirb` | 1 intrusive | wordlist dir brute force | no `-o` outside `OUTPUT_ROOT` |
| `dirsearch` | 1 intrusive | content discovery | `--proxy`, `-r` recursive-reload, `--clear-screen` |
| `wfuzz` | 1 intrusive | generic fuzzer | `--cmd`/`--follow`/`-p` proxy/`--pickle`/`--file` write paths |
| `wpscan` | 1 intrusive | CMS enumeration | `--proxy`, `--random-useragent` |
| `sslyze` | 1 intrusive | TLS posture of lab endpoints | `--hashir`/`--starttls`/file writes |
| `hydra` | 2 credential | login brute force against lab services | see §6.1 |
| `medusa` | 2 credential | parallel login attempts | same class of refusals |
| `sqlmap` | 2 credential | SQLi against lab web apps | see §6.2 |
| `crunch` | 0 recon | local wordlist generation | §6.3 |
| `theHarvester` | 2 credential | domain OSINT | §6.4 |

### 6.1 hydra

`-l`, `-p` are `credential` class values; `-L`, `-P`, `-m`, `-x` are `READ_FLAGS`
confined to `OUTPUT_ROOT` + `/usr/share/wordlists` + `/usr/share/seclists`.
Refused: `-o`/`-O` outside `OUTPUT_ROOT`, `-b`/`-c`/`-R` session-file read/write
(state smuggling and resume past approval), `-e` except the literal `ns`/`n`/`s`,
`-K`, `-M` (target list), `--restore`, and any service module whose syntax
embeds a URL (`http-get-form`/`http-post-form`/`https-*`): the form spec carries
a destination the wrapper cannot see as an argv target, so those modules are
permitted only when the module's host field is given as a separate in-scope
token — which in practice means **operator-approved direct-IP forms only**, and
the simplest honest rule is to permit `ssh`, `ftp`, `http-get`, `https-get` and
refuse the `-form` variants.

### 6.2 sqlmap

`-u`/`--url` is a `DESTINATION_FLAGS` target. `-d`, `--dbms`, `--level`, `--risk`,
`--threads`, `--timeout`, `--technique`, `--tables`, `--dump`, `--dump-all`
permitted. Refused: everything in the existing `DESTRUCTIVE_FLAGS`
(`--os-shell --os-pwn --os-cmd --file-write --file-dest --sql-shell`), plus
`--tamper` and `--preprocessor` (**both import Python modules from disk — an exec
primitive in a permit-list world**), `--wizard`, `-m` (target list), `-g` and
`--google`/`--bing` (targets chosen by a search engine), `--dns-server`,
`--sql-file`/`--sql-query` (file read + direct query bypass), `--csrf-token`
value stays permitted, `--output-dir` confined, `--tmp-path` confined.

### 6.3 crunch

No network at all: `LOCAL_SAFE`-shaped, but its output is the point, so `-o`
must land in `OUTPUT_ROOT`. The hazard is size, not escape — `crunch 6 12 -t
user%%` emits ~10^9 lines and the 600 s step cap kills it *after* the VM disk is
full. New bound, enforced in the wrapper before execution and mirrored in the
tool description:

```
MAX_GENERATED_LINES = 5_000_000
```

computed from min/max length and the charset/`-t` placeholder count; a request
whose arithmetic exceeds it is refused with the computed number in the message.

### 6.4 theHarvester and the LOOKUP problem

`-d <domain>` is scope-governable and must match `scope.domains`. But `-b all`
also queries Google, Bing, crt.sh, HackerTarget and LinkedIn **from inside the
child process**. Those destinations never appear in argv, so argv inspection
cannot see them, let alone confine them.

This is a real limit of the existing design, stated here rather than papered
over: *the scope layer governs destinations the wrapper can see.* Today every
permitted binary names its destination in argv, so that limit has never bitten.
For `theHarvester` there are three honest options and this spec takes the third:

1. ship it anyway and rely on the human approving a tier-2 step — unacceptable,
   because it converts a hard control into a review checkbox;
2. give the child a proxy — blocked, since proxy flags are themselves refused
   (§6.1) and refusing them would defeat the purpose;
3. **enforce egress in the VM**, in the installer, with an nftables rule that
   the `kestrel` user may only reach `scope.networks` plus an explicit
   `egress` list from `scope.json`, and treat any binary that dials out on its
   own as requiring that rule to be installed. `theHarvester` is therefore
   gated: `classify()` returns `credential`, and the wrapper reports
   `egress_enforced: false` in `inventory` output so Kestrel can tell the
   operator the control is not yet in place.

## 7. `inventory` mode

New request mode, tier 0, no targets required, read-only, never executes:

```json
{"mode": "inventory", "protocol": 1}
→ {"ok": true, "binaries": {"recon": [...], "intrusive": [...],
   "credential": [...], "denied": [...]}, "strict": [...], "scope": {...},
   "egress_enforced": false, "version": 1}
```

Presence is determined by `os.path.exists` on each name under `SAFE_PATH` only —
no `exec`, no `which` subprocess, no `PATH` inherited from the request. `denied`
lists the names from `DENIED_BINARIES` that exist, so the operator can see what
the policy is protecting against without the wrapper executing anything.

This closes a live gap: today Kestrel cannot tell whether `hydra` is installed,
so it either invents a scan or invents a refusal.

## 8. Kestrel side

* `prompt_router` security keyword set gains the new binaries; the router rule
  keeps mandating `kali_task` rather than a shell excuse.
* `agent.kali_task` description states the caps it currently states (steps,
  per-step and total timeout) plus the new tier facts, and stops naming tools
  the wrapper does not permit.
* **Drift test:** every binary named in `prompt_router`/`agent`/`kali/README`
  must exist in `RECON ∪ INTRUSIVE ∪ CREDENTIAL ∪ LOCAL_SAFE ∪
  ALLOWED_SUBCOMMANDS` keys, or be explicitly listed as refused in
  `DENIED_BINARIES`. This is the check that would have caught `hydra` and
  `sqlmap` being advertised in the router text while `classify()` refused them
  (observed live, 2026-11-15).

## 9. Testing

* Table-driven policy tests per new binary: every permitted flag accepted in a
  `check`, representative denied flags refused, values in each class confined or
  scope-checked, tier escalation asserted.
* Strict-mode tests: undeclared flag refused with the flag named; a non-strict
  binary unchanged.
* `crunch` size-guard tests at the boundary, including the `-t` placeholder
  arithmetic.
* `inventory` tests against a fake `SAFE_PATH` with files present/absent, and a
  test proving `inventory` never spawns a child (mutation-checked).
* End-to-end fake-bridge flow for a credential-tier task: one approval, then a
  second confirmation, then execution; denial at either point runs nothing.
* `kali/tests/test_kestrel_run.py` and `tests/test_kali_*.py` suites in full,
  then both suites from a clean tree, then live `check` and one live `execute`
  per new tier against the VM.

## 10. Sequencing

1. `inventory` mode — no policy risk, tells us what is installed.
2. Tier mapping for `credential` + drift test.
3. Strict mechanism, pilot-migrate one existing binary.
4. Tier-0 additions, born strict.
5. Tier-1 additions, born strict.
6. `crunch` with the size guard.
7. hydra, medusa, sqlmap.
8. Egress rule in the installer + `theHarvester`.
9. Kestrel router/description/README/`TESTING.md`, counts updated.
10. Both suites, live verification.

Each step is independently shippable and leaves the tree green.
