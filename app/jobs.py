from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.captions import srt_plain_text, vtt_to_window_srt
from app.clip import ClipWindow, NoHeatmapError, pick_peak_window
from app.library import save_from_job
from app.music import track_path
from app.render import RenderError, mix_background_music, render_short, trim_to_duration
from app.youtube import (
    YoutubeError,
    download_captions,
    download_section,
    fetch_metadata,
    heatmap_payload,
    parse_video_id,
    pick_caption_langs,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "jobs"

MIN_CLIP = 15.0
MAX_CLIP = 60.0


class JobBusyError(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    url: str
    status: str = "queued"
    error: str | None = None
    warning: str | None = None
    title: str | None = None
    video_id: str | None = None
    duration: float | None = None
    clip_start: float | None = None
    clip_end: float | None = None
    suggested_start: float | None = None
    suggested_end: float | None = None
    heatmap: list[dict[str, float]] = field(default_factory=list)
    output_path: str | None = None
    base_path: str | None = None
    library_id: str | None = None
    captions_text: str = ""
    music_id: str = "none"
    progress: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "error": self.error,
            "warning": self.warning,
            "progress": self.progress,
            "title": self.title,
            "video_id": self.video_id,
            "duration": self.duration,
            "clip_start": self.clip_start,
            "clip_end": self.clip_end,
            "suggested_start": self.suggested_start,
            "suggested_end": self.suggested_end,
            "heatmap": self.heatmap,
            "has_video": bool(self.output_path and Path(self.output_path).exists()),
            "music_id": self.music_id,
            "captions_text": self.captions_text,
            "library_id": self.library_id,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def create(self, url: str) -> Job:
        """Analyze only — user picks the clip window, then calls generate()."""
        video_id = parse_video_id(url)
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        with self._lock:
            if any(j.status not in {"done", "error", "ready"} for j in self._jobs.values()):
                raise JobBusyError("A short is already generating. Wait for it to finish.")
            job = Job(id=uuid.uuid4().hex[:12], url=watch_url, video_id=video_id)
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._analyze, args=(job.id,), daemon=True)
        thread.start()
        self._thread = thread
        return job

    def generate(self, job_id: str, start: float, end: float) -> Job:
        job = self.get(job_id)
        if not job:
            raise YoutubeError("Job not found.")
        if job.status not in {"ready", "done", "error"}:
            raise JobBusyError("Still analyzing — wait a moment.")
        if job.duration is None:
            raise YoutubeError("Analyze the video first.")

        start, end = self._normalize_window(start, end, float(job.duration))
        with self._lock:
            if any(
                j.id != job_id and j.status not in {"done", "error", "ready"}
                for j in self._jobs.values()
            ):
                raise JobBusyError("Another short is generating.")
            job.status = "queued"
            job.error = None
            job.warning = None
            job.progress = None
            job.clip_start = start
            job.clip_end = end
            job.output_path = None
            job.base_path = None

        thread = threading.Thread(target=self._generate, args=(job_id,), daemon=True)
        thread.start()
        self._thread = thread
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _set(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)

    def _normalize_window(self, start: float, end: float, duration: float) -> tuple[float, float]:
        start = max(0.0, float(start))
        end = min(duration, float(end))
        if end <= start:
            raise YoutubeError("Clip end must be after start.")
        length = end - start
        if length < MIN_CLIP:
            end = min(duration, start + MIN_CLIP)
            start = max(0.0, end - MIN_CLIP)
        if end - start > MAX_CLIP:
            end = start + MAX_CLIP
        if end > duration:
            end = duration
            start = max(0.0, end - min(MAX_CLIP, end))
        return round(start, 1), round(end, 1)

    def _analyze(self, job_id: str) -> None:
        with self._run_lock:
            job = self.get(job_id)
            if not job:
                return
            try:
                self._set(job_id, status="analyzing", progress="Reading heatmap…")
                meta = fetch_metadata(job.url)
                self._set(
                    job_id,
                    title=meta["title"],
                    duration=meta["duration"],
                    heatmap=heatmap_payload(meta["heatmap"]),
                    video_id=meta["id"] or job.video_id,
                )
                self._set(job_id, status="picking", progress="Finding peak window…")
                window = pick_peak_window(meta["heatmap"], meta["duration"])
                # Prefetch captions into cache so generate is less likely to hit 429.
                try:
                    langs = pick_caption_langs(
                        meta.get("subtitles") or {}, meta.get("automatic_captions") or {}
                    )
                    download_captions(
                        job.url,
                        DATA_DIR / job.id / "caption_prefetch",
                        langs,
                        video_id=meta.get("id") or job.video_id,
                    )
                except Exception:
                    pass
                self._set(
                    job_id,
                    status="ready",
                    clip_start=window.start,
                    clip_end=window.end,
                    suggested_start=window.start,
                    suggested_end=window.end,
                    progress=None,
                )
            except (YoutubeError, NoHeatmapError) as exc:
                self._set(job_id, status="error", error=str(exc), progress=None)
            except Exception as exc:
                self._set(job_id, status="error", error=f"Unexpected error: {exc}", progress=None)

    def _generate(self, job_id: str) -> None:
        with self._run_lock:
            job = self.get(job_id)
            if not job or job.clip_start is None or job.clip_end is None:
                return
            workdir = DATA_DIR / job.id
            workdir.mkdir(parents=True, exist_ok=True)
            window = ClipWindow(start=float(job.clip_start), end=float(job.clip_end))
            try:
                meta = fetch_metadata(job.url)

                self._set(
                    job_id,
                    status="downloading",
                    progress="Downloading the clip from YouTube. Long videos can take a few minutes.",
                )
                raw = download_section(
                    job.url,
                    window,
                    workdir / "clip_raw",
                    progress_cb=lambda msg: self._set(job_id, progress=msg),
                )
                self._set(job_id, progress="Trimming to exact window…")
                source = trim_to_duration(raw, workdir / "clip.mp4", window.duration)
                self._set(job_id, progress=None)

                self._set(job_id, status="captions", progress="Downloading captions…")
                warning = None
                srt_path = None
                langs = pick_caption_langs(meta.get("subtitles") or {}, meta.get("automatic_captions") or {})
                vtt = download_captions(
                    job.url,
                    workdir,
                    langs,
                    video_id=meta.get("id") or job.video_id,
                )
                if vtt:
                    srt_path = vtt_to_window_srt(vtt, window, workdir / "captions.srt")
                if not srt_path:
                    warning = (
                        "No captions for this clip (YouTube may be rate-limiting subtitle downloads). "
                        "Try Generate again in a minute — captions are cached when they succeed."
                    )
                    captions_text = ""
                else:
                    plain_src = srt_path.with_suffix(".srt") if srt_path.suffix == ".ass" else srt_path
                    captions_text = srt_plain_text(plain_src if plain_src.exists() else srt_path)
                self._set(job_id, progress=None)

                self._set(job_id, status="rendering", warning=warning)
                output = render_short(source, workdir / "short.mp4", srt_path)
                base = workdir / "short_base.mp4"
                base.write_bytes(output.read_bytes())

                library_id = None
                try:
                    saved = save_from_job(
                        job_id=job_id,
                        video_path_src=str(output),
                        title=job.title or meta.get("title") or "Short",
                        source_url=job.url,
                        video_id=job.video_id,
                        clip_start=window.start,
                        clip_end=window.end,
                        captions_text=captions_text,
                        music_id="none",
                    )
                    library_id = saved["id"]
                except Exception:
                    library_id = None

                self._set(
                    job_id,
                    status="done",
                    output_path=str(output),
                    base_path=str(base),
                    captions_text=captions_text,
                    music_id="none",
                    library_id=library_id,
                    progress=None,
                )
            except (YoutubeError, NoHeatmapError, RenderError, FileNotFoundError) as exc:
                self._set(job_id, status="error", error=str(exc), progress=None)
            except Exception as exc:
                self._set(job_id, status="error", error=f"Unexpected error: {exc}", progress=None)

    def apply_music(self, job_id: str, track_id: str) -> Job:
        job = self.get(job_id)
        if not job or job.status != "done" or not job.base_path or not job.output_path:
            raise YoutubeError("Generate a short first, then pick music.")
        music = track_path(track_id)
        mix_background_music(Path(job.base_path), music, Path(job.output_path))
        self._set(job_id, music_id=track_id or "none")
        # Refresh library copy if present
        if job.library_id:
            try:
                from app.library import LIBRARY_DIR

                lib_video = LIBRARY_DIR / job.library_id / "short.mp4"
                if lib_video.parent.exists():
                    lib_video.write_bytes(Path(job.output_path).read_bytes())
            except Exception:
                pass
        updated = self.get(job_id)
        assert updated is not None
        return updated


store = JobStore()
