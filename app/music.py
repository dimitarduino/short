from __future__ import annotations

import array
import math
import random
import re
import wave
from pathlib import Path

from app.render import RenderError, ffmpeg_bin, run_ffmpeg

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
MUSIC_DIR = ROOT / "data" / "music"
STATIC_MUSIC_DIR = APP_DIR / "static"
MUSIC_VERSION = "moods-v1"
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}

TRACKS = [
    {
        "id": "none",
        "name": "No music",
        "mood": "Keep the original audio only",
    },
    {
        "id": "motivational",
        "name": "Motivational",
        "mood": "Rising piano + pulse — get-up-and-go under speech",
        "style": "motivational",
    },
    {
        "id": "inspirational",
        "name": "Inspirational",
        "mood": "Warm major pads and soft bells — hopeful",
        "style": "inspirational",
    },
    {
        "id": "sad",
        "name": "Sad",
        "mood": "Minor piano, sparse and heavy",
        "style": "sad",
    },
    {
        "id": "happy",
        "name": "Happy",
        "mood": "Bright major plucks and light bounce",
        "style": "happy",
    },
    {
        "id": "uplift",
        "name": "Uplift",
        "mood": "Building energy, claps-feel kick, triumphant",
        "style": "uplift",
    },
]


def list_tracks() -> list[dict]:
    ensure_tracks()
    built_in = [{"id": t["id"], "name": t["name"], "mood": t["mood"]} for t in TRACKS]
    custom = [
        {"id": t["id"], "name": t["name"], "mood": t["mood"]}
        for t in _custom_tracks()
    ]
    return built_in + custom


def track_path(track_id: str) -> Path | None:
    if track_id in (None, "", "none"):
        return None
    for custom in _custom_tracks():
        if custom["id"] == track_id:
            return custom["path"]
    ensure_tracks()
    path = MUSIC_DIR / f"{track_id}.m4a"
    if not path.exists():
        raise RenderError(f"Unknown music track: {track_id}")
    return path


def _slug(stem: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "track"


def _custom_tracks() -> list[dict]:
    if not STATIC_MUSIC_DIR.is_dir():
        return []
    used: set[str] = set()
    tracks: list[dict] = []
    files = sorted(
        (
            p
            for p in STATIC_MUSIC_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")
        ),
        key=lambda p: p.name.lower(),
    )
    for path in files:
        base = _slug(path.stem)
        tid = f"file-{base}"
        n = 2
        while tid in used:
            tid = f"file-{base}-{n}"
            n += 1
        used.add(tid)
        tracks.append(
            {
                "id": tid,
                "name": path.stem,
                "mood": f"Custom · {path.suffix.lstrip('.').upper()}",
                "path": path,
            }
        )
    return tracks


def ensure_tracks() -> None:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    marker = MUSIC_DIR / ".version"
    current = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    if current != MUSIC_VERSION:
        for stale in MUSIC_DIR.glob("*.m4a"):
            stale.unlink(missing_ok=True)
        for stale in MUSIC_DIR.glob("*.wav"):
            stale.unlink(missing_ok=True)
    for track in TRACKS:
        if track["id"] == "none":
            continue
        dest = MUSIC_DIR / f"{track['id']}.m4a"
        if dest.exists() and dest.stat().st_size > 20_000:
            continue
        _render_mood(track["style"], dest)
    marker.write_text(MUSIC_VERSION, encoding="utf-8")


def _render_mood(style: str, dest: Path) -> None:
    sr = 22050
    seconds = 28.0
    n = int(sr * seconds)
    left = [0.0] * n
    right = [0.0] * n
    rng = random.Random(
        {"motivational": 101, "inspirational": 202, "sad": 303, "happy": 404, "uplift": 505}[style]
    )

    if style == "motivational":
        # D major-ish rising feel
        chords = [
            [146.83, 185.00, 220.00],  # D
            [164.81, 196.00, 246.94],  # E minor
            [174.61, 220.00, 261.63],  # F#
            [196.00, 246.94, 293.66],  # G
        ]
        bar = 1.6
        key_gain, pad_gain, bass_gain = 0.17, 0.06, 0.11
        kick_gain, hat_gain, vinyl_gain = 0.12, 0.02, 0.008
        rise = True
        filter_keep = 0.48
        stereo = 0.15
        master_af = (
            "highpass=f=80,lowpass=f=4500,equalizer=f=320:t=q:w=1:g=2,"
            "acompressor=threshold=-20dB:ratio=2.5:attack=25:release=220,loudnorm=I=-19:TP=-2:LRA=9"
        )
    elif style == "inspirational":
        chords = [
            [261.63, 329.63, 392.00, 493.88],  # Cmaj add
            [220.00, 261.63, 329.63, 440.00],  # Am add
            [174.61, 220.00, 261.63, 349.23],  # F
            [196.00, 246.94, 293.66, 392.00],  # G
        ]
        bar = 2.8
        key_gain, pad_gain, bass_gain = 0.10, 0.14, 0.07
        kick_gain, hat_gain, vinyl_gain = 0.0, 0.0, 0.006
        rise = False
        filter_keep = 0.55
        stereo = 0.30
        master_af = (
            "highpass=f=70,lowpass=f=5200,equalizer=f=1800:t=q:w=1:g=1.5,"
            "acompressor=threshold=-22dB:ratio=2:attack=50:release=350,loudnorm=I=-21:TP=-2.5:LRA=10"
        )
    elif style == "sad":
        chords = [
            [220.00, 261.63, 311.13],  # A minor
            [174.61, 207.65, 261.63],  # F
            [196.00, 233.08, 293.66],  # G
            [146.83, 174.61, 220.00],  # D minor
        ]
        bar = 3.2
        key_gain, pad_gain, bass_gain = 0.15, 0.08, 0.09
        kick_gain, hat_gain, vinyl_gain = 0.0, 0.0, 0.01
        rise = False
        filter_keep = 0.62
        stereo = 0.12
        master_af = (
            "highpass=f=75,lowpass=f=3200,equalizer=f=200:t=q:w=1:g=1,"
            "acompressor=threshold=-24dB:ratio=2:attack=60:release=400,loudnorm=I=-22:TP=-3:LRA=11"
        )
    elif style == "happy":
        chords = [
            [261.63, 329.63, 392.00],  # C
            [293.66, 369.99, 440.00],  # D
            [329.63, 415.30, 493.88],  # E
            [196.00, 246.94, 293.66],  # G
        ]
        bar = 1.333  # bouncy ~90bpm
        key_gain, pad_gain, bass_gain = 0.16, 0.03, 0.08
        kick_gain, hat_gain, vinyl_gain = 0.10, 0.04, 0.005
        rise = False
        filter_keep = 0.40
        stereo = 0.18
        master_af = (
            "highpass=f=90,lowpass=f=5500,equalizer=f=2500:t=q:w=1.2:g=2,"
            "acompressor=threshold=-18dB:ratio=2:attack=15:release=180,loudnorm=I=-18:TP=-1.5:LRA=8"
        )
    else:  # uplift
        chords = [
            [130.81, 164.81, 196.00],  # C low
            [146.83, 185.00, 220.00],  # D
            [164.81, 196.00, 246.94],  # Em
            [174.61, 220.00, 261.63],  # F — climb
        ]
        bar = 1.5
        key_gain, pad_gain, bass_gain = 0.14, 0.09, 0.12
        kick_gain, hat_gain, vinyl_gain = 0.14, 0.03, 0.004
        rise = True
        filter_keep = 0.45
        stereo = 0.22
        master_af = (
            "highpass=f=70,lowpass=f=4800,equalizer=f=110:t=q:w=0.9:g=2.5,"
            "acompressor=threshold=-19dB:ratio=3:attack=20:release=200,loudnorm=I=-18:TP=-1.8:LRA=8"
        )

    prev = 0.0
    for i in range(n):
        t = i / sr
        progress = t / seconds
        chord = chords[int(t / bar) % len(chords)]
        local = t % bar

        if style == "happy":
            local_hit = local % (bar / 2)
            env = _pluck(local_hit, 0.01, 3.5)
        elif style == "sad":
            env = _pluck(local, 0.05, 0.55)
        elif style == "inspirational":
            env = 0.5 + 0.5 * math.sin(math.pi * min(1.0, local / (bar * 0.85)))
        else:
            env = _pluck(local, 0.02, 1.6)

        energy = 1.0
        if rise:
            energy = 0.55 + 0.55 * progress  # builds over the loop

        keys = 0.0
        if key_gain:
            for idx, f in enumerate(chord):
                weight = 0.55 if idx == 0 else 0.28 if idx == 1 else 0.17
                if style in {"sad", "inspirational"}:
                    keys += _piano(f, local) * weight
                elif style == "happy":
                    keys += _pluck_tone(f, local_hit if style == "happy" else local) * weight
                else:
                    keys += _tine(f, local) * weight
            keys *= env * key_gain * energy

        pad = 0.0
        if pad_gain:
            for idx, f in enumerate(chord):
                detune = 1.0 + (0.0035 if idx % 2 == 0 else -0.0025)
                fund = f * (0.5 if style in {"motivational", "uplift"} else 1.0)
                pad += math.sin(2 * math.pi * fund * detune * t)
                if style == "inspirational" and idx >= 2:
                    # soft bell partial
                    pad += 0.25 * math.sin(2 * math.pi * f * 2.0 * t) * math.exp(-1.2 * local)
            pad = (pad / max(1, len(chord))) * pad_gain * (0.7 + 0.3 * env) * energy

        root = chord[0] * 0.5
        if style == "happy":
            bass_env = _pluck((t % (bar / 2)), 0.01, 6.0)
            bass = bass_gain * math.sin(2 * math.pi * root * t) * bass_env * energy
        elif style == "uplift":
            bass = bass_gain * math.sin(2 * math.pi * root * t) * (0.5 + 0.5 * env) * energy
        else:
            bass = bass_gain * math.sin(2 * math.pi * root * t) * (0.45 + 0.55 * env)

        kick = 0.0
        if kick_gain:
            period = bar / 2 if style in {"happy", "motivational", "uplift"} else bar
            beat = t % period
            if beat < 0.18:
                kick = kick_gain * math.sin(2 * math.pi * (55 - 26 * beat) * t) * math.exp(-13 * beat) * energy

        hat = 0.0
        if hat_gain:
            step = bar / 4
            pos = t % step
            if 0.004 < pos < 0.035:
                hat = hat_gain * (rng.random() * 2 - 1) * math.exp(-80 * (pos - 0.004)) * energy

        # Soft clap-ish noise on uplift backbeat
        clap = 0.0
        if style == "uplift":
            half = t % bar
            if 0.72 < half < 0.78:
                clap = 0.05 * (rng.random() * 2 - 1) * math.exp(-60 * (half - 0.72)) * energy

        vinyl = vinyl_gain * (rng.random() * 2 - 1)
        sample = keys + pad + bass + kick + hat + clap + vinyl
        sample = filter_keep * sample + (1.0 - filter_keep) * prev
        prev = sample

        delay = int(0.01 * sr)
        l = sample
        r = sample * (1.0 - stereo) + (left[i - delay] * stereo if i > delay else sample * 0.9)
        left[i] = l
        right[i] = r

    wav_path = MUSIC_DIR / f"{dest.stem}.wav"
    _write_wav(wav_path, sr, left, right)
    _master_to_m4a(wav_path, dest, master_af)


def _tine(freq: float, t: float) -> float:
    s = math.sin(2 * math.pi * freq * t)
    s += 0.3 * math.sin(2 * math.pi * freq * 2.0 * t)
    s += 0.1 * math.sin(2 * math.pi * freq * 3.01 * t)
    return s / 1.4


def _piano(freq: float, t: float) -> float:
    s = math.sin(2 * math.pi * freq * t)
    s += 0.4 * math.sin(2 * math.pi * freq * 2.0 * t) * math.exp(-2.2 * t)
    s += 0.15 * math.sin(2 * math.pi * freq * 3.0 * t) * math.exp(-3.8 * t)
    return s / 1.6


def _pluck_tone(freq: float, t: float) -> float:
    s = math.sin(2 * math.pi * freq * t)
    s += 0.5 * math.sin(2 * math.pi * freq * 2.0 * t)
    s *= math.exp(-4.5 * max(0.0, t))
    return s


def _pluck(t: float, attack: float, decay: float) -> float:
    if t < 0:
        return 0.0
    if t < attack:
        return t / attack
    return math.exp(-(t - attack) * decay)


def _write_wav(path: Path, sr: int, left: list[float], right: list[float]) -> None:
    peak = max(1e-6, max(abs(x) for x in left), max(abs(x) for x in right))
    scale = 0.58 * 32767 / peak
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        frames = array.array("h")
        for l, r in zip(left, right):
            frames.append(int(max(-32767, min(32767, l * scale))))
            frames.append(int(max(-32767, min(32767, r * scale))))
        wav.writeframes(frames.tobytes())


def _master_to_m4a(wav: Path, dest: Path, af: str) -> None:
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(wav),
        "-af",
        af,
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        str(dest),
    ]
    try:
        run_ffmpeg(cmd)
    except RenderError:
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(wav),
            "-af",
            "highpass=f=70,lowpass=f=3500,volume=0.85",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(dest),
        ]
        run_ffmpeg(cmd)
    wav.unlink(missing_ok=True)
