"""Canonical knowledge storage; the application account database is not modified."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def raw_object_path(raw_key: str, raw_root: str | None = None) -> Path | None:
    """Locate a captured original whichever way its key was recorded.

    Live ingest stores an absolute path, while the handoff snapshot rewrites the
    same field to ``data/raw/<name>`` so a corpus can move between hosts. A
    relative key resolves against the process working directory, which under
    systemd is ``current/`` rather than the ``data/`` directory beside it, so
    reading it verbatim misses every captured file. Captures are flat inside the
    raw directory, so the file name is the only part that identifies one; using
    just the name also keeps a crafted key from escaping the root.
    """
    name = Path(str(raw_key or "").strip()).name
    root = str(raw_root if raw_root is not None else os.environ.get("KNOWLEDGE_RAW_DIR", "")).strip()
    if not name or not root:
        return None
    path = Path(root).resolve() / name
    return path if path.is_file() else None


class KnowledgeStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, content_hash TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS documents_source ON documents(source_id);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, data TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id, ordinal);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL,
                    error TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS traces (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL
                );
            """)

    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def put_document(self, doc: dict, chunks: list[dict]) -> None:
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)", (
                doc["id"], doc["sourceId"], doc["status"], doc["contentHash"],
                json.dumps(doc, ensure_ascii=False),
            ))
            con.execute("DELETE FROM chunks WHERE document_id=?", (doc["id"],))
            con.executemany("INSERT INTO chunks VALUES(?,?,?,?)", [
                (c["id"], doc["id"], c["ordinal"], json.dumps(c, ensure_ascii=False)) for c in chunks
            ])
            if doc.get('supersedesId'):
                previous=con.execute('SELECT data FROM documents WHERE id=?',(doc['supersedesId'],)).fetchone()
                if previous is None or doc['supersedesId']==doc['id']:
                    raise ValueError('被替代文档必须存在且不能是文档自身')
                old=json.loads(previous[0]);old['status']='archived'
                con.execute('UPDATE documents SET status=?,data=? WHERE id=?',
                    ('archived',json.dumps(old,ensure_ascii=False),old['id']))

    def documents(self, *, published_only=False) -> list[dict]:
        query = "SELECT data FROM documents"
        if published_only:
            query += " WHERE status='published'"
        with self.connect() as con:
            return [json.loads(r[0]) for r in con.execute(query)]

    def chunks(self, *, published_only=True) -> list[dict]:
        query = "SELECT c.data FROM chunks c JOIN documents d ON d.id=c.document_id"
        if published_only:
            query += " WHERE d.status='published'"
        with self.connect() as con:
            return [json.loads(r[0]) for r in con.execute(query)]

    def get_document(self, doc_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT data FROM documents WHERE id=?", (doc_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_document_content(self, doc_id: str) -> dict | None:
        """Return the complete source document used for grounded generation.

        Captured originals are kept outside SQLite.  When available, reread the
        original file from the server-owned raw directory so a parser update or
        a repaired canonical row is reflected in agent context.  The endpoint
        never accepts a caller-provided path and falls back to the stored
        canonical body when the original is unavailable.
        """
        with self.connect() as con:
            row = con.execute(
                "SELECT data FROM documents WHERE id=? AND status='published'",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        document = json.loads(row[0])
        content = self._read_original_content(document) or document.get("content", "")
        return {
            "id": document.get("id", ""),
            "documentId": document.get("id", ""),
            "title": document.get("title", ""),
            "content": content,
            "sourceUrl": document.get("sourceUrl", ""),
            "publishedAt": document.get("publishedAt"),
            "updatedAt": document.get("updatedAt", ""),
        }

    @staticmethod
    def _read_original_content(document: dict) -> str:
        path = raw_object_path(document.get("rawObjectKey", ""))
        if path is None:
            return ""
        try:
            # Keep one malformed or unexpectedly large capture from exhausting
            # a worker while dozens of requests are reading source documents.
            if path.stat().st_size > 8 * 1024 * 1024:
                return ""
            payload = path.read_bytes()
            source_url = str(document.get("sourceUrl", ""))
            suffix = Path(urlsplit(source_url).path).suffix.lower()
            if suffix == ".pdf" or payload[:5] == b"%PDF-":
                from pypdf import PdfReader
                from io import BytesIO
                reader = PdfReader(BytesIO(payload))
                return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
            if suffix in {".docx", ".xlsx", ".doc", ".xls"}:
                from .attachments import office_text
                return office_text(payload, suffix).strip()
            from .html_body import parse_html
            _title, text, _date, _pages = parse_html(payload, source_url)
            return str(text or "").strip()
        except Exception:
            return ""

    def get_chunks(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self.connect() as con:
            rows = con.execute(f"SELECT c.data FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id IN ({marks}) AND d.status='published'", ids)
            found = {r["id"]: r for r in (json.loads(row[0]) for row in rows)}
        return [found[i] for i in ids if i in found]

    def neighbors(self, document_id: str, ordinal: int) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT c.data FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.document_id=? AND c.ordinal BETWEEN ? AND ? AND d.status='published' ORDER BY c.ordinal", (document_id, ordinal-1, ordinal+1))
            return [json.loads(r[0]) for r in rows]

    def set_status(self, doc_id: str, status: str) -> None:
        doc = self.get_document(doc_id)
        if doc is None:
            raise ValueError("知识文档不存在")
        doc["status"] = status
        with self.connect() as con:
            con.execute("UPDATE documents SET status=?,data=? WHERE id=?", (status, json.dumps(doc, ensure_ascii=False), doc_id))

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def setting(self, key: str) -> Any:
        with self.connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def job(self, url: str, status: str, error: str = "") -> None:
        with self.connect() as con:
            con.execute("INSERT INTO jobs(url,status,error,created_at) VALUES(?,?,?,?)", (url,status,error,utcnow()))

    def trace(self, trace_id: str, data: dict) -> None:
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO traces VALUES(?,?,?)", (trace_id,utcnow(),json.dumps(data,ensure_ascii=False)))

    def get_trace(self, trace_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT data FROM traces WHERE id=?", (trace_id,)).fetchone()
        return json.loads(row[0]) if row else None
