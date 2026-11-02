#!/usr/bin/env python3
"""Move AgentBot's memory folder to a dedicated drive.

Copies %LOCALAPPDATA%\\AgentMemory plus the Chroma/FTS index from
%APPDATA%\\AgentBot, verifies every file, writes AGENT_MEMORY_DIR into the
config, and only then removes the originals. Close AgentBot first: open
SQLite/Chroma files can block the copy.

Run:
    python migrate_memory.py D:\\AgentBrain
    python migrate_memory.py D:\\AgentBrain --dry-run
    python migrate_memory.py D:\\AgentBrain --keep-source --yes

The destination is the memory folder itself (not its parent).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import config

AGENT_PROCESS_HINTS = ("agent_gui_qt.py", "launch_agent.py")


class MigrationError(RuntimeError):
    pass


def _tree_sizes(root):
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): p.stat().st_size
        for p in root.rglob("*") if p.is_file()
    }


def _tree_bytes(root):
    return sum(_tree_sizes(root).values())


def _is_empty_tree(root):
    return not any(p.is_file() for p in Path(root).rglob("*"))


def _fmt_bytes(n):
    return f"{n / (1024 ** 2):.1f} MB"


def _verify(src, dst):
    src_files, dst_files = _tree_sizes(src), _tree_sizes(dst)
    missing = sorted(set(src_files) - set(dst_files))
    wrong = sorted(
        k for k in src_files if k in dst_files and src_files[k] != dst_files[k])
    if missing or wrong:
        raise MigrationError(
            f"copy verification failed: {len(missing)} missing, "
            f"{len(wrong)} size mismatches")
    return len(src_files)


def _free_bytes(path):
    probe = Path(path)
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _rag_locations():
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    base = Path(appdata) / "AgentBot"
    return base / "rag_db", base / "rag_state.json"


def _validate(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise MigrationError("source and destination are the same folder")
    if destination.is_relative_to(source):
        raise MigrationError("destination is inside the source folder")
    if source.is_relative_to(destination):
        raise MigrationError("source is inside the destination folder")
    if destination.exists():
        entries = list(destination.iterdir())
        # AgentBot may have already created an empty rag_db on first start.
        only_empty_rag = (
            len(entries) == 1
            and entries[0].name == "rag_db"
            and entries[0].is_dir()
            and _is_empty_tree(entries[0]))
        if entries and not only_empty_rag:
            raise MigrationError(f"destination is not empty: {destination}")
    return source, destination


def _set_memory_dir(destination, config_path=None):
    path = Path(config_path or config.CONFIG_PATH)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise MigrationError(f"cannot parse {path}: {e}") from e
        if not isinstance(data, dict):
            raise MigrationError(f"{path} does not contain a JSON object")
    data["AGENT_MEMORY_DIR"] = str(destination)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        raise MigrationError(f"cannot write {path}: {e}") from e


def migrate(destination, source=None, rag_dir=None, rag_state=None,
            keep_source=False, dry_run=False, set_config=True,
            config_path=None):
    """Copy, verify, retarget, then delete. Returns a report as str lines."""
    source, destination = _validate(
        source or config.DEFAULT_MEMORY_DIR, destination)
    rag_dir = Path(rag_dir) if rag_dir is not None else _rag_locations()[0]
    rag_state = Path(rag_state) if rag_state is not None else _rag_locations()[1]

    lines = []
    needed = 0
    if source.exists():
        needed += _tree_bytes(source)
        lines.append(f"source: {source} ({_fmt_bytes(needed)})")
    else:
        lines.append(f"source: {source} (missing, nothing to copy)")
    if rag_dir.exists():
        rag_bytes = _tree_bytes(rag_dir)
        needed += rag_bytes
        lines.append(f"rag index: {rag_dir} ({_fmt_bytes(rag_bytes)})")
    lines.append(f"destination: {destination}")

    free = _free_bytes(destination)
    if needed > free:
        raise MigrationError(
            f"not enough space: need {_fmt_bytes(needed)}, "
            f"destination drive has {_fmt_bytes(free)}")
    if dry_run:
        lines.append("dry run: no files were changed")
        return lines

    try:
        destination.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copytree(source, destination, dirs_exist_ok=True)
            lines.append(
                f"copied {_verify(source, destination)} files to {destination}")
    except MigrationError:
        raise
    except Exception as e:
        raise MigrationError(f"copy failed, nothing was deleted: {e}") from e

    rag_copied = False
    dest_rag = destination / "rag_db"
    if rag_dir.exists():
        if dest_rag.exists() and not _is_empty_tree(dest_rag):
            lines.append(
                f"note: {dest_rag} already exists; left the old index in place")
        else:
            if dest_rag.exists():
                shutil.rmtree(dest_rag)
            try:
                shutil.copytree(rag_dir, dest_rag)
                _verify(rag_dir, dest_rag)
                rag_copied = True
                lines.append(f"copied the RAG index to {dest_rag}")
            except MigrationError:
                raise
            except Exception as e:
                raise MigrationError(
                    f"RAG copy failed, nothing was deleted: {e}") from e

    rag_state_copied = False
    if rag_state.exists():
        try:
            shutil.copy2(rag_state, destination / rag_state.name)
            rag_state_copied = True
            lines.append(f"copied {rag_state.name}")
        except OSError as e:
            raise MigrationError(
                f"RAG state copy failed, nothing was deleted: {e}") from e

    if set_config:
        _set_memory_dir(destination, config_path)
        lines.append(f"AGENT_MEMORY_DIR = {destination}")

    if not keep_source:
        if source.exists():
            try:
                shutil.rmtree(source)
                lines.append(f"removed {source}")
            except OSError as e:
                lines.append(f"warning: could not fully remove {source}: {e}")
        if rag_copied:
            try:
                shutil.rmtree(rag_dir)
                lines.append(f"removed {rag_dir}")
            except OSError as e:
                lines.append(f"warning: could not fully remove {rag_dir}: {e}")
        if rag_state_copied:
            try:
                rag_state.unlink()
                lines.append(f"removed {rag_state}")
            except OSError as e:
                lines.append(f"warning: could not remove {rag_state}: {e}")

    return lines


def _running_agent():
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if any(hint in cmd for hint in AGENT_PROCESS_HINTS):
            return f"pid {proc.pid}: {cmd[:120]}"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help=r"new memory folder, e.g. D:\AgentBrain")
    parser.add_argument("--source", help="override the current memory folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="report sizes and plan, change nothing")
    parser.add_argument("--keep-source", action="store_true",
                        help="copy but leave the originals in place")
    parser.add_argument("--no-set-config", action="store_true",
                        help="do not write AGENT_MEMORY_DIR")
    parser.add_argument("--yes", action="store_true", help="skip the prompt")
    parser.add_argument("--force", action="store_true",
                        help="run even if AgentBot appears to be running")
    args = parser.parse_args(argv)

    if not args.force:
        running = _running_agent()
        if running:
            print(f"ERROR: AgentBot is running ({running}).")
            print("Close the GUI and Telegram bridge, then run again "
                  "(or pass --force).")
            return 1

    if not args.yes and not args.dry_run:
        answer = input(f"Move memory to {args.destination}? Type yes: ")
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    try:
        report = migrate(
            args.destination,
            source=args.source,
            keep_source=args.keep_source,
            dry_run=args.dry_run,
            set_config=not args.no_set_config,
        )
    except MigrationError as e:
        print(f"ERROR: {e}")
        return 1

    for line in report:
        print(line)
    if not args.dry_run:
        print("\nRestart AgentBot so the new AGENT_MEMORY_DIR takes effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
