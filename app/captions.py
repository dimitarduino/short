from __future__ import annotations

import html
import re
from pathlib import Path

from app.clip import ClipWindow

TAG_RE = re.compile(r"<[^>]+>")
# YouTube timed word markers: <00:00:01.240>
WORD_TIME_RE = re.compile(r"<(\d{2}:)?\d{2}:\d{2}\.\d{3}>")
ENTITY_JUNK_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;|&#x[0-9a-fA-F]+;")
SPACE_RE = re.compile(r"\s+")


def vtt_to_window_srt(vtt_path: Path, window: ClipWindow, out_path: Path) -> Path | None:
    """Build an animated ASS caption file (uppercase, cleaned). Path may end in .srt for callers."""
    cues = _parse_vtt(vtt_path.read_text(encoding="utf-8", errors="replace"))
    words: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        if end <= window.start or start >= window.end:
            continue
        cue_start = max(0.0, start - window.start)
        cue_end = min(window.duration, end - window.start)
        if cue_end - cue_start < 0.04:
            continue
        parts = _words_from_cue(text, start, end, window)
        if parts:
            words.extend(parts)
        else:
            cleaned = clean_caption_text(text)
            if cleaned:
                words.append((cue_start, cue_end, cleaned))

    if not words:
        return None

    # Prefer .ass next to requested path
    ass_path = out_path.with_suffix(".ass")
    ass_path.write_text(_build_ass(words, window.duration), encoding="utf-8")
    # Keep a plain uppercase SRT too (meta / fallback)
    srt_blocks = [
        f"{i}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(words, start=1)
    ]
    out_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    return ass_path


def clean_caption_text(text: str) -> str:
    """Strip tags/entities and force uppercase for burn-in."""
    if not text:
        return ""
    # Timed word tags first so times aren't left as garbage
    text = WORD_TIME_RE.sub(" ", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    # Any leftover entities
    text = ENTITY_JUNK_RE.sub(" ", text)
    text = html.unescape(text)
    # Drop music notes / bracket noise often in auto-captions
    text = re.sub(r"\[.*?\]|\(.*?\)", " ", text)
    text = text.replace("\u200b", " ").replace("\xa0", " ")
    text = SPACE_RE.sub(" ", text).strip()
    # Keep letters, numbers, basic punctuation for speech
    text = re.sub(r"[^\w\s'\-,.!?]", " ", text, flags=re.UNICODE)
    text = SPACE_RE.sub(" ", text).strip()
    return text.upper()


def _words_from_cue(
    raw: str, abs_start: float, abs_end: float, window: ClipWindow
) -> list[tuple[float, float, str]]:
    """If VTT has per-word timestamps, use them; else split cue evenly."""
    events: list[tuple[float, str]] = []
    # YouTube auto: <00:00:01.240><c>word</c> or plain text with optional times
    for match in re.finditer(
        r"<((?:\d{2}:)?\d{2}:\d{2}\.\d{3})>(?:<c[^>]*>)?\s*([^<]*?)\s*(?:</c>)?",
        raw,
    ):
        try:
            t = _parse_timestamp(match.group(1))
        except ValueError:
            continue
        chunk = clean_caption_text(match.group(2) or "")
        for word in chunk.split():
            events.append((t, word))

    if len(events) >= 2:
        out: list[tuple[float, float, str]] = []
        for i, (t, word) in enumerate(events):
            next_t = events[i + 1][0] if i + 1 < len(events) else abs_end
            start = max(0.0, t - window.start)
            end = min(window.duration, max(next_t, t + 0.12) - window.start)
            if end <= 0 or start >= window.duration or end <= start:
                continue
            out.append((start, end, word))
        return _group_words(out, min_words=5, max_words=6)

    cleaned = clean_caption_text(TAG_RE.sub(" ", WORD_TIME_RE.sub(" ", raw)))
    return _even_split_words(cleaned.split(), abs_start, abs_end, window)


def _even_split_words(
    words: list[str], abs_start: float, abs_end: float, window: ClipWindow
) -> list[tuple[float, float, str]]:
    words = [w for w in words if w]
    if not words:
        return []
    cue_start = max(0.0, abs_start - window.start)
    cue_end = min(window.duration, abs_end - window.start)
    dur = max(0.12, cue_end - cue_start)

    groups: list[str] = []
    i = 0
    while i < len(words):
        remaining = len(words) - i
        take = 6 if remaining >= 6 else remaining
        # If leftover would be 1–4 words, pull them into this chunk when possible
        if remaining > 6 and remaining - 6 < 5:
            take = remaining  # one longer final phrase rather than a tiny leftover
            if take > 8:
                take = 6
        chunk = words[i : i + take]
        groups.append(" ".join(chunk))
        i += take

    slot = dur / max(1, len(groups))
    out: list[tuple[float, float, str]] = []
    for idx, text in enumerate(groups):
        start = cue_start + idx * slot
        end = cue_start + (idx + 1) * slot
        out.append((start, min(cue_end, end), text))
    return _merge_overlapping(out)


def _group_words(
    words: list[tuple[float, float, str]], min_words: int = 5, max_words: int = 6
) -> list[tuple[float, float, str]]:
    if not words:
        return []
    grouped: list[tuple[float, float, str]] = []
    buf: list[tuple[float, float, str]] = []
    for item in words:
        buf.append(item)
        if len(buf) >= max_words:
            grouped.append((buf[0][0], buf[-1][1], " ".join(w[2] for w in buf)))
            buf = []
    if buf:
        if len(buf) < min_words and grouped:
            # Fold short leftover into previous phrase
            prev_start, _, prev_text = grouped[-1]
            grouped[-1] = (
                prev_start,
                buf[-1][1],
                f"{prev_text} {' '.join(w[2] for w in buf)}".strip(),
            )
        else:
            grouped.append((buf[0][0], buf[-1][1], " ".join(w[2] for w in buf)))
    # Keep natural speech timing — do not stretch phrases (that desyncs captions).
    return _merge_overlapping(grouped)


def _merge_overlapping(
    cues: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """Prevent overlapping dialogue lines that flicker when ASS stacks them."""
    if not cues:
        return []
    ordered = sorted(cues, key=lambda c: c[0])
    out: list[tuple[float, float, str]] = [ordered[0]]
    for start, end, text in ordered[1:]:
        prev_start, prev_end, prev_text = out[-1]
        if start < prev_end:
            # End previous just before next starts
            gap = max(0.05, start - 0.04)
            if gap > prev_start + 0.2:
                out[-1] = (prev_start, gap, prev_text)
            else:
                # Merge into one longer phrase
                out[-1] = (prev_start, max(prev_end, end), f"{prev_text} {text}".strip())
                continue
        out.append((start, end, text))
    return out


def _build_ass(words: list[tuple[float, float, str]], duration: float) -> str:
    header = """[Script Info]
Title: Heatmap Shorts
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,0,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for start, end, text in words:
        start = max(0.0, start)
        end = min(duration, max(end, start + 0.35))
        # Soft fade only — no scale pop (that caused blinking)
        anim = r"{\fad(60,80)}"
        safe = text.replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},Default,,0,0,0,,{anim}{safe}\n"
        )
    return "".join(lines)


def _parse_vtt(content: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n"))
    for block in blocks:
        lines = [
            ln
            for ln in block.split("\n")
            if ln.strip()
            and not ln.strip().startswith("WEBVTT")
            and not ln.strip().startswith("NOTE")
            and not ln.strip().startswith("Kind:")
            and not ln.strip().startswith("Language:")
        ]
        if not lines:
            continue
        time_line = None
        text_start = 0
        for i, line in enumerate(lines):
            if "-->" in line:
                time_line = line.strip()
                text_start = i + 1
                break
        if not time_line:
            continue
        try:
            start_raw, end_raw = [part.strip() for part in time_line.split("-->")]
            end_raw = end_raw.split(" ")[0]
            start = _parse_timestamp(start_raw)
            end = _parse_timestamp(end_raw)
        except ValueError:
            continue
        # Keep raw (with possible word timestamps) for animation; clean later
        text = "\n".join(lines[text_start:])
        if text.strip():
            cues.append((start, end, text))
    return cues


def _parse_timestamp(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    raise ValueError(f"Bad timestamp: {value}")


def srt_plain_text(path: Path, limit: int = 900) -> str:
    if not path.exists():
        return ""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = raw.strip()
        if not text or text.isdigit() or "-->" in text:
            continue
        if text.startswith("[") or text.startswith("Style:") or text.startswith("Dialogue:"):
            # ASS dialogue: take text after last ,,
            if text.startswith("Dialogue:"):
                parts = text.split(",,", 1)
                if len(parts) == 2:
                    plain = re.sub(r"\{.*?\}", "", parts[1])
                    lines.append(clean_caption_text(plain))
            continue
        lines.append(clean_caption_text(text))
    blob = " ".join(l for l in lines if l)
    return blob[:limit]


def _srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _ass_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    cs = int(round(seconds * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"
