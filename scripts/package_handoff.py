"""Build a self-contained knowledge-service release without private/community data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git"),
    )


def prepare_data(source_db: Path, release_root: Path) -> int:
    data_root = release_root / "data"
    raw_root = data_root / "raw"
    raw_root.mkdir(parents=True)
    snapshot = data_root / "knowledge.db"
    with sqlite3.connect(source_db) as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("DELETE FROM documents WHERE json_extract(data,'$.authority')='community'")
        target.execute("DELETE FROM documents WHERE id LIKE 'acceptance-%'")
        target.execute("DELETE FROM traces")
        target.execute("DELETE FROM jobs")
        target.execute("DELETE FROM settings")
        for key, raw in target.execute("SELECT id,data FROM documents").fetchall():
            document = json.loads(raw)
            raw_key = str(document.get("rawObjectKey") or "")
            if raw_key:
                original = Path(raw_key)
                if not original.is_file():
                    raise FileNotFoundError(f"raw source is missing: {original}")
                destination = raw_root / original.name
                shutil.copy2(original, destination)
                document["rawObjectKey"] = f"data/raw/{original.name}"
                target.execute(
                    "UPDATE documents SET data=? WHERE id=?",
                    (json.dumps(document, ensure_ascii=False), key),
                )
        target.commit()
        published = int(
            target.execute(
                "SELECT count(*) FROM documents WHERE status='published'"
            ).fetchone()[0]
        )
        target.execute("VACUUM")
    return published


def file_manifest(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "HANDOFF.json":
            continue
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--version")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = args.version or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with tempfile.TemporaryDirectory(prefix="zhixing-knowledge-release-") as temporary:
        release_root = Path(temporary) / "zhixing-knowledge"
        current = release_root / "current"
        current.mkdir(parents=True)
        copy_tree(repo / "knowledge_service", current / "knowledge_service")
        copy_tree(repo / "deploy", current / "deploy")
        copy_tree(repo / "docs", current / "docs")
        copy_tree(repo / "scripts", current / "scripts")
        shutil.copy2(repo / ".env.example", current / ".env.example")
        shutil.copy2(repo / "README.md", release_root / "README.md")

        published = prepare_data(Path(args.db).resolve(), release_root)
        handoff = {
            "scope": "knowledge service; credentials, traces, jobs and community corpus excluded",
            "version": version,
            "publishedDocuments": published,
            "files": file_manifest(release_root),
        }
        (release_root / "HANDOFF.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        archive_path = output / f"zhixing-knowledge-{version}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(release_root, arcname=release_root.name)

    checksum = digest(archive_path)
    sidecar = archive_path.with_suffix(archive_path.suffix + ".sha256")
    sidecar.write_text(f"{checksum}  {archive_path.name}\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive_path), "sha256": checksum}, ensure_ascii=False))


if __name__ == "__main__":
    main()
