"""
Minimal client for the Imou / Lechange (easy4ip) Open Platform HTTP API.

Signing rule (from the Development Specification):
    raw  = "time:{unix_seconds},nonce:{uuid4},appSecret:{appSecret}"
    sign = lowercase hex MD5(raw)   # UTF-8

Every request is a POST of:
    {"system": {"ver","appId","sign","time","nonce"}, "id": <uuid>, "params": {...}}

Docs: https://open.imoulife.com/book/http/develop.html
"""

import hashlib
import logging
import threading
import time
import uuid

import requests

log = logging.getLogger(__name__)

# Pick the data centre that matches your developer console account.
# Common values:
#   https://openapi.easy4ip.com/openapi      (global default)
#   https://openapi-sg.easy4ip.com/openapi   (Singapore)
#   https://openapi-fk.easy4ip.com/openapi   (Frankfurt)
#   https://openapi-or.easy4ip.com/openapi   (Oregon)
#   https://openapi.lechange.cn/openapi      (mainland China)
DEFAULT_BASE_URL = "https://openapi.easy4ip.com/openapi"


class ImouError(RuntimeError):
    """Raised when the Imou API returns a non-zero result code."""

    def __init__(self, endpoint, code, msg):
        self.endpoint = endpoint
        self.code = code
        self.msg = msg
        super().__init__(f"{endpoint} failed (code={code}): {msg}")


class ImouClient:
    def __init__(self, app_id, app_secret, base_url=DEFAULT_BASE_URL, timeout=20):
        if not app_id or not app_secret:
            raise ValueError("IMOU_APP_ID and IMOU_APP_SECRET are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._token = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    # ---------------------------------------------------------------- signing

    def _sign(self, ts, nonce):
        raw = f"time:{ts},nonce:{nonce},appSecret:{self.app_secret}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def call(self, endpoint, params=None, with_token=True):
        """POST one OpenAPI endpoint and return result.data (a dict)."""
        ts = int(time.time())
        nonce = str(uuid.uuid4())
        payload_params = dict(params or {})
        if with_token:
            payload_params["token"] = self.access_token()

        body = {
            "system": {
                "ver": "1.0",
                "appId": self.app_id,
                "sign": self._sign(ts, nonce),
                "time": ts,
                "nonce": nonce,
            },
            "id": str(uuid.uuid4()),
            "params": payload_params,
        }

        resp = self._session.post(
            f"{self.base_url}/{endpoint}", json=body, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result") or {}
        code = str(result.get("code", ""))
        if code != "0":
            raise ImouError(endpoint, code, result.get("msg", "unknown error"))
        return result.get("data") or {}

    # ------------------------------------------------------------------ token

    def access_token(self):
        """Administrator token, cached until ~5 minutes before it expires."""
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            data = self.call("accessToken", {}, with_token=False)
            self._token = data["accessToken"]
            ttl = int(data.get("expireTime", 3600))
            self._token_expires_at = time.time() + max(ttl - 300, 60)
            log.info("Imou accessToken refreshed (valid %ss)", ttl)
            return self._token

    # ----------------------------------------------------------------- device

    def device_list(self, query_range="1-10"):
        return self.call("deviceBaseList", {"queryRange": query_range})

    # ------------------------------------------------------------ live stream

    def bind_device_live(self, device_id, channel_id="0", stream_id=1):
        """Create a cloud live address for the device. Idempotent-ish: if the
        stream already exists Imou returns an error, so callers fall back to
        get_live_stream_info()."""
        return self.call(
            "bindDeviceLive",
            {
                "deviceId": device_id,
                "channelId": str(channel_id),
                "streamId": int(stream_id),
            },
        )

    def get_live_stream_info(self, device_id, channel_id="0"):
        return self.call(
            "getLiveStreamInfo",
            {"deviceId": device_id, "channelId": str(channel_id)},
        )

    def hls_url(self, device_id, channel_id="0", stream_id=1):
        """Return a playable .m3u8 URL served by Imou's cloud.

        streamId: 0 = main / HD, 1 = sub / SD.
        SD is the sane default on a 512 MB free container.
        """
        stream_id = int(stream_id)

        def _pick(streams):
            for s in streams or []:
                if int(s.get("streamId", -1)) == stream_id and s.get("hls"):
                    return s["hls"]
            for s in streams or []:  # any stream is better than none
                if s.get("hls"):
                    return s["hls"]
            return None

        # 1. Is a live address already bound?
        try:
            url = _pick(self.get_live_stream_info(device_id, channel_id).get("streams"))
            if url:
                return url
        except ImouError as exc:
            log.debug("getLiveStreamInfo: %s", exc)

        # 2. Otherwise create one.
        data = self.bind_device_live(device_id, channel_id, stream_id)
        url = _pick(data.get("streams"))
        if url:
            return url

        # 3. Bind said OK but gave us nothing useful -> re-query.
        url = _pick(self.get_live_stream_info(device_id, channel_id).get("streams"))
        if not url:
            raise ImouError("hls_url", "-1", f"no HLS address for {device_id}")
        return url

    # ------------------------------------------------------------------- push

    def set_message_callback(self, callback_url, callback_flag="alarm", status="on"):
        """Register (or disable) the alarm webhook. One URL per developer
        account -- setting a new one replaces the old one."""
        return self.call(
            "setMessageCallback",
            {
                "callbackFlag": callback_flag,
                "basePush": "2",
                "callbackUrl": callback_url,
                "status": status,
            },
        )

    def get_message_callback(self):
        return self.call("getMessageCallback", {})
