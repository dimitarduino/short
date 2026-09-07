from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

YOUTUBE_CATEGORIES = [
    {"id": "22", "name": "People & Blogs"},
    {"id": "24", "name": "Entertainment"},
    {"id": "27", "name": "Education"},
    {"id": "28", "name": "Science & Technology"},
    {"id": "10", "name": "Music"},
    {"id": "20", "name": "Gaming"},
    {"id": "1", "name": "Film & Animation"},
    {"id": "23", "name": "Comedy"},
]


class MetaError(RuntimeError):
    pass


def generate_metadata(title: str, captions: str = "", source_url: str = "") -> dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        out = _fallback(title, captions)
        out["warning"] = "GEMINI_API_KEY missing — used local draft."
        return out
    try:
        payload = _call_gemini(title, captions, source_url, key)
    except MetaError as exc:
        out = _fallback(title, captions)
        out["warning"] = str(exc)
        return out
    return _normalize(payload, title)


def _call_gemini(title: str, captions: str, source_url: str, key: str) -> dict[str, Any]:
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
    prompt = (
        "You write short-form social copy that goes viral on YouTube Shorts, TikTok, and X.\n"
        "Return ONLY valid JSON (no markdown) with keys:\n"
        "- title: max ~90 chars, curiosity hook + 2-4 hashtags\n"
        "- description: 2-4 punchy sentences, then a blank line, then hashtags\n"
        "- tags: array of 8-12 short tags without #\n"
        "- category_id: YouTube category id as a string "
        '(prefer "24" Entertainment, "22" People & Blogs, "27" Education, "28" Science & Technology, "23" Comedy)\n'
        "- category_name: matching name\n"
        "- x_text: under 260 chars, hook + hashtags\n"
        "- tiktok_caption: 1-2 lines + hashtags including #fyp\n\n"
        "Rules: specific > generic, no clickbait lies, no 'watch till the end' spam, "
        "use the captions for concrete details. Each regenerate should feel fresh — vary the hook.\n\n"
        f"Original title: {title}\n"
        f"Source: {source_url}\n"
        f"Captions: {captions[:900] or '(none)'}\n"
        f"Variation seed: {os.urandom(3).hex()}"
    )
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 1.0,
                "responseMimeType": "application/json",
            },
        }
    ).encode("utf-8")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MetaError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MetaError(f"Gemini request failed: {exc}") from exc

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MetaError(f"Unexpected Gemini response: {str(data)[:300]}") from exc

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetaError(f"Gemini returned non-JSON: {text[:200]}") from exc


def _normalize(raw: dict[str, Any], fallback_title: str) -> dict[str, Any]:
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in re.split(r"[,#]", tags) if part.strip()]
    tags = [str(t).lstrip("#").strip() for t in tags if str(t).strip()]
    category_id = str(raw.get("category_id") or "24")
    category_name = next(
        (c["name"] for c in YOUTUBE_CATEGORIES if c["id"] == category_id),
        str(raw.get("category_name") or "Entertainment"),
    )
    title = str(raw.get("title") or fallback_title).strip()
    description = str(raw.get("description") or "").strip()
    return {
        "title": title[:100],
        "description": description,
        "tags": tags[:15],
        "category_id": category_id,
        "category_name": category_name,
        "x_text": str(raw.get("x_text") or title)[:260],
        "tiktok_caption": str(raw.get("tiktok_caption") or title)[:150],
        "ai": True,
    }


def _fallback(title: str, captions: str) -> dict[str, Any]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9']{2,}", f"{title} {captions}")
    tags = []
    for word in words:
        low = word.lower()
        if low not in tags and low not in {"the", "and", "that", "this", "with", "from"}:
            tags.append(low)
        if len(tags) >= 10:
            break
    tags = tags or ["shorts", "viral", "clip"]
    hashtags = " ".join(f"#{t}" for t in tags[:4])
    hook = title.strip() or "This moment hits different"
    return {
        "title": f"{hook} {hashtags}".strip()[:100],
        "description": (
            f"{hook}\n\nThe most replayed stretch, cut as a vertical short.\n\n{hashtags} #shorts #fyp"
        ),
        "tags": tags + ["shorts", "fyp"],
        "category_id": "24",
        "category_name": "Entertainment",
        "x_text": f"{hook}\n{hashtags}"[:260],
        "tiktok_caption": f"{hook} {hashtags} #fyp"[:150],
        "ai": False,
    }
