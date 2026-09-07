from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TT_TOKEN_PATH = DATA_DIR / "tiktok_token.json"
TT_STATE_PATH = DATA_DIR / "tiktok_oauth_state.json"
TT_DISABLED_PATH = DATA_DIR / "tiktok_disconnected"


def tt_client_key() -> str:
    return (os.getenv("TIKTOK_CLIENT_KEY") or os.getenv("TIKTOK_CLIENT_ID") or "").strip()


def tt_client_secret() -> str:
    return (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()


def tt_redirect_uri() -> str:
    return (os.getenv("TIKTOK_REDIRECT_URI") or "http://localhost:8000/auth/tiktok/callback").strip()


def tt_redirect_is_https() -> bool:
    return tt_redirect_uri().lower().startswith("https://")


def tt_configured() -> bool:
    """True only when Login Kit credentials exist (needed for Connect OAuth)."""
    return bool(tt_client_key() and tt_client_secret())


def _user_disconnected() -> bool:
    return TT_DISABLED_PATH.exists()


def tt_connected() -> bool:
    """Connected only when a live token file exists and user hasn't disconnected."""
    if _user_disconnected():
        return False
    if not TT_TOKEN_PATH.exists():
        return False
    try:
        return bool(json.loads(TT_TOKEN_PATH.read_text(encoding="utf-8")).get("access_token"))
    except json.JSONDecodeError:
        return False


def tt_profile() -> dict[str, Any] | None:
    if not tt_connected():
        return None
    try:
        token = json.loads(TT_TOKEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    user = token.get("user") if isinstance(token.get("user"), dict) else {}
    if not (user.get("display_name") or user.get("avatar_url")) and token.get("access_token"):
        user = _fetch_user(token["access_token"], token.get("open_id") or user.get("open_id"))
        if user.get("display_name") or user.get("open_id"):
            token["user"] = user
            TT_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    display = (user.get("display_name") or "").strip()
    open_id = (user.get("open_id") or token.get("open_id") or "").strip()
    if not display and not open_id:
        return None
    return {
        "display_name": display or "TikTok user",
        "open_id": open_id,
        "avatar_url": (user.get("avatar_url") or "").strip(),
        "username": (user.get("username") or "").strip(),
    }


def tt_status() -> dict[str, Any]:
    """Rich status for the UI — TikTok's OAuth is picky."""
    # Do NOT auto-connect from .env refresh token — Connect must open TikTok's picker.
    configured = tt_configured()
    connected = tt_connected()
    profile = tt_profile()
    redirect = tt_redirect_uri()
    https_ok = tt_redirect_is_https()
    problems: list[str] = []
    if not tt_client_key():
        problems.append("Missing TIKTOK_CLIENT_KEY (from developers.tiktok.com → your app).")
    if not tt_client_secret():
        problems.append("Missing TIKTOK_CLIENT_SECRET.")
    if configured and not https_ok:
        problems.append(
            "TikTok rejects http:// redirects. Use ngrok HTTPS and set TIKTOK_REDIRECT_URI "
            "to https://….ngrok-free.app/auth/tiktok/callback (same URI in Login Kit)."
        )
    if "fb.dimnai.com" in redirect and "/oauth/tiktok/" in redirect:
        problems.append(
            "Redirect points at FB3 (fb.dimnai.com/oauth/…). Use this app’s ngrok URL "
            "…/auth/tiktok/callback instead, or Connect will fail with invalid state."
        )
    can_connect = configured and https_ok and "fb.dimnai.com/oauth/tiktok" not in redirect
    if connected and profile:
        label = f"Connected as {profile['display_name']}"
    elif connected:
        label = "Connected · API ready"
    elif can_connect:
        label = "Ready to connect — opens TikTok account picker"
    elif configured and not https_ok:
        label = "Needs HTTPS redirect (ngrok)"
    else:
        label = "Add Client Key + Secret in .env"
    return {
        "configured": configured,
        "connected": connected,
        "can_connect": can_connect and not connected,
        "redirect_uri": redirect,
        "redirect_https": https_ok,
        "mode": "oauth",
        "label": label,
        "profile": profile,
        "problems": problems,
    }


def tt_setup_help() -> str:
    redirect = tt_redirect_uri()
    return (
        "TikTok Login Kit setup\n\n"
        "1. Create an app at https://developers.tiktok.com/\n"
        "2. Add products: Login Kit + Content Posting API\n"
        "3. Request scopes: user.info.basic, video.upload, video.publish\n"
        "4. TikTok requires an https:// redirect URI (http://localhost is rejected).\n"
        "   In a second terminal:  ngrok http 8000\n"
        "   Copy the https URL, then set in .env:\n"
        "     TIKTOK_CLIENT_KEY=...\n"
        "     TIKTOK_CLIENT_SECRET=...\n"
        f"     TIKTOK_REDIRECT_URI=https://YOUR_SUBDOMAIN.ngrok-free.app/auth/tiktok/callback\n"
        "5. Paste the exact same redirect URI into Login Kit → Redirect URI.\n"
        "6. Open the app via that ngrok https URL (or localhost is fine to click Connect;\n"
        "   the callback must hit the https URI TikTok knows).\n"
        f"\nCurrent TIKTOK_REDIRECT_URI={redirect or '(empty)'}\n"
        f"HTTPS ok: {tt_redirect_is_https()}\n"
        f"Client key set: {bool(tt_client_key())}\n"
        f"Client secret set: {bool(tt_client_secret())}\n"
    )


def tt_auth_url() -> str:
    key = tt_client_key()
    if not key or not tt_client_secret():
        raise RuntimeError(tt_setup_help())
    if not tt_redirect_is_https():
        raise RuntimeError(
            "TikTok only allows https:// redirect URIs.\n\n" + tt_setup_help()
        )
    # Starting Connect: clear disconnect flag so callback can save the session.
    TT_DISABLED_PATH.unlink(missing_ok=True)
    state = secrets.token_urlsafe(24)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TT_STATE_PATH.write_text(json.dumps({"state": state}), encoding="utf-8")
    params = {
        "client_key": key,
        "scope": "user.info.basic,video.upload,video.publish",
        "response_type": "code",
        "redirect_uri": tt_redirect_uri(),
        "state": state,
        # Always show TikTok login / account picker (don't silently reuse last session).
        "disable_auto_auth": "1",
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)


def tt_exchange_code(code: str, state: str | None = None) -> None:
    if not tt_client_key() or not tt_client_secret():
        raise RuntimeError("Missing TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET.")
    if TT_STATE_PATH.exists() and state:
        saved = json.loads(TT_STATE_PATH.read_text(encoding="utf-8"))
        if saved.get("state") and saved["state"] != state:
            raise RuntimeError("OAuth state mismatch. Try Connect TikTok again.")
    # TikTok sometimes URL-encodes the code; normalize once
    code = (code or "").strip()
    body = urlencode(
        {
            "client_key": tt_client_key(),
            "client_secret": tt_client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": tt_redirect_uri(),
        }
    ).encode("utf-8")
    req = Request(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(
            f"TikTok token exchange failed: {exc.read().decode('utf-8', errors='replace')[:500]}"
        ) from exc
    token = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    # Some responses put error at top level with access_token missing
    err = payload.get("error") or token.get("error")
    if err and err not in ("ok", "success", 0, "0"):
        desc = payload.get("error_description") or token.get("error_description") or payload
        raise RuntimeError(f"TikTok auth error: {err} — {desc}")
    if not token.get("access_token"):
        raise RuntimeError(f"TikTok auth response missing access_token: {payload}")
    token["obtained_at"] = time.time()
    token["user"] = _fetch_user(token["access_token"], token.get("open_id"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TT_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    TT_STATE_PATH.unlink(missing_ok=True)
    TT_DISABLED_PATH.unlink(missing_ok=True)


def tt_disconnect() -> None:
    """Drop the session. Keeps Client Key/Secret in .env (app config)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TT_TOKEN_PATH.unlink(missing_ok=True)
    TT_STATE_PATH.unlink(missing_ok=True)
    TT_DISABLED_PATH.write_text("1", encoding="utf-8")


def tt_reconnect() -> dict[str, Any]:
    """Always open TikTok OAuth so the user can pick / confirm the account."""
    return {"connected": False, "oauth_url": tt_auth_url()}


def _fetch_user(access_token: str, open_id: str | None = None) -> dict[str, Any]:
    fields = "open_id,union_id,avatar_url,display_name"
    url = f"https://open.tiktokapis.com/v2/user/info/?fields={fields}"
    req = Request(url, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        user = ((payload.get("data") or {}).get("user")) or {}
        if open_id and not user.get("open_id"):
            user["open_id"] = open_id
        return user
    except Exception:
        return {"open_id": open_id or "", "display_name": ""}


def _access_token() -> str:
    if _user_disconnected():
        raise RuntimeError("TikTok is disconnected. Click Connect TikTok first.")
    if TT_TOKEN_PATH.exists():
        token = json.loads(TT_TOKEN_PATH.read_text(encoding="utf-8"))
        access = token.get("access_token")
        refresh = token.get("refresh_token")
        if refresh and tt_client_key() and tt_client_secret():
            refreshed = _refresh(refresh)
            if refreshed.get("access_token"):
                token.update(refreshed)
                token["obtained_at"] = time.time()
                if not token.get("user"):
                    token["user"] = _fetch_user(token["access_token"], token.get("open_id"))
                TT_TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
                return token["access_token"]
        if access:
            return access
    raise RuntimeError("TikTok is not connected. Click Connect TikTok first.")


def _refresh(refresh_token: str) -> dict[str, Any]:
    body = urlencode(
        {
            "client_key": tt_client_key(),
            "client_secret": tt_client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = Request(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload
    except Exception:
        return {}


def _api(token: str, url: str, body: dict | None = None, method: str = "POST") -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method=method,
    )
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"TikTok API error: {exc.read().decode('utf-8', errors='replace')[:500]}") from exc


def post_video(video_path: str, caption: str) -> dict[str, Any]:
    token = _access_token()
    path = Path(video_path)
    if not path.exists():
        raise RuntimeError("Video file missing for TikTok upload.")
    size = path.stat().st_size
    # Single-chunk upload (matches FB3) — more reliable for inbox.
    chunk = size
    total_chunks = 1

    preferred = (os.getenv("TIKTOK_POST_MODE") or "inbox").strip().lower()
    creator = _api(token, "https://open.tiktokapis.com/v2/post/publish/creator_info/query/", {})
    privacy_options = (creator.get("data") or {}).get("privacy_level_options") or ["SELF_ONLY"]
    env_privacy = (os.getenv("TIKTOK_PRIVACY_LEVEL") or "SELF_ONLY").strip()
    privacy = env_privacy if env_privacy in privacy_options else (
        "SELF_ONLY" if "SELF_ONLY" in privacy_options else privacy_options[0]
    )

    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk,
        "total_chunk_count": total_chunks,
    }
    direct_body = {
        "post_info": {
            "title": (caption or "Short")[:2200],
            "privacy_level": privacy,
            "disable_duet": True,
            "disable_comment": False,
            "disable_stitch": True,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": source_info,
    }
    inbox_body = {"source_info": source_info}

    mode = "inbox" if preferred == "inbox" else "direct"
    init_url = (
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        if mode == "inbox"
        else "https://open.tiktokapis.com/v2/post/publish/video/init/"
    )
    body = inbox_body if mode == "inbox" else direct_body

    try:
        init = _api(token, init_url, body)
    except RuntimeError as exc:
        # Unaudited apps often can't Direct Post — fall back to inbox.
        if mode == "direct" and any(
            x in str(exc).lower()
            for x in ("unaudited", "privacy_level", "integration guidelines", "scope")
        ):
            mode = "inbox"
            init = _api(
                token,
                "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
                inbox_body,
            )
        else:
            raise

    data = init.get("data") or {}
    err = (init.get("error") or {}).get("code")
    if err not in (None, "ok", 0, "0"):
        raise RuntimeError(f"TikTok init failed: {init}")
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url:
        raise RuntimeError(f"TikTok did not return upload_url: {init}")

    raw = path.read_bytes()
    req = Request(
        upload_url,
        data=raw,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(raw)),
            "Content-Range": f"bytes 0-{len(raw) - 1}/{len(raw)}",
        },
        method="PUT",
    )
    with urlopen(req, timeout=300) as resp:
        resp.read()

    status = {"publish_id": publish_id}
    final_state = ""
    for _ in range(30):
        time.sleep(2)
        status = _api(
            token,
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            {"publish_id": publish_id},
        )
        final_state = ((status.get("data") or {}).get("status") or "").upper()
        if final_state in {
            "PUBLISH_COMPLETE",
            "SEND_TO_USER_INBOX",
            "COMPLETE",
            "SUCCESS",
            "FAILED",
            "PUBLISH_FAILED",
        }:
            break

    if final_state in {"FAILED", "PUBLISH_FAILED"}:
        raise RuntimeError(f"TikTok publish failed: {status.get('data')}")

    note = (
        "Draft sent to TikTok inbox — open the TikTok app to post. "
        "Inbox mode cannot set the caption via API; copy it from the app."
        if mode == "inbox"
        else f"Posted to TikTok ({privacy})."
    )
    return {
        "ok": True,
        "publish_id": publish_id,
        "privacy": privacy if mode == "direct" else None,
        "mode": mode,
        "status": status.get("data"),
        "caption": (caption or "")[:500],
        "title_applied": mode == "direct",
        "note": note,
    }
