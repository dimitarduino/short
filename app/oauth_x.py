from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
X_TOKEN_PATH = DATA_DIR / "x_token.json"
X_PKCE_PATH = DATA_DIR / "x_pkce.json"


def x_client_id() -> str:
    return (os.getenv("X_CLIENT_ID") or os.getenv("X_API_KEY") or "").strip()


def x_client_secret() -> str:
    return (os.getenv("X_CLIENT_SECRET") or os.getenv("X_API_SECRET") or "").strip()


def x_redirect_uri() -> str:
    return (os.getenv("X_REDIRECT_URI") or "http://localhost:8000/auth/x/callback").strip()


def x_configured() -> bool:
    return bool(x_client_id())


def x_connected() -> bool:
    if X_TOKEN_PATH.exists():
        return True
    # OAuth 2 user bearer only. An ACCESS_TOKEN + ACCESS_SECRET pair is OAuth 1.0a
    # and cannot post video with the v2 Bearer upload path used here.
    token = os.getenv("X_ACCESS_TOKEN", "").strip()
    secret = os.getenv("X_ACCESS_SECRET", "").strip()
    return bool(token and not secret)


def x_auth_url() -> str:
    client_id = x_client_id()
    if not client_id:
        raise RuntimeError("Set X_CLIENT_ID (or X_API_KEY) in .env")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    X_PKCE_PATH.write_text(json.dumps({"verifier": verifier, "state": state}), encoding="utf-8")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": x_redirect_uri(),
        "scope": "tweet.read tweet.write users.read offline.access media.write",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return "https://twitter.com/i/oauth2/authorize?" + urlencode(params)


def x_exchange_code(code: str, state: str | None = None) -> None:
    if not X_PKCE_PATH.exists():
        raise RuntimeError("Missing PKCE session. Click Connect X again.")
    pkce = json.loads(X_PKCE_PATH.read_text(encoding="utf-8"))
    if state and pkce.get("state") and state != pkce["state"]:
        raise RuntimeError("OAuth state mismatch. Try Connect X again.")
    client_id = x_client_id()
    client_secret = x_client_secret()
    body = urlencode(
        {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": x_redirect_uri(),
            "code_verifier": pkce["verifier"],
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    req = Request("https://api.twitter.com/2/oauth2/token", data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            token = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"X token exchange failed: {exc.read().decode('utf-8', errors='replace')[:400]}") from exc
    token["obtained_at"] = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    X_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    X_PKCE_PATH.unlink(missing_ok=True)


def x_disconnect() -> None:
    X_TOKEN_PATH.unlink(missing_ok=True)


def _access_token() -> str:
    if X_TOKEN_PATH.exists():
        token = json.loads(X_TOKEN_PATH.read_text(encoding="utf-8"))
        access = token.get("access_token")
        refresh = token.get("refresh_token")
        # Refresh if we have a refresh token (best-effort)
        if refresh and x_client_id():
            refreshed = _refresh(refresh)
            if refreshed.get("access_token"):
                token.update(refreshed)
                token["obtained_at"] = time.time()
                X_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
                return token["access_token"]
        if access:
            return access
    env = os.getenv("X_ACCESS_TOKEN", "").strip()
    secret = os.getenv("X_ACCESS_SECRET", "").strip()
    if env and not secret:
        return env
    if env and secret:
        raise RuntimeError(
            "X_ACCESS_TOKEN looks like OAuth 1.0a (ACCESS_SECRET is also set). "
            "Click Connect X to authorize with OAuth 2.0 (needed for video posts)."
        )
    raise RuntimeError("X is not connected. Click Connect X first.")


def _refresh(refresh_token: str) -> dict[str, Any]:
    client_id = x_client_id()
    client_secret = x_client_secret()
    body = urlencode(
        {
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "client_id": client_id,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    req = Request("https://api.twitter.com/2/oauth2/token", data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def post_video(video_path: str, text: str) -> dict[str, Any]:
    token = _access_token()
    path = Path(video_path)
    if not path.exists():
        raise RuntimeError("Video file missing for X upload.")
    size = path.stat().st_size
    media_id = _upload_video(token, path, size)
    _wait_processing(token, media_id)
    tweet = _create_tweet(token, text[:280], media_id)
    return {"ok": True, "id": tweet.get("data", {}).get("id"), "media_id": media_id}


def _upload_video(token: str, path: Path, size: int) -> str:
    # Prefer v2 initialize/append/finalize; fall back to legacy command API.
    try:
        return _upload_v2(token, path, size)
    except Exception:
        return _upload_legacy(token, path, size)


def _upload_v2(token: str, path: Path, size: int) -> str:
    init_body = json.dumps(
        {
            "media_type": "video/mp4",
            "media_category": "tweet_video",
            "total_bytes": size,
        }
    ).encode("utf-8")
    init = Request(
        "https://api.x.com/2/media/upload/initialize",
        data=init_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(init, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    media_id = str(data.get("data", {}).get("id") or data.get("id") or "")
    if not media_id:
        raise RuntimeError(f"X media init failed: {data}")

    chunk = 4 * 1024 * 1024
    raw = path.read_bytes()
    for i, start in enumerate(range(0, size, chunk)):
        part = raw[start : start + chunk]
        boundary = f"----Heatmap{secrets.token_hex(8)}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="segment_index"\r\n\r\n{i}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="chunk"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + part + f"\r\n--{boundary}--\r\n".encode()
        append = Request(
            f"https://api.x.com/2/media/upload/{media_id}/append",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        with urlopen(append, timeout=120) as resp:
            resp.read()

    fin = Request(
        f"https://api.x.com/2/media/upload/{media_id}/finalize",
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(fin, timeout=60) as resp:
        resp.read()
    return media_id


def _upload_legacy(token: str, path: Path, size: int) -> str:
    # INIT
    init_body = urlencode(
        {
            "command": "INIT",
            "media_type": "video/mp4",
            "media_category": "tweet_video",
            "total_bytes": str(size),
        }
    ).encode()
    init = Request(
        "https://upload.twitter.com/1.1/media/upload.json",
        data=init_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urlopen(init, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    media_id = str(data.get("media_id_string") or data.get("media_id") or "")
    if not media_id:
        raise RuntimeError(f"X legacy media INIT failed: {data}")

    chunk = 4 * 1024 * 1024
    raw = path.read_bytes()
    for i, start in enumerate(range(0, size, chunk)):
        part = raw[start : start + chunk]
        boundary = f"----Heatmap{secrets.token_hex(8)}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="command"\r\n\r\nAPPEND\r\n',
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="media_id"\r\n\r\n{media_id}\r\n'.encode(),
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="segment_index"\r\n\r\n{i}\r\n'.encode(),
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="media"; filename="blob"\r\n',
                b"Content-Type: application/octet-stream\r\n\r\n",
                part,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        append = Request(
            "https://upload.twitter.com/1.1/media/upload.json",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        with urlopen(append, timeout=120) as resp:
            resp.read()

    fin_body = urlencode({"command": "FINALIZE", "media_id": media_id}).encode()
    fin = Request(
        "https://upload.twitter.com/1.1/media/upload.json",
        data=fin_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urlopen(fin, timeout=60) as resp:
        resp.read()
    return media_id


def _wait_processing(token: str, media_id: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = f"https://upload.twitter.com/1.1/media/upload.json?command=STATUS&media_id={media_id}"
        req = Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError):
            return
        info = data.get("processing_info") or {}
        state = info.get("state")
        if not state or state == "succeeded":
            return
        if state == "failed":
            raise RuntimeError(f"X media processing failed: {info}")
        time.sleep(int(info.get("check_after_secs") or 3))
    raise RuntimeError("Timed out waiting for X video processing.")


def _create_tweet(token: str, text: str, media_id: str) -> dict[str, Any]:
    body = json.dumps({"text": text, "media": {"media_ids": [media_id]}}).encode("utf-8")
    req = Request(
        "https://api.twitter.com/2/tweets",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"X tweet failed: {exc.read().decode('utf-8', errors='replace')[:500]}") from exc
