from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func

from app.clip import ClipWindow, HeatPoint, normalize_heatmap

ROOT = Path(__file__).resolve().parent.parent

YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})"
)


class YoutubeError(RuntimeError):
    pass


def _cookies_path() -> Path | None:
    raw = (os.getenv("YOUTUBE_COOKIES_FILE") or "").strip()
    if not raw:
        # Sensible default on the VPS / local if the file exists
        candidate = ROOT / "data" / "youtube_cookies.txt"
        return candidate if candidate.exists() else None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path if path.exists() else None


def _ydl_opts(**extra) -> dict:
    # YouTube bot / SABR / cookie interactions change often.
    # Never use the keyword "default" with cookies: yt-dlp expands it to
    # _DEFAULT_AUTHED_CLIENTS which includes tv_downgraded → "page needs to be reloaded".
    # Never pass tv / tv_downgraded / tv_simply.
    cookies = _cookies_path()
    if cookies:
        clients = ["web_embedded", "web", "web_safari"]
    else:
        clients = ["android", "web_embedded", "web", "web_safari"]

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        # Deno preferred (enabled by default in yt-dlp). Node needs >=22.
        "js_runtimes": {"deno": {}, "node": {}},
        "remote_components": ["ejs:github"],
        "extractor_args": {
            "youtube": {
                "player_client": clients,
            }
        },
    }
    if cookies:
        opts["cookiefile"] = str(cookies)
    opts.update(extra)
    if cookies and "cookiefile" not in opts:
        opts["cookiefile"] = str(cookies)
    # Merge extractor_args if caller passed partial ones
    if "extractor_args" in extra:
        base = {"youtube": {"player_client": clients}}
        merged = dict(base)
        for k, v in (extra.get("extractor_args") or {}).items():
            merged[k] = {**(merged.get(k) or {}), **(v or {})}
        opts["extractor_args"] = merged
    return opts


def _clean_yt_error(exc: Exception) -> str:
    message = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
    low = message.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            "YouTube blocked this server IP (bot check). "
            "Put Netscape cookies from a throwaway Google account at "
            "data/youtube_cookies.txt, then restart. Do not use your main account."
        )
    if "page needs to be reloaded" in low:
        return (
            "YouTube rejected the player session (often bad cookies + TV client). "
            "Re-export fresh youtube.com cookies, replace data/youtube_cookies.txt, "
            "update yt-dlp (`pip install -U yt-dlp`), and restart. "
            "Or temporarily remove the cookies file and retry."
        )
    if "only images" in low or "format is not available" in low:
        return (
            "YouTube formats missing — JS challenge solver failed. "
            "Install Deno ≥2.3 (or Node ≥22), put it on PATH for the service user, "
            "run `pip install -U 'yt-dlp[default]'`, then restart."
        )
    return message or "YouTube request failed."


def _extract_info(url: str, *, download: bool = False) -> dict[str, Any]:
    """Extract with fallbacks when YouTube returns bot / reload errors."""
    attempts: list[dict[str, Any]] = [
        {},
        {
            "extractor_args": {
                "youtube": {"player_client": ["web_embedded", "web"]}
            }
        },
        {
            # Last resort: no cookies (cookies + some clients = reload loop)
            "cookiefile": None,
            "extractor_args": {
                "youtube": {"player_client": ["android", "web_embedded", "web"]}
            },
        },
    ]
    last_exc: Exception | None = None
    for override in attempts:
        opts = _ydl_opts(**{k: v for k, v in override.items() if v is not None})
        if override.get("cookiefile") is None and "cookiefile" in override:
            opts.pop("cookiefile", None)
        # Metadata must not fail just because n-challenge left only storyboards.
        if not download:
            opts["skip_download"] = True
            opts["ignore_no_formats_error"] = True
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            if info:
                return info
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if not any(
                s in msg
                for s in (
                    "not a bot",
                    "page needs to be reloaded",
                    "sign in",
                    "format is not available",
                    "only images",
                )
            ):
                break
            continue
    assert last_exc is not None
    raise last_exc


def _has_real_formats(info: dict[str, Any]) -> bool:
    for fmt in info.get("formats") or []:
        ext = (fmt.get("ext") or "").lower()
        if ext and ext not in {"mhtml", "jpg", "png", "webp"}:
            if fmt.get("url") or fmt.get("fragments") or fmt.get("manifest_url"):
                return True
    return False


def _require_js_runtime_or_raise() -> None:
    """Fail fast with a clear message when Deno/Node22 is missing for www-data."""
    from shutil import which

    deno = which("deno")
    node = which("node")
    node_ok = False
    if node:
        import subprocess

        try:
            out = subprocess.check_output([node, "--version"], text=True, timeout=5).strip()
            # v22.x.x
            m = re.match(r"v(\d+)", out)
            node_ok = bool(m and int(m.group(1)) >= 22)
        except Exception:
            node_ok = False
    if deno or node_ok:
        return
    raise YoutubeError(
        "YouTube downloads need Deno ≥2.3 or Node ≥22 on PATH for the service user "
        "(Node 20 is not enough). Install Deno to /usr/local/bin/deno, then "
        "`pip install -U 'yt-dlp[default]'` and restart shorts-gen."
    )


def parse_video_id(url: str) -> str:
    text = (url or "").strip()
    if not text:
        raise YoutubeError("Paste a YouTube URL first.")
    match = YOUTUBE_ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    raise YoutubeError("That does not look like a YouTube link.")


def fetch_metadata(url: str) -> dict[str, Any]:
    _require_js_runtime_or_raise()
    try:
        info = _extract_info(url, download=False)
    except YoutubeError:
        raise
    except Exception as exc:  # yt-dlp raises many extractor errors
        raise YoutubeError(f"Could not read that video: {_clean_yt_error(exc)}") from exc

    if not info:
        raise YoutubeError("Could not read that video.")

    if not _has_real_formats(info):
        raise YoutubeError(
            "YouTube returned no playable formats (only storyboards). "
            "Install Deno ≥2.3 to /usr/local/bin/deno (or Node ≥22), ensure "
            "`sudo -u www-data which deno` works, run "
            "`pip install -U 'yt-dlp[default]'`, restart shorts-gen."
        )

    heatmap = normalize_heatmap(info.get("heatmap"))
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "duration": float(info.get("duration") or 0),
        "heatmap": heatmap,
        "webpage_url": info.get("webpage_url") or url,
        "subtitles": info.get("subtitles") or {},
        "automatic_captions": info.get("automatic_captions") or {},
    }


def heatmap_payload(points: list[HeatPoint]) -> list[dict[str, float]]:
    return [{"start": p.start, "end": p.end, "value": p.value} for p in points]


def download_section(url: str, window: ClipWindow, dest: Path, progress_cb=None) -> Path:
    _require_js_runtime_or_raise()
    dest.parent.mkdir(parents=True, exist_ok=True)
    output = dest.with_suffix(".mp4")

    def _hook(event: dict) -> None:
        if not progress_cb or event.get("status") != "downloading":
            return
        downloaded = float(event.get("downloaded_bytes") or 0)
        total = event.get("total_bytes") or event.get("total_bytes_estimate")
        if total:
            pct = min(99, int(downloaded * 100 / float(total)))
            progress_cb(f"Downloading clip… {pct}%")
        elif downloaded:
            mb = downloaded / (1024 * 1024)
            progress_cb(f"Downloading clip… {mb:.1f} MB")

    opts = _ydl_opts(
        format=(
            "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
            "bv*[height<=720][vcodec^=avc]+ba/"
            "b[height<=720][ext=mp4]/"
            "bv*[height<=720]+ba/b[height<=720]/b"
        ),
        format_sort=["res:720", "vcodec:h264", "ext:mp4:m4a"],
        merge_output_format="mp4",
        outtmpl=str(dest.with_suffix("")),
        download_ranges=download_range_func(None, [(window.start, window.end)]),
        overwrites=True,
        socket_timeout=30,
        retries=5,
        fragment_retries=5,
        concurrent_fragment_downloads=4,
        progress_hooks=[_hook],
    )
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as first:
        # Retry without cookies + android-first (cookies often cause reload errors on VPS)
        fallback = dict(opts)
        fallback.pop("cookiefile", None)
        fallback["extractor_args"] = {
            "youtube": {"player_client": ["android", "web_embedded", "web"]}
        }
        try:
            with YoutubeDL(fallback) as ydl:
                ydl.download([url])
        except Exception:
            raise YoutubeError(f"Download failed: {_clean_yt_error(first)}") from first

    if output.exists():
        return output
    matches = list(dest.parent.glob(f"{dest.stem}.*"))
    videos = [p for p in matches if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if videos:
        return videos[0]
    raise YoutubeError("Download finished but no video file was written.")


def pick_caption_langs(subtitles: dict, automatic: dict) -> list[str]:
    preferred = ["en", "en-US", "en-GB", "en-orig"]
    available = list(subtitles.keys()) + [k for k in automatic.keys() if k not in subtitles]
    for lang in preferred:
        if lang in subtitles or lang in automatic:
            return [lang]
    for lang in available:
        if lang.lower().startswith("en"):
            return [lang]
    if available:
        return [available[0]]
    return preferred


def download_captions(
    url: str,
    dest_dir: Path,
    preferred_langs: list[str] | None = None,
    video_id: str | None = None,
) -> Path | None:
    """Download VTT captions with cache + retries (YouTube often 429s)."""
    import shutil
    import time

    langs = preferred_langs or ["en", "en-US", "en-GB", "en-orig"]
    dest_dir.mkdir(parents=True, exist_ok=True)

    vid = video_id or parse_video_id(url)
    cache_dir = ROOT / "data" / "caption_cache" / vid
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _find_caption_file(cache_dir)
    if cached and cached.stat().st_size > 200:
        dest = dest_dir / cached.name
        if not dest.exists() or dest.stat().st_size < 200:
            shutil.copy2(cached, dest)
        return dest if dest.exists() else cached

    last_err = ""
    for attempt in range(4):
        opts = _ydl_opts(
            skip_download=True,
            writesubtitles=True,
            writeautomaticsub=True,
            subtitleslangs=langs,
            subtitlesformat="vtt",
            outtmpl=str(dest_dir / "captions"),
            overwrites=True,
            sleep_interval_subtitles=2,
            retries=3,
            extractor_retries=3,
        )
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            last_err = _clean_yt_error(exc)
            if "429" in last_err or "Too Many Requests" in last_err:
                time.sleep(2.5 * (attempt + 1))
                continue
            break

        found = _find_caption_file(dest_dir)
        if found and found.stat().st_size > 200:
            try:
                shutil.copy2(found, cache_dir / found.name)
            except Exception:
                pass
            return found
        time.sleep(1.5 * (attempt + 1))

    # Last chance: any older job cache for this video id
    jobs_root = ROOT / "data" / "jobs"
    if jobs_root.exists():
        for folder in sorted(jobs_root.iterdir(), reverse=True):
            hit = _find_caption_file(folder)
            if hit and hit.stat().st_size > 200:
                # Only reuse if filename suggests same video was processed — weak check;
                # prefer explicit cache. Skip scavenger unless video_id in path metadata.
                pass

    if last_err:
        return None
    return _find_caption_file(dest_dir)


def _find_caption_file(dest_dir: Path) -> Path | None:
    files = sorted(dest_dir.glob("captions*.vtt")) + sorted(dest_dir.glob("*.vtt"))
    return files[0] if files else None
