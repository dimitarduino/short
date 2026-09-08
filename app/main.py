from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from app.jobs import JobBusyError, store
from app.meta import YOUTUBE_CATEGORIES, generate_metadata
from app.music import list_tracks
from app.publish import (
    connections,
    create_schedule,
    list_scheduled,
    start_scheduler,
    youtube_auth_url,
    youtube_exchange_code,
    youtube_redirect_uri,
)
from app.render import ffmpeg_bin
from app.youtube import YoutubeError

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

ROOT = Path(__file__).resolve().parent
templates = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html"]),
)

app = FastAPI(title="Heatmap Shorts")
static_dir = ROOT / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class CreateJobBody(BaseModel):
    url: str


class GenerateBody(BaseModel):
    start: float
    end: float


class MusicBody(BaseModel):
    track_id: str = "none"


class MetaSaveBody(BaseModel):
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str = "24"
    x_text: str = ""
    tiktok_caption: str = ""


class ScheduleBody(BaseModel):
    platforms: list[str]
    publish_at: str
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str = "24"
    x_text: str = ""
    tiktok_caption: str = ""


@app.on_event("startup")
def _startup() -> None:
    start_scheduler()
    try:
        list_tracks()
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return templates.get_template("index.html").render()


@app.get("/index.php", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_alias() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)


@app.get("/api/connections")
def get_connections() -> dict:
    return {"connections": connections(), "youtube_redirect": youtube_redirect_uri()}


@app.get("/api/health")
def health() -> dict:
    ffmpeg_ok = True
    try:
        ffmpeg_bin()
    except Exception:
        ffmpeg_ok = False
    return {"ok": True, "ffmpeg": ffmpeg_ok, "connections": connections()}


@app.get("/api/music")
def music_tracks() -> dict:
    return {"tracks": list_tracks()}


@app.get("/api/music/{track_id}/audio")
def music_audio(track_id: str) -> FileResponse:
    from app.music import track_path

    if track_id == "none":
        raise HTTPException(status_code=404, detail="No preview for silence.")
    try:
        path = track_path(track_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Track not found.")
    media = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/api/categories")
def categories() -> dict:
    return {"categories": YOUTUBE_CATEGORIES}


@app.post("/api/jobs")
def create_job(body: CreateJobBody) -> dict:
    try:
        job = store.create(body.url)
    except YoutubeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@app.post("/api/jobs/{job_id}/generate")
def generate_job(job_id: str, body: GenerateBody) -> dict:
    try:
        job = store.generate(job_id, body.start, body.end)
    except YoutubeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@app.post("/api/jobs/cancel")
def cancel_jobs() -> dict:
    """Clear stuck in-flight jobs so a new analyze can start."""
    return store.cancel_busy()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return store.cancel_busy(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str) -> FileResponse:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done" or not job.output_path:
        raise HTTPException(status_code=409, detail="Video is not ready yet.")
    path = Path(job.output_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video file is missing.")
    filename = _download_name(job.title or "short")
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.post("/api/jobs/{job_id}/music")
def set_music(job_id: str, body: MusicBody) -> dict:
    try:
        job = store.apply_music(job_id, body.track_id)
    except YoutubeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_dict()


@app.post("/api/jobs/{job_id}/meta")
def job_meta(job_id: str) -> dict:
    job = store.get(job_id)
    if not job or job.status != "done":
        raise HTTPException(status_code=400, detail="Generate a short first.")
    meta = generate_metadata(job.title or "Short", job.captions_text, job.url)
    if job.library_id:
        from app.library import update_metadata

        update_metadata(job.library_id, meta)
    return meta


@app.post("/api/jobs/{job_id}/meta/save")
def save_job_meta(job_id: str, body: MetaSaveBody) -> dict:
    job = store.get(job_id)
    if not job or job.status != "done":
        raise HTTPException(status_code=400, detail="Generate a short first.")
    metadata = body.model_dump()
    if job.library_id:
        from app.library import update_metadata

        item = update_metadata(job.library_id, metadata)
        return {"ok": True, "library": item}
    return {"ok": True, "library": None}


@app.post("/api/jobs/{job_id}/schedule")
def schedule_job(job_id: str, body: ScheduleBody) -> dict:
    job = store.get(job_id)
    if not job or job.status != "done" or not job.output_path:
        raise HTTPException(status_code=400, detail="Generate a short first.")
    metadata = {
        "title": body.title,
        "description": body.description,
        "tags": body.tags,
        "category_id": body.category_id,
        "x_text": body.x_text or body.title,
        "tiktok_caption": body.tiktok_caption or body.title,
    }
    if job.library_id:
        from app.library import update_metadata

        update_metadata(job.library_id, metadata)
    try:
        item = create_schedule(
            job_id=job_id,
            video_path=job.output_path,
            platforms=body.platforms,
            publish_at=body.publish_at,
            metadata=metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return item


@app.get("/api/library")
def library_list() -> dict:
    from app.library import list_items

    return {"items": list_items()}


@app.get("/api/library/{item_id}")
def library_get(item_id: str) -> dict:
    from app.library import get_item

    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Not found.")
    return item


@app.get("/api/library/{item_id}/video")
def library_video(item_id: str) -> FileResponse:
    from app.library import get_item, video_path

    item = get_item(item_id)
    path = video_path(item_id)
    if not item or not path:
        raise HTTPException(status_code=404, detail="Video not found.")
    return FileResponse(path, media_type="video/mp4", filename=_download_name(item.get("title") or "short"))


@app.delete("/api/library/{item_id}")
def library_delete(item_id: str) -> dict:
    from app.library import delete_item

    if not delete_item(item_id):
        raise HTTPException(status_code=404, detail="Not found.")
    return {"ok": True}


@app.post("/api/library/{item_id}/schedule")
def library_schedule(item_id: str, body: ScheduleBody) -> dict:
    from app.library import get_item, update_metadata, video_path

    item = get_item(item_id)
    path = video_path(item_id)
    if not item or not path:
        raise HTTPException(status_code=404, detail="Library item not found.")
    metadata = {
        "title": body.title,
        "description": body.description,
        "tags": body.tags,
        "category_id": body.category_id,
        "x_text": body.x_text or body.title,
        "tiktok_caption": body.tiktok_caption or body.title,
    }
    update_metadata(item_id, metadata)
    try:
        scheduled = create_schedule(
            job_id=item.get("job_id") or item_id,
            video_path=str(path),
            platforms=body.platforms,
            publish_at=body.publish_at,
            metadata=metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return scheduled


@app.post("/api/connections/youtube/disconnect")
def disconnect_youtube() -> dict:
    from app.publish import YT_TOKEN_PATH

    if YT_TOKEN_PATH.exists():
        YT_TOKEN_PATH.unlink()
    return {"connections": connections()}


@app.post("/api/connections/x/disconnect")
def disconnect_x() -> dict:
    from app.oauth_x import x_disconnect

    x_disconnect()
    return {"connections": connections()}


@app.post("/api/connections/tiktok/disconnect")
def disconnect_tiktok() -> dict:
    from app.oauth_tiktok import tt_disconnect

    tt_disconnect()
    return {"connections": connections()}


@app.post("/api/connections/tiktok/connect")
def connect_tiktok() -> dict:
    from app.oauth_tiktok import tt_reconnect

    try:
        result = tt_reconnect()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connections": connections(), **result}


@app.get("/auth/youtube")
def auth_youtube() -> RedirectResponse:
    try:
        return RedirectResponse(youtube_auth_url())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/oauth2callback")
@app.get("/api/oauth/youtube/callback")
@app.get("/auth/youtube/callback")
def auth_youtube_callback(code: str | None = None, error: str | None = None) -> RedirectResponse:
    if error:
        return RedirectResponse("/?yt=denied")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code.")
    try:
        youtube_exchange_code(code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"YouTube auth failed: {exc}") from exc
    return RedirectResponse("/?yt=connected")


@app.get("/auth/x")
def auth_x() -> RedirectResponse:
    from app.oauth_x import x_auth_url

    try:
        return RedirectResponse(x_auth_url())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/auth/x/callback")
def auth_x_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> RedirectResponse:
    from app.oauth_x import x_exchange_code

    if error:
        return RedirectResponse("/?x=denied")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code.")
    try:
        x_exchange_code(code, state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"X auth failed: {exc}") from exc
    return RedirectResponse("/?x=connected")


@app.get("/auth/tiktok")
def auth_tiktok():
    from app.oauth_tiktok import tt_auth_url, tt_setup_help

    try:
        return RedirectResponse(tt_auth_url())
    except Exception as exc:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'><title>TikTok setup</title>"
            "<style>body{font:14px/1.45 ui-monospace,monospace;background:#0b0d0a;color:#e8e4d9;"
            "max-width:42rem;margin:2rem auto;padding:0 1rem}a{color:#c8f542}pre{white-space:pre-wrap;"
            "border:1px solid #333;padding:1rem;background:#121510}</style></head><body>"
            "<p><a href='/'>← Back</a></p>"
            "<h1>TikTok Connect needs setup</h1>"
            f"<pre>{exc}</pre>"
            f"<pre>{tt_setup_help()}</pre>"
            "</body></html>"
        )
        return HTMLResponse(body, status_code=400)


@app.get("/auth/tiktok/callback")
@app.get("/oauth/tiktok/callback")
def auth_tiktok_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    from app.oauth_tiktok import tt_exchange_code, tt_setup_help

    if error:
        detail = error_description or error
        body = (
            "<!doctype html><html><head><meta charset='utf-8'><title>TikTok denied</title>"
            "<style>body{font:14px/1.45 ui-monospace,monospace;background:#0b0d0a;color:#e8e4d9;"
            "max-width:42rem;margin:2rem auto;padding:0 1rem}a{color:#c8f542}pre{white-space:pre-wrap;"
            "border:1px solid #333;padding:1rem}</style></head><body>"
            "<p><a href='/'>← Back</a></p>"
            f"<h1>TikTok said no</h1><pre>{detail}</pre>"
            f"<pre>{tt_setup_help()}</pre></body></html>"
        )
        return HTMLResponse(body, status_code=400)
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code.")
    try:
        tt_exchange_code(code, state)
    except Exception as exc:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'><title>TikTok auth failed</title>"
            "<style>body{font:14px/1.45 ui-monospace,monospace;background:#0b0d0a;color:#e8e4d9;"
            "max-width:42rem;margin:2rem auto;padding:0 1rem}a{color:#c8f542}pre{white-space:pre-wrap;"
            "border:1px solid #333;padding:1rem}</style></head><body>"
            "<p><a href='/'>← Back</a></p>"
            f"<h1>TikTok token exchange failed</h1><pre>{exc}</pre>"
            f"<pre>{tt_setup_help()}</pre></body></html>"
        )
        return HTMLResponse(body, status_code=400)
    return RedirectResponse("/?tiktok=connected")


def _download_name(title: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in title).strip()
    safe = safe.replace(" ", "-")[:60] or "short"
    return f"{safe}.mp4"
