from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class RenderError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RenderError("ffmpeg is not installed. Install it with Homebrew: brew install ffmpeg")
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RenderError("ffprobe is not installed (comes with ffmpeg).")
    return path


def probe_duration(path: Path) -> float:
    cmd = [
        ffprobe_bin(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float((out.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def trim_to_duration(source: Path, dest: Path, duration: float) -> Path:
    """
    yt-dlp section downloads often start at the previous keyframe, so the file
    is longer than the requested window and captions drift. Trim leading pad so
    t=0 matches the clip window start.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = max(1.0, float(duration))
    actual = probe_duration(source)
    pad = max(0.0, actual - duration) if actual > 0 else 0.0

    def _run(ss: float, reencode: bool) -> None:
        cmd = [ffmpeg_bin(), "-y"]
        if ss > 0.02:
            cmd += ["-ss", f"{ss:.3f}"]
        cmd += ["-i", str(source), "-t", f"{duration:.3f}"]
        if reencode:
            cmd += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
            ]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", str(dest)]
        run_ffmpeg(cmd)

    try:
        _run(pad, reencode=False)
    except RenderError:
        _run(pad, reencode=True)

    if not dest.exists():
        raise RenderError("Could not trim clip to the selected window.")
    return dest


def run_ffmpeg(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        tail = err[-1200:] if err else str(exc)
        raise RenderError(f"ffmpeg failed: {tail}") from exc


def render_short(source: Path, dest: Path, captions: Path | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if captions and captions.exists():
        vf = f"{vf},{_subtitles_filter(captions)}"

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(source),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    run_ffmpeg(cmd)
    if not dest.exists():
        raise RenderError("ffmpeg finished but no output file was written.")
    return dest


def mix_background_music(video: Path, music: Path | None, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if music is None:
        if video.resolve() != dest.resolve():
            dest.write_bytes(video.read_bytes())
        return dest

    # Music bed at ~0.12–0.15; light ducking so it's still audible under speech.
    filter_complex = (
        "[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo,volume=1.0[vo];"
        "[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "volume=0.13,highpass=f=60,lowpass=f=12000[bg];"
        "[vo]asplit=2[vo1][sc];"
        "[bg][sc]sidechaincompress=threshold=0.12:ratio=2.5:attack=40:release=500:makeup=1:knee=8[duck];"
        "[vo1][duck]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
        "alimiter=limit=0.95[a]"
    )
    simple_mix = (
        "[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo,volume=1.0[vo];"
        "[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.13[bg];"
        "[vo][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
        "alimiter=limit=0.95[a]"
    )

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(video),
        "-stream_loop",
        "-1",
        "-i",
        str(music),
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        run_ffmpeg(cmd)
    except RenderError:
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video),
            "-stream_loop",
            "-1",
            "-i",
            str(music),
            "-filter_complex",
            simple_mix,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        run_ffmpeg(cmd)
    if not dest.exists():
        raise RenderError("Mixing music failed: no output file.")
    return dest


def _subtitles_filter(captions: Path) -> str:
    path = captions.resolve().as_posix().replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
    if captions.suffix.lower() == ".ass":
        return f"ass='{path}'"
    style = (
        "FontName=Arial Black,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=4,Shadow=0,Alignment=5,MarginV=0,Bold=1"
    )
    return f"subtitles='{path}':force_style='{style}'"
