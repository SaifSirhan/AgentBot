from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import autonomous_loop as engine


class CurriculumError(RuntimeError):
    pass


SANDBOX_ROOT = Path(os.environ.get("AGENTBOT_CURRICULUM_SANDBOX", r"C:\Temp\AgentSandbox"))
QUARANTINE_ROOT = Path(os.environ.get(
    "AGENTBOT_SKILL_QUARANTINE", Path(__file__).resolve().parent / "skill_quarantine"))
DEFAULT_IMAGE = "python:3.12-slim"
MAX_ATTEMPTS = 3
MAX_STEPS = 12
MAX_FILE_BYTES = 64 * 1024
MAX_CODE_BYTES = 12 * 1024
MAX_OUTPUT_CHARS = 8000
MAX_SKILL_CHARS = 6000
MAX_FAILURE_CHARS = 2000
WORKSPACE = "/workspace"
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")

CATALOG = {
    "csv_summary": "summarize a seeded CSV into a checked JSON artifact",
    "log_report": "count seeded log levels into a checked JSON artifact",
    "local_http_service": "build and start a checked loopback health service",
}

TOOL_DESCRIPTIONS = {
    "sandbox_write": (
        'Write a file inside /workspace. args: {"path": "relative/path", '
        '"content": "text"}'
    ),
    "sandbox_read": 'Read a file inside /workspace. args: {"path": "relative/path"}',
    "sandbox_list": 'List one directory inside /workspace. args: {"path": "."}',
    "sandbox_run_python": 'Run Python inside the sandbox. args: {"code": "..."}',
    "sandbox_start_python": (
        'Start a Python file in the background. args: {"path": "server.py"}'
    ),
    "sandbox_stop_process": "Stop background programs started in the sandbox. args: {}",
    "sandbox_http_get": (
        'Fetch a loopback URL from inside the sandbox. args: {"url": '
        '"http://127.0.0.1:8765/health"}'
    ),
}

TOOL_ARGS = {
    "sandbox_write": {"path": str, "content": str},
    "sandbox_read": {"path": str},
    "sandbox_list": {"path": str},
    "sandbox_run_python": {"code": str},
    "sandbox_start_python": {"path": str},
    "sandbox_stop_process": {},
    "sandbox_http_get": {"url": str},
}


@dataclass(frozen=True)
class Challenge:
    challenge_id: str
    title: str
    goal: str
    fixtures: Mapping[str, str]
    verifier: Mapping[str, Any]


def _challenge_prompt() -> str:
    return (
        "You are an autonomous agent testing your own capabilities. Look at your "
        "available tools. Generate a complex, multi-step task you have never done "
        "before (for example: build a local web server, write a script to parse a "
        "CSV, or automate a game). Do not ask for permission. "
        "You are confined to an isolated Linux sandbox with only these tools: "
        + json.dumps(TOOL_DESCRIPTIONS, ensure_ascii=False)
        + "\n\nCHALLENGE IDS:\n"
        + "\n".join(f"- {key}: {value}" for key, value in CATALOG.items())
        + "\n\nChoose exactly one challenge id from that list. Do not invent an "
        "unverified task. Reply with ONLY JSON: {\"challenge\": \"<id>\"}."
    )


def build_challenge(challenge_id: str, seed: int = 0) -> Challenge:
    if challenge_id not in CATALOG:
        raise CurriculumError(f"Unknown challenge: {challenge_id!r}.")
    rng = random.Random(f"{challenge_id}:{seed}")
    if challenge_id == "csv_summary":
        products = ["alpha", "beta", "gamma", "delta"]
        rows = []
        for _ in range(rng.randint(4, 7)):
            rows.append((rng.choice(products), rng.randint(1, 9), rng.randint(100, 999)))
        csv_text = "product,quantity,unit_price\n" + "".join(
            f"{name},{quantity},{cents / 100:.2f}\n" for name, quantity, cents in rows
        )
        expected = {
            "rows": len(rows),
            "total_quantity": sum(quantity for _, quantity, _ in rows),
            "total_revenue": round(sum(quantity * cents for _, quantity, cents in rows) / 100, 2),
        }
        return Challenge(
            challenge_id,
            "CSV summary",
            "Read /workspace/sales.csv with columns product,quantity,unit_price. "
            "Write /workspace/summary.json with exactly these keys: rows (data row "
            "count), total_quantity, and total_revenue (sum of quantity × unit price, "
            "rounded to two decimals). Do not modify sales.csv.",
            {"sales.csv": csv_text},
            {"type": "json", "path": "summary.json", "expected": expected},
        )
    if challenge_id == "log_report":
        levels = ["INFO", "WARN", "ERROR"]
        lines = [
            f"2026-01-01T00:{index:02d}:00Z {rng.choice(levels)} event {index}"
            for index in range(rng.randint(7, 12))
        ]
        expected = {
            "total": len(lines),
            "errors": sum(" ERROR " in line for line in lines),
            "warnings": sum(" WARN " in line for line in lines),
        }
        return Challenge(
            challenge_id,
            "Log report",
            "Read /workspace/app.log. Write /workspace/report.json with exactly "
            "these keys: total (all lines), errors (ERROR lines), and warnings "
            "(WARN lines). Do not modify app.log.",
            {"app.log": "\n".join(lines) + "\n"},
            {"type": "json", "path": "report.json", "expected": expected},
        )
    return Challenge(
        challenge_id,
        "Loopback health service",
        "Create /workspace/server.py (standard library only) that serves GET "
        "/health on 127.0.0.1:8765 with JSON exactly "
        '{"ok": true, "service": "agentbot"}. Start it in the background with '
        "sandbox_start_python. Leave the service running for the verifier.",
        {"BRIEF.txt": "Build a small loopback health service.\n"},
        {
            "type": "http",
            "url": "http://127.0.0.1:8765/health",
            "expected_json": {"ok": True, "service": "agentbot"},
        },
    )


def choose_challenge(llm: Callable[[str], str], seed: int) -> Challenge:
    reply = llm(_challenge_prompt())
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        raise CurriculumError("Challenge selection was not JSON.")
    try:
        data = json.loads(match.group(0))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CurriculumError(f"Challenge selection was invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"challenge"} or not isinstance(
            data["challenge"], str):
        raise CurriculumError("Challenge selection must contain exactly one challenge id.")
    if data["challenge"] not in CATALOG:
        raise CurriculumError(f"Unknown challenge: {data['challenge']!r}.")
    return build_challenge(data["challenge"], seed)


def resolve_workspace_path(root: Path, relative: str, *, allow_root: bool = False) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise CurriculumError("A relative sandbox path is required.")
    if relative.startswith(("/", "\\")) or Path(relative).is_absolute():
        raise CurriculumError("Absolute paths are not allowed in the sandbox.")
    parts = Path(relative).parts
    if any(part in ("..", ".") for part in parts) or any(":" in part for part in parts):
        raise CurriculumError("Sandbox paths cannot contain traversal or drive syntax.")
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise CurriculumError("Path escapes the sandbox workspace.")
    if candidate == root and not allow_root:
        raise CurriculumError("A file path is required, not the workspace root.")
    return candidate


def validate_loopback_url(url: str) -> bool:
    if not isinstance(url, str):
        raise CurriculumError("A loopback URL is required.")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise CurriculumError("Only http loopback URLs are allowed.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise CurriculumError("Credentials are not allowed in sandbox URLs.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CurriculumError("Invalid loopback URL port.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise CurriculumError("Invalid loopback URL port.")
    if parsed.fragment:
        raise CurriculumError("URL fragments are not allowed.")
    return True


def validate_image_name(image: str) -> str:
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise CurriculumError("Invalid container image name.")
    return image


def build_container_command(name: str, image: str, run_dir: Path) -> list[str]:
    validate_image_name(image)
    return [
        "docker", "run", "--detach", "--rm", "--pull=never", "--name", name,
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "256m",
        "--memory-swap", "256m",
        "--cpus", "1",
        "-v", f"{Path(run_dir).resolve()}:{WORKSPACE}:rw",
        "-w", WORKSPACE,
        "--entrypoint", "python",
        image,
        "-c", "import time; time.sleep(10**9)",
    ]


class DockerSandbox:
    def __init__(
        self,
        *,
        run_dir: Path,
        image: str = DEFAULT_IMAGE,
        run_command: Callable[..., Any] | None = None,
        name: str | None = None,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.image = validate_image_name(image)
        self.name = name or f"agentbot-curriculum-{uuid.uuid4().hex[:12]}"
        self._run_command = run_command or subprocess.run
        self._started = False

    def _run(self, args: list[str], *, input_text: str | None = None,
             timeout: float = 30) -> Any:
        return self._run_command(
            args, input=input_text, capture_output=True, text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def image_available(self) -> bool:
        result = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.image])
        return result.returncode == 0

    def start(self) -> None:
        if not self.image_available():
            raise CurriculumError(
                f"Container image '{self.image}' is not available locally. "
                f"Pull it manually: docker pull {self.image}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        result = self._run(build_container_command(self.name, self.image, self.run_dir),
                           timeout=60)
        if result.returncode != 0:
            raise CurriculumError(
                "Container failed to start: "
                + ((result.stderr or result.stdout or "").strip()[:500] or "unknown error"))
        self._started = True

    def stop(self) -> None:
        try:
            self._run(["docker", "rm", "-f", self.name], timeout=20)
        except Exception:
            pass
        self._started = False

    def seed_files(self, files: Mapping[str, str]) -> None:
        for relative, content in files.items():
            self.write_workspace_file(relative, content)

    def write_workspace_file(self, relative: str, content: str) -> str:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise CurriculumError("Sandbox file content is too large.")
        target = resolve_workspace_path(self.run_dir, relative)
        if target.is_symlink():
            raise CurriculumError("Refusing to write through a sandbox symlink.")
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent.resolve()
        if parent != self.run_dir and self.run_dir not in parent.parents:
            raise CurriculumError("Path escapes the sandbox workspace.")
        descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return f"Wrote {relative} ({len(content.encode('utf-8'))} bytes)."

    def read_workspace_file(self, relative: str) -> str:
        target = resolve_workspace_path(self.run_dir, relative)
        if not target.is_file():
            raise CurriculumError(f"Missing artifact: /workspace/{relative}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise CurriculumError(f"Artifact is too large: /workspace/{relative}")
        return target.read_text(encoding="utf-8")

    def list_workspace(self, relative: str = ".") -> str:
        target = resolve_workspace_path(self.run_dir, relative, allow_root=True)
        if not target.is_dir():
            raise CurriculumError(f"Not a directory: /workspace/{relative}")
        names = sorted(entry.name for entry in target.iterdir())[:100]
        return "\n".join(names) if names else "(empty directory)"

    def _exec(self, args: list[str], *, input_text: str | None = None,
              timeout: float = 15, workdir: str = WORKSPACE) -> Any:
        return self._run(
            ["docker", "exec", "-i", "-w", workdir, self.name] + args,
            input_text=input_text, timeout=timeout)

    def _python(self, code: str, timeout: float = 15, *, isolated: bool = False) -> str:
        args = ["python"] + (["-I", "-P"] if isolated else []) + ["-"]
        result = self._exec(
            args, input_text=code, timeout=timeout,
            workdir="/tmp" if isolated else WORKSPACE)
        output = (result.stdout or result.stderr or "").strip()
        output = output[-MAX_OUTPUT_CHARS:]
        if result.returncode != 0:
            return "ERROR: " + (output or f"exit code {result.returncode}")
        return output or "OK (no output)"

    def run_python(self, code: str) -> str:
        if not isinstance(code, str) or not code.strip():
            raise CurriculumError("Python code is required.")
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise CurriculumError("Python code is too large.")
        return self._python(code)

    def start_python(self, relative: str) -> str:
        target = resolve_workspace_path(self.run_dir, relative)
        if target.suffix.lower() != ".py" or not target.is_file():
            raise CurriculumError("A sandbox .py file must exist before it can be started.")
        container_path = f"{WORKSPACE}/{target.relative_to(self.run_dir).as_posix()}"
        result = self._run(
            ["docker", "exec", "-d", "-w", WORKSPACE, self.name, "python",
             container_path], timeout=20)
        if result.returncode != 0:
            raise CurriculumError(
                "Could not start sandbox program: "
                + ((result.stderr or result.stdout or "").strip()[:500] or "unknown error"))
        return f"Started {container_path} in the background."

    def stop_background_processes(self) -> str:
        code = (
            "import os, signal\n"
            "me = os.getpid()\n"
            "for name in os.listdir('/proc'):\n"
            "    if not name.isdigit() or int(name) == me:\n"
            "        continue\n"
            "    try:\n"
            "        cmd = open('/proc/' + name + '/cmdline', 'rb').read()\n"
            "    except OSError:\n"
            "        continue\n"
            "    if b'/workspace/' in cmd:\n"
            "        try:\n"
            "            os.kill(int(name), signal.SIGKILL)\n"
            "        except OSError:\n"
            "            pass\n"
            "print('stopped')\n"
        )
        return self._python(code, timeout=10, isolated=True)

    def http_get(self, url: str) -> str:
        validate_loopback_url(url)
        code = (
            "import sys, urllib.request\n"
            "with urllib.request.urlopen(sys.argv[1], timeout=3) as response:\n"
            "    print(response.read(65537).decode('utf-8', 'replace'))\n"
        )
        result = self._run(
            ["docker", "exec", "-i", "-w", "/tmp", self.name, "python",
             "-I", "-P", "-c", code, url], timeout=10)
        if result.returncode != 0:
            raise CurriculumError(
                "Sandbox HTTP request failed: "
                + ((result.stderr or result.stdout or "").strip()[:500] or "unknown error"))
        return (result.stdout or "")[:MAX_OUTPUT_CHARS].strip()

    def authorize(self, tool: str, args: dict[str, Any]) -> bool:
        schema = TOOL_ARGS.get(tool)
        if schema is None or not isinstance(args, dict) or set(args) != set(schema):
            return False
        if any(not isinstance(args[key], expected) for key, expected in schema.items()):
            return False
        try:
            if tool == "sandbox_http_get":
                validate_loopback_url(args["url"])
            elif tool in {"sandbox_write", "sandbox_read", "sandbox_start_python"}:
                resolve_workspace_path(self.run_dir, args["path"])
            elif tool == "sandbox_list":
                resolve_workspace_path(self.run_dir, args["path"], allow_root=True)
        except CurriculumError:
            return False
        return True

    @property
    def tool_descriptions(self) -> Mapping[str, str]:
        return TOOL_DESCRIPTIONS

    def execute_tool(self, tool: str, args: dict[str, Any],
                     stop_event: Any) -> engine.ToolResult:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError()
        if not self.authorize(tool, args):
            return engine.ToolResult(False, f"ERROR: Sandbox denied {tool!r}.")
        try:
            if tool == "sandbox_write":
                observation = self.write_workspace_file(args["path"], args["content"])
            elif tool == "sandbox_read":
                observation = self.read_workspace_file(args["path"])[:MAX_OUTPUT_CHARS]
            elif tool == "sandbox_list":
                observation = self.list_workspace(args["path"])
            elif tool == "sandbox_run_python":
                observation = self.run_python(args["code"])
            elif tool == "sandbox_start_python":
                observation = self.start_python(args["path"])
            elif tool == "sandbox_stop_process":
                observation = self.stop_background_processes()
            else:
                observation = self.http_get(args["url"])
        except InterruptedError:
            raise
        except Exception as exc:
            return engine.ToolResult(False, f"ERROR: {type(exc).__name__}: {exc}")
        ok = not observation.lstrip().lower().startswith("error:")
        return engine.ToolResult(ok, observation)

    def verify(self, challenge: Challenge) -> engine.Verification:
        verifier = challenge.verifier
        if verifier["type"] == "json":
            path = verifier["path"]
            try:
                raw = self.read_workspace_file(path)
            except CurriculumError as exc:
                return engine.Verification(False, str(exc))
            try:
                actual = json.loads(raw)
            except json.JSONDecodeError as exc:
                return engine.Verification(
                    False, f"Invalid JSON in /workspace/{path}: {exc}")
            expected = verifier["expected"]
            if not isinstance(actual, dict) or set(actual) != set(expected):
                return engine.Verification(
                    False,
                    f"/workspace/{path} keys {sorted(actual) if isinstance(actual, dict) else type(actual).__name__} "
                    f"do not match {sorted(expected)}.")
            differences = []
            for key, wanted in expected.items():
                got = actual[key]
                if isinstance(wanted, bool) or not isinstance(got, (int, float)) or isinstance(got, bool):
                    if got != wanted:
                        differences.append(f"{key}: expected {wanted!r}, got {got!r}")
                elif round(float(got), 2) != round(float(wanted), 2):
                    differences.append(f"{key}: expected {wanted!r}, got {got!r}")
            if differences:
                return engine.Verification(
                    False, f"/workspace/{path} mismatch: " + "; ".join(differences))
            return engine.Verification(True, f"/workspace/{path} matches expected values.")
        if verifier["type"] == "http":
            last = "service did not respond"
            for _ in range(12):
                try:
                    raw = self.http_get(verifier["url"])
                    actual = json.loads(raw)
                    if actual == verifier["expected_json"]:
                        return engine.Verification(True, f"{verifier['url']} returned expected JSON.")
                    last = f"{verifier['url']} returned {raw!r}"
                except Exception as exc:
                    last = f"{verifier['url']} failed: {exc}"
                time.sleep(0.25)
            return engine.Verification(False, last)
        return engine.Verification(False, "Unknown verifier type.")


def _sanitize_failure(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text or "")).strip()
    if len(text) > MAX_FAILURE_CHARS:
        text = text[:MAX_FAILURE_CHARS] + " [truncated]"
    return text or "No verifier evidence was produced."


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"{message.get('role', 'user').upper()}:\n{message.get('content', '')}"
        for message in messages)


def _loop_llm(llm: Callable[[str], str]) -> Callable[[list[dict[str, Any]]], str]:
    return lambda messages: llm(_flatten_messages(messages))


def _default_llm(prompt: str) -> str:
    import agent
    reply = agent.ask_llm_direct(prompt, max_tokens=1500)
    if not isinstance(reply, str) or not reply.strip() or reply.lstrip().lower().startswith("error"):
        raise CurriculumError(f"LLM call failed: {str(reply)[:200]}")
    return reply.strip()


def _draft_prompt(challenge: Challenge, history: tuple[engine.RecordedAction, ...],
                  evidence: str) -> str:
    steps = "\n".join(
        f"{record.step}. {record.tool} ok={record.ok}: {record.observation[:500]}"
        for record in history)
    return (
        "You just learned how to do this. Write SKILL.md documenting the exact "
        "steps you took. Return ONLY a SKILL.md with YAML frontmatter containing "
        "exactly name (lowercase slug) and description, followed by a concise body. "
        "Do not include API keys, tokens, passwords, user home paths, Docker host "
        "instructions, permission-bypass advice, or content outside /workspace.\n\n"
        f"Challenge: {challenge.title}\nGoal:\n{challenge.goal}\n\n"
        f"Verifier evidence: {evidence}\n\nSuccessful tool steps:\n{steps}"
    )


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|password|passwd|secret|token|bearer\s+[a-z0-9]|"
    r"BEGIN [A-Z ]*PRIVATE KEY|sk-[a-z0-9]{8,}|ghp_[a-z0-9]{20,})")
_ESCAPE_RE = re.compile(
    r"(?i)(bypass.{0,40}(permission|sandbox|auth)|ignore.{0,40}permission|"
    r"disable.{0,40}sandbox|--privileged|docker\.sock|--network\s+host|"
    r"[A-Za-z]:\\|(?:^|\s)\.\./|/home/|os\.environ)")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not isinstance(text, str) or not text.startswith("---"):
        raise CurriculumError("SKILL.md must start with YAML frontmatter.")
    end = text.find("\n---", 3)
    if end == -1:
        raise CurriculumError("SKILL.md frontmatter is unterminated.")
    frontmatter = {}
    for line in text[3:end].strip().splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise CurriculumError("SKILL.md frontmatter lines must be key: value.")
        frontmatter[key.strip()] = value.strip()
    return frontmatter, text[end + 4:].lstrip("\n")


def validate_skill_draft(text: str) -> dict[str, str]:
    frontmatter, body = _parse_frontmatter(text)
    if set(frontmatter) != {"name", "description"}:
        raise CurriculumError("SKILL.md frontmatter must contain exactly name and description.")
    name = frontmatter["name"]
    description = frontmatter["description"]
    if not _NAME_RE.fullmatch(name):
        raise CurriculumError("SKILL.md name must be a lowercase slug.")
    if not description or len(description) > 200:
        raise CurriculumError("SKILL.md description must be 1-200 characters.")
    if not body.strip() or len(body) > MAX_SKILL_CHARS or len(body.splitlines()) > 300:
        raise CurriculumError("SKILL.md body is empty or oversized.")
    combined = name + "\n" + description + "\n" + body
    if _SECRET_RE.search(combined):
        raise CurriculumError("SKILL.md contains secret-like content.")
    if _ESCAPE_RE.search(body):
        raise CurriculumError("SKILL.md contains sandbox-escape or permission-bypass content.")
    if not name:
        raise CurriculumError("SKILL.md name is required.")
    return {"name": name, "description": description, "text": text}


def _write_quarantine(
    root: Path,
    challenge: Challenge,
    skill: dict[str, str],
    attempts: int,
    evidence: str,
    history: tuple[engine.RecordedAction, ...],
    image: str,
) -> Path:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = skill["name"]
    directory = root / f"{stamp}-{slug}"
    while directory.exists():
        directory = root / f"{stamp}-{slug}-{uuid.uuid4().hex[:6]}"
    directory.mkdir()
    draft = skill["text"].rstrip() + "\n"
    (directory / "SKILL.md").write_text(draft, encoding="utf-8")
    metadata = {
        "run_id": directory.name,
        "challenge": challenge.challenge_id,
        "attempts": attempts,
        "verifier_evidence": evidence,
        "image": image,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        "tool_calls": [
            {
                "step": record.step,
                "tool": record.tool,
                "ok": record.ok,
                "observation_sha256": hashlib.sha256(
                    record.observation.encode("utf-8")).hexdigest(),
            }
            for record in history
        ],
    }
    (directory / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8")
    return directory / "SKILL.md"


def run_curriculum(
    *,
    llm: Callable[[str], str] | None = None,
    sandbox: Any | None = None,
    quarantine_root: Path = QUARANTINE_ROOT,
    sandbox_root: Path = SANDBOX_ROOT,
    image: str = DEFAULT_IMAGE,
    seed: int | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    challenge_id: str | None = None,
    loop_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise CurriculumError("max_attempts must be between 1 and 10.")
    if seed is None:
        seed = int(time.time())
    call = llm or _default_llm
    owns_sandbox = sandbox is None
    if owns_sandbox:
        run_dir = Path(sandbox_root).resolve() / (
            "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid.uuid4().hex[:8])
        sandbox = DockerSandbox(run_dir=run_dir, image=image)
    challenge = (
        build_challenge(challenge_id, seed) if challenge_id is not None
        else choose_challenge(call, seed)
    )
    evidence = "No verifier evidence was produced."
    attempts = 0
    history: tuple[engine.RecordedAction, ...] = ()
    cleanup_needed = False
    try:
        sandbox.start()
        sandbox.seed_files(challenge.fixtures)
        factory = loop_factory or (
            lambda loop_llm, loop_sandbox, loop_challenge: engine.AutonomousLoop(
                llm=_loop_llm(loop_llm),
                execute_tool=loop_sandbox.execute_tool,
                verify_goal=lambda _goal, _history: loop_sandbox.verify(loop_challenge),
                authorize=loop_sandbox.authorize,
                tool_descriptions=loop_sandbox.tool_descriptions,
                max_steps=MAX_STEPS,
            )
        )
        failure = None
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            goal = challenge.goal
            if failure:
                goal += (
                    "\n\nYour last task failed with this error: "
                    + _sanitize_failure(failure)
                    + ". Analyze the error, write a fix, and try again."
                )
            cleanup_needed = True
            result = factory(call, sandbox, challenge).run(goal)
            history = tuple(result.history)
            verdict = sandbox.verify(challenge)
            evidence = verdict.evidence
            try:
                sandbox.stop_background_processes()
            except Exception:
                pass
            cleanup_needed = False
            if verdict.achieved:
                try:
                    draft = call(_draft_prompt(challenge, history, evidence))
                    skill = validate_skill_draft(draft)
                    quarantined = _write_quarantine(
                        Path(quarantine_root), challenge, skill, attempts,
                        evidence, history, image)
                except CurriculumError as exc:
                    return {
                        "status": "verified_skill_rejected",
                        "challenge": challenge.challenge_id,
                        "attempts": attempts,
                        "verification": evidence,
                        "quarantine": None,
                        "error": str(exc),
                    }
                return {
                    "status": "quarantined",
                    "challenge": challenge.challenge_id,
                    "attempts": attempts,
                    "verification": evidence,
                    "quarantine": str(quarantined),
                    "error": None,
                }
            failure = evidence
        return {
            "status": "failed",
            "challenge": challenge.challenge_id,
            "attempts": attempts,
            "verification": evidence,
            "quarantine": None,
            "error": _sanitize_failure(failure),
        }
    finally:
        try:
            if cleanup_needed:
                sandbox.stop_background_processes()
        except Exception:
            pass
        sandbox.stop()


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded, sandboxed self-directed curriculum challenge.")
    parser.add_argument("--image", default=DEFAULT_IMAGE,
                        help="Local Docker image; it is never pulled automatically")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--challenge", choices=sorted(CATALOG), default=None)
    parser.add_argument("--sandbox-root", type=Path, default=SANDBOX_ROOT)
    parser.add_argument("--quarantine-root", type=Path, default=QUARANTINE_ROOT)
    args = parser.parse_args(argv)
    try:
        result = run_curriculum(
            image=args.image, seed=args.seed, max_attempts=args.max_attempts,
            challenge_id=args.challenge, sandbox_root=args.sandbox_root,
            quarantine_root=args.quarantine_root)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "quarantined" else 1


if __name__ == "__main__":
    sys.exit(_cli())
