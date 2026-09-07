# Heatmap Shorts

Local web app that turns a YouTube video’s **Most replayed** stretch into a 9:16 short with captions.

Use it only on videos you own or are allowed to reuse. YouTube’s terms generally disallow downloading other people’s content.

## Requirements

- Python 3.12+
- [ffmpeg](https://ffmpeg.org/) (`brew install ffmpeg`)
- A JS runtime for yt-dlp YouTube challenges: **Deno ≥ 2.3** (recommended) or **Node.js ≥ 22**
  - Node 20 and older are rejected (`JS runtimes: node-… (unsupported)` → only storyboard images)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), paste a watch URL, and generate. The video must already have a Most replayed heatmap (newer or rarely watched videos will fail on purpose).

Jobs write files under `data/jobs/` (gitignored). Only one short generates at a time.

## Music, copy, and scheduling

After a short is ready you can:

- Pick a **background bed** (original generated loops, not commercial songs)
- Generate **title / description / tags / category** (`GEMINI_API_KEY` in `.env` for Gemini; otherwise a local draft)
- Choose **YouTube, X, and TikTok** and a time on the calendar (defaults to **tomorrow 12:00**)
- **Schedule without publishing now**

Connect accounts on the start screen via OAuth (no manual download/upload):

- **YouTube** — uploads privately now with native `publishAt` so it goes live later
- **X** — queued locally; at the scheduled time the app posts the video via the X API
- **TikTok** — queued locally; at the scheduled time the app posts via Content Posting API

Copy `.env.example` to `.env` and add OAuth app credentials. Redirect URIs must match exactly:

| Platform | Redirect URI |
|----------|--------------|
| YouTube | `http://localhost:8000/oauth2callback` |
| X | `http://localhost:8000/auth/x/callback` |
| TikTok | **Must be `https://…`** — TikTok rejects `http://localhost`. Run `ngrok http 8000`, then use `https://YOUR_SUBDOMAIN.ngrok-free.app/auth/tiktok/callback` in both `.env` (`TIKTOK_REDIRECT_URI`) and Login Kit. |

Also set `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` from [developers.tiktok.com](https://developers.tiktok.com/). Enable **Login Kit** + **Content Posting API**, and request scopes `user.info.basic`, `video.upload`, `video.publish`. A lone `TIKTOK_ACCESS_TOKEN` is not enough for Connect.

Open the app at [http://localhost:8000](http://localhost:8000) (not `127.0.0.1` — OAuth treats them as different hosts).

X video posting needs a paid API tier with `media.write`. TikTok unaudited apps may only allow private/`SELF_ONLY` posts until TikTok audits your app.
