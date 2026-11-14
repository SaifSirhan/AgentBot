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
# I5/I6: pipes are read as bytes, in chunks, and capped in bytes.
READ_CHUNK_BYTES = 8192
# I7: the post-kill wait is bounded so a stuck child cannot hang the wrapper.
KILL_WAIT_SECONDS = 5
READER_JOIN_SECONDS = 2

RECON = frozenset({"nmap", "whatweb", "dig", "host", "whois", "curl",
                   "openssl", "searchsploit", "ping", "traceroute",
                   "arp-scan", "httpx"})
INTRUSIVE = frozenset({"ffuf", "gobuster", "feroxbuster", "nuclei",
                       "nikto", "masscan", "nc", "dirsearch", "wfuzz",
                       "wpscan", "sslyze"})
LOCAL_SAFE = frozenset({"searchsploit", "openssl"})
# Credential-tier work is the one place where a successful step means someone
# else's authentication failed, so it is also the tier that asks you twice.
CREDENTIAL = frozenset({"hydra", "sqlmap"})
DENIED_BINARIES = frozenset({"bash", "sh", "zsh", "dash", "python3", "python",
                             "perl", "ruby", "rm", "dd", "mkfs", "mount",
                             "systemctl", "crontab"})
TIERS = {"recon": 0, "intrusive": 1, "credential": 2, "exploitation": 3}
DESTRUCTIVE_FLAGS = {
    "sqlmap": ("--os-shell", "--os-pwn", "--os-cmd", "--file-write",
               "--file-dest", "--sql-shell",
               # A tamper script and a preprocessor are both Python imported and
               # executed by sqlmap itself: an exec primitive wearing a scanning
               # flag, and the reason the strict tables are built the way they
               # are. Reading a local file is the same class of door.
               "--tamper", "--preprocessor", "--file-read", "--load-file",
               "--sql-file", "--sql-query"),
    "curl": ("-T", "--upload-file"),
    "nc": ("-e", "--exec", "-c"),
}
DENIED_FLAGS = {
    # Flood and spoof primitives: ping -f/-l change the rate, -s changes the
    # source, and traceroute -g asks someone else's router to route for us.
    # arp-scan --localnet asks the interface which segment to sweep, so the
    # scope file never sees the answer.
    "ping": ("-f", "-l", "-L", "-s", "--file"),
    "traceroute": ("-g", "-s", "-F", "--address"),
    "arp-scan": ("-l", "--localnet", "-b", "--BRIDGE", "-O"),
    "httpx": ("-proxy", "--proxy", "-listener", "--listener", "-resx",
              "--version-path"),
    # `-z` is wfuzz's scanner spec: `file,/etc/shadow` reads a path through a
    # value the walker cannot separate from the scanner name. `-w` does the
    # same job confined. `--pickle`/`-R` resume a saved session past approval,
    # and `-p` is a relay.
    "wfuzz": ("-z", "-p", "--proxy", "--pickle", "-R", "--resume",
              "--file", "--csv"),
    "wpscan": ("--proxy", "--url-file"),
    "dirsearch": ("-p", "--proxy", "--save-config"),
    # A proxy or a TOR circuit is a relay: the destination the scope checked is
    # not the destination that gets dialed. And a search engine as a target
    # source means Google chose the targets, not the operator.
    "sqlmap": ("--proxy", "--proxy-file", "--ignore-proxy", "--tor",
               "--tor-type", "--check-tor", "--google", "--bing", "-g",
               "--config-file", "--direct"),
    # curl proxies and remote-name writes are exfiltration or write paths.
    # R3: following a redirect leaves the destination we validated.
    # R9: protocol control (--proto*), FTP quote/port and telnet options are
    # relay primitives, and a redirect budget re-enables what R3 refuses.
    "curl": ("-O", "--remote-name", "--remote-name-all", "--output-dir",
             "-x", "--proxy",
             "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
             "--preproxy", "--resolve", "--connect-to", "--unix-socket",
             "--abstract-unix-socket", "--dns-servers", "--dns-interface",
             "--dns-ipv4-addr", "--dns-ipv6-addr",
             "-L", "--location", "--location-trusted",
             "--proto", "--proto-default", "--proto-redir", "--max-redirs",
             "-Q", "--quote", "-P", "--ftp-port", "-t", "--telnet-option",
             "-U", "--proxy-user"),
    "ffuf": ("-x", "--proxy", "-r", "-input-cmd"),
    "gobuster": ("--proxy", "-r", "--followredirect"),
    "feroxbuster": ("--proxy", "-r", "--redirects"),
    "nikto": ("-useproxy",),
    "nuclei": ("-proxy", "-t", "-templates", "--templates",
               "-workflow", "--workflow", "-code", "-headless",
               "-follow-redirects", "-fr"),
    "whatweb": ("--proxy", "--custom-plugin", "--follow-redirect",
                "-i", "--input-file", "--max-redirects"),
    # R9: -b is an FTP bounce relay, --dns-servers picks out-of-scope
    # resolvers, -S spoofs the source address and --ip-options asks for
    # loose source routing. None has a lab need.
    "nmap": ("--proxies", "--datadir", "--script-args-file",
             "--script-updatedb", "-iR", "--excludefile", "-D", "--resume",
             "-b", "--dns-servers", "-S", "--ip-options"),
    "dig": ("-f", "-b"),
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
    "hydra": ("-M",),
    "sqlmap": ("-m",),
    "httpx": ("-l", "--list"),
    "dirsearch": ("-l", "--list"),
    "sslyze": ("--targets_in",),
}
OUTPUT_ROOT = Path("/tmp/kestrel")
# R2: the only directories a known read flag may open besides OUTPUT_ROOT.
READ_ONLY_DIRS = ("/usr/share/wordlists", "/usr/share/seclists",
                  "/usr/share/nmap", "/usr/share/whatweb",
                  "/etc/ssl/certs")
# R1: flags that consume the next token (or an attached value) as a value
# instead of a target. This is not an allowlist of permitted flags: unknown
# flags stay allowed, but every value they carry must still pass R2/R3/R8, and
# every bare token must still parse as a target (R1).
# R8: a value flag is only half a licence. Its value must still end as a
# validated target, a confined path (PATH_FLAGS) or a class-licensed
# non-destination (NON_DESTINATION_VALUES); R9 removed the destination-bearing
# flags that used to be licensed here.
VALUE_FLAGS = {
    "nmap": ("-p", "-e", "-s", "-T", "-g", "--source-port",
             "-PS", "-PA", "-PU", "-PY", "--script", "--script-args",
             "--exclude", "--max-rtt-timeout", "--initial-rtt-timeout",
             "--host-timeout", "--scan-delay", "--max-scan-delay",
             "--version-intensity", "--min-rate", "--max-rate",
             "--max-retries", "--min-parallelism", "--max-parallelism",
             "--min-hostgroup", "--max-hostgroup", "--script-timeout",
             "--ttl", "--data-length", "--spoof-mac",
             "--servicedb", "--versiondb", "--stylesheet",
             "-oN", "-oX", "-oG", "-oA", "-oS"),
    "whatweb": ("--log-brief", "--log-verbose", "--log-errors", "--log-json",
                "--log-xml", "-a", "--agent", "--aggression", "-U",
                "--user-agent", "-u", "--url", "-H", "--header", "-c",
                "--cookies", "--max-threads", "--wait", "--open-timeout",
                "--read-timeout"),
    "dig": ("-p", "-t", "-q", "-y", "-c", "-k"),
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
             "-Y", "--speed-limit", "-z", "--time-cond",
             "--url", "--max-filesize", "--limit-rate",
             "--ciphers", "--cert-type", "--key-type", "--pass", "--libcurl",
             "--netrc-file", "--keepalive-time"),
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
    "searchsploit": ("-t", "-m", "--mirror", "-p", "--path", "--id",
                     "--exclude", "--nmap"),
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
    # R11: -output is a whole single-dash flag, not -o with an attached
    # "utput"; it must be known here so the atomic match wins over -o.
    "nuclei": ("-u", "--url", "-o", "-output", "--output", "-severity",
               "-tags", "-rl", "--rate-limit", "-H", "--header", "-etags",
               "-retries", "-timeout", "-stats-interval", "-si", "-mhe",
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
    # hydra's short `-x` charset spec is left undeclared on purpose: it is
    # written as `1-6:$/#`, and `$` is refused in every argv token.
    "hydra": ("-l", "-p", "-e", "-s", "-m", "-t", "-c", "-w", "-D",
              "-L", "-P", "-C", "-o"),
    # ping's -L is preload (a flood primitive), and traceroute's -s is a spoofed
    # source: neither is declared, both are refused by name below.
    "ping": ("-c", "-i", "-t", "-W", "-w"),
    "traceroute": ("-m", "-w", "-q", "-p", "-t", "-P", "-z"),
    "arp-scan": ("-t", "-r"),
    # A destination flag still has to be declared as a value flag: the permit
    # list is built from VALUE_FLAGS/BOOLEAN_FLAGS, and DESTINATION_FLAGS only
    # says how the value is validated.
    "httpx": ("-u", "--url", "-o", "-timeout", "-threads", "-rps",
              "-retries"),
    "dirsearch": ("-u", "--url", "-e", "--extensions", "-t", "--threads",
                  "--timeout", "-w", "-i", "-x", "--wordlist", "-o",
                  "--output"),
    "wfuzz": ("-u", "--url", "-w", "--wordlist", "-t", "--timeout",
              "--repeat", "--hc", "--sc", "--hf", "--sf", "--output-file",
              "-o", "--headers", "--user-agent"),
    "wpscan": ("--url", "-e", "--enumerate", "--request-timeout", "--threads",
               "--output", "--format", "--wordlist", "--max-id"),
    "sslyze": ("--retries", "--json_out", "--conn_retries", "--timeout"),
    "sqlmap": ("-u", "--url", "--data", "--data-ascii", "--data-binary",
               "--data-urlencode", "--method", "--cookie", "--user-agent",
               "--auth", "--auth-type", "--headers", "-p", "--dbms",
               "--exclude-sysdbs", "--level", "--risk", "--threads",
               "--timeout", "--delay", "--retries", "--technique",
               "--string", "--not-string", "--regexp", "--code",
               "--text-only", "--prefix", "--suffix", "--answers",
               "--crawl", "--crawl-timeout", "--tables", "--columns",
               "--schemas", "-v", "--error-limit", "--tcp-timeout"),
}
# R2: read flags may also open the packaged wordlists/scripts/CA bundle.
READ_FLAGS = {
    "ffuf": ("-w", "--wordlist"),
    "dirsearch": ("--wordlist",),
    "wfuzz": ("-w", "--wordlist"),
    "wpscan": ("--wordlist",),
    "gobuster": ("-w", "--wordlist"),
    "feroxbuster": ("-w", "--wordlist"),
    "openssl": ("-in", "-CAfile", "-cert", "-key", "-untrusted"),
    "hydra": ("-L", "-P", "-C"),
    "sqlmap": ("--password-file",),
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
    "nuclei": ("-o", "-output", "--output", "-debug-log"),
    "nikto": ("-o", "-output", "--output"),
    "masscan": ("-oX", "-oJ", "-oG", "-oL"),
    "hydra": ("-o",),
    "httpx": ("-o",),
    "dirsearch": ("-o", "--output"),
    "wfuzz": ("--output-file", "-o"),
    "wpscan": ("--output", "--log"),
    "sslyze": ("--json_out",),
    "sqlmap": ("--output-dir",),
}
# R10: these flags carry the destination itself. Their values must always
# parse as a target and be scope-checked; they can never be licensed as a
# non-destination and never fall through to the path rules.
DESTINATION_FLAGS = {
    "curl": ("--url",),
    "whatweb": ("-u", "--url"),
    "nuclei": ("-u",),
    "ffuf": ("-u",),
    "dig": ("-q",),
    "masscan": ("--range",),
    "sqlmap": ("-u", "--url"),
    "httpx": ("-u", "--url"),
    "dirsearch": ("-u", "--url"),
    "wfuzz": ("-u", "--url"),
    "wpscan": ("--url",),
}
# R4: these values are script names / script arguments, so they are not
# parsed as targets; classify checks the names and target_tokens checks the
# argument pieces (which may still be paths or destinations).
NAME_FLAGS = {"nmap": ("--script", "--script-args")}
# R8: the only licence a non-destination value gets. Every entry names a value
# class from VALUE_CLASSES, so a flag cannot license "anything". A flag that is
# neither here, nor in PATH_FLAGS/READ_FLAGS, nor in DESTINATION_FLAGS has no
# licence at all: its value must parse as a target or be a confined path.
NON_DESTINATION_VALUES = {
    "nmap": {
        "-p": "ports", "-g": "ports", "--source-port": "ports",
        "-PS": "ports", "-PA": "ports", "-PU": "ports", "-PY": "ports",
        "-s": "word", "-e": "word", "-T": "number",
        "--max-rtt-timeout": "number", "--initial-rtt-timeout": "number",
        "--host-timeout": "number", "--scan-delay": "number",
        "--max-scan-delay": "number", "--script-timeout": "number",
        "--version-intensity": "number", "--min-rate": "number",
        "--max-rate": "number", "--max-retries": "number",
        "--min-parallelism": "number", "--max-parallelism": "number",
        "--min-hostgroup": "number", "--max-hostgroup": "number",
        "--ttl": "number", "--data-length": "number",
    },
    "whatweb": {
        "-a": "number", "--aggression": "number", "--max-threads": "number",
        "--wait": "number", "--open-timeout": "number",
        "--read-timeout": "number", "-U": "text", "--user-agent": "text",
        "--agent": "text", "-H": "text", "--header": "text",
        "-c": "text", "--cookies": "text",
    },
    "dig": {"-p": "ports", "-t": "record", "-c": "word", "-y": "text"},
    "host": {"-t": "record", "-c": "word", "-W": "number", "-R": "number",
             "-a": "word"},
    "whois": {"-p": "ports"},
    "curl": {
        "-H": "text", "--header": "text", "-A": "text", "--user-agent": "text",
        "-d": "text", "--data": "text", "--data-raw": "text",
        "--data-binary": "text", "--data-ascii": "text",
        "--data-urlencode": "text", "-F": "text", "--form": "text",
        "--form-string": "text", "-X": "method", "--request": "method",
        "-m": "number", "--max-time": "number",
        "--connect-timeout": "number", "-w": "text", "--write-out": "text",
        "-u": "credential", "--user": "credential", "--retry": "number",
        "--retry-delay": "number", "--retry-max-time": "number",
        "-C": "number", "--continue-at": "number", "-r": "range",
        "--range": "range", "-y": "number", "--speed-time": "number",
        "-Y": "number", "--speed-limit": "number",
        "--max-filesize": "number", "--limit-rate": "number",
        "--ciphers": "text", "--cert-type": "word", "--key-type": "word",
        "--pass": "text", "--keepalive-time": "number",
    },
    "openssl": {
        "-starttls": "word", "-verify": "number", "-certform": "word",
        "-keyform": "word", "-inform": "word", "-outform": "word",
        "-alpn": "text", "-cipher": "text", "-ciphersuites": "text",
        "-sigalgs": "text", "-groups": "text", "-curves": "text",
        "-named_curve": "text", "-keymatexport": "text",
        "-keymatexportlen": "number", "-max_send_frag": "number",
        "-split_send_frag": "number", "-read_buf": "number",
        "-write_buf": "number", "-days": "number", "-checkend": "number",
        "-subj": "text", "-addtrust": "word", "-addreject": "word",
        "-alias": "text", "-setalias": "text", "-extensions": "word",
    },
    "searchsploit": {"-t": "text", "--exclude": "text", "--id": "number",
                     "-m": "number", "--mirror": "number"},
    "ffuf": {
        "-t": "number", "--threads": "number", "-mc": "number",
        "-fc": "number", "-fs": "number", "-fl": "number", "-fw": "number",
        "-fr": "text", "-H": "text", "--header": "text", "-X": "method",
        "--method": "method", "-d": "text", "--data": "text",
        "-b": "text", "--cookie": "text", "-p": "number", "--delay": "number",
        "-rate": "number", "-timeout": "number", "-maxtime": "number",
        "-maxtime-job": "number", "-recursion-depth": "number",
        "-of": "word", "--output-format": "word",
        "-request-proto": "word", "-mode": "word", "-match": "word",
        "-filter": "word",
    },
    "gobuster": {
        "-t": "number", "--threads": "number", "-x": "text",
        "--extensions": "text", "-s": "text", "--statuscodes": "text",
        "-b": "text", "--statuscodesblacklist": "text", "-a": "text",
        "--useragent": "text", "-c": "text", "--cookies": "text",
        "-H": "text", "--headers": "text", "-P": "text", "--password": "text",
        "-U": "text", "--username": "text", "--timeout": "number",
        "--delay": "number", "-m": "word", "--mode": "word",
        "--method": "method",
    },
    "feroxbuster": {
        "-t": "number", "--threads": "number", "-x": "text",
        "--extensions": "text", "-s": "text", "--statuscodes": "text",
        "-H": "text", "--header": "text", "-b": "text", "--cookies": "text",
        "-a": "text", "--useragent": "text", "-T": "number",
        "--timeout": "number", "--rate-limit": "number", "-d": "number",
        "--depth": "number", "-L": "number", "--scan-limit": "number",
    },
    "nuclei": {
        "-severity": "text", "-tags": "text", "-etags": "text",
        "-rl": "number", "--rate-limit": "number", "-H": "text",
        "--header": "text", "-retries": "number", "-timeout": "number",
        "-stats-interval": "number", "-si": "number", "-mhe": "number",
        "-max-host-error": "number", "-output-format": "word", "-of": "word",
    },
    "nikto": {
        "-p": "ports", "-port": "ports", "-id": "text", "-Tuning": "text",
        "-Format": "word", "-Cgidirs": "text", "-mutate": "number",
        "-evasion": "number", "-timeout": "number", "-Pause": "number",
        "-Display": "word",
    },
    "masscan": {
        "-p": "ports", "--ports": "ports", "--rate": "number",
        "--adapter": "word", "--adapter-port": "ports",
        "--source-port": "ports", "--retries": "number",
        "--timeout": "number", "--wait": "number", "-e": "word",
        "--interface": "word",
    },
    "nc": {"-p": "ports", "-w": "number", "-q": "number", "-i": "number",
           "-G": "number", "-T": "word", "-X": "word", "-P": "text"},
    "ping": {"-c": "number", "-i": "number", "-t": "number",
             "-W": "number", "-w": "number"},
    "traceroute": {"-m": "number", "-w": "number", "-q": "number",
                   "-p": "ports", "-t": "number", "-P": "word",
                   "-z": "number"},
    "arp-scan": {"-t": "number", "-r": "number"},
    "httpx": {"-timeout": "number", "-threads": "number",
              "-rps": "number", "-retries": "number"},
    "dirsearch": {"-t": "number", "--threads": "number",
                  "--timeout": "number", "-e": "wordset",
                  "--extensions": "wordset", "-w": "wordset",
                  "-i": "wordset", "-x": "wordset"},
    "wfuzz": {"-t": "number", "--timeout": "number", "--repeat": "number",
              "--hc": "wordset", "--sc": "wordset", "--hf": "text",
              "--sf": "text", "--headers": "text", "--user-agent": "text"},
    "wpscan": {"-e": "wordset", "--enumerate": "wordset",
               "--request-timeout": "number", "--threads": "number",
               "--format": "word", "--max-id": "number"},
    "sslyze": {"--retries": "number", "--conn_retries": "number",
               "--timeout": "number"},
    # hydra: `-e` is the empty/salt guess spec (ns, n or s), so a word and never
    # a path; `-s` is a port; the rest are counts. Credentials use the existing
    # class, which already refuses `/` and `@`.
    "hydra": {"-e": "word", "-s": "ports", "-m": "number", "-t": "number",
              "-c": "number", "-w": "number", "-D": "number",
              "-l": "credential", "-p": "credential"},
    # sqlmap: the request-interpolated strings get the same licence curl's
    # `--data` already carries. Nothing here may name a host: `-u` is a
    # destination, and `--dbms`/`--technique` are words.
    "sqlmap": {"--level": "number", "--risk": "number",
               "--threads": "number", "--timeout": "number",
               "--delay": "number", "--retries": "number",
               "--error-limit": "number", "--tcp-timeout": "number",
               "--crawl": "number", "--crawl-timeout": "number",
               "-v": "number", "--code": "number", "--dbms": "word",
               "--technique": "word", "--auth-type": "word",
               "--method": "method", "-p": "word",
               "--exclude-sysdbs": "word", "--tables": "word",
               "--columns": "word", "--schemas": "word", "--string": "text",
               "--not-string": "text", "--regexp": "text",
               "--text-only": "text", "--prefix": "text", "--suffix": "text",
               "--answers": "text", "--headers": "text", "--cookie": "text",
               "--user-agent": "text", "--data": "text",
               "--data-ascii": "text", "--data-binary": "text",
               "--data-urlencode": "text", "--auth": "credential"},
}
# R11: the record types dig/host accept as a positional argument. AXFR/IXFR
# are zone transfers, not lookups, so they are deliberately absent.
RECORD_TYPES = frozenset({"A", "AAAA", "MX", "TXT", "NS", "SOA", "CNAME",
                          "PTR", "SRV", "ANY", "CAA"})
RECORD_POSITIONALS = frozenset({"dig", "host"})
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH",
                          "OPTIONS", "TRACE"})
# R8: value classes. Every class except "text" has a pattern that cannot match
# a host, an IP, a URL or a userinfo form (no dot, no "@", and ":" only as a
# port-spec protocol prefix), so a structured match may be accepted before the
# host-shaped rule -- that is what keeps `nmap -p T:80` usable. "text" can
# contain anything, so a text-licensed value is still host-shaped-checked.
# R14 adds two licences that are decided BEFORE target parsing, because the
# host-shaped rule would otherwise refuse legitimate values: a credential pair
# (no "@", no "/", no URL scheme) and a decimal number for a numeric flag
# (digits with at most one dot, which a four-octet IP can never match).
VALUE_CLASSES = {
    "number": re.compile(r"[0-9]+(?:ms|s|m|h|d|[kKmMgG])?"),
    # A port spec: a number, a range, `-` (all ports), a service name, or any
    # of those behind a T:/U:/S:/P: protocol prefix. No dot and no "@", so it
    # can never describe a host, an IP or a URL.
    "ports": re.compile(
        r"(?:[TUSP]:)?(?:[0-9]+(?:-[0-9]+)?|[A-Za-z][A-Za-z0-9_-]*|-)"),
    "range": re.compile(r"(?:[0-9]+-[0-9]*|-[0-9]+)"),
    "word": re.compile(r"[A-Za-z0-9_-]+"),
    # A comma-separated list of words -- status-code sets, extension lists. No
    # slash, no "@", no scheme, so it can never describe a path or a host the
    # way a bare `text` licence would.
    "wordset": re.compile(r"[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*"),
    "record": RECORD_TYPES,
    "method": HTTP_METHODS,
    # R14: the pattern excludes "@" and "/"; _class_matches additionally
    # refuses a URL scheme, so `user:pass` is licensed but `ftp:host` is not.
    "credential": re.compile(r"[^@/]+"),
    "text": re.compile(r".+", re.DOTALL),
}
STRUCTURED_CLASSES = frozenset(VALUE_CLASSES) - {"text"}
# R14: `^\d+(\.\d+)?$`. Only a flag licensed "number" may use it, and only
# before target parsing: `0.5` would otherwise parse as the public domain
# "0.5". A four-octet IP has three dots and can never fullmatch this.
DECIMAL_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# R8: a value that fullmatches this and carries ".", ":" or "@" is host-shaped:
# it must parse as a target and be scope-checked, or be refused.
HOST_SHAPED_RE = re.compile(r"[A-Za-z0-9._:*@+-]+")
# R8: an NSE script-argument name is an identifier, never a destination.
SCRIPT_ARG_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
# R13: these curl flags READ a file when the value carries a file sigil, so
# the value is a path and not a destination. Elsewhere a leading "@" is not
# file syntax (dig's @resolver is a bare token), so R8 keeps deciding it.
FILE_REFERENCE_FLAGS = {
    "curl": frozenset({"-d", "--data", "--data-raw", "--data-binary",
                       "--data-ascii", "--data-urlencode", "-F", "--form",
                       "--form-string"}),
}
DATA_URLENCODE_FLAG = "--data-urlencode"
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
# A strict binary lists every flag it accepts, and anything absent from that
# list is refused with the flag and the binary named. The rest of the wrapper is
# a deny-list -- an unknown flag is permitted and only its value is constrained
# -- which is affordable for twelve binaries that have each been through three
# review rounds, and not for a toolkit that keeps growing. The escape was always
# a flag nobody thought to refuse: nmap --proxies, curl -O, ffuf -input-cmd.
#
# Absence here is a decision, not an oversight: hydra's -b/-R/-S resume a saved
# session, which would let a run continue past the approval that started it, and
# -k is declared nowhere because its output semantics are not established. Every
# entry below is a flag whose meaning and value class are known.
STRICT_BINARIES = frozenset({"hydra", "sqlmap", "masscan", "ping",
                              "traceroute", "arp-scan", "httpx", "dirsearch",
                              "wfuzz", "wpscan", "sslyze"})
BOOLEAN_FLAGS = {
    "hydra": ("-q", "-f", "-F", "-u", "-I", "-v", "-4", "-6", "-h", "-V"),
    # `--level`/`--risk`/`--threads` are values, not booleans, and `-v` takes a
    # verbosity number: listing a value-taking flag as boolean would let the
    # permit list pass an argv where the number was read as a bare target.
    "sqlmap": ("--batch", "--banner", "--check-internet", "--current-db",
               "--count", "--dbs", "--dump", "--dump-all", "--fingerprint",
               "--forms", "--is-dba", "--offline", "--random-agent",
               "--skip-waf", "--predict-output", "--fresh-queries",
               "--flush-session", "--no-cast", "--crawl-log", "--version",
               "--help"),
    # The pilot migration. These are the boolean flags masscan documents as
    # output style, local reporting or an active banner grab -- no write, no
    # exec, no proxy, no target source. `--banners` belongs at intrusive
    # because it talks to the target, which the tier already says.
    "masscan": ("-h", "--help", "-V", "--version", "-v", "-d", "--debug",
                "--stats", "--echo", "--verify", "--only-open", "--banners",
                "-6", "--exclude-udp"),
    "ping": ("-4", "-6", "-n", "-q", "-N"),
    "traceroute": ("-4", "-6", "-n", "-A", "-N"),
    "arp-scan": ("-q", "-v", "-x", "-h", "--help", "--version"),
    # httpx declares only what changes the output, not what gets dialed.
    "httpx": ("-json", "-silent", "-no-color", "-title", "-status-code",
              "-tech-detect", "-web-server", "-h", "--help", "--version"),
    "dirsearch": ("-r", "--recursive", "--random-agent", "-q", "--quiet",
                  "--no-color", "-h", "--help", "--version"),
    "wfuzz": ("-c", "-k", "-N", "-B", "-q", "--mute", "-h", "--help",
              "-V", "--version"),
    "wpscan": ("--random-useragent", "--force", "--no-banner", "--verbose",
               "-h", "--help", "--version", "--only-enum-ok"),
    "sslyze": ("--regular", "--slow_target", "--offline",
               "--print_out_json", "--print_out_txt", "-h", "--help",
               "--version"),
}
# `-h`/`--version` are the only flags that may stand in place of a target: an
# argv that is nothing but a report request asks the installed version what it
# can do, which is how the rest of these tables get built from evidence rather
# than from memory. The moment anything else is in the argv it is a scan again.
REPORT_FLAGS = frozenset({"-h", "-V", "--help", "--version"})


def PERMITTED_FLAGS(binary):
    """Every flag name a strict binary may use, however it is written."""
    permitted = set(VALUE_FLAGS.get(binary, ()))
    permitted |= set(BOOLEAN_FLAGS.get(binary, ()))
    permitted |= set(NO_VALUE_FLAGS.get(binary, ()))
    return frozenset(permitted)
# Positional arguments that are not targets: searchsploit takes search terms,
# gobuster takes a mode word, nc takes a port number after the host.
FREE_POSITIONALS = frozenset({"searchsploit"})
SUBCOMMAND_WORDS = {
    "gobuster": frozenset({"dir", "dns", "fuzz", "vhost", "s3", "tftp"}),
    # hydra names the service as a positional word after the target. Only the
    # modules that take no inline spec are listed: `http-post-form` needs
    # `&` between fields, which is refused in every argv token, so the whole
    # *-form family stays unreachable rather than half-supported.
    "hydra": frozenset({"ssh", "ftp", "sftp", "telnet", "http-get",
                        "https-get", "mysql", "mssql", "postgres", "oracle",
                        "vnc", "smb", "pop3", "imap", "smtp", "ldap", "svn",
                        "irc", "snmp"}),
}
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SHELL_CHARS = frozenset(";|&$`><\n\r")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63})+$"
)
# R8: `scheme:rest` without `//`. The rest being all digits means this is a
# host:port pair, not a URL, so the caller checks that before using it.
SCHEME_ONLY_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):(.*)$", re.DOTALL)
# R8: the schemes that make `prefix:rest@host` a URL. With any other prefix
# the text before "@" is userinfo, so `user:pass@host:port` still resolves to
# its host. Every scheme curl/nmap can actually dial is listed, so a denied
# scheme cannot be smuggled in behind a username.
URL_SCHEMES = frozenset({
    "http", "https", "ftp", "ftps", "file", "gopher", "dict", "ldap", "ldaps",
    "imap", "imaps", "pop3", "pop3s", "smtp", "smtps", "telnet", "tftp",
    "scp", "sftp", "ssh", "rtsp", "smb", "smbs", "ws", "wss", "data",
    "mailto", "news", "nntp", "rsync", "git", "mqtt",
})


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
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1):
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
    R8: a scheme-only URL (`ftp:evil.com`, `http:host`) is a URL and gets the
    same http/https rule, and a userinfo form (`user@host`,
    `user:pass@host:port`) is stripped to the host so the host is what gets
    scope-checked. `host:port` is still a host with a port, not a scheme.
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
    scheme_only = SCHEME_ONLY_RE.match(raw)
    if (scheme_only and "@" in raw
            and scheme_only.group(1).lower() not in URL_SCHEMES):
        # R8: `user:pass@host:port` is a userinfo form, not a URL: the text
        # before the colon is a username. A real scheme (ftp:user@host) is
        # still parsed as a URL below, so it cannot dodge the http rule.
        raw = raw.rsplit("@", 1)[1]
        scheme_only = None
    if "://" in raw or (scheme_only and not scheme_only.group(2).isdigit()):
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https"):
            return None
        raw = parts.hostname or parts.path.split("/")[0]
        if not raw:
            return None
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1]
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


def _is_host_shaped(value):
    """R8: a value that could be a host, IP, URL or userinfo form.

    Such a value must parse as a target and be scope-checked, or be refused;
    it may never be waved through as a licensed non-destination.
    """
    return (HOST_SHAPED_RE.fullmatch(value) is not None
            and any(character in value for character in ".:@"))


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
    still consumes the token after it, so its value cannot hide from R2/R3/R8.
    R11: the single-dash binaries match a whole known flag name first, so
    nuclei `-output` is one flag and never `-o` with an attached "utput".
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
            elif len(token) == len(name):
                yield ("flag", name, None)
            else:
                # R8: a no-value flag with an attached remainder would drop
                # that remainder uninspected, so refuse the form instead.
                raise PolicyError(
                    "flag %s does not take an attached value" % name)
            continue
        item, index = _unknown_item(tokens, index, token)
        yield item


def _is_path_like(value):
    return (value.startswith(("/", "./", "../", "~"))
            or "/" in value or "\\" in value)


def _file_reference(value, binary, flag):
    """R13: the path curl would READ, or None when there is no file sigil.

    A leading `@` (`-d @file`), a form/data field's `=@` or `=<`
    (`-F file=@file`, `-F field=<file`), and `--data-urlencode name@file` all
    name a file. The sigil is not part of the name: treating `@/etc/passwd` as
    a relative path kept it "under" OUTPUT_ROOT while curl read the absolute
    file. A plain `name=value` carries no sigil and stays ordinary data.
    An attached `-f=value` leaves a leading "=" on the value, which is not
    part of the name either, so it is stripped before the sigil is read.
    Only the flags that really read files get this licence; every other value
    keeps R8's target-or-confined-path rule, and `_confine_path` strips a
    leading "@" for all of them, so no sigil can hide an absolute path.
    """
    if flag not in FILE_REFERENCE_FLAGS.get(binary, ()):
        return None
    while value.startswith("="):
        value = value[1:]
    if value.startswith("@"):
        return value[1:]
    _, separator, rest = value.partition("=")
    if separator and rest[:1] in ("@", "<"):
        return rest[1:]
    if flag == DATA_URLENCODE_FLAG and "=" not in value:
        # curl's grammar: `name@filename` only when there is no "=" at all;
        # with one, the value is `name=content` and is encoded literally.
        name, separator, rest = value.partition("@")
        if separator and name:
            return rest
    return None


def _path_roots(binary, flag):
    if flag in READ_FLAGS.get(binary, ()):
        return (OUTPUT_ROOT,) + READ_ONLY_DIRS
    return (OUTPUT_ROOT,)


def _confine_path(value, roots):
    """R2: resolve symlinks and relative paths, then require containment.

    The root itself is not a writable destination: a path that resolves to
    OUTPUT_ROOT (or a read-only root) exactly is refused, not just paths
    that escape it.
    R13: a leading `@` is curl's file-reference sigil, so the real path is
    confined, not a relative name that happens to start with "@".
    """
    if value.startswith("@"):
        value = value[1:]
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


def _class_matches(licensed, part):
    """Does part fit the value class this flag is licensed for?"""
    pattern = VALUE_CLASSES[licensed]
    if isinstance(pattern, frozenset):
        return part.upper() in pattern
    if pattern.fullmatch(part) is None:
        return False
    if licensed == "credential":
        # R14: a credential may not carry a URL scheme either.
        scheme = SCHEME_ONLY_RE.match(part)
        return not (scheme and scheme.group(1).lower() in URL_SCHEMES)
    return True


def _inspect_reference(part, binary, flag):
    """R13: account for a file reference; True when one was present.

    The referenced path is confined to OUTPUT_ROOT. It is a file, not a
    destination, so it is not target-parsed: `@payload.json` is a name under
    the root, and `@8.8.8.8` is a file name too, never a host to dial.
    """
    reference = _file_reference(part, binary, flag)
    if reference is None:
        return False
    _confine_path(reference, (OUTPUT_ROOT,))
    return True


def _inspect_part(part, licensed, scope, targets, label, binary=None,
                  flag=None):
    """R8: one value part ends as a target, a confined path or a licence.

    There is no fourth outcome. A structured class (ports, numbers, record
    types, methods, identifiers) may license a part only when that part does
    not parse as a target, so a destination can never hide in a licence.
    Anything that parses as a target is scope-checked. A host-shaped part that
    does not parse is refused, a path-like part is confined, and only then may
    a free-text licence apply. An unlicensed part is a refusal.
    R13: a curl file reference is resolved first, so the path curl actually
    reads is the path that gets confined.
    R14: a credential pair and a decimal number are decided before target
    parsing -- both are host-shaped, and neither can name a destination.
    """
    if _inspect_reference(part, binary, flag):
        return
    if licensed == "credential":
        if _class_matches(licensed, part):
            return
        raise PolicyError(
            "%s value %r is not a plain credential pair" % (label, part))
    if licensed == "number" and DECIMAL_NUMBER_RE.fullmatch(part):
        return
    target = _target_from_token(part)
    if target is not None:
        targets.append(_validate_target(target[0], target[1], scope))
        return
    if licensed in STRUCTURED_CLASSES and _class_matches(licensed, part):
        return
    if _is_host_shaped(part):
        raise PolicyError(
            "%s value %r looks like a host but is not a target the "
            "policy can validate" % (label, part))
    if _is_path_like(part):
        _confine_path(part, (OUTPUT_ROOT,))
        return
    if licensed is not None and _class_matches(licensed, part):
        return
    raise PolicyError(
        "%s value %r is not a target, a confined path or a licensed "
        "non-destination" % (label, part))


def _destination_value(binary, flag, value, scope, targets):
    """R10: a destination flag's value must parse as a target, always."""
    if binary == "masscan" and flag == "--range":
        ends = value.split("-")
        if len(ends) != 2:
            raise PolicyError("--range must be two IP addresses, A-B")
        parsed = []
        for end in ends:
            target = _target_from_token(end)
            if target is None or target[0] != "ip":
                raise PolicyError(
                    "--range end %r is not an IP address" % end)
            parsed.append(target[1])
        start, finish = parsed
        if start.version != finish.version or int(start) > int(finish):
            raise PolicyError("--range must run from a lower IP to a higher one")
        # Both ends in one scope network: a range that crosses out of scope
        # is refused even when each end happens to be in a different network.
        if not any(start in network and finish in network
                   for network in scope.networks):
            raise PolicyError("--range %r crosses out of scope" % value)
        targets.append(_validate_target("ip", start, scope))
        targets.append(_validate_target("ip", finish, scope))
        return
    target = _target_from_token(value)
    if target is None:
        raise PolicyError(
            "flag %s needs a target, got %r" % (flag, value))
    targets.append(_validate_target(target[0], target[1], scope))


def _inspect_value(binary, flag, value, scope, targets):
    """R8: account for a flag value, comma-separated parts included."""
    if flag in DESTINATION_FLAGS.get(binary, ()):
        _destination_value(binary, flag, value, scope, targets)
        return
    if (flag in PATH_FLAGS.get(binary, ())
            or flag in READ_FLAGS.get(binary, ())):
        # `-oN 8.8.8.8` is refused: the argv would carry a public host while the
        # Targets line the human approves names only in-scope hosts. Only a
        # literal address is refused — not a domain-shaped filename —
        # because `a.b` is how every legitimate output name looks ("scan.txt",
        # "report.lab.local.xml") and the output is confined to /tmp/kestrel
        # either way. A value with a separator is a path, always.
        if "/" not in value and "\\" not in value and "://" not in value:
            try:
                ipaddress.ip_address(value.strip("@[]"))
                raise PolicyError(
                    "flag %s value %r is an address, not an output name"
                    % (flag, value))
            except ValueError:
                pass
        _confine_path(value, _path_roots(binary, flag))
        return
    label = "flag %s" % flag
    # R13: a file reference is ONE path, even when the name contains a comma,
    # so it is resolved before the comma split. Mixed values still get the
    # per-part check below.
    if _inspect_reference(value, binary, flag):
        return
    licensed = NON_DESTINATION_VALUES.get(binary, {}).get(flag)
    for part in value.split(","):
        if not part:
            raise PolicyError("flag %s has an empty value part" % flag)
        _inspect_part(part, licensed, scope, targets, label, binary, flag)


def _check_dig_option(token, scope, targets):
    """R11: a dig `+option` is never a destination.

    R8 still applies to an attached value, so `+subnet=8.8.8.8` cannot ride
    in as an option: it is scope-checked like any other destination.
    """
    name, separator, value = token[1:].partition("=")
    if not VALUE_CLASSES["word"].fullmatch(name):
        raise PolicyError("dig option %r is not a plain option name" % token)
    if not separator:
        return
    if not value:
        raise PolicyError("dig option %r needs a value" % token)
    _inspect_part(value, "word", scope, targets, "dig option %s" % name)


def _check_script_args(value, scope):
    """R4/R8: split on `,` then `=`, and account for every piece.

    A piece that names a destination is scope-checked and a path-like piece
    is confined to OUTPUT_ROOT; the read-only wordlist directories are not
    granted here. Argument names are licensed identifiers, never destinations,
    and an argument value goes through the same R8 inspection as a flag value.
    """
    if not value:
        raise PolicyError("--script-args requires a value")
    # Script-argument destinations are scope-checked but are not scan targets,
    # so they are validated into a list of their own (round-1 behavior).
    checked = []
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
        if not SCRIPT_ARG_NAME_RE.fullmatch(name):
            raise PolicyError(
                "script argument name %r is not an identifier" % name)
        if not separator:
            continue           # a boolean script argument: name only
        if not argument:
            raise PolicyError("script argument %r needs a value" % name)
        _inspect_part(argument, "text", scope, checked,
                      "script argument %s" % name)


def target_tokens(argv, scope):
    """R1/R8: account for every token, and return the validated targets.

    Nothing is dropped silently. A bare token must parse as a target or be an
    explicitly licensed positional (search terms, a gobuster mode word, an nc
    port, a dig/host record type, a dig +option). A flag value must end as a
    validated in-scope target, a confined path, or a class-licensed
    non-destination. Anything else is a refusal.
    """
    binary = argv[0]
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
            if binary in RECORD_POSITIONALS and token.upper() in RECORD_TYPES:
                continue           # R11: dig/host positional record type
            if binary == "dig" and token.startswith("+"):
                _check_dig_option(token, scope, targets)
                continue           # R11: dig +option, never a destination
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
        _inspect_value(binary, token, value, scope, targets)
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
        # An IPv6 network is never inside an IPv4 scope network: subnet_of
        # raises TypeError across versions, which must be a denial, not a crash.
        if not any(value.subnet_of(network) for network in scope.networks
                   if network.version == value.version):
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
    elif binary in CREDENTIAL:
        tier = "credential"
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

    if binary in STRICT_BINARIES:
        # The tables above name escapes that have been found. This one is the
        # net for the escapes nobody has found yet, on a version this host has
        # never run: a flag that is not declared is not permitted.
        permitted = PERMITTED_FLAGS(binary)
        for flag in sorted(flags - permitted):
            raise PolicyError(
                "flag %s is not permitted for %s" % (flag, binary))

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


def _report_only(argv):
    """True when the argv is nothing but a request for the tool's own report.

    `hydra -h` asks the installed version what it can do. That is how a strict
    table gets built from evidence instead of from memory, and it runs no
    attack and reveals nothing about the lab -- but the licence is for the whole
    argv, so one real flag beside it makes the step a scan again.
    """
    return (argv[0] in STRICT_BINARIES and len(argv) > 1
            and all(token in REPORT_FLAGS for token in argv[1:]))


def validate_steps(steps, scope):
    _check_output_root()
    highest = "recon"
    targets = []
    for step in steps:
        argv = step["argv"]
        tier = classify(argv)
        found = target_tokens(argv, scope)
        needs_target = ((argv[0] not in LOCAL_SAFE
                         or (argv[0] == "openssl"
                             and len(argv) > 1
                             and argv[1] == "s_client"))
                        and not _report_only(argv))
        if not found and needs_target:
            raise PolicyError("%s requires a target" % argv[0])
        targets.extend(found)
        if TIERS[tier] > TIERS[highest]:
            highest = tier
    return highest, sorted(set(targets))


def _read_stream(stream, limit, holder):
    """I5: read BYTES, cap in bytes, and never let an error escape the thread.

    Decoding happens once, in run_step, with errors="replace": a child that
    emits non-UTF-8 bytes must not kill a reader thread (which used to leave
    stdout empty, truncated False and a traceback on stderr). The loop keeps
    draining after the cap so the child never blocks on a full pipe. The
    buffer is published up front and extended in place, so a read failure
    still delivers the partial output and is recorded instead of raised.
    """
    total = 0
    data = bytearray()
    holder["bytes"] = data
    try:
        while True:
            chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total <= limit:
                data.extend(chunk)
            else:
                remaining = limit - (total - len(chunk))
                if remaining > 0:
                    data.extend(chunk[:remaining])
    except Exception as exc:
        holder["error"] = "%s: %s" % (type(exc).__name__, exc)
    holder["truncated"] = total > limit


def _stream_text(holder):
    """I5: one decode, with replacement characters for undecodable bytes."""
    return holder.get("bytes", b"").decode("utf-8", errors="replace")


def _cut_utf8(data, limit):
    """I6: cut to at most limit bytes without splitting a UTF-8 character."""
    if limit <= 0:
        return b""
    cut = data[:limit]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    if cut and cut[-1] >= 0xC0:
        cut = cut[:-1]
    return cut


def _hash_outputs(result):
    """I6: hash exactly the delivered text, after every truncation."""
    for key in ("stdout", "stderr"):
        result[key + "_sha256"] = hashlib.sha256(
            result[key].encode("utf-8")).hexdigest()


def _stop_process(process):
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass                 # fall back to killing the child alone
    try:
        process.kill()
    except OSError:
        pass                     # already gone; the bounded wait below decides


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
        try:
            # I7: bounded, so an unkillable child cannot hang the wrapper.
            process.wait(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    for reader in readers:
        reader.join(READER_JOIN_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    read_errors = ["%s: %s" % (name, holder["error"])
                   for name, holder in (("stdout", stdout), ("stderr", stderr))
                   if "error" in holder]
    result = {
        "argv": list(argv),
        "exit_code": None if timed_out else process.returncode,
        "stdout": _stream_text(stdout),
        "stderr": _stream_text(stderr),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "truncated": bool(stdout.get("truncated") or stderr.get("truncated")),
        "read_errors": read_errors,
    }
    _hash_outputs(result)
    return result


def run_steps(steps, total_timeout):
    deadline = time.monotonic() + total_timeout
    results = []
    total_output = 0
    for step in steps:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break                # I7: no extra grant once the budget is spent
        result = run_step(step["argv"], min(step["timeout"], remaining))
        # I6: one shared byte budget across BOTH streams and across steps.
        budget = max(0, MAX_TOTAL_BYTES - total_output)
        cut = False
        for key in ("stdout", "stderr"):
            data = result[key].encode("utf-8")
            if len(data) > budget:
                data = _cut_utf8(data, budget)
                result[key] = data.decode("utf-8")
                cut = True
            budget -= len(data)
            total_output += len(data)
        if cut:
            result["truncated"] = True
        _hash_outputs(result)    # I6: hash what the client actually receives
        results.append(result)
        if result["timed_out"] or result["exit_code"] not in (0, None):
            break
    return results


def append_audit(path, record):
    """Append one JSONL record; OSError is the caller's signal to refuse (I7)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def parse_request(raw):
    try:
        request = json.loads(raw)
    except ValueError as exc:
        raise ProtocolError("request must be valid JSON") from exc
    # I7: `type(v) is int` refuses JSON true and 1.0, which `== 1` accepted.
    version = request.get("version") if isinstance(request, dict) else None
    if type(version) is not int or version != 1:
        raise ProtocolError("request must use protocol version 1")
    return request


def _respond(status, payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    return status


def _ok(**payload):
    return _respond(0, {"ok": True, **payload})


def _denied(reason, status=2):
    return _respond(status, {"ok": False, "denied": True, "reason": reason})


def _audit_or_refuse(audit_path, record):
    """I7: an audit write failure is a refusal, never an escaped OSError."""
    try:
        append_audit(audit_path, record)
    except OSError as exc:
        return _denied("could not write the audit log: %s" % exc, 3)
    return None


def main(scope_path=None, audit_path=None):
    """I7: no exception escapes as a traceback; every failure is JSON.

    Exit codes: 0 completed, 2 policy denial, 3 protocol/configuration error,
    1 an unexpected internal failure (the response still carries the error).
    """
    try:
        return _main(scope_path, audit_path)
    except Exception as exc:
        try:
            return _respond(1, {"ok": False, "error": "%s: %s"
                                % (type(exc).__name__, exc)})
        except Exception:
            return 1             # stdout itself is gone (e.g. a closed pipe)


EGRESS_RULE = "/etc/kestrel/egress.conf"


def _binary_present(name):
    """Does this permitted name actually exist on this host?

    SAFE_PATH is a policy constant, never the inherited PATH, so a request cannot
    describe a search path for itself. No child is spawned to find out: asking
    `command -v` would run something.
    """
    for directory in SAFE_PATH.split(":"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


def build_inventory():
    """Report what the wrapper can run here, so Kestrel need not guess."""
    inventory = {tier: sorted(name for name in binaries
                              if _binary_present(name))
                 for tier, binaries in (("recon", RECON),
                                        ("intrusive", INTRUSIVE),
                                        ("credential", CREDENTIAL),
                                        ("local_safe", LOCAL_SAFE))}
    # Which binaries are strict is a policy fact, not a presence fact: it tells
    # Kestrel that an unknown flag there is a refusal rather than an omission.
    inventory["strict"] = sorted(STRICT_BINARIES)
    # The refused names are presence, not permission: they tell the operator what
    # the policy is protecting against on this host.
    inventory["denied_present"] = sorted(name for name in DENIED_BINARIES
                                         if _binary_present(name))
    inventory["egress_enforced"] = os.path.isfile(EGRESS_RULE)
    return inventory


def _main(scope_path, audit_path):
    scope_path = scope_path or DEFAULT_SCOPE
    audit_path = audit_path or DEFAULT_AUDIT
    try:
        request = parse_request(sys.stdin.read())
        scope = load_scope(scope_path)
    except ProtocolError as exc:
        return _denied(str(exc), 3)
    except PolicyError as exc:
        return _denied(str(exc))

    # I7: a request that does not say "execute" must never run anything.
    mode = request.get("mode", "check")
    if mode not in ("scope", "check", "execute", "inventory"):
        return _denied("unknown mode %r" % mode, 3)
    if mode == "inventory":
        # Read-only and target-free, so it takes no steps and no objective: this
        # is the wrapper describing itself, never a request to do something.
        refused = _audit_or_refuse(audit_path, {"event": "inventory"})
        if refused is not None:
            return refused
        return _ok(inventory=build_inventory())
    if mode == "scope":
        refused = _audit_or_refuse(audit_path, {"event": "scope",
                                               "scope": scope.as_dict()})
        if refused is not None:
            return refused
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
        refused = _audit_or_refuse(audit_path, {
            "event": "check",
            "ts": datetime.now(timezone.utc).isoformat(),
            "objective_sha256": hashlib.sha256(
                objective.encode("utf-8")).hexdigest(),
            "risk": risk,
            "targets": targets,
            "argv": [step["argv"] for step in normalized],
        })
        if refused is not None:
            return refused
        return _ok(risk=risk, targets=targets,
                   steps=[step["argv"] for step in normalized])

    plan = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "objective_sha256": hashlib.sha256(
            objective.encode("utf-8")).hexdigest(),
        "approval": str(request.get("approval", "unknown")),
        "risk": risk,
        "targets": targets,
        "steps": normalized,
    }
    # I7: the intent record must be on disk BEFORE anything runs. If that
    # write fails, refuse to execute: no audit, no run.
    refused = _audit_or_refuse(audit_path, {"event": "execute_intent", **plan})
    if refused is not None:
        return refused
    try:
        results = run_steps(normalized, total_timeout)
    except PolicyError as exc:
        return _denied(str(exc))
    record = {
        "event": "execute",
        **plan,
        "ts": datetime.now(timezone.utc).isoformat(),
        "results": [{
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "truncated": result["truncated"],
            "stdout_sha256": result["stdout_sha256"],
            "stderr_sha256": result["stderr_sha256"],
        } for result in results],
    }
    try:
        append_audit(audit_path, record)
    except OSError as exc:
        # The steps already ran, so return their results with an honest error
        # rather than pretending nothing happened.
        return _respond(3, {
            "ok": False,
            "error": "audit write failed after execution: %s" % exc,
            "results": results, "risk": risk, "targets": targets,
        })
    return _ok(results=results, risk=risk, targets=targets)


if __name__ == "__main__":
    raise SystemExit(main())
