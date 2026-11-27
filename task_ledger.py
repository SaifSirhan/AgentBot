"""Persistent manual task ledger for Kestrel."""
import copy
import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

import config as _cfg


SCHEMA_VERSION = 1
STATUSES = frozenset({"planned", "active", "done"})
TASK_KEYS = frozenset({
    "id", "title", "status", "created_at", "updated_at", "completed_at",
})
MAX_TITLE_LENGTH = 200


class TaskLedgerError(Exception):
    """The saved ledger could not be read or validated safely."""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _valid_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _normalize_title(title):
    if not isinstance(title, str):
        raise ValueError("Task title must be text")
    normalized = " ".join(title.split())
    if not normalized or len(normalized) > MAX_TITLE_LENGTH:
        raise ValueError(
            f"Task title must be between 1 and {MAX_TITLE_LENGTH} characters"
        )
    return normalized


class TaskLedger:
    def __init__(self, path=None):
        self.path = Path(
            path if path is not None else _cfg.memory_path("task_ledger.json")
        )
        self._lock = threading.RLock()
        self._tasks = []
        self._focused_task_id = None
        self._load()

    @property
    def focused_task_id(self):
        with self._lock:
            return self._focused_task_id

    def list_tasks(self):
        with self._lock:
            return copy.deepcopy(self._tasks)

    def add_task(self, title):
        title = _normalize_title(title)
        now = _utc_now()
        task = {
            "id": uuid.uuid4().hex,
            "title": title,
            "status": "planned",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        with self._lock:
            self._commit(self._tasks + [task], self._focused_task_id)
        return copy.deepcopy(task)

    def focus_task(self, task_id):
        with self._lock:
            self._find(task_id)
            self._commit(self._tasks, task_id)
            return copy.deepcopy(self._find(task_id))

    def activate_task(self, task_id):
        now = _utc_now()
        with self._lock:
            task = self._find(task_id)
            if task["status"] != "planned":
                raise ValueError("Only planned tasks can be activated")
            tasks = []
            for current in self._tasks:
                updated = dict(current)
                if current["status"] == "active":
                    updated["status"] = "planned"
                    updated["updated_at"] = now
                elif current["id"] == task_id:
                    updated["status"] = "active"
                    updated["updated_at"] = now
                tasks.append(updated)
            self._commit(tasks, self._focused_task_id)
            return copy.deepcopy(self._find(task_id))

    def complete_task(self, task_id):
        now = _utc_now()
        with self._lock:
            task = self._find(task_id)
            if task["status"] == "done":
                raise ValueError("Task is already complete")
            tasks = []
            for current in self._tasks:
                updated = dict(current)
                if current["id"] == task_id:
                    updated["status"] = "done"
                    updated["updated_at"] = now
                    updated["completed_at"] = now
                tasks.append(updated)
            self._commit(tasks, self._focused_task_id)
            return copy.deepcopy(self._find(task_id))

    def reopen_task(self, task_id):
        now = _utc_now()
        with self._lock:
            task = self._find(task_id)
            if task["status"] != "done":
                raise ValueError("Only completed tasks can be reopened")
            tasks = []
            for current in self._tasks:
                updated = dict(current)
                if current["id"] == task_id:
                    updated["status"] = "planned"
                    updated["updated_at"] = now
                    updated["completed_at"] = None
                tasks.append(updated)
            self._commit(tasks, self._focused_task_id)
            return copy.deepcopy(self._find(task_id))

    def delete_task(self, task_id):
        with self._lock:
            self._find(task_id)
            tasks = [task for task in self._tasks if task["id"] != task_id]
            focused = self._focused_task_id
            if focused == task_id:
                focused = None
            self._commit(tasks, focused)

    def _find(self, task_id):
        for task in self._tasks:
            if task["id"] == task_id:
                return task
        raise KeyError(task_id)

    def _commit(self, tasks, focused_task_id):
        self._write(tasks, focused_task_id)
        self._tasks = tasks
        self._focused_task_id = focused_task_id

    def _load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as exc:
            raise TaskLedgerError(f"Could not read task ledger: {exc}") from exc

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TaskLedgerError("Task ledger contains invalid JSON") from exc

        self._tasks, self._focused_task_id = self._validate(payload)

    @staticmethod
    def _validate(payload):
        if not isinstance(payload, dict):
            raise TaskLedgerError("Task ledger must be a JSON object")
        if type(payload.get("version")) is not int or payload["version"] != SCHEMA_VERSION:
            raise TaskLedgerError("Task ledger has an unsupported schema version")
        tasks = payload.get("tasks")
        focused = payload.get("focused_task_id")
        if not isinstance(tasks, list):
            raise TaskLedgerError("Task ledger tasks must be a list")
        if focused is not None and not isinstance(focused, str):
            raise TaskLedgerError("Focused task ID must be text or null")

        validated = []
        seen_ids = set()
        active_count = 0
        for task in tasks:
            if not isinstance(task, dict) or set(task) != TASK_KEYS:
                raise TaskLedgerError("Task ledger contains an invalid task record")
            task_id = task["id"]
            title = task["title"]
            status = task["status"]
            completed_at = task["completed_at"]
            if not isinstance(task_id, str) or not task_id or task_id in seen_ids:
                raise TaskLedgerError("Task IDs must be unique non-empty text")
            if not isinstance(title, str) or not title or title != title.strip():
                raise TaskLedgerError("Task ledger contains an invalid task title")
            if len(title) > MAX_TITLE_LENGTH:
                raise TaskLedgerError("Task ledger contains an overlong task title")
            if not isinstance(status, str) or status not in STATUSES:
                raise TaskLedgerError("Task ledger contains an invalid task status")
            if not _valid_timestamp(task["created_at"]):
                raise TaskLedgerError("Task ledger contains an invalid creation time")
            if not _valid_timestamp(task["updated_at"]):
                raise TaskLedgerError("Task ledger contains an invalid update time")
            if status == "done":
                if not _valid_timestamp(completed_at):
                    raise TaskLedgerError("Completed tasks need a completion time")
            elif completed_at is not None:
                raise TaskLedgerError("Open tasks cannot have a completion time")
            if status == "active":
                active_count += 1
            seen_ids.add(task_id)
            validated.append(dict(task))

        if active_count > 1:
            raise TaskLedgerError("Task ledger cannot contain multiple active tasks")
        if focused is not None and focused not in seen_ids:
            raise TaskLedgerError("Task ledger focus refers to a missing task")
        return validated, focused

    def _write(self, tasks, focused_task_id):
        payload = {
            "version": SCHEMA_VERSION,
            "focused_task_id": focused_task_id,
            "tasks": tasks,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, indent=2
        ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
