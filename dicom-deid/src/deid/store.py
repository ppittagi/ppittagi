"""Persistence: SQLite stand-ins for the ``deid`` and ``reid`` schemas.

``deid.sqlite`` holds templates, jobs, results and the audit trail.
``reid.sqlite`` holds the re-identification keys (subject and UID mappings)
and lives in its own directory so it can be permissioned separately.
The table layout mirrors the PostgreSQL design so the move is mechanical.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEID_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_config (
  study_id              TEXT PRIMARY KEY,
  date_mode             TEXT NOT NULL DEFAULT 'SHIFT' CHECK (date_mode IN ('SHIFT','KEEP','REMOVE')),
  retain_original_days  INTEGER NOT NULL DEFAULT 30,
  created_at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS redaction_template (
  id          INTEGER PRIMARY KEY,
  code        TEXT NOT NULL,
  name        TEXT NOT NULL,
  study_key   TEXT NOT NULL,             -- study id, or '_global' for shared templates
  kind        TEXT NOT NULL CHECK (kind IN ('REDACT','PASS','REJECT')),
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE (code, study_key)
);
CREATE TABLE IF NOT EXISTS template_version (
  id               INTEGER PRIMARY KEY,
  template_id      INTEGER NOT NULL REFERENCES redaction_template(id),
  version          INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','IN_REVIEW','APPROVED','RETIRED')),
  spec_json        TEXT NOT NULL,
  content_sha256   TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  submitted_at     TEXT,
  approved_by      TEXT,
  approved_at      TEXT,
  approval_meaning TEXT,
  UNIQUE (template_id, version),
  CHECK (status NOT IN ('APPROVED','RETIRED') OR (approved_by IS NOT NULL AND approved_by <> created_by))
);
CREATE TRIGGER IF NOT EXISTS template_version_frozen
BEFORE UPDATE ON template_version
WHEN OLD.status IN ('APPROVED','RETIRED')
 AND (NEW.spec_json <> OLD.spec_json OR NEW.content_sha256 <> OLD.content_sha256
      OR NOT (NEW.status = OLD.status OR (OLD.status = 'APPROVED' AND NEW.status = 'RETIRED')))
BEGIN SELECT RAISE(ABORT, 'approved template versions are immutable'); END;
CREATE TABLE IF NOT EXISTS template_sample (
  id                INTEGER PRIMARY KEY,
  version_id        INTEGER NOT NULL REFERENCES template_version(id),
  sample_sha256     TEXT NOT NULL,
  matched           INTEGER NOT NULL,
  trace_json        TEXT NOT NULL,
  preview_before    TEXT,
  preview_after     TEXT,
  created_by        TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  UNIQUE (version_id, sample_sha256)
);
CREATE TABLE IF NOT EXISTS processing_job (
  id            INTEGER PRIMARY KEY,
  study_id      TEXT NOT NULL,
  site_id       TEXT NOT NULL,
  subject_no    TEXT NOT NULL,
  uploaded_by   TEXT NOT NULL,
  source_name   TEXT NOT NULL,
  status        TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  finished_at   TEXT
);
CREATE TABLE IF NOT EXISTS job_item (
  id                   INTEGER PRIMARY KEY,
  job_id               INTEGER NOT NULL REFERENCES processing_job(id),
  source_path          TEXT NOT NULL,
  original_sha256      TEXT NOT NULL,
  sop_class_uid        TEXT,
  transfer_syntax      TEXT,
  status               TEXT NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING','PROCESSING','DONE','QUARANTINED','REJECTED','FAILED')),
  reason_code          TEXT,
  template_version_id  INTEGER REFERENCES template_version(id),
  match_trace_json     TEXT,
  deid_path            TEXT,
  deid_sha256          TEXT,
  preview_before       TEXT,
  preview_after        TEXT,
  attempts             INTEGER NOT NULL DEFAULT 0,
  processed_at         TEXT
);
CREATE INDEX IF NOT EXISTS job_item_job_status ON job_item (job_id, status);
CREATE TABLE IF NOT EXISTS quarantine_review (
  id                   INTEGER PRIMARY KEY,
  item_id              INTEGER NOT NULL REFERENCES job_item(id),
  action               TEXT NOT NULL CHECK (action IN ('REPROCESS','REJECT')),
  template_version_id  INTEGER REFERENCES template_version(id),
  reviewer             TEXT NOT NULL,
  comment              TEXT NOT NULL,
  reviewed_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY,
  occurred_at  TEXT NOT NULL,
  actor        TEXT NOT NULL,
  action       TEXT NOT NULL,
  entity_type  TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  before_json  TEXT,
  after_json   TEXT,
  reason       TEXT,
  prev_hash    TEXT NOT NULL,
  row_hash     TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

REID_SCHEMA = """
CREATE TABLE IF NOT EXISTS subject_mapping (
  study_id      TEXT NOT NULL,
  patient_hmac  TEXT NOT NULL,     -- HMAC-SHA256 of the original PatientID; the plain value is never stored
  subject_no    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (study_id, patient_hmac)
);
CREATE TABLE IF NOT EXISTS subject_offset (
  study_id          TEXT NOT NULL,
  subject_no        TEXT NOT NULL,
  date_offset_days  INTEGER NOT NULL CHECK (date_offset_days BETWEEN 1 AND 365),
  PRIMARY KEY (study_id, subject_no)
);
CREATE TABLE IF NOT EXISTS uid_mapping (
  study_id      TEXT NOT NULL,
  original_uid  TEXT NOT NULL,
  new_uid       TEXT NOT NULL UNIQUE,
  PRIMARY KEY (study_id, original_uid)
);
"""

GENESIS_HASH = "0" * 64


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def new_uid() -> str:
    """A 2.25 UID derived from a random UUID (no organisation root needed)."""
    return f"2.25.{int.from_bytes(secrets.token_bytes(16), 'big')}"


class SubjectConflict(Exception):
    pass


class Store:
    """All database access. Safe to share between the API and the worker thread."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.deid_dir = self.data_dir / "deid"
        reid_dir = self.data_dir / "reid"
        for d in (self.raw_dir, self.deid_dir, reid_dir):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(reid_dir, 0o700)
        self._lock = threading.RLock()
        self.db = _connect(self.data_dir / "deid.sqlite")
        self.db.executescript(DEID_SCHEMA)
        self.reid = _connect(reid_dir / "reid.sqlite")
        self.reid.executescript(REID_SCHEMA)
        self._hmac_key = self._load_hmac_key(reid_dir / "hmac.key")

    @staticmethod
    def _load_hmac_key(path: Path) -> bytes:
        env = os.environ.get("DEID_HMAC_KEY")
        if env:
            return env.encode()
        if not path.exists():
            path.write_bytes(secrets.token_bytes(32))
            os.chmod(path, 0o600)
        return path.read_bytes()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- audit trail -----------------------------------------------------------

    def audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str, entity_id: Any,
              before: Any = None, after: Any = None, reason: str | None = None) -> None:
        """Append one hash-chained audit row inside the caller's transaction."""
        prev = conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = prev[0] if prev else GENESIS_HASH
        row = {
            "occurred_at": now(), "actor": actor, "action": action, "entity_type": entity_type,
            "entity_id": str(entity_id),
            "before_json": None if before is None else json.dumps(before, ensure_ascii=False, sort_keys=True),
            "after_json": None if after is None else json.dumps(after, ensure_ascii=False, sort_keys=True),
            "reason": reason,
        }
        row_hash = hashlib.sha256((prev_hash + json.dumps(row, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        conn.execute(
            "INSERT INTO audit_log (occurred_at, actor, action, entity_type, entity_id, before_json, after_json, reason, prev_hash, row_hash)"
            " VALUES (:occurred_at, :actor, :action, :entity_type, :entity_id, :before_json, :after_json, :reason, :prev_hash, :row_hash)",
            {**row, "prev_hash": prev_hash, "row_hash": row_hash},
        )

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        """Recompute the hash chain; returns (ok, first broken row id)."""
        prev_hash = GENESIS_HASH
        for r in self.query("SELECT * FROM audit_log ORDER BY id"):
            row = {k: r[k] for k in ("occurred_at", "actor", "action", "entity_type", "entity_id", "before_json", "after_json", "reason")}
            expected = hashlib.sha256((prev_hash + json.dumps(row, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
            if r["prev_hash"] != prev_hash or r["row_hash"] != expected:
                return False, r["id"]
            prev_hash = r["row_hash"]
        return True, None

    # --- re-identification keys --------------------------------------------------

    def patient_hmac(self, study_id: str, patient_id: str) -> str:
        return hmac.new(self._hmac_key, f"{study_id}\x00{patient_id}".encode(), hashlib.sha256).hexdigest()

    def link_subject(self, study_id: str, patient_id: str | None, subject_no: str) -> int:
        """Bind a PatientID to a subject and return the subject's date offset.

        Raises SubjectConflict if the PatientID already belongs to another subject.
        A file without a PatientID gets the subject's offset without a binding.
        """
        key = self.patient_hmac(study_id, patient_id) if patient_id else None
        with self._lock:
            self.reid.execute("BEGIN IMMEDIATE")
            try:
                row = key and self.reid.execute(
                    "SELECT subject_no FROM subject_mapping WHERE study_id = ? AND patient_hmac = ?", (study_id, key)
                ).fetchone()
                if row and row[0] != subject_no:
                    raise SubjectConflict(subject_no)
                if key and not row:
                    self.reid.execute(
                        "INSERT INTO subject_mapping VALUES (?, ?, ?, ?)", (study_id, key, subject_no, now())
                    )
                off = self.reid.execute(
                    "SELECT date_offset_days FROM subject_offset WHERE study_id = ? AND subject_no = ?", (study_id, subject_no)
                ).fetchone()
                if off is None:
                    offset = secrets.randbelow(365) + 1
                    self.reid.execute("INSERT INTO subject_offset VALUES (?, ?, ?)", (study_id, subject_no, offset))
                else:
                    offset = off[0]
                self.reid.execute("COMMIT")
                return offset
            except BaseException:
                self.reid.execute("ROLLBACK")
                raise

    def map_uid(self, study_id: str, original: str) -> str:
        """Stable original -> new UID mapping per study."""
        with self._lock:
            row = self.reid.execute(
                "SELECT new_uid FROM uid_mapping WHERE study_id = ? AND original_uid = ?", (study_id, original)
            ).fetchone()
            if row:
                return row[0]
            uid = new_uid()
            self.reid.execute("INSERT INTO uid_mapping VALUES (?, ?, ?)", (study_id, original, uid))
            return uid

    # --- study configuration -----------------------------------------------------

    def study_config(self, study_id: str) -> dict:
        row = self.one("SELECT * FROM study_config WHERE study_id = ?", (study_id,))
        if row:
            return row
        with self.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO study_config (study_id, created_at) VALUES (?, ?)", (study_id, now()))
        return self.one("SELECT * FROM study_config WHERE study_id = ?", (study_id,))
