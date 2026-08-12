"""
Google Drive upload + retention, using an OAuth *user* refresh token.

Why a user token and not a service account: a service account has no Drive
storage quota of its own, so files it creates in a shared folder are rejected
with storageQuotaExceeded unless you have a Workspace Shared Drive. A normal
OAuth refresh token writes into your own 15 GB and costs nothing.

Scope is drive.file (non-sensitive): the app can only see files it created
itself, so it cannot read the rest of your Drive.
"""

import datetime as dt
import json
import logging
import os
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"

_lock = threading.Lock()
_service = None
_folder_id = None


def _load_credentials():
    """Read token JSON from GOOGLE_TOKEN_JSON, or from a file path in
    GOOGLE_TOKEN_FILE (handy for local runs)."""
    raw = os.getenv("GOOGLE_TOKEN_JSON")
    if not raw:
        path = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
        if not os.path.exists(path):
            raise RuntimeError(
                "No Google credentials: set GOOGLE_TOKEN_JSON (contents of "
                "token.json) or GOOGLE_TOKEN_FILE (path to it)."
            )
        raw = open(path, encoding="utf-8").read()

    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError("Token has no refresh_token; regenerate it.")
        creds.refresh(Request())
    return creds


def service():
    global _service
    with _lock:
        if _service is None:
            _service = build(
                "drive", "v3", credentials=_load_credentials(), cache_discovery=False
            )
        return _service


def folder_id():
    """ID of the destination folder, created on first use."""
    global _folder_id
    if _folder_id:
        return _folder_id

    explicit = os.getenv("DRIVE_FOLDER_ID")
    if explicit:
        _folder_id = explicit
        return _folder_id

    name = os.getenv("DRIVE_FOLDER_NAME", "Imou Motion Clips")
    svc = service()
    query = (
        f"name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    found = (
        svc.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=1)
        .execute()
        .get("files", [])
    )
    if found:
        _folder_id = found[0]["id"]
    else:
        created = (
            svc.files()
            .create(body={"name": name, "mimeType": FOLDER_MIME}, fields="id")
            .execute()
        )
        _folder_id = created["id"]
        log.info("Created Drive folder %r (%s)", name, _folder_id)
    return _folder_id


def upload(local_path, drive_name, mime_type="video/mp4"):
    """Upload one file and return its Drive file id."""
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=False)
    meta = {"name": drive_name, "parents": [folder_id()]}
    created = (
        service()
        .files()
        .create(body=meta, media_body=media, fields="id,name,size")
        .execute()
    )
    log.info(
        "Uploaded %s (%s bytes) -> %s",
        created.get("name"),
        created.get("size"),
        created.get("id"),
    )
    return created["id"]


def prune(retention_days):
    """Delete clips older than retention_days so 15 GB never fills up.
    Only touches files this app created (drive.file scope)."""
    if retention_days <= 0:
        return 0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    query = (
        f"'{folder_id()}' in parents and trashed = false "
        f"and createdTime < '{stamp}'"
    )
    svc = service()
    deleted = 0
    page = None
    while True:
        resp = (
            svc.files()
            .list(q=query, fields="nextPageToken, files(id,name)", pageSize=100,
                  pageToken=page)
            .execute()
        )
        for f in resp.get("files", []):
            try:
                svc.files().delete(fileId=f["id"]).execute()
                deleted += 1
            except HttpError as exc:
                log.warning("Could not delete %s: %s", f.get("name"), exc)
        page = resp.get("nextPageToken")
        if not page:
            break
    if deleted:
        log.info("Retention: deleted %d clip(s) older than %dd", deleted, retention_days)
    return deleted


def storage_quota():
    """Best-effort {limit, usage} in bytes; returns None if not permitted."""
    try:
        about = service().about().get(fields="storageQuota").execute()
        q = about.get("storageQuota", {})
        return {"limit": int(q.get("limit", 0)), "usage": int(q.get("usage", 0))}
    except Exception as exc:  # noqa: BLE001 - purely informational
        log.debug("storageQuota unavailable: %s", exc)
        return None
