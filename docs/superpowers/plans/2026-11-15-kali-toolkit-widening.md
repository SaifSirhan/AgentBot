# Kali Toolkit Widening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Note for this session:** subagent types were unavailable (Explore/Plan/general-purpose removed), so the loop ran sequentially in one agent with the same verification criteria. No independent reviewer seat.

**Goal:** Give Kestrel a much wider Kali toolkit without weakening scope confinement, human approval, or wrapper-computed risk tiers.

**Architecture:** Replace the deny-list-only posture with a per-binary *permit list* of flags, staged so no currently working scan can break. Add `inventory` mode so Kestrel can learn what the VM actually has, add the `credential` tier so double-approval becomes reachable, then add tools onto the strict path.

**Tech Stack:** Python 3.11 (pyenv-win on the host, system Python in the VM), `unittest`, PySide6 on the Kestrel side, JSON-over-stdin protocol v1, nftables for VM egress.

**Spec:** `docs/superpowers/specs/2026-11-15-kali-toolkit-widening-design.md`

## Global Constraints

- Protocol stays `version: 1`; modes are `scope | check | execute | inventory`.
- Every destination visible in argv must resolve inside `scope.networks` / `scope.domains`.
- `classify()` derives the tier from argv; the model's declared `risk` is never used for approval.
- `TIERS = {"recon": 0, "intrusive": 1, "credential": 2, "exploitation": 3}` — `exploitation` stays unreachable.
- `MAX_STEPS = 8`, `MAX_STEP_TIMEOUT = 600`, `MAX_TOTAL_TIMEOUT = 1800`, `OUTPUT_ROOT = /tmp/kestrel`, `READ_ONLY_DIRS` unchanged.
- Never permit a flag that execs, loads a module, reads an out-of-scope file, lists targets, or proxies.
- `setoolkit`, `ettercap`, `arpspoof`, shells, interpreters and `sudo` are permanently refused.
- Test commands: from repo root `python -m unittest discover -s tests -t tests -p "test_*.py"` (baseline 1,744); from `kali/` `python -m unittest discover -s tests -t tests -p "test_*.py"` (baseline 144, 2 skips).
- Commit after every task; never bundle unrelated working-tree changes.

---

### Task 1: `inventory` mode

**Files:**
- Modify: `kali/kestrel_run.py` (`_main` mode branch, new `_binary_present`/`build_inventory`, `EGRESS_RULE`)
- Modify: `kali/tests/test_kestrel_run.py` (new `InventoryModeTests`)
- Modify: `kali/README.md` (§7 live checks, one command)

**Interfaces:**
- Consumes: `_ok(**fields)`, `_audit_or_refuse(path, event)`, `SAFE_PATH`, `RECON`, `INTRUSIVE`, `LOCAL_SAFE`, `DENIED_BINARIES`, `TIERS`.
- Produces: `build_inventory() -> dict` with keys `recon`, `intrusive`, `credential`, `local_safe`, `denied_present`, `strict`, `egress_enforced`; request `{"mode": "inventory"}` → `{"ok": true, "inventory": {...}, "version": 1}`.

- [ ] **Step 1: Failing tests**

```python
class InventoryModeTests(unittest.TestCase):
    def _run(self, request, files):
        """files: mapping of name -> True/False for the fake SAFE_PATH."""
        root = self.enterContext(tempfile.TemporaryDirectory())
        bindir = os.path.join(root, "bin")
        os.mkdir(bindir)
        for name, present in files.items():
            if present:
                path = os.path.join(bindir, name)
                open(path, "w").close()
                os.chmod(path, 0o755)
        with mock.patch.object(w, "SAFE_PATH", bindir + ":/nonexistent"), \
                mock.patch.object(w, "EGRESS_RULE",
                                  os.path.join(root, "egress.conf")):
            return w._main_for_test(request)

    def test_lists_present_binaries_by_tier(self):
        response = self._run({"mode": "inventory"},
                             {"nmap": True, "hydra": True, "masscan": False})
        self.assertTrue(response["ok"])
        self.assertIn("nmap", response["inventory"]["recon"])
        self.assertIn("hydra", response["inventory"]["credential"])
        self.assertNotIn("masscan", response["inventory"]["intrusive"])

    def test_never_spawns_a_child(self):
        with mock.patch.object(w.subprocess, "Popen",
                               side_effect=AssertionError("spawned")):
            response = self._run({"mode": "inventory"}, {"nmap": True})
        self.assertTrue(response["ok"])

    def test_egress_flag_tracks_the_rule_file(self):
        self.assertFalse(self._run({"mode": "inventory"}, {})["inventory"]
                         ["egress_enforced"])
```

Note: `_main_for_test` does not exist — add it in Step 3 as a thin wrapper that injects `sys.stdin`/paths, or reuse the existing test harness the module already has for `_main`. **Read `kali/tests/test_kestrel_run.py` first and use its existing helper for driving `_main`; do not invent a second harness.**

- [ ] **Step 2: Verify RED** — `cd kali && python -m unittest tests.test_kestrel_run -v`, expect `unknown mode` denial or missing `inventory`.
- [ ] **Step 3: Implement**

```python
EGRESS_RULE = "/etc/kestrel/egress.conf"


def _binary_present(name):
    """PATH-restricted existence check. Never execs, never inherits PATH."""
    for directory in SAFE_PATH.split(":"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


def build_inventory():
    groups = {"recon": sorted(RECON), "intrusive": sorted(INTRUSIVE),
              "credential": sorted(CREDENTIAL), "local_safe": sorted(LOCAL_SAFE),
              "denied_present": sorted(n for n in DENIED_BINARIES
                                       if _binary_present(n)),
              "strict": sorted(STRICT_BINARIES),
              "egress_enforced": os.path.isfile(EGRESS_RULE)}
    return {name: [binary for binary in binaries if _binary_present(binary)]
            for name, binaries in groups.items()}
```

and in `_main`, before the steps parsing block:

```python
    if mode == "inventory":
        refused = _audit_or_refuse(audit_path, {"event": "inventory"})
        if refused is not None:
            return refused
        return _ok(inventory=build_inventory())
```

Add `"inventory"` to the accepted modes tuple.
- [ ] **Step 4: Verify GREEN**, then mutation-check: make `_binary_present` always return True → a test must fail; make `inventory` execute → the no-spawn test must fail.
- [ ] **Step 5: Commit** `feat(kali): report installed toolkit and policy state over the protocol`

---

### Task 2: credential tier, strict mechanism, and the three credential binaries together

**Why merged.** `prompt_router.INTENT_RULES["security"]` already advertises
`sqlmap` and `hydra` while `classify()` denies them, and the drift test's whole
purpose is to catch that mismatch — so a task that adds the drift test must not
be green until the binaries exist. Splitting "make it reachable" (tier mapping)
from "audit its flags" (strict table) would land a reachable-but-unaudited
binary for one commit: exactly the posture this plan is replacing. Tier
mapping, strict enforcement, minimum declared flags and the drift test therefore
ship in one task.

**Files:** Modify `kali/kestrel_run.py` (`CREDENTIAL`, `STRICT_BINARIES`,
`BOOLEAN_FLAGS`, `PERMITTED_FLAGS`, `classify`), `kali/tests/test_kestrel_run.py`,
`tests/test_kali_routing.py`; read-only on `prompt_router.py`/`agent.py`.
**Produces:** `CREDENTIAL = frozenset({"hydra", "medusa", "sqlmap"})`;
`classify()` returns `"credential"` for them; `PERMITTED_FLAGS(binary) -> frozenset`;
`PERMITTED_BINARIES = RECON | INTRUSIVE | CREDENTIAL | LOCAL_SAFE` for the drift test.
- [ ] **Step 1:** failing tests — `classify(["hydra","-l","u","-P","/usr/share/wordlists/x","127.0.0.1","ssh"]) == "credential"`; `validate_steps` reports `credential`; `needs_second_confirmation("credential") is True`; an **undeclared** hydra flag (`--restore`) is refused naming the flag; `sqlmap --tamper` and `http-post-form` service spec refused (spec §6.1/§6.2); drift test parsing `prompt_router.INTENT_RULES["security"]` and the `kali_task` description asserting every tool-shaped token is in `PERMITTED_BINARIES` or `DENIED_BINARIES`.
- [ ] **Step 2:** RED (hydra → PolicyError "not allowed"; drift fails on `sqlmap`, `hydra`).
- [ ] **Step 3:** implement `CREDENTIAL`, the `elif binary in CREDENTIAL` branch, `STRICT_BINARIES = {hydra, medusa, sqlmap}` with their minimum `BOOLEAN_FLAGS`/`VALUE_FLAGS` declarations, `PERMITTED_FLAGS`, and the undeclared-flag refusal inside `classify` for strict binaries only.
- [ ] **Step 4:** GREEN + mutation (move the CREDENTIAL branch after the `DENIED_BINARIES` check → must fail; make strict enforcement unconditional → the non-strict pilot in Task 3 must fail).
- [ ] **Step 5:** Commit `feat(kali): credential tier with strict flag permission`.

---

### Task 3: pilot-migrate one existing binary to strict

**Files:** Modify `kali/kestrel_run.py` (`STRICT_BINARIES`, `BOOLEAN_FLAGS["masscan"]`), `kali/tests/test_kestrel_run.py`.
**Produces:** masscan in `STRICT_BINARIES` with its full declared flag set, proving a binary that already works can switch posture without a behaviour change.
- [ ] **Step 1:** tests — every flag exercised anywhere in the existing masscan tests is declared; one undeclared invented flag is refused; `masscan -p80 127.0.0.1` still classifies `intrusive`.
- [ ] **Step 2:** RED.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** GREEN; then live `check` of a real masscan command against the VM and compare the verdict with the pre-change verdict.
- [ ] **Step 5:** Commit `feat(kali): migrate masscan to the strict flag permit list`.

---

### Task 4: tier-0 additions, born strict

**Binaries:** `httpx`, `arp-scan`, `traceroute`, `ping`.
Each needs: `RECON` entry, `BOOLEAN_FLAGS`, `VALUE_FLAGS`, `PATH_FLAGS`/`READ_FLAGS` where applicable, `DESTINATION_FLAGS`, denies (`httpx -l/-listener/-proxy/-res-x`; `arp-scan -r/-t`; `traceroute -g/-F`; `ping -f`), and a `STRICT_BINARIES` entry.
- [ ] Steps: table-driven failing tests → RED → implement → GREEN → live `check` for one command per binary → commit `feat(kali): add httpx, arp-scan, traceroute and ping as strict recon tools`.

### Task 5: tier-1 additions, born strict

**Binaries:** `dirb`, `dirsearch`, `wfuzz`, `wpscan`, `sslyze`. Refusals per spec §6 (`--proxy`, `wfuzz --cmd/--pickle/--file`, `httpx -listener`, `dirsearch --clear-screen`).
- [ ] Steps as Task 4, commit `feat(kali): add content discovery and TLS posture tools at intrusive tier`.

### Task 6: `crunch` with a generated-size guard

**Files:** Modify `kali/kestrel_run.py` (`MAX_GENERATED_LINES`, `_crunch_line_count`, `LOCAL_SAFE`, `FREE_POSITIONALS`, `STRICT_BINARIES`, tests), `agent.py` (`kali_task` description mentions the cap).
- [ ] **Step 1:** tests at the boundary, including `-t` placeholder arithmetic (`username%%` → 26^2 × literal positions) and a refusal message containing the computed count. **Also assert `crunch 6 8 -o <OUTPUT_ROOT>/w.txt` is not refused for lacking a target** — crunch joins `LOCAL_SAFE` and `FREE_POSITIONALS` (ledger ruling: its positionals are lengths and a template, never destinations).
- [ ] **Step 3:** implement `_crunch_line_count(min_len, max_len, charset_size, template)`; refuse before execution when it exceeds `MAX_GENERATED_LINES`; `-o` confined to `OUTPUT_ROOT`.
- [ ] **Step 5:** Commit `feat(kali): crunch wordlist generation bounded before it runs`.

### Task 7: close the credential-tier escape surface

These binaries became reachable in Task 2; this task *narrows* what they can do,
it does not open them. Per the ledger ruling, nothing here may re-tier a binary
(the tier drives the second confirmation) or remove a Task 2 declaration.

**Files:** Modify `kali/kestrel_run.py`, `kali/tests/test_kestrel_run.py`, `tests/test_kali_tools.py`, `tests/test_kali_flow.py`.
**Specific refusals:** spec §6.1 and §6.2 — `http-post-form`/`http-get-form` service modules for hydra (their form spec carries a destination the wrapper never sees as an argv target), `--tamper`/`--preprocessor` (**load Python modules from disk — an exec primitive**) plus `-m`/`-g`/`--google`/`--sql-file`/`--sql-query`/`--tmp-path` escapes for sqlmap, session and resume flags (`-b`/`-c`/`-R`/`--restore`) for both, `-M`/`-L` target lists.
- [ ] **Step 1:** failing tests for each refusal above, each asserting the refusal names the flag, plus a flow test that a credential-tier task asks twice and that denying the second dialog runs nothing (`execute_intent` absent from the audit).
- [ ] **Step 2:** RED — expect the currently-declared-minimal strict tables to accept the flag under test.
- [ ] **Step 3:** implement the refusals and the value classes (`credential`, confined `READ_FLAGS` wordlists, `OUTPUT_ROOT` outputs).
- [ ] **Step 4:** GREEN + mutation (delete one refusal, delete the tier branch, re-tier hydra to `intrusive` — each must fail a test).
- [ ] **Step 5:** live `check` of a hydra command against `127.0.0.1` (no execute) and one `http-post-form` refusal, then commit `feat(kali): close hydra, medusa and sqlmap escape surfaces`.

### Task 8: VM egress enforcement + `theHarvester`

**Files:** Modify `kali/install_kestrel_run.sh`, `kali/tests/test_install_assets.py`, `kali/kestrel_run.py` (`EGRESS_RULE` path constant is used by Task 1).
- [ ] **Step 1:** asset tests asserting the installer writes `/etc/kestrel/egress.conf` naming the permitted lookup sources, installs an nftables table for the `kestrel` user only, `bash -n` passes, and the script refuses to run as non-root / with a symlinked `/tmp/kestrel`.
- [ ] **Step 3:** implement; `theHarvester` becomes a strict credential-tier binary whose `-d` must match `scope.domains`.
- [ ] **Step 4:** `bash -n kali/install_kestrel_run.sh` must pass.
- [ ] **Step 5:** Commit `feat(kali): confine kestrel egress and add theHarvester behind it`.

### Task 9: Kestrel-side text, README, TESTING.md

- [ ] Update `kali/README.md` §1 (tool list + tiers), §4 (egress + domains), §7 (live checks per new tier, including the two-dialog credential flow), plus `inventory` usage.
- [ ] Update `prompt_router.INTENT_RULES["security"]` and the `kali_task` description to name the new binaries and the caps; keep the prompt budget assertions green (static < 4,100 tokens, security-turn delta ≤ 512).
- [ ] Update `TESTING.md` counts.
- [ ] Commit `docs(kali): document the widened toolkit, tiers and egress rule`.

### Task 10: full verification

- [ ] Both suites from a clean tree, reporting each `Ran N tests`/`OK` line, not just the exit code.
- [ ] Live: `inventory`, one tier-0 `execute`, one tier-1 `execute`, one credential-tier `execute` proving the second dialog appears and that denying it runs nothing (audit shows `execute_intent` absent).
- [ ] Mutation pass over each new table entry.
- [ ] Report deltas from baseline honestly, including anything that could not be verified live.
