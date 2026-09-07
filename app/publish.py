from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCHEDULE_PATH = DATA_DIR / "scheduled.json"
YT_TOKEN_PATH = DATA_DIR / "youtube_token.json"

_lock = threading.Lock()
_scheduler_started = False


def connections() -> dict[str, Any]:
    from app.oauth_tiktok import tt_status
    from app.oauth_x import x_configured, x_connected

    yt_configured = bool(os.getenv("YOUTUBE_CLIENT_ID") and os.getenv("YOUTUBE_CLIENT_SECRET"))
    return {
        "youtube": {
            "configured": yt_configured,
            "connected": YT_TOKEN_PATH.exists(),
            "mode": "oauth",
            "label": "Upload & schedule privately",
        },
        "x": {
            "configured": x_configured(),
            "connected": x_connected(),
            "mode": "oauth",
            "label": "OAuth · post video via X API",
        },
        "tiktok": tt_status(),
    }


def list_scheduled() -> list[dict[str, Any]]:
    return _load()


def create_schedule(
    *,
    job_id: str,
    video_path: str,
    platforms: list[str],
    publish_at: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    when = _parse_when(publish_at)
    if when <= datetime.now(timezone.utc):
        raise ValueError("Pick a time in the future. Posts are scheduled, not published now.")
    allowed = {"youtube", "x", "tiktok"}
    platforms = [p for p in platforms if p in allowed]
    if not platforms:
        raise ValueError("Choose at least one platform.")
    if not Path(video_path).exists():
        raise ValueError("The short file is missing.")

    status = connections()
    for platform in platforms:
        info = status.get(platform) or {}
        if not info.get("connected"):
            label = {"youtube": "YouTube", "x": "X", "tiktok": "TikTok"}[platform]
            raise ValueError(f"Connect {label} with OAuth first (start screen).")

    item = {
        "id": uuid.uuid4().hex[:12],
        "job_id": job_id,
        "video_path": video_path,
        "platforms": platforms,
        "publish_at": when.isoformat(),
        "metadata": metadata,
        "status": "scheduled",
        "results": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        items = _load()
        items.insert(0, item)
        _save(items)

    # YouTube: upload now as private + native publishAt (does not go live yet).
    if "youtube" in platforms:
        try:
            result = upload_youtube_scheduled(video_path, metadata, when)
            _patch(item["id"], results={"youtube": result})
            item["results"]["youtube"] = result
        except Exception as exc:
            _patch(item["id"], results={"youtube": {"ok": False, "error": str(exc)}})
            item["results"]["youtube"] = {"ok": False, "error": str(exc)}

    # TikTok inbox has no schedule API — upload the draft immediately (like FB3).
    if "tiktok" in platforms:
        try:
            result = _post_tiktok(video_path, metadata)
            _patch(item["id"], results={"tiktok": result})
            item["results"]["tiktok"] = result
        except Exception as exc:
            err = {"ok": False, "error": str(exc)}
            _patch(item["id"], results={"tiktok": err})
            item["results"]["tiktok"] = err

    # X: queued for API post at publish_at (scheduler).
    if "x" in platforms:
        note = {
            "ok": False,
            "queued": True,
            "note": "Will post via X API at the scheduled time.",
        }
        _patch(item["id"], results={"x": note})
        item["results"]["x"] = note

    # If nothing left queued, mark posted when only immediate platforms ran.
    still_queued = any(
        (item["results"].get(p) or {}).get("queued") for p in item["platforms"]
    )
    if not still_queued and item["platforms"]:
        _patch(item["id"], status="posted", posted_at=datetime.now(timezone.utc).isoformat())
        item["status"] = "posted"

    return item


def youtube_redirect_uri() -> str:
    configured = os.getenv("YOUTUBE_REDIRECT_URI", "").strip()
    if configured:
        return configured
    # Must match Authorized redirect URIs in Google Cloud exactly
    # (localhost ≠ 127.0.0.1 for Google OAuth).
    return "http://localhost:8000/oauth2callback"


def youtube_auth_url(redirect_uri: str | None = None) -> str:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri or youtube_redirect_uri(),
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/youtube.upload",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def youtube_exchange_code(code: str, redirect_uri: str | None = None) -> None:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    body = urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri or youtube_redirect_uri(),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read().decode("utf-8"))
    token["obtained_at"] = datetime.now(timezone.utc).isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    YT_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")


def upload_youtube_scheduled(video_path: str, metadata: dict[str, Any], when: datetime) -> dict[str, Any]:
    token = _youtube_access_token()
    publish_at = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "snippet": {
            "title": metadata.get("title") or "Short",
            "description": metadata.get("description") or "",
            "tags": metadata.get("tags") or [],
            "categoryId": str(metadata.get("category_id") or "24"),
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": False,
        },
    }
    init = Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(Path(video_path).stat().st_size),
        },
        method="POST",
    )
    with urlopen(init, timeout=60) as resp:
        upload_url = resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube did not return an upload URL. Connect the account first.")
    data = Path(video_path).read_bytes()
    put = Request(
        upload_url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
        method="PUT",
    )
    with urlopen(put, timeout=600) as resp:
        uploaded = json.loads(resp.read().decode("utf-8"))
    return {
        "ok": True,
        "id": uploaded.get("id"),
        "privacy": "private",
        "publishAt": publish_at,
        "note": "Uploaded privately. YouTube will publish at the scheduled time.",
    }


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()


def _scheduler_loop() -> None:
    import time

    while True:
        try:
            _fire_due()
        except Exception:
            pass
        time.sleep(20)


def _fire_due() -> None:
    now = datetime.now(timezone.utc)
    with _lock:
        items = _load()
    for item in items:
        if item.get("status") != "scheduled":
            continue
        when = _parse_when(item["publish_at"])
        if when > now:
            continue
        results = dict(item.get("results") or {})
        video = item.get("video_path")
        meta = item.get("metadata") or {}
        for platform in item.get("platforms") or []:
            if platform == "youtube":
                continue
            existing = results.get(platform) or {}
            if existing.get("ok") and not existing.get("queued"):
                continue
            try:
                if platform == "x":
                    results["x"] = _post_x(video, meta)
                elif platform == "tiktok":
                    results["tiktok"] = _post_tiktok(video, meta)
            except Exception as exc:
                results[platform] = {"ok": False, "error": str(exc)}
        _patch(item["id"], status="posted", results=results, posted_at=now.isoformat())


def _post_x(video_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
    from app.oauth_x import post_video

    text = metadata.get("x_text") or metadata.get("title") or "Short"
    return post_video(video_path, text)


def _post_tiktok(video_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
    from app.oauth_tiktok import post_video

    caption = metadata.get("tiktok_caption") or metadata.get("title") or "Short"
    return post_video(video_path, caption)


def _youtube_access_token() -> str:
    if not YT_TOKEN_PATH.exists():
        raise RuntimeError("YouTube is not connected. Click Connect YouTube first.")
    token = json.loads(YT_TOKEN_PATH.read_text(encoding="utf-8"))
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    if not access and not refresh:
        raise RuntimeError("YouTube token is empty. Connect again.")
    if refresh:
        refreshed = _refresh_youtube(refresh)
        if refreshed.get("access_token"):
            token.update(refreshed)
            YT_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
            return token["access_token"]
    return access


def _refresh_youtube(refresh_token: str) -> dict[str, Any]:
    body = urlencode(
        {
            "client_id": os.getenv("YOUTUBE_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("YOUTUBE_CLIENT_SECRET", "").strip(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _parse_when(value: str) -> datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _load() -> list[dict[str, Any]]:
    if not SCHEDULE_PATH.exists():
        return []
    try:
        return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save(items: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCHEDULE_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def _patch(item_id: str, **kwargs: Any) -> None:
    with _lock:
        items = _load()
        for item in items:
            if item.get("id") == item_id:
                for key, value in kwargs.items():
                    if key == "results":
                        merged = dict(item.get("results") or {})
                        merged.update(value)
                        item["results"] = merged
                    else:
                        item[key] = value
                break
        _save(items)
