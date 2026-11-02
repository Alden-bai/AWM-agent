"""Canonical artifacts, SQLite snapshots and append-only evidence."""
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x") as f:
        f.write(canonical(value) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def now():
    return datetime.now(timezone.utc).isoformat()


def db_check(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("database_integrity_failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("database_foreign_keys_failed")


def db_fingerprint(path):
    """Logical state, including schema and sorted rows (independent of page layout)."""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {}
        for kind, name, _, _ in schema:
            if kind == "table":
                quoted = '"' + name.replace('"', '""') + '"'
                data = [[{"blob": v.hex()} if isinstance(v, bytes) else v for v in row]
                        for row in db.execute(f"SELECT * FROM {quoted}")]
                tables[name] = sorted(data, key=canonical)
        return digest({"schema": schema, "tables": tables})


def snapshot(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or target.exists():
        raise ValueError("snapshot_destination_must_be_new")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
    db_check(target)
    return file_hash(target)


class Recorder:
    def __init__(self, folder, rollout_id, attempt_id="a0"):
        self.folder = Path(folder)
        self.path = self.folder / "events.jsonl"
        self.rollout_id, self.attempt_id = rollout_id, attempt_id
        self.seq, self.previous = 0, None
        if self.path.exists():
            raise ValueError("attempt_exists; use a new attempt, never replay uncertain writes")

    def blob(self, value, visibility):
        content = canonical(value)
        h = hashlib.sha256(content.encode()).hexdigest()
        path = self.folder / "blobs" / visibility / (h + ".json")
        if not path.exists():
            write_json(path, value)
        return {"path": str(path.relative_to(self.folder)), "sha256": file_hash(path), "visibility": visibility}

    def emit(self, event_type, visibility="audit_private", turn_id=None, **data):
        self.seq += 1
        event = {"event_id": uuid.uuid4().hex, "parent_event_id": self.previous,
                 "rollout_id": self.rollout_id, "attempt_id": self.attempt_id,
                 "seq": self.seq, "event_type": event_type, "time": now(),
                 "turn_id": turn_id, "visibility": visibility, **data}
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(canonical(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.previous = event["event_id"]
        return event["event_id"]


def seal(folder):
    folder = Path(folder)
    paths = [p for p in folder.rglob("*") if p.is_file() and
             p.relative_to(folder).parts[0] in {"initial.db", "final.db", "events.jsonl", "trajectory.json", "model_turns", "blobs", "task.json", "audit", "tool_catalog.json"}]
    result = {str(p.relative_to(folder)): file_hash(p) for p in sorted(paths)}
    write_json(folder / "evidence_seal.json", result)
    return result


def verify_seal(folder):
    folder = Path(folder)
    hashes = read_json(folder / "evidence_seal.json")
    for name, expected in hashes.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or file_hash(path) != expected:
            raise ValueError("evidence_hash_mismatch: " + name)
    return hashes

