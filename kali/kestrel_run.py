#!/usr/bin/env python3
"""Kestrel's forced-command policy wrapper for the Kali lab."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SCOPE = Path(os.environ.get("KESTREL_SCOPE", "/etc/kestrel/scope.json"))
DEFAULT_AUDIT = Path(os.environ.get("KESTREL_AUDIT",
                                   "/var/log/kestrel/audit.jsonl"))
MAX_STEPS = 8
MAX_STEP_TIMEOUT = 600
MAX_TOTAL_TIMEOUT = 1800
MAX_STREAM_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

RECON = frozenset({"nmap", "whatweb", "dig", "host", "whois", "curl",
                   "openssl", "searchsploit"})
INTRUSIVE = frozenset({"ffuf", "gobuster", "feroxbuster", "nuclei",
                       "nikto", "masscan", "nc"})
LOCAL_SAFE = frozenset({"searchsploit", "openssl"})
DENIED_BINARIES = frozenset({"bash", "sh", "zsh", "dash", "python3", "python",
                             "perl", "ruby", "rm", "dd", "mkfs", "mount",
                             "systemctl", "crontab"})
TIERS = {"recon": 0, "intrusive": 1, "credential": 2, "exploitation": 3}
DESTRUCTIVE_FLAGS = {
    "sqlmap": ("--os-shell", "--os-pwn", "--os-cmd", "--file-write",
               "--file-dest", "--sql-shell"),
    "curl": ("-T", "--upload-file"),
    "nc": ("-e", "--exec", "-c"),
}
DENIED_FLAGS = {
    # curl proxies and remote-name writes are exfiltration or write paths.
    "curl": ("-O", "--remote-name", "--output-dir", "-x", "--proxy",
             "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
             "--preproxy", "--resolve", "--connect-to", "--unix-socket",
             "--abstract-unix-socket", "--dns-servers", "--dns-interface",
             "--dns-ipv4-addr", "--dns-ipv6-addr"),
    "ffuf": ("-x", "--proxy"),
    "gobuster": ("--proxy",),
    "feroxbuster": ("--proxy",),
    "nikto": ("-useproxy",),
    "nuclei": ("-proxy", "-t", "-templates", "--templates",
               "-workflow", "--workflow", "-code", "-headless"),
    "whatweb": ("--proxy", "--custom-plugin"),
    "nmap": ("--proxies", "--datadir", "--script-args-file",
             "--script-updatedb"),
    # Dynamic engines/providers load native code; s_client can use a proxy.
    "openssl": ("-engine", "-provider", "-provider-path", "-proxy"),
}
DANGEROUS_NSE = frozenset({"exploit", "brute", "dos", "malware", "fuzzer",
                           "external", "broadcast"})
ALLOWED_SUBCOMMANDS = {"openssl": frozenset({"s_client", "x509"})}
TARGET_LIST_FLAGS = {
    "nmap": ("-iL",),
    "nuclei": ("-l",),
    "ffuf": ("-request",),
    "curl": ("-K", "--config"),
}
# Flags whose next value is a path, not a target, so it is not scope-checked.
PATH_FLAGS = {
    "ffuf": ("-w", "--wordlist"),
    "gobuster": ("-w", "--wordlist"),
    "feroxbuster": ("-w", "--wordlist"),
    "openssl": ("-in", "-CAfile", "-cert", "-key", "-untrusted"),
}
OUTPUT_ROOT = Path(os.environ.get("KESTREL_OUTPUT", "/tmp/kestrel"))
OUTPUT_PATH_FLAGS = {
    "nmap": ("-oN", "-oX", "-oG", "-oA", "-oS"),
    "ffuf": ("-o",),
    "gobuster": ("-o",),
    "feroxbuster": ("--output", "-o"),
    "nuclei": ("--output", "-output", "-o"),
    "nikto": ("--output", "-output", "-o"),
    "masscan": ("-oX", "-oJ", "-oG", "-oL"),
    "curl": ("--output", "-o"),
    "whatweb": ("--log-brief", "--log-verbose", "--log-errors",
                "--log-json", "--log-xml"),
    "openssl": ("-out", "-keylogfile", "-writerand"),
}
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SHELL_CHARS = frozenset(";|&$`><\n\r")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63})+$"
)


class PolicyError(RuntimeError):
    """The request violates the lab policy."""


class ProtocolError(RuntimeError):
    """The request is not valid protocol version 1."""


@dataclass(frozen=True)
class Scope:
    expires: str
    allow_public: bool
    networks: tuple
    domains: tuple

    def as_dict(self):
        return {
            "version": 1,
            "expires": self.expires,
            "allow_public": self.allow_public,
            "networks": [str(network) for network in self.networks],
            "domains": list(self.domains),
        }


def load_scope(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError("scope file is missing or invalid") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PolicyError("scope file must have version 1")
    expires = data.get("expires")
    if not isinstance(expires, str):
        raise PolicyError("scope expiry is missing")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError("scope expiry is invalid") from exc
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expiry.astimezone(timezone.utc):
        raise PolicyError("scope has expired")
    networks = data.get("networks", [])
    domains = data.get("domains", [])
    if not isinstance(networks, list) or not isinstance(domains, list):
        raise PolicyError("scope networks and domains must be lists")
    try:
        parsed_networks = tuple(
            ipaddress.ip_network(str(item), strict=False) for item in networks
        )
    except ValueError as exc:
        raise PolicyError("scope contains an invalid network") from exc
    return Scope(
        expires=expires,
        allow_public=data.get("allow_public") is True,
        networks=parsed_networks,
        domains=tuple(str(item).lower().rstrip(".") for item in domains),
    )


def _public_ip(address):
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved
        or address.is_unspecified
    )


def _private_domain(domain):
    return any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in ("local", "lab", "lan", "internal", "home")
    )


def _domain_allowed(domain, scope):
    for pattern in scope.domains:
        if pattern.startswith("*."):
            if domain.endswith(pattern[1:]):
                return True
        elif domain == pattern:
            return True
    return False


def _target_from_token(token):
    raw = token.strip()
    if not raw:
        return None
    if raw.startswith("@"):
        raw = raw[1:]
    if not raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return None
        try:
            return ("ip", ipaddress.ip_address(raw[1:end]))
        except ValueError:
            return None
    if "://" in raw:
        try:
            raw = urllib.parse.urlsplit(raw).hostname or ""
        except ValueError:
            return None
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if host and port.isdigit():
            raw = host
    try:
        if "/" in raw:
            return ("network", ipaddress.ip_network(raw, strict=False))
        return ("ip", ipaddress.ip_address(raw))
    except ValueError:
        pass
    if HOST_RE.match(raw):
        return ("domain", raw.lower().rstrip("."))
    return None


def target_tokens(argv):
    binary = argv[0]
    path_flags = (tuple(PATH_FLAGS.get(binary, ()))
                  + tuple(OUTPUT_PATH_FLAGS.get(binary, ())))
    skip_next = False
    targets = []
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in path_flags:
            skip_next = True
            continue
        if any(token.startswith(flag + "=") for flag in path_flags):
            continue
        if token.startswith("-"):
            continue
        target = _target_from_token(token)
        if target is not None:
            targets.append(target)
    return targets


def _validate_target(kind, value, scope):
    if kind == "ip":
        if _public_ip(value) and not scope.allow_public:
            raise PolicyError("public IP %s is outside scope" % value)
        if not any(value in network for network in scope.networks):
            raise PolicyError("target %s is outside scope" % value)
        return str(value)
    if kind == "network":
        if _public_ip(value.network_address) and not scope.allow_public:
            raise PolicyError("public network %s is outside scope" % value)
        if not any(value.subnet_of(network) for network in scope.networks):
            raise PolicyError("target %s is outside scope" % value)
        return str(value)
    if not _private_domain(value) and not scope.allow_public:
        raise PolicyError("public domain %s is outside scope" % value)
    if not _domain_allowed(value, scope):
        raise PolicyError("target %s is outside scope" % value)
    return value


def _output_paths(argv, binary):
    flags = sorted(OUTPUT_PATH_FLAGS.get(binary, ()), key=len, reverse=True)
    for index, arg in enumerate(argv):
        for flag in flags:
            if arg == flag:
                yield argv[index + 1] if index + 1 < len(argv) else ""
                break
            if flag.startswith("--") and arg.startswith(flag + "="):
                yield arg.split("=", 1)[1]
                break
            if (not flag.startswith("--") and arg.startswith(flag)
                    and len(arg) > len(flag)):
                yield arg[len(flag):]
                break


def _output_path_ok(path):
    if not path or "~" in path or "\x00" in path:
        return False
    target = Path(path)
    if not target.is_absolute():
        target = Path(OUTPUT_ROOT) / target
    try:
        resolved = target.resolve()
        root = Path(OUTPUT_ROOT).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def _safe_env():
    env = {
        "PATH": SAFE_PATH,
        "HOME": str(OUTPUT_ROOT),
        "TMPDIR": str(OUTPUT_ROOT),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # Windows needs these to start a child; they are absent on Kali.
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def classify(argv):
    binary = argv[0]
    if "/" in binary or "\\" in binary:
        raise PolicyError("binary paths are not allowed")
    if binary in DENIED_BINARIES:
        raise PolicyError("binary %r is not allowed" % binary)
    if binary in RECON:
        tier = "recon"
    elif binary in INTRUSIVE:
        tier = "intrusive"
    else:
        raise PolicyError("binary %r is not allowed" % binary)

    subcommands = ALLOWED_SUBCOMMANDS.get(binary)
    if subcommands is not None and (
            len(argv) < 2 or argv[1] not in subcommands):
        raise PolicyError(
            "%s subcommand must be one of %s"
            % (binary, ", ".join(sorted(subcommands))))

    for flag in DENIED_FLAGS.get(binary, ()):
        if flag in argv or any(
                arg.startswith(flag + "=") for arg in argv):
            raise PolicyError("flag %s is not allowed" % flag)
    if binary == "curl" and any(
            re.match(r"^-[^-]*[xO]", arg) for arg in argv):
        raise PolicyError("curl -O and proxy short flags are not allowed")

    for flag in DESTRUCTIVE_FLAGS.get(binary, ()):
        if flag in argv:
            raise PolicyError("flag %s is not allowed" % flag)
    for flag in TARGET_LIST_FLAGS.get(binary, ()):
        if flag in argv or any(arg.startswith(flag + "=") for arg in argv):
            raise PolicyError(
                "flag %s is not allowed; write targets directly" % flag)
    if binary == "nmap" and any(
            arg == "--script-args" or arg.startswith("--script-args=")
            for arg in argv):
        raise PolicyError("--script-args is not allowed")
    for path in _output_paths(argv, binary):
        if not _output_path_ok(path):
            raise PolicyError(
                "output paths must be under %s" % OUTPUT_ROOT)
    for arg in argv:
        if any(character in arg for character in SHELL_CHARS):
            raise PolicyError("shell control characters are not allowed")

    if binary == "curl":
        if any(flag in argv for flag in (
                "-d", "--data", "--data-binary", "-F", "--form",
                "-T", "--upload-file")):
            tier = "intrusive"
        for flag in ("-X", "--request"):
            if flag in argv:
                index = argv.index(flag)
                method = argv[index + 1].upper() if index + 1 < len(argv) else ""
                if method not in ("GET", "HEAD", ""):
                    tier = "intrusive"
    if binary == "nmap":
        for index, arg in enumerate(argv):
            if arg == "--script" or arg.startswith("--script="):
                value = (
                    argv[index + 1]
                    if arg == "--script" and index + 1 < len(argv)
                    else arg.split("=", 1)[1]
                )
                if not re.fullmatch(
                        r"[A-Za-z0-9_*-]+(?:,[A-Za-z0-9_*-]+)*", value):
                    raise PolicyError(
                        "only comma-separated built-in NSE names are allowed")
                names = {part.lower() for part in value.split(",")}
                if names & DANGEROUS_NSE:
                    raise PolicyError(
                        "exploitation, brute, dos, and malware NSE scripts "
                        "are not allowed")
                tier = "intrusive"
    return tier


def validate_steps(steps, scope):
    highest = "recon"
    targets = []
    for step in steps:
        argv = step["argv"]
        tier = classify(argv)
        found = target_tokens(argv)
        needs_target = (argv[0] not in LOCAL_SAFE
                        or (argv[0] == "openssl"
                            and len(argv) > 1
                            and argv[1] == "s_client"))
        if not found and needs_target:
            raise PolicyError("%s requires a target" % argv[0])
        for kind, value in found:
            targets.append(_validate_target(kind, value, scope))
        if TIERS[tier] > TIERS[highest]:
            highest = tier
    return highest, sorted(set(targets))


def _read_stream(stream, limit, holder):
    total = 0
    chunks = []
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total <= limit:
            chunks.append(chunk)
        else:
            remaining = limit - (total - len(chunk))
            if remaining > 0:
                chunks.append(chunk[:remaining])
    holder["text"] = "".join(chunks)
    holder["truncated"] = total > limit


def _stop_process(process):
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def run_step(argv, timeout):
    started = time.monotonic()
    try:
        Path(OUTPUT_ROOT).mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PolicyError("could not create %s: %s" % (OUTPUT_ROOT, exc)) from exc
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(OUTPUT_ROOT),
            env=_safe_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise PolicyError("could not start %s: %s" % (argv[0], exc)) from exc

    stdout = {}
    stderr = {}
    readers = [
        threading.Thread(target=_read_stream,
                         args=(process.stdout, MAX_STREAM_BYTES, stdout),
                         daemon=True),
        threading.Thread(target=_read_stream,
                         args=(process.stderr, MAX_STREAM_BYTES, stderr),
                         daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process(process)
        process.wait()
    for reader in readers:
        reader.join(2)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()

    output = stdout.get("text", "")
    errors = stderr.get("text", "")
    return {
        "argv": list(argv),
        "exit_code": None if timed_out else process.returncode,
        "stdout": output,
        "stderr": errors,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "truncated": bool(stdout.get("truncated") or stderr.get("truncated")),
        "stdout_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def run_steps(steps, total_timeout):
    deadline = time.monotonic() + total_timeout
    results = []
    total_output = 0
    for step in steps:
        remaining = int(min(step["timeout"],
                            max(1, deadline - time.monotonic())))
        result = run_step(step["argv"], remaining)
        allowed = max(0, MAX_TOTAL_BYTES - total_output)
        if len(result["stdout"]) + len(result["stderr"]) > allowed:
            result["stdout"] = result["stdout"][:allowed]
            result["truncated"] = True
        total_output += len(result["stdout"]) + len(result["stderr"])
        results.append(result)
        if result["timed_out"] or result["exit_code"] not in (0, None):
            break
    return results


def append_audit(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def parse_request(raw):
    try:
        request = json.loads(raw)
    except ValueError as exc:
        raise ProtocolError("request must be valid JSON") from exc
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ProtocolError("request must use protocol version 1")
    return request


def _respond(status, payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    return status


def _ok(**payload):
    return _respond(0, {"ok": True, **payload})


def _denied(reason, status=2):
    return _respond(status, {"ok": False, "denied": True, "reason": reason})


def main(scope_path=None, audit_path=None):
    scope_path = scope_path or DEFAULT_SCOPE
    audit_path = audit_path or DEFAULT_AUDIT
    try:
        request = parse_request(sys.stdin.read())
        scope = load_scope(scope_path)
    except ProtocolError as exc:
        return _denied(str(exc), 3)
    except PolicyError as exc:
        return _denied(str(exc))

    mode = request.get("mode", "execute")
    if mode not in ("scope", "check", "execute"):
        return _denied("unknown mode %r" % mode, 3)
    if mode == "scope":
        append_audit(audit_path, {"event": "scope",
                                  "scope": scope.as_dict()})
        return _ok(scope=scope.as_dict())

    steps = request.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        return _denied("steps must contain 1-%d commands" % MAX_STEPS, 3)
    normalized = []
    for step in steps:
        if not isinstance(step, dict):
            return _denied("each step must be an object", 3)
        argv = step.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and arg for arg in argv)):
            return _denied("argv must be a non-empty list of strings", 3)
        timeout = step.get("timeout", 120)
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not 1 <= timeout <= MAX_STEP_TIMEOUT):
            return _denied(
                "step timeout must be 1-%d seconds" % MAX_STEP_TIMEOUT, 3)
        normalized.append({"argv": argv, "timeout": timeout})

    total_timeout = request.get("total_timeout", 300)
    if (not isinstance(total_timeout, int) or isinstance(total_timeout, bool)
            or not 1 <= total_timeout <= MAX_TOTAL_TIMEOUT):
        return _denied(
            "total_timeout must be 1-%d seconds" % MAX_TOTAL_TIMEOUT, 3)
    objective = request.get("objective", "")
    if not isinstance(objective, str) or not objective.strip():
        return _denied("objective is required", 3)

    try:
        risk, targets = validate_steps(normalized, scope)
    except PolicyError as exc:
        return _denied(str(exc))

    if mode == "check":
        append_audit(audit_path, {
            "event": "check",
            "ts": datetime.now(timezone.utc).isoformat(),
            "objective_sha256": hashlib.sha256(
                objective.encode("utf-8")).hexdigest(),
            "risk": risk,
            "targets": targets,
            "argv": [step["argv"] for step in normalized],
        })
        return _ok(risk=risk, targets=targets,
                   steps=[step["argv"] for step in normalized])

    results = run_steps(normalized, total_timeout)
    append_audit(audit_path, {
        "event": "execute",
        "ts": datetime.now(timezone.utc).isoformat(),
        "objective_sha256": hashlib.sha256(
            objective.encode("utf-8")).hexdigest(),
        "approval": str(request.get("approval", "unknown")),
        "risk": risk,
        "targets": targets,
        "steps": normalized,
        "results": [{
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "truncated": result["truncated"],
            "stdout_sha256": result["stdout_sha256"],
        } for result in results],
    })
    return _ok(results=results, risk=risk, targets=targets)


if __name__ == "__main__":
    raise SystemExit(main())
