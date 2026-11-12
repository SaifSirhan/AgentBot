"""Client for the Kali SSH policy wrapper."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV = "KESTREL_KALI_CONFIG"


class KaliError(RuntimeError):
    """Base error for the Kali bridge."""


class KaliUnavailable(KaliError):
    """The VM, SSH client, or config is unavailable."""


class KaliPolicyError(KaliError):
    """The wrapper refused the task on policy grounds."""


class KaliProtocolError(KaliError):
    """The wrapper returned an unexpected response."""


@dataclass(frozen=True)
class KaliConfig:
    host: str
    port: int
    user: str
    key_path: str
    known_hosts: str
    connect_timeout: int = 10


def config_path(path=None):
    if path:
        return Path(path)
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # An empty base would make the path relative to the launch directory.
        raise KaliUnavailable("APPDATA is not set; no Kali config path")
    return Path(appdata) / "Kestrel" / "kali.json"


def load_config(path=None):
    file_path = config_path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KaliUnavailable(
            "Kali config is missing or invalid: %s" % file_path) from exc
    if not isinstance(data, dict):
        raise KaliUnavailable("Kali config must be an object")

    host = data.get("host")
    user = data.get("user")
    key_path = data.get("key_path")
    known_hosts = data.get("known_hosts")
    port = data.get("port", 22)
    connect_timeout = data.get("connect_timeout", 10)
    if not all(isinstance(value, str) and value
               for value in (host, user, key_path, known_hosts)):
        raise KaliUnavailable("Kali config is missing host, user, key_path, "
                              "or known_hosts")
    if (not isinstance(port, int) or isinstance(port, bool)
            or not 1 <= port <= 65535):
        raise KaliUnavailable("Kali config port is invalid")
    if (not isinstance(connect_timeout, int) or isinstance(connect_timeout, bool)
            or not 1 <= connect_timeout <= 120):
        raise KaliUnavailable("Kali config connect_timeout is invalid")
    return KaliConfig(
        host=host,
        port=port,
        user=user,
        key_path=os.path.expandvars(key_path),
        known_hosts=os.path.expandvars(known_hosts),
        connect_timeout=connect_timeout,
    )


def ssh_argv(config):
    return [
        "ssh",
        "-i", config.key_path,
        "-p", str(config.port),
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "UserKnownHostsFile=" + config.known_hosts,
        "-o", "ConnectTimeout=%d" % config.connect_timeout,
        "%s@%s" % (config.user, config.host),
    ]


def _run(request, config, timeout, runner=None):
    runner = runner or subprocess.run
    argv = ssh_argv(config)
    # The wrapper reads stdin as UTF-8. Encode the request ourselves and pass
    # bytes so the console/locale codepage never touches the payload.
    payload_bytes = json.dumps(request).encode("utf-8")
    try:
        completed = runner(
            argv,
            input=payload_bytes,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise KaliUnavailable("OpenSSH client was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise KaliUnavailable("Kali SSH call timed out") from exc
    except OSError as exc:
        raise KaliUnavailable("Kali SSH call failed: %s" % exc) from exc

    raw_stdout = completed.stdout
    if isinstance(raw_stdout, bytes):
        try:
            stdout = raw_stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KaliProtocolError("Kali returned non-UTF-8 output") from exc
    else:
        stdout = raw_stdout or ""

    raw_stderr = completed.stderr
    if isinstance(raw_stderr, bytes):
        stderr = raw_stderr.decode("utf-8", "replace")
    else:
        stderr = raw_stderr or ""

    if completed.returncode != 0 and not stdout.strip():
        detail = stderr.strip()[:300]
        raise KaliUnavailable(
            "Kali SSH exited %d: %s" % (completed.returncode, detail))
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise KaliProtocolError("Kali returned invalid JSON") from exc
    try:
        # The ok gate runs first, exactly as the brief orders it. A payload
        # with no mapping surface (list, string, number, null) is "not ok".
        ok = payload.get("ok")
    except AttributeError:
        ok = None
    if ok is not True:
        # Non-dict payloads carry no refusal text; fall back to the generic
        # message. Same exception type as any other refusal.
        if not isinstance(payload, dict):
            raise KaliProtocolError("Kali refused the request")
        reason = str(payload.get("reason") or payload.get("error")
                     or "Kali refused the request")
        if payload.get("denied") is True:
            raise KaliPolicyError(reason)
        raise KaliProtocolError(reason)
    # Success shape: ok is True and the payload is a mapping.
    assert isinstance(payload, dict)
    return payload


def scope(timeout=15, config=None, path=None, runner=None):
    config = config or load_config(path)
    payload = _run({"version": 1, "mode": "scope"}, config, timeout, runner)
    result = payload.get("scope")
    if not isinstance(result, dict):
        raise KaliProtocolError("Kali returned no scope object")
    return result


def check(objective, steps, total_timeout=300, config=None, path=None,
          runner=None):
    config = config or load_config(path)
    request = {
        "version": 1,
        "mode": "check",
        "objective": objective,
        "steps": steps,
        "total_timeout": total_timeout,
    }
    # Check mode only evaluates policy on Kali and runs no scanners, so the
    # budget stays small; total_timeout is not honoured here.
    timeout = max(60, config.connect_timeout + 30)
    return _run(request, config, timeout, runner)


def task(objective, steps, total_timeout=300, config=None, path=None,
         runner=None):
    config = config or load_config(path)
    request = {
        "version": 1,
        "mode": "execute",
        "objective": objective,
        "steps": steps,
        "total_timeout": total_timeout,
        "approval": "desktop",
    }
    timeout = max(60, total_timeout + config.connect_timeout + 30)
    return _run(request, config, timeout, runner)
