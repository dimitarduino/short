from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = ROOT / "data" / "library"
INDEX_PATH = LIBRARY_DIR / "index.json"

_lock = threading.Lock()


def list_items() -> list[dict[str, Any]]:
    with _lock:
        items = _load_index()
    return items


def get_item(item_id: str) -> dict[str, Any] | None:
    with _lock:
        for item in _load_index():
            if item.get("id") == item_id:
                return item
    return None


def video_path(item_id: str) -> Path | None:
    path = LIBRARY_DIR / item_id / "short.mp4"
    return path if path.exists() else None


def save_from_job(
    *,
    job_id: str,
    video_path_src: str,
    title: str,
    source_url: str,
    video_id: str | None,
    clip_start: float | None,
    clip_end: float | None,
    captions_text: str = "",
    music_id: str = "none",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    src = Path(video_path_src)
    if not src.exists():
        raise FileNotFoundError("Short video missing — cannot save to library.")

    item_id = uuid.uuid4().hex[:12]
    folder = LIBRARY_DIR / item_id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / "short.mp4"
    shutil.copy2(src, dest)

    meta = {
        "title": title or "Short",
        "description": "",
        "tags": [],
        "category_id": "24",
        "x_text": "",
        "tiktok_caption": "",
        **(metadata or {}),
    }
    item = {
        "id": item_id,
        "job_id": job_id,
        "title": meta.get("title") or title or "Short",
        "source_url": source_url,
        "video_id": video_id,
        "clip_start": clip_start,
        "clip_end": clip_end,
        "captions_text": captions_text,
        "music_id": music_id,
        "metadata": meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "has_video": True,
    }
    (folder / "meta.json").write_text(json.dumps(item, indent=2), encoding="utf-8")

    with _lock:
        items = _load_index()
        items.insert(0, item)
        _save_index(items)
    return item


def update_metadata(item_id: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        items = _load_index()
        for item in items:
            if item.get("id") != item_id:
                continue
            merged = dict(item.get("metadata") or {})
            merged.update({k: v for k, v in metadata.items() if v is not None})
            item["metadata"] = merged
            item["title"] = merged.get("title") or item.get("title")
            folder = LIBRARY_DIR / item_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "meta.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
            _save_index(items)
            return item
    return None


def delete_item(item_id: str) -> bool:
    with _lock:
        items = _load_index()
        new_items = [i for i in items if i.get("id") != item_id]
        if len(new_items) == len(items):
            return False
        _save_index(new_items)
    folder = LIBRARY_DIR / item_id
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return True


def _load_index() -> list[dict[str, Any]]:
    if not INDEX_PATH.exists():
        return []
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_index(items: list[dict[str, Any]]) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")
