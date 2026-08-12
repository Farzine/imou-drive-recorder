"""
Imou motion -> Google Drive recorder.

Flow:
    Imou cloud POSTs an alarm to /hook/<WEBHOOK_SECRET>
    -> we answer 200 immediately (Imou disables callbacks that don't)
    -> a worker thread asks the Imou API for the camera's HLS address
    -> ffmpeg grabs N seconds
    -> the clip is uploaded to Google Drive and the temp file is deleted
    -> old clips are pruned so the free 15 GB never fills
"""

import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import drive
import recorder
from imou import ImouClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("imou-recorder")


def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------- settings

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
CLIP_SECONDS = int(os.getenv("CLIP_SECONDS", "15"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "14"))
STREAM_SOURCE = os.getenv("STREAM_SOURCE", "hls").strip().lower()
STREAM_ID = int(os.getenv("STREAM_ID", "1"))          # 0 = HD main, 1 = SD sub
UPLOAD_SNAPSHOT = _env_bool("UPLOAD_SNAPSHOT", True)
TMP_DIR = os.getenv("TMP_DIR", "/tmp")

# Only these alarm types trigger a recording. Use "*" for everything.
ALLOWED_MSG_TYPES = {
    t.strip()
    for t in os.getenv(
        "ALLOWED_MSG_TYPES",
        "videoMotion,human,crossLineDetection,crossRegionDetection",
    ).split(",")
    if t.strip()
}

# LAN mode only (STREAM_SOURCE=rtsp)
CAMERA_IP = os.getenv("CAMERA_IP", "")
CAMERA_USER = os.getenv("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.getenv("CAMERA_PASSWORD", "")

imou = ImouClient(
    app_id=os.getenv("IMOU_APP_ID", ""),
    app_secret=os.getenv("IMOU_APP_SECRET", ""),
    base_url=os.getenv("IMOU_BASE_URL", "https://openapi.easy4ip.com/openapi"),
)

app = Flask(__name__)

# ------------------------------------------------------------------- state

jobs = queue.Queue(maxsize=20)
_last_event = {}          # device_id -> monotonic timestamp
_cooldown_lock = threading.Lock()
_last_prune = 0.0
stats = {
    "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "events_received": 0,
    "events_skipped": 0,
    "clips_uploaded": 0,
    "failures": 0,
    "last_event": None,
    "last_error": None,
}


def rtsp_url():
    return (
        f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:554"
        f"/cam/realmonitor?channel=1&subtype={STREAM_ID}"
    )


def should_record(device_id):
    """One recording per device per cooldown window; motion fires in bursts."""
    now = time.monotonic()
    with _cooldown_lock:
        last = _last_event.get(device_id, 0)
        if now - last < COOLDOWN_SECONDS:
            return False
        _last_event[device_id] = now
        return True


def maybe_prune():
    global _last_prune
    if time.monotonic() - _last_prune < 3600:
        return
    _last_prune = time.monotonic()
    try:
        drive.prune(RETENTION_DAYS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Retention pass failed: %s", exc)


# ------------------------------------------------------------------ worker


def handle_event(event):
    device_id = event["device_id"]
    channel_id = event.get("channel_id", "0")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"{device_id}_{event.get('msg_type', 'motion')}_{stamp}"

    # 1. Snapshot first: it is already sitting on Imou's CDN and never fails
    #    the way a live pull can.
    if UPLOAD_SNAPSHOT and event.get("snapshot_url"):
        jpg = os.path.join(TMP_DIR, base + ".jpg")
        if recorder.download_snapshot(event["snapshot_url"], jpg):
            try:
                drive.upload(jpg, base + ".jpg", "image/jpeg")
            except Exception as exc:  # noqa: BLE001
                log.warning("Snapshot upload failed: %s", exc)
            finally:
                if os.path.exists(jpg):
                    os.remove(jpg)

    # 2. Video clip.
    if STREAM_SOURCE == "rtsp":
        if not CAMERA_IP:
            raise RuntimeError("STREAM_SOURCE=rtsp but CAMERA_IP is unset")
        source, is_hls = rtsp_url(), False
    else:
        source, is_hls = imou.hls_url(device_id, channel_id, STREAM_ID), True

    mp4 = os.path.join(TMP_DIR, base + ".mp4")
    try:
        if not recorder.record(source, mp4, CLIP_SECONDS, is_hls):
            raise RuntimeError("recording produced no usable file")
        drive.upload(mp4, base + ".mp4")
        stats["clips_uploaded"] += 1
    finally:
        if os.path.exists(mp4):
            os.remove(mp4)

    maybe_prune()


def worker():
    log.info("Worker thread up (source=%s, clip=%ss, cooldown=%ss)",
             STREAM_SOURCE, CLIP_SECONDS, COOLDOWN_SECONDS)
    while True:
        event = jobs.get()
        try:
            handle_event(event)
        except Exception as exc:  # noqa: BLE001
            stats["failures"] += 1
            stats["last_error"] = f"{type(exc).__name__}: {exc}"
            log.exception("Event handling failed")
        finally:
            jobs.task_done()


threading.Thread(target=worker, name="recorder", daemon=True).start()


# ------------------------------------------------------------------ routes


@app.get("/")
@app.get("/healthz")
def healthz():
    """Ping target for the uptime cron that keeps the free dyno awake."""
    return jsonify(ok=True, service="imou-drive-recorder"), 200


@app.get("/status")
def status():
    body = dict(stats)
    body["queue_depth"] = jobs.qsize()
    body["config"] = {
        "stream_source": STREAM_SOURCE,
        "clip_seconds": CLIP_SECONDS,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "retention_days": RETENTION_DAYS,
        "allowed_msg_types": sorted(ALLOWED_MSG_TYPES),
    }
    quota = drive.storage_quota()
    if quota and quota.get("limit"):
        body["drive_used_gb"] = round(quota["usage"] / 2**30, 2)
        body["drive_limit_gb"] = round(quota["limit"] / 2**30, 2)
    return jsonify(body), 200


@app.route("/hook/<secret>", methods=["POST", "GET"])
def hook(secret):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        return jsonify(ok=False), 404

    # Imou sometimes probes the URL with GET before enabling it.
    if request.method == "GET":
        return jsonify(ok=True), 200

    payload = request.get_json(silent=True) or {}
    log.info("Alarm payload: %s", payload)
    stats["events_received"] += 1
    stats["last_event"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    device_id = payload.get("did") or payload.get("deviceId")
    msg_type = payload.get("msgType", "")
    pics = payload.get("picUrlArray") or []

    accept = bool(device_id) and (
        "*" in ALLOWED_MSG_TYPES or msg_type in ALLOWED_MSG_TYPES
    )
    if accept and not should_record(device_id):
        log.info("Cooldown active for %s, skipping", device_id)
        accept = False

    if accept:
        event = {
            "device_id": device_id,
            "channel_id": str(payload.get("cid", payload.get("channelId", "0"))),
            "msg_type": msg_type or "motion",
            "snapshot_url": pics[0] if pics else None,
        }
        try:
            jobs.put_nowait(event)
        except queue.Full:
            stats["events_skipped"] += 1
            log.warning("Job queue full, dropping event")
    else:
        stats["events_skipped"] += 1

    # ALWAYS 200. Imou stops pushing to endpoints that fail repeatedly.
    return jsonify(ok=True), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
