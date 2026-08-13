"""
Capture a short clip with ffmpeg.

Two sources, chosen by STREAM_SOURCE:

  hls  -- pull Imou's cloud HLS address. Works from any server on the
          internet, so it is the only option on Render/Cloud Run.
          Costs ~10-25 s of latency: HLS is a segmented format and the
          camera has to wake and publish before the playlist fills.

  rtsp -- pull the camera's LAN stream directly. Best quality, near-zero
          latency, but only works when the recorder itself is on the same
          network as the camera (mini PC, NAS, Raspberry Pi, old laptop).
"""

import logging
import os
import shutil
import subprocess
import time

import requests

log = logging.getLogger(__name__)


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def wait_for_playlist(url, attempts=6, delay=3, timeout=8):
    """Imou returns the .m3u8 address before the camera has actually started
    publishing. Poll until the playlist lists at least one segment."""
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.ok and ".ts" in resp.text:
                log.info("HLS playlist ready after %d attempt(s)", i)
                return True
            log.debug("Playlist attempt %d: HTTP %s, %d bytes",
                      i, resp.status_code, len(resp.content))
        except requests.RequestException as exc:
            log.debug("Playlist attempt %d failed: %s", i, exc)
        time.sleep(delay)
    log.warning("HLS playlist never became ready; trying ffmpeg anyway")
    return False


def build_command(source_url, out_path, duration, is_hls):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

    if is_hls:
        # 15 s socket timeout, and start a few segments back so we catch the
        # moment just before the webhook fired.
        cmd += ["-rw_timeout", "15000000", "-live_start_index", "-3"]
    else:
        cmd += ["-rtsp_transport", "tcp"]

    cmd += [
        "-i", source_url,
        "-t", str(duration),
        "-c:v", "copy",      # no video re-encode: 0.1 vCPU is enough
        "-c:a", "aac",       # remux-safe audio (ADTS -> MP4 needs this)
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd


def record_extending(source_url, out_path, last_motion_getter,
                     min_seconds=10, max_seconds=120, idle_seconds=12,
                     is_hls=True):
    """Record for as long as motion keeps happening, like the Imou app does.

    Starts ffmpeg with -t max_seconds as a hard ceiling, then stops early once
    `last_motion_getter()` (a unix timestamp updated by each new webhook) has
    been quiet for idle_seconds. Stopping is done by writing 'q' to ffmpeg's
    stdin rather than killing it, so the MP4 gets its moov atom written and the
    file is actually playable.

    Resulting duration ~= motion duration + idle_seconds, clamped to
    [min_seconds, max_seconds].
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed in this container")
    if is_hls:
        wait_for_playlist(source_url)

    cmd = build_command(source_url, out_path, max_seconds, is_hls)
    log.info("Recording (adaptive, max %ss) -> %s", max_seconds, out_path)

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    reason = "ffmpeg reached -t limit"

    try:
        while proc.poll() is None:
            time.sleep(0.5)
            elapsed = time.monotonic() - started
            if elapsed >= max_seconds + 15:
                reason = "hard timeout"
                proc.kill()
                break
            if elapsed < min_seconds:
                continue
            quiet = time.time() - last_motion_getter()
            if quiet >= idle_seconds:
                reason = f"motion stopped ({quiet:.0f}s quiet)"
                try:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    proc.terminate()
                break
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream and stream.close()
            except OSError:
                pass

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size < 10_000:
        log.error("Recording unusable (%d bytes)", size)
        return False
    log.info("Stopped: %s. %.2f MB in %.0fs",
             reason, size / 1_048_576, time.monotonic() - started)
    return True


def record(source_url, out_path, duration=15, is_hls=True):
    """Record `duration` seconds to out_path. Returns True on a usable file."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed in this container")

    if is_hls:
        wait_for_playlist(source_url)

    cmd = build_command(source_url, out_path, duration, is_hls)
    log.info("Recording %ss -> %s", duration, out_path)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=duration + 60,
        )
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out")
        return False

    if proc.returncode != 0:
        log.error("ffmpeg exit %s: %s", proc.returncode,
                  proc.stderr.decode("utf-8", "replace")[-800:])

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size < 10_000:  # anything this small is a header and nothing else
        log.error("Recording unusable (%d bytes)", size)
        return False

    log.info("Recorded %.2f MB", size / 1_048_576)
    return True


def download_snapshot(url, out_path, timeout=15):
    """Imou puts alarm stills in picUrlArray. They appear instantly, unlike
    the video, and they expire after about a day -- worth grabbing."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        with open(out_path, "wb") as fh:
            fh.write(resp.content)
        return len(resp.content) > 1000
    except requests.RequestException as exc:
        log.warning("Snapshot download failed: %s", exc)
        return False