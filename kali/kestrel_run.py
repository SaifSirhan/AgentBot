#!/usr/bin/env python3
"""Kestrel's forced-command policy wrapper for the Kali lab."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# R5: these are policy constants, never environment lookups. sshd AcceptEnv
# must not let the SSH client choose the scope file, the audit destination or
# the confinement root.
DEFAULT_SCOPE = Path("/etc/kestrel/scope.json")
DEFAULT_AUDIT = Path("/var/log/kestrel/audit.jsonl")
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
    # R3: following a redirect leaves the destination we validated.
    "curl": ("-O", "--remote-name", "--remote-name-all", "--output-dir",
             "-x", "--proxy",
             "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
             "--preproxy", "--resolve", "--connect-to", "--unix-socket",
             "--abstract-unix-socket", "--dns-servers", "--dns-interface",
             "--dns-ipv4-addr", "--dns-ipv6-addr",
             "-L", "--location", "--location-trusted"),
    "ffuf": ("-x", "--proxy", "-r", "-input-cmd"),
    "gobuster": ("--proxy", "-r", "--followredirect"),
    "feroxbuster": ("--proxy", "-r", "--redirects"),
    "nikto": ("-useproxy",),
    "nuclei": ("-proxy", "-t", "-templates", "--templates",
               "-workflow", "--workflow", "-code", "-headless",
               "-follow-redirects", "-fr"),
    "whatweb": ("--proxy", "--custom-plugin", "--follow-redirect",
                "-i", "--input-file"),
    "nmap": ("--proxies", "--datadir", "--script-args-file",
             "--script-updatedb", "-iR", "--excludefile", "-D", "--resume"),
    "dig": ("-f",),
    "masscan": ("-iL", "--excludefile", "--resume"),
    # Dynamic engines/providers load native code; s_client can use a proxy.
    "openssl": ("-engine", "-provider", "-provider-path", "-proxy"),
}
DANGEROUS_NSE = frozenset({"exploit", "brute", "dos", "malware", "fuzzer",
                           "external", "broadcast"})
# R4: a category word only selects scripts, it does not vouch for them, so the
# categories that may be requested are fixed and every other script must be
# named individually. DANGEROUS_NSE stays as a second layer.
NSE_SAFE_CATEGORIES = frozenset({"default", "safe", "version", "discovery"})
NSE_ALLOWLIST = frozenset({
    "banner", "http-title", "http-headers", "http-server-header",
    "http-robots.txt", "http-favicon", "ssl-cert",
    "ssl-enum-ciphers", "ssh-hostkey", "ssh2-enum-algos", "ftp-anon",
    "smb-os-discovery", "smb-protocols", "dns-service-discovery", "mysql-info",
})
# The allowlist above is the effective gate; this pattern only rejects
# wildcards, paths and operators up front. A dot must be legal because the
# allowlisted script http-robots.txt carries one.
NSE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
# newtargets appends script-discovered hosts to the scan set, which is the
# same scope escape as -iR/-iL: the walk never validated those targets.
DENIED_SCRIPT_ARGS = frozenset({"newtargets"})
ALLOWED_SUBCOMMANDS = {"openssl": frozenset({"s_client", "x509"})}
TARGET_LIST_FLAGS = {
    "nmap": ("-iL",),
    "nuclei": ("-l",),
    "ffuf": ("-request",),
    "curl": ("-K", "--config"),
}
OUTPUT_ROOT = Path("/tmp/kestrel")
# R2: the only directories a known read flag may open besides OUTPUT_ROOT.
READ_ONLY_DIRS = ("/usr/share/wordlists", "/usr/share/seclists",
                  "/usr/share/nmap", "/usr/share/whatweb",
                  "/etc/ssl/certs")
# R1: flags that consume the next token (or an attached value) as a value
# instead of a target. This is not an allowlist of permitted flags: unknown
# flags stay allowed, but every value they carry must still pass R2/R3, and
# every bare token must still parse as a target (R1).
VALUE_FLAGS = {
    "nmap": ("-p", "-e", "-S", "-s", "-T", "-b", "-g", "--source-port",
             "-PS", "-PA", "-PU", "-PY", "--script", "--script-args",
             "--exclude", "--max-rtt-timeout", "--initial-rtt-timeout",
             "--host-timeout", "--scan-delay", "--max-scan-delay",
             "--version-intensity", "--min-rate", "--max-rate",
             "--max-retries", "--min-parallelism", "--max-parallelism",
             "--min-hostgroup", "--max-hostgroup", "--script-timeout",
             "--ttl", "--data-length", "--ip-options", "--spoof-mac",
             "--dns-servers", "--servicedb", "--versiondb", "--stylesheet",
             "-oN", "-oX", "-oG", "-oA", "-oS"),
    "whatweb": ("--log-brief", "--log-verbose", "--log-errors", "--log-json",
                "--log-xml", "-a", "--agent", "--aggression", "-U",
                "--user-agent", "-u", "--url", "-H", "--header", "-c",
                "--cookies", "--max-threads", "--wait", "--open-timeout",
                "--read-timeout", "--max-redirects"),
    "dig": ("-p", "-t", "-q", "-y", "-b", "-c", "-k"),
    "host": ("-t", "-W", "-a", "-c", "-R"),
    "whois": ("-h", "-p"),
    "curl": ("-H", "--header", "-A", "--user-agent", "-e", "--referer",
             "-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
             "--data-urlencode", "-F", "--form", "--form-string",
             "-X", "--request", "-m", "--max-time", "--connect-timeout",
             "-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar",
             "-b", "--cookie", "--trace", "--trace-ascii", "--stderr",
             "-w", "--write-out", "--cacert", "--cert", "--key", "-E",
             "-u", "--user", "--retry", "--retry-delay", "--retry-max-time",
             "-C", "--continue-at", "-r", "--range", "-y", "--speed-time",
             "-Y", "--speed-limit", "-z", "--time-cond", "-Q", "--quote",
             "-P", "--ftp-port", "-t", "--telnet-option", "-U",
             "--proxy-user", "--url", "--max-filesize", "--limit-rate",
             "--ciphers", "--cert-type", "--key-type", "--pass", "--libcurl",
             "--netrc-file", "--proto", "--proto-default", "--proto-redir",
             "--max-redirs", "--keepalive-time"),
    "openssl": ("-connect", "-in", "-out", "-CAfile", "-cert", "-key",
                "-untrusted", "-sess_out", "-keylogfile", "-writerand",
                "-servername", "-starttls", "-sess_in", "-CApath",
                "-verify", "-verify_hostname", "-verify_ip", "-certform",
                "-keyform", "-inform", "-outform", "-alpn", "-cipher",
                "-ciphersuites", "-sigalgs", "-groups", "-curves",
                "-named_curve", "-keymatexport", "-keymatexportlen",
                "-max_send_frag", "-split_send_frag", "-read_buf",
                "-write_buf", "-signkey", "-CA", "-CAkey", "-CAserial",
                "-extfile", "-extensions", "-days", "-checkend", "-subj",
                "-addtrust", "-addreject", "-alias", "-setalias"),
    "searchsploit": ("-m", "--mirror", "-p", "--path", "--id", "--exclude",
                     "--nmap"),
    "ffuf": ("-w", "--wordlist", "-u", "--url", "-t", "--threads", "-mc",
             "-fc", "-fs", "-fl", "-fw", "-fr", "-o", "--output", "-H",
             "--header", "-X", "--method", "-d", "--data", "-b", "--cookie",
             "-p", "--delay", "-rate", "-timeout", "-maxtime",
             "-maxtime-job", "-recursion-depth", "-of", "--output-format",
             "-od", "--output-dir", "-debug-log", "-request-proto", "-mode",
             "-match", "-filter"),
    "gobuster": ("-w", "--wordlist", "-u", "--url", "-t", "--threads",
                 "-x", "--extensions", "-s", "--statuscodes", "-b",
                 "--statuscodesblacklist", "-o", "--output", "-a",
                 "--useragent", "-c", "--cookies", "-H", "--headers", "-P",
                 "--password", "-U", "--username", "--timeout", "--delay",
                 "-p", "--patternfile", "-m", "--mode", "--method"),
    "feroxbuster": ("-w", "--wordlist", "-u", "--url", "-t", "--threads",
                    "-x", "--extensions", "-s", "--statuscodes", "-o",
                    "--output", "-H", "--header", "-b", "--cookies", "-a",
                    "--useragent", "-T", "--timeout", "--rate-limit",
                    "-d", "--depth", "-L", "--scan-limit", "--debug-log"),
    "nuclei": ("-u", "--url", "-o", "--output", "-severity", "-tags", "-rl",
               "--rate-limit", "-H", "--header", "-etags", "-retries",
               "-timeout", "-stats-interval", "-si", "-mhe",
               "-max-host-error", "-eh", "-exclude-hosts", "-debug-log",
               "-output-format", "-of"),
    "nikto": ("-h", "-host", "-p", "-port", "-o", "-output", "--output",
              "-id", "-Tuning", "-Format", "-vhost", "-root", "-Cgidirs",
              "-mutate", "-evasion", "-timeout", "-Pause", "-Display"),
    "masscan": ("-p", "--ports", "--range", "--rate", "-oX", "-oJ", "-oG",
                "-oL", "--adapter", "--adapter-ip", "--adapter-port",
                "--router-ip", "--source-ip", "--source-port", "--retries",
                "--timeout", "--wait", "--exclude", "-e", "--interface"),
    "nc": ("-p", "-w", "-s", "-q", "-i", "-G", "-T", "-X", "-x", "-P"),
}
# R2: read flags may also open the packaged wordlists/scripts/CA bundle.
READ_FLAGS = {
    "ffuf": ("-w", "--wordlist"),
    "gobuster": ("-w", "--wordlist"),
    "feroxbuster": ("-w", "--wordlist"),
    "openssl": ("-in", "-CAfile", "-cert", "-key", "-untrusted"),
}
# Values of these flags are always filesystem paths, never targets: they are
# confined to OUTPUT_ROOT (plus READ_ONLY_DIRS for READ_FLAGS) instead of
# being scope-checked, so `-oN scan.txt` stays a usable relative output.
PATH_FLAGS = {
    "nmap": ("-oN", "-oX", "-oG", "-oA", "-oS", "--servicedb", "--versiondb",
             "--stylesheet"),
    "whatweb": ("--log-brief", "--log-verbose", "--log-errors", "--log-json",
                "--log-xml"),
    "dig": ("-k",),
    "curl": ("-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar",
             "-b", "--cookie", "--trace", "--trace-ascii", "--stderr",
             "--libcurl", "--cacert", "--cert", "--key", "-E",
             "--netrc-file", "-z", "--time-cond"),
    "openssl": ("-in", "-out", "-CAfile", "-cert", "-key", "-untrusted",
                "-sess_out", "-sess_in", "-CApath", "-keylogfile",
                "-writerand", "-signkey", "-CA", "-CAkey", "-CAserial",
                "-extfile"),
    "searchsploit": ("--nmap",),
    "ffuf": ("-w", "--wordlist", "-o", "--output", "-od", "--output-dir",
             "-debug-log"),
    "gobuster": ("-w", "--wordlist", "-o", "--output", "-p", "--patternfile"),
    "feroxbuster": ("-w", "--wordlist", "-o", "--output", "--debug-log"),
    "nuclei": ("-o", "--output", "-debug-log"),
    "nikto": ("-o", "-output", "--output"),
    "masscan": ("-oX", "-oJ", "-oG", "-oL"),
}
# R4: these values are script names / script arguments, so they are not
# parsed as targets; classify checks the names and target_tokens checks the
# argument pieces (which may still be paths or destinations).
NAME_FLAGS = {"nmap": ("--script", "--script-args")}
# Binaries that really do bundle short flags (getopt style). The Go/Perl/Ruby
# scanners take single-dash flags atomically, so `-mc` is one flag, not `-m -c`.
BUNDLE_FLAGS = frozenset({"curl", "nmap", "nc"})
# Flags that take no value; listed only so an attached-value prefix match
# cannot mistake a longer flag name for a denied short one (ffuf -recursion
# is not -r, nuclei -tl is not -t).
NO_VALUE_FLAGS = {
    "ffuf": ("-recursion",),
    "nuclei": ("-tl",),
}
# Positional arguments that are not targets: searchsploit takes search terms,
# gobuster takes a mode word, nc takes a port number after the host.
FREE_POSITIONALS = frozenset({"searchsploit"})
SUBCOMMAND_WORDS = {
    "gobuster": frozenset({"dir", "dns", "fuzz", "vhost", "s3", "tftp"}),
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
    """Parse one token as a target, or return None when it is not one.

    R1/R3: only http/https URLs are destinations, and a single trailing dot
    on a hostname is normalized before matching so `example.com.` is refused
    as public instead of being dropped.
    """
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
            parts = urllib.parse.urlsplit(raw)
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https"):
            return None
        raw = parts.hostname or ""
        if not raw:
            return None
    if raw.endswith("."):
        raw = raw[:-1]
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
        return ("domain", raw.lower())
    return None


def _flag_names(binary):
    """Every flag name we know for this binary: value, denied or boolean."""
    names = set(VALUE_FLAGS.get(binary, ()))
    for table in (DENIED_FLAGS, DESTRUCTIVE_FLAGS, TARGET_LIST_FLAGS,
                  NO_VALUE_FLAGS):
        names.update(table.get(binary, ()))
    return names


def _short_flags(binary):
    return sorted(
        (name for name in _flag_names(binary)
         if name.startswith("-") and not name.startswith("--")),
        key=len, reverse=True)


def _match_short(body, shorts):
    """Longest known short flag whose name prefixes body (longest first)."""
    for name in shorts:
        if body.startswith(name[1:]):
            return name
    return None


def _unknown_item(tokens, index, name):
    """R1: an unknown flag is allowed, but its value is still accounted for.

    The next token is treated as the flag's value when it cannot be another
    flag, so `--some-new-write-flag /etc/shadow` is confined exactly like a
    known one. With no value there is nothing to check.
    """
    if index < len(tokens) and not tokens[index].startswith("-"):
        return ("value", name, tokens[index]), index + 1
    return ("flag", name, None), index


def _iter_items(argv):
    """Normalize argv into (kind, name, value) items.

    kind is "bare" for a positional token, "flag" for a flag that takes no
    value, and "value" for a flag plus the value it consumes. R1: bundled
    short clusters are expanded for the getopt-style binaries, `--flag=value`
    is split on the first `=`, and a value-taking flag inside a cluster takes
    the rest of the cluster (curl `-so FILE` == `-s -o FILE`). An unknown flag
    still consumes the token after it, so its value cannot hide from R2/R3.
    """
    binary = argv[0]
    tokens = argv[1:]
    if binary == "openssl":
        tokens = tokens[1:]        # the subcommand, checked by classify
    values = set(VALUE_FLAGS.get(binary, ()))
    known = _flag_names(binary)
    shorts = _short_flags(binary)
    bundle = binary in BUNDLE_FLAGS
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-") or token == "-":
            yield ("bare", token, None)
            continue
        if token.startswith("--"):
            if "=" in token:
                name, value = token.split("=", 1)
                yield ("value", name, value)
            elif token in values:
                value = tokens[index] if index < len(tokens) else ""
                index += 1
                yield ("value", token, value)
            elif token in known:
                yield ("flag", token, None)
            else:
                item, index = _unknown_item(tokens, index, token)
                yield item
            continue
        if bundle:
            body = token[1:]
            while body:
                name = _match_short(body, shorts)
                if name is None:
                    if len(body) == 1:
                        item, index = _unknown_item(tokens, index, "-" + body)
                        yield item
                        body = ""
                    else:
                        yield ("flag", "-" + body[0], None)
                        body = body[1:]
                    continue
                body = body[len(name) - 1:]
                if name in values:
                    if body:
                        value, body = body, ""
                    else:
                        value = tokens[index] if index < len(tokens) else ""
                        index += 1
                    yield ("value", name, value)
                else:
                    yield ("flag", name, None)
            continue
        # Atomic single-dash flags: the Go/Perl/Ruby scanners and openssl use
        # names like -mc or -connect, so they are never split into letters.
        if token in known:
            if token in values:
                value = tokens[index] if index < len(tokens) else ""
                index += 1
                yield ("value", token, value)
            else:
                yield ("flag", token, None)
            continue
        if "=" in token:
            name, value = token.split("=", 1)
            yield ("value", name, value)
            continue
        name = _match_short(token[1:], shorts)
        if name is not None:
            if name in values:
                yield ("value", name, token[len(name):])
            else:
                yield ("flag", name, None)
            continue
        item, index = _unknown_item(tokens, index, token)
        yield item


def _is_path_like(value):
    return (value.startswith(("/", "./", "../", "~"))
            or "/" in value or "\\" in value)


def _path_roots(binary, flag):
    if flag in READ_FLAGS.get(binary, ()):
        return (OUTPUT_ROOT,) + READ_ONLY_DIRS
    return (OUTPUT_ROOT,)


def _confine_path(value, roots):
    """R2: resolve symlinks and relative paths, then require containment.

    The root itself is not a writable destination: a path that resolves to
    OUTPUT_ROOT (or a read-only root) exactly is refused, not just paths
    that escape it.
    """
    if not value:
        raise PolicyError("a path value is required")
    if "://" in value:
        # R3: a URL is not a filesystem path. Resolving it as a relative
        # path would keep it "under" the root and hand curl a file://
        # destination, so refuse it here instead.
        raise PolicyError("path %r is a URL, not a filesystem path" % value)
    try:
        candidate = Path(os.path.expanduser(value))
        if not candidate.is_absolute():
            candidate = Path(OUTPUT_ROOT) / candidate
        resolved = candidate.resolve()
        allowed = [Path(root).resolve() for root in roots]
    except (OSError, RuntimeError, ValueError) as exc:
        raise PolicyError("path %r cannot be resolved" % value) from exc
    if not any(root in resolved.parents for root in allowed):
        raise PolicyError(
            "path %r must stay under %s"
            % (value, " or ".join(str(root) for root in roots)))


def _check_nse_scripts(value):
    """R4: only the fixed safe categories and the named scripts may run.

    `*`, wildcards, paths and expressions are refused by the name pattern;
    anything else must be on the allowlist, so `ssh-brute` can no longer ride
    in under the `brute` category word.
    """
    if not value:
        raise PolicyError("--script requires a script name")
    for part in value.split(","):
        if not NSE_NAME_RE.fullmatch(part):
            raise PolicyError(
                "only comma-separated built-in NSE names are allowed")
        name = part.lower()
        if name in DANGEROUS_NSE:
            raise PolicyError(
                "exploitation, brute, dos, and malware NSE scripts "
                "are not allowed")
        if name not in NSE_SAFE_CATEGORIES and name not in NSE_ALLOWLIST:
            raise PolicyError(
                "NSE script %r is not on the policy allowlist" % part)


def _check_script_args(value, scope):
    """R4: split on `,` then `=`, and account for every piece.

    A piece that names a destination is scope-checked and a path-like piece
    is confined to OUTPUT_ROOT; the read-only wordlist directories are not
    granted here. Argument names are identifiers, never destinations.
    """
    if not value:
        raise PolicyError("--script-args requires a value")
    for piece in value.split(","):
        name, separator, argument = piece.partition("=")
        name = name.strip()
        if not name:
            raise PolicyError("--script-args needs named arguments")
        lowered = name.lower()
        if (lowered in DENIED_SCRIPT_ARGS
                or lowered.endswith(".newtargets")):
            raise PolicyError(
                "script argument %r would add unvalidated targets" % name)
        if _is_path_like(name):
            _confine_path(name, (OUTPUT_ROOT,))
        if not separator:
            continue
        target = _target_from_token(argument)
        if target is not None:
            _validate_target(target[0], target[1], scope)
        elif _is_path_like(argument):
            _confine_path(argument, (OUTPUT_ROOT,))


def target_tokens(argv, scope):
    """R1: account for every token, and return the validated targets.

    Nothing is dropped silently any more. A bare token must parse as a
    target; a flag value must either be a confined path or a target that is
    itself in scope. Anything else is a refusal.
    """
    binary = argv[0]
    path_flags = (set(PATH_FLAGS.get(binary, ()))
                  | set(READ_FLAGS.get(binary, ())))
    name_flags = set(NAME_FLAGS.get(binary, ()))
    targets = []
    for kind, token, value in _iter_items(argv):
        if kind == "flag":
            continue
        if kind == "bare":
            if binary in FREE_POSITIONALS:
                continue           # searchsploit takes search terms
            if token in SUBCOMMAND_WORDS.get(binary, ()):
                continue           # gobuster dir/dns/fuzz/vhost/s3/tftp
            if binary == "nc" and token.isdigit():
                continue           # the port after the host
            target = _target_from_token(token)
            if target is None:
                raise PolicyError(
                    "token %r is not a target the policy can validate" % token)
            targets.append(_validate_target(target[0], target[1], scope))
            continue
        if token in name_flags:
            if token == "--script-args":
                _check_script_args(value, scope)
            continue               # classify enforces R4 on --script names
        if not value:
            raise PolicyError("flag %s requires a value" % token)
        if token in path_flags:
            _confine_path(value, _path_roots(binary, token))
            continue
        target = _target_from_token(value)
        if target is not None:
            targets.append(_validate_target(target[0], target[1], scope))
        elif _is_path_like(value):
            _confine_path(value, (OUTPUT_ROOT,))
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


def _check_control_characters(argv):
    """R6: refuse shell metacharacters and every C0 control byte (incl. NUL).

    Popen raises ValueError on an embedded null, so no argv element may carry
    one; the remaining C0 bytes are terminal/line-editing escapes.
    """
    for arg in argv:
        if any(character in SHELL_CHARS or ord(character) < 0x20
               for character in arg):
            raise PolicyError("shell control characters are not allowed")


def _check_output_root():
    """R7: the confinement root must not be squattable via /tmp.

    A symlinked root would redirect every confined write elsewhere, and a
    root owned by another uid (or group/world accessible) lets another local
    account read or replace the lab's output.
    """
    root = os.fspath(OUTPUT_ROOT)
    if not os.path.lexists(root):
        return
    if os.path.islink(root):
        raise PolicyError("output root %s must not be a symlink" % root)
    if os.name == "posix":
        info = os.stat(root)
        if info.st_uid != os.getuid():
            raise PolicyError("output root %s is not owned by this uid" % root)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise PolicyError("output root %s must be mode 0700" % root)


def classify(argv):
    _check_control_characters(argv)
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

    # R1: flag decisions are made on the expanded walk, so a denied flag
    # hidden in a short cluster (`-sO`, `-vL`) or behind `=` is still seen.
    items = list(_iter_items(argv))
    flags = {name for kind, name, _ in items if kind != "bare"}
    for flag in DENIED_FLAGS.get(binary, ()):
        if flag in flags:
            raise PolicyError("flag %s is not allowed" % flag)
    for flag in DESTRUCTIVE_FLAGS.get(binary, ()):
        if flag in flags:
            raise PolicyError("flag %s is not allowed" % flag)
    for flag in TARGET_LIST_FLAGS.get(binary, ()):
        if flag in flags:
            raise PolicyError(
                "flag %s is not allowed; write targets directly" % flag)

    if binary == "curl":
        if any(name in ("-d", "--data", "--data-raw", "--data-binary",
                        "--data-ascii", "--data-urlencode", "-F", "--form",
                        "--form-string", "-T", "--upload-file")
               for kind, name, _ in items if kind != "bare"):
            tier = "intrusive"
        for kind, name, value in items:
            if kind == "value" and name in ("-X", "--request"):
                if value.upper() not in ("GET", "HEAD", ""):
                    tier = "intrusive"
    if binary == "nmap":
        # R4 on the expanded walk, so `--script=*` is seen too. The
        # --script-args pieces are checked in target_tokens, which has scope.
        for kind, name, value in items:
            if kind == "value" and name == "--script":
                _check_nse_scripts(value)
                tier = "intrusive"
    return tier


def validate_steps(steps, scope):
    _check_output_root()
    highest = "recon"
    targets = []
    for step in steps:
        argv = step["argv"]
        tier = classify(argv)
        found = target_tokens(argv, scope)
        needs_target = (argv[0] not in LOCAL_SAFE
                        or (argv[0] == "openssl"
                            and len(argv) > 1
                            and argv[1] == "s_client"))
        if not found and needs_target:
            raise PolicyError("%s requires a target" % argv[0])
        targets.extend(found)
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
    _check_output_root()
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
