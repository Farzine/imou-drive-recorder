# Imou motion → Google Drive recorder

Motion on an Imou camera → a short MP4 clip lands in your Google Drive.
No Imou cloud subscription, no paid hosting, no device on your LAN.

---

## Three things the original plan got wrong

Worth reading before you build, because each one breaks the system silently.

**1. A cloud server cannot reach `rtsp://192.168.1.100`.** That address only
exists inside your house. Render's container is on the public internet and will
sit there timing out forever. Port-forwarding 554 would "fix" it while exposing
a camera with a factory password to the entire internet — and on most consumer
ISPs in Bangladesh you're behind CGNAT anyway, so you don't have a forwardable
public IP to begin with.

*The fix:* Imou's own cloud will hand you an HLS address for the camera via
`bindDeviceLive` / `getLiveStreamInfo`. That URL is reachable from anywhere.
This app pulls **that** instead of RTSP, so nothing has to change on your
network.

**2. A Google OAuth app left in "Testing" revokes its refresh token after
7 days.** The build works, you celebrate, and the following week every upload
dies with `invalid_grant`. `drive.file` is a non-sensitive scope, so you can
flip the consent screen to **In production** with no review and no security
audit. Do it before you generate the token — Step 1.6.

**3. Recording inside the webhook request makes Imou drop you.** The original
handler blocks for 15+ seconds before answering. Imou's docs are explicit that
a callback which repeatedly fails to return 200 gets disabled. This app answers
in ~1 ms and records on a background thread.

Also: `token.json` does not belong in the repo, even a private one. It goes in
an environment variable.

---

## Architecture

```
                    ┌──────────────┐
   motion ────────► │ Imou camera  │
                    └──────┬───────┘
                           │ (camera's own cloud link)
                    ┌──────▼───────┐
                    │  Imou cloud  │
                    └──┬────────┬──┘
        alarm webhook  │        │  HLS pull (bindDeviceLive)
                       ▼        ▲
              ┌────────────────────────┐
              │  Render free web svc   │
              │  Flask + ffmpeg        │
              └───────────┬────────────┘
                          │ Drive API v3
                    ┌─────▼──────┐
                    │ Google Drive│  15 GB, auto-pruned
                    └─────────────┘
```

Your LAN is not in that diagram anywhere. That's the point.

---

## What it costs

| Piece | Free allowance | This app's usage |
|---|---|---|
| Google Drive | 15 GB | ~0.5–3 MB per 15 s clip; auto-deletes after 14 days |
| Render web service | 750 instance-hours/mo | 730 h if kept awake 24/7 — just fits |
| Imou API calls | 30,000/mo | ~3 calls per motion event |
| Imou message push | monthly free quota | 1 per event |

Check **Console → My Resources** for your account's real numbers; Imou meters
live-stream minutes separately from API calls, and the free grant has changed
before. A 60 s cooldown is the main thing keeping all of these in bounds.

---

## Step 1 — Google Drive API

1. [Google Cloud Console](https://console.cloud.google.com/) → new project,
   e.g. `imou-drive-recorder`.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **OAuth consent screen** → User type **External** → fill in app name and your
   own email for both support and developer contact.
4. **Data access / Scopes** → add **only**
   `https://www.googleapis.com/auth/drive.file`.
   This scope lets the app see files *it created* and nothing else in your Drive.
5. **Audience → Test users** → add your own Google account.
6. **🔴 Publish the app.** On the consent screen (or Audience page) click
   **Publish app** so the status reads **In production**. `drive.file` is
   non-sensitive, so this needs no verification. Skip this and your token dies
   in exactly 7 days.
7. **Credentials → Create credentials → OAuth client ID → Desktop app** →
   download the JSON, rename it `credentials.json`, put it in the project root.

## Step 2 — Generate the Drive token (once, on your own machine)

```bash
git clone <your repo> && cd imou-drive-recorder
pip install -r requirements.txt
python tools/generate_token.py
```

A browser opens; approve. You'll see an "unverified app" interstitial —
**Advanced → Go to … (unsafe)**. That warning is about *other people* trusting
your app; it's your own app and your own account.

This writes `token.json`. Confirm it contains a `refresh_token`. Keep it out of
git (`.gitignore` already handles it).

## Step 3 — Imou Open Platform

1. Register at [open.imoulife.com](https://open.imoulife.com/) with the **same
   account the camera is bound to in the Imou Life app**. A camera bound to a
   different account is invisible to the API.
2. **Console → My App** → copy **AppID** and **AppSecret**.
3. Note the **data centre** your account sits in and set `IMOU_BASE_URL` to
   match (`openapi.easy4ip.com` is the global default;
   `openapi-sg`, `openapi-fk`, `openapi-or` are the regional ones). A wrong
   data centre returns "device not found" for a device that plainly exists.
4. Find your device ID:

```bash
export IMOU_APP_ID=... IMOU_APP_SECRET=...
python tools/imou_cli.py devices
```

5. Verify the cloud stream works before deploying anything:

```bash
python tools/imou_cli.py hls <deviceId> 1        # 1 = SD, 0 = HD
ffplay "<the m3u8 url it prints>"                 # should show live video
```

If this fails, nothing downstream can work — fix it here.

## Step 4 — Deploy to Render

1. Push to a **private** GitHub repo. Check that `token.json` and
   `credentials.json` are *not* in it.
2. [Render](https://render.com/) → **New + → Web Service** → connect the repo.
3. Runtime **Docker**, instance type **Free**, region closest to you
   (Singapore for South Asia).
4. Environment variables — see `.env.example` for the full list:

| Key | Value |
|---|---|
| `IMOU_APP_ID` | from My App |
| `IMOU_APP_SECRET` | from My App |
| `IMOU_BASE_URL` | your data centre |
| `WEBHOOK_SECRET` | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `GOOGLE_TOKEN_JSON` | entire contents of `token.json`, one line |
| `STREAM_SOURCE` | `hls` |
| `STREAM_ID` | `1` (SD) |
| `CLIP_SECONDS` | `15` |
| `COOLDOWN_SECONDS` | `60` |
| `RETENTION_DAYS` | `14` |

5. Deploy. First build takes a few minutes (it's installing ffmpeg). Then:

```bash
curl https://<your-app>.onrender.com/healthz     # {"ok":true,...}
curl https://<your-app>.onrender.com/status
```

## Step 5 — Register the webhook

```bash
python tools/imou_cli.py set-callback \
  https://<your-app>.onrender.com/hook/<WEBHOOK_SECRET>
python tools/imou_cli.py get-callback            # read it back
```

One callback URL per developer account — registering a new one replaces the old.
The secret in the path is what stops strangers from triggering recordings and
burning your quota.

## Step 6 — Keep the free instance awake

Render spins a free service down after 15 minutes idle, and a cold start takes
about a minute. A motion alarm arriving during that minute produces a clip of
whatever happens *after* the container wakes — usually an empty hallway.

Free fix: [cron-job.org](https://cron-job.org/) → new job → GET
`https://<your-app>.onrender.com/healthz` every 10 minutes. 730 hours of uptime
per month sits just under Render's 750-hour free grant.

## Step 7 — Test

Wave at the camera. Within 30–60 seconds:

- Render logs show `Alarm payload: {...}` then `Recorded x.xx MB` then `Uploaded`
- a `Imou Motion Clips` folder appears in your Drive with a `.jpg` and `.mp4`

If nothing arrives, `curl .../status` — it reports events received, skipped,
uploaded, failures, and the last error string.

---

## Expectations worth setting

**The clip starts after the motion, not during it.** HLS is a segmented format;
between the camera waking, publishing, and the playlist filling, you're looking
at roughly 10–25 seconds of lag. `-live_start_index -3` claws back a few seconds
by starting three segments into the past, and the alarm JPEG that Imou attaches
to the webhook *is* from the moment of motion — which is why this app uploads it
too. If you need the clip to contain the actual event, you need LAN mode.

**Storage math.** SD 15 s ≈ 0.5–3 MB. Fifty events a day on a 14-day retention
is around 1–2 GB. Set `RETENTION_DAYS=0` to disable pruning and you will
eventually fill 15 GB and uploads will start failing.

**Free tier is best-effort.** A cold start, a Render restart, or an Imou quota
trip means a missed event, with no retry. For anything you'd actually rely on,
this isn't it.

---

## LAN mode (better clips, needs an always-on machine)

If you have a Raspberry Pi, NAS, mini PC, or a laptop that stays on, running the
same code there gets you full-resolution clips with no HLS delay, because it can
reach the camera directly:

```bash
STREAM_SOURCE=rtsp
CAMERA_IP=192.168.1.100
CAMERA_USER=admin
CAMERA_PASSWORD=<the safety code on the sticker under the camera>
```

The webhook still has to be reachable from Imou's cloud — use a free Cloudflare
Tunnel rather than port-forwarding. Everything else is identical.

---

## Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `STREAM_SOURCE` | `hls` | `hls` (cloud) or `rtsp` (LAN) |
| `STREAM_ID` | `1` | 0 = main/HD, 1 = sub/SD. Swap if resolution looks wrong on your model |
| `CLIP_SECONDS` | `15` | |
| `COOLDOWN_SECONDS` | `60` | Per-device. Motion fires in bursts; without this you'll OOM a 512 MB container |
| `RETENTION_DAYS` | `14` | 0 = keep forever |
| `UPLOAD_SNAPSHOT` | `true` | Uploads the alarm JPEG, which is from the actual moment of motion |
| `ALLOWED_MSG_TYPES` | `videoMotion,human,…` | `*` for everything |
| `DRIVE_FOLDER_NAME` | `Imou Motion Clips` | Created on first upload |
| `DRIVE_FOLDER_ID` | — | Overrides the name |

## Troubleshooting

| Symptom | Cause |
|---|---|
| `invalid_grant` after ~a week | Consent screen still in Testing. Step 1.6, then regenerate the token |
| `OP1011` | Daily interface-call cap hit. Raise `COOLDOWN_SECONDS` |
| Callback stopped firing | Your endpoint returned non-200 too often. Check `/status`, re-register |
| `device not found` | Wrong `IMOU_BASE_URL` data centre, or camera bound to a different Imou account |
| ffmpeg: 0-byte file | Camera hadn't started publishing. Raise the `attempts` in `wait_for_playlist` |
| First clip after quiet hours is blank | Cold start. Step 6 |
| `storageQuotaExceeded` | Drive full. Lower `RETENTION_DAYS` |

## Endpoints

| Route | Purpose |
|---|---|
| `GET /healthz` | Uptime ping |
| `GET /status` | Counters, config, Drive usage, last error |
| `POST /hook/<secret>` | Imou alarm callback |

## Reference

- [Development specification (signing)](https://open.imoulife.com/book/http/develop.html)
- [accessToken](https://open.imoulife.com/book/http/accessToken.html)
- [bindDeviceLive](https://open.imoulife.com/book/http/device/live/bindDeviceLive.html) ·
  [getLiveStreamInfo](https://open.imoulife.com/book/http/device/live/getLiveStreamInfo.html)
- [setMessageCallback](https://open.imoulife.com/book/http/push/setMessageCallback.html) ·
  [event format](https://open.imoulife.com/book/push/event.html)
- [Google Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Render free tier](https://render.com/docs/free)
