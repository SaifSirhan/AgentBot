"""Copy legacy application data to Kestrel without removing the originals."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile


def _reject_link(path):
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise OSError(f"Refusing to follow a link or junction: {candidate}")


def _database_manifest(root):
    manifest = {}
    for path in root.rglob("*"):
        _reject_link(path)
        relative = path.relative_to(root)
        if path.is_dir():
            manifest[relative] = None
        else:
            with path.open("rb") as handle:
                manifest[relative] = hashlib.file_digest(handle, "sha256").digest()
    return manifest


def _copy_database(source, destination, counts):
    manifest = _database_manifest(source)
    files = sum(value is not None for value in manifest.values())
    if destination.exists():
        if not destination.is_dir() or _database_manifest(destination) != manifest:
            raise OSError("Refusing to merge different database snapshots; "
                          "keep both copies and choose one database explicitly")
        counts["skipped"] += files
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent,
                                     prefix=".kestrel-database-") as staging:
        snapshot = Path(staging) / "snapshot"
        shutil.copytree(source, snapshot)
        if _database_manifest(snapshot) != manifest:
            raise OSError("Database changed while copying; stop all database users")
        os.replace(snapshot, destination)
    counts["copied"] += files


def merge_tree(source, destination, counts):
    temporary = None
    try:
        _reject_link(source)
        if os.path.lexists(destination.parent):
            _reject_link(destination.parent)
        if os.path.lexists(destination):
            _reject_link(destination)
        if source.is_dir():
            if source.name == "rag_db" or (source / "chroma.sqlite3").is_file():
                _copy_database(source, destination, counts)
                return
            destination.mkdir(parents=True, exist_ok=True)
            for child in source.iterdir():
                merge_tree(child, destination / child.name, counts)
            return
        if not source.is_file():
            raise OSError(f"Not a regular file: {source}")
        if destination.exists():
            if not destination.is_file():
                raise OSError(f"Destination is not a file: {destination}")
            if destination.stat().st_mtime_ns >= source.stat().st_mtime_ns:
                counts["skipped"] += 1
                return
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent,
                                         prefix=".kestrel-migrate-", delete=False) as f:
            temporary = Path(f.name)
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        counts["copied"] += 1
    except OSError as exc:
        counts["errors"] += 1
        print(f"ERROR: {source} -> {destination}: {exc}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    print("Close AgentBot/Kestrel before copying, including any database users.")
    print("Original folders will be kept. Newer or equal destination files are kept.")
    counts = {"copied": 0, "skipped": 0, "errors": 0}
    roots = []
    for variable in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(variable)
        if not value:
            print(f"Skipping {variable}: not set.")
            continue
        base = Path(value)
        roots.append((base / "AgentBot", base / "Kestrel"))
        if variable == "LOCALAPPDATA":
            roots.append((base / "AgentMemory", base / "Kestrel" / "AgentMemory"))
    for source, destination in roots:
        if not os.path.lexists(source):
            print(f"Skipping missing source: {source}")
            continue
        before = counts["copied"]
        merge_tree(source, destination, counts)
        print(f"{source} -> {destination}: {counts['copied'] - before} files copied")
    print(f"Summary: {counts['copied']} files copied, "
          f"{counts['skipped']} skipped, {counts['errors']} errors.")
    print("Explicit AGENT_MEMORY_DIR and WAKE_WORD_MODEL settings are unchanged.")
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
