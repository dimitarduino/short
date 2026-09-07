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
    # Datacenter IPs often get "Sign in to confirm you’re not a bot".
    # Prefer non-web player clients first; cookies help when YouTube still challenges.
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "js_runtimes": {"node": {}},
        "extractor_args": {
            "youtube": {
                # Avoid plain "web" client which triggers PO-token / bot checks on VPS IPs.
                "player_client": ["tv", "web_safari", "android"],
            }
        },
    }
    cookies = _cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    opts.update(extra)
    # Keep cookies if caller overwrote opts without them
    if cookies and "cookiefile" not in opts:
        opts["cookiefile"] = str(cookies)
    return opts


def _clean_yt_error(exc: Exception) -> str:
    message = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
    if "Sign in to confirm" in message or "not a bot" in message.lower():
        return (
            "YouTube blocked this server IP (bot check). "
            "Export cookies from a browser (throwaway Google account), put them at "
            "data/youtube_cookies.txt on the VPS, set YOUTUBE_COOKIES_FILE if needed, "
            "then restart shorts-gen. Prefer a secondary account — not your main Google login."
        )
    return message or "YouTube request failed."


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
    opts = _ydl_opts(skip_download=True, extract_flat=False)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many extractor errors
        raise YoutubeError(f"Could not read that video: {_clean_yt_error(exc)}") from exc

    if not info:
        raise YoutubeError("Could not read that video.")

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
    except Exception as exc:
        raise YoutubeError(f"Download failed: {_clean_yt_error(exc)}") from exc

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
