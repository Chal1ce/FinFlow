"""Atomic files and content identities shared by the daily pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def safe_path(root, relative):
    path = (Path(root) / relative).resolve()
    path.relative_to(Path(root).resolve())
    return path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def register_file(store, context, path, kind, parents=(), **metadata):
    from core.ids import artifact_uid

    path = Path(path)
    content_hash = sha256(path)
    uid = artifact_uid(kind, content_hash, *parents, metadata.get("identity", ""))
    store.upsert_artifact(
        context,
        {
            "artifact_uid": uid,
            "artifact_type": kind,
            "path": context.relative_path(path),
            "sha256": content_hash,
            "status": "success",
            "parent_artifact_uid": next(iter(parents), None),
            "document_uid": metadata.get("document_uid"),
            "metadata": metadata,
        },
    )
    for parent in parents:
        store.edge(uid, parent)
    return uid
