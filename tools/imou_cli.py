"""
Imou helper CLI. Run from the project root with IMOU_APP_ID / IMOU_APP_SECRET
set (or a .env you have sourced).

    python tools/imou_cli.py devices
    python tools/imou_cli.py hls <deviceId> [streamId]
    python tools/imou_cli.py new-secret
    python tools/imou_cli.py set-callback https://your-app.onrender.com/hook/<secret>
    python tools/imou_cli.py get-callback
    python tools/imou_cli.py disable-callback
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from imou import ImouClient  # noqa: E402

client = ImouClient(
    app_id=os.getenv("IMOU_APP_ID", ""),
    app_secret=os.getenv("IMOU_APP_SECRET", ""),
    base_url=os.getenv("IMOU_BASE_URL", "https://openapi.easy4ip.com/openapi"),
)


SAFE_SECRET_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def validate_callback_url(url):
    """Catch the two failure modes that produce OP1003: a shell-mangled URL,
    and a secret containing characters that are not legal in a URL path."""
    ok = True
    if not url.startswith("https://"):
        print(f"! Must be https, got: {url}")
        ok = False
    if " " in url:
        print("! URL contains a space -- your shell split the argument.")
        ok = False

    path = url.split("/hook/", 1)[-1] if "/hook/" in url else ""
    bad = sorted(set(path) - SAFE_SECRET_CHARS)
    if bad:
        print(f"! Secret contains characters unsafe in a URL/shell: {bad}")
        print("  Generate a clean one:  python tools/imou_cli.py new-secret")
        print("  then update WEBHOOK_SECRET on Render and redeploy.")
        ok = False
    elif not path:
        print("! No /hook/<secret> segment found in the URL.")
        ok = False
    return ok


def preflight(url):
    """Imou validates the address, and a callback it cannot reach is useless
    anyway. Confirm our own endpoint answers 200 first."""
    import requests
    print(f"Checking {url} ...")
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"! Unreachable: {exc}")
        return False
    if r.status_code == 404:
        print("! 404 -- the secret in this URL does not match WEBHOOK_SECRET "
              "on Render. Fix the env var (and redeploy) or the URL.")
        return False
    if r.status_code != 200:
        print(f"! HTTP {r.status_code}; Imou requires a 200.")
        return False
    print("  200 OK")
    return True


def list_devices(verbose=False):
    """deviceBaseList first; some accounts only expose one of the `type`
    values, so try each. Fall back to listDeviceDetailsByPage."""
    from imou import ImouError

    found = False
    for list_type in ("bindAndShare", "bind", "share"):
        try:
            for d in client.iter_devices(limit=20, list_type=list_type):
                found = True
                channels = d.get("channels") or [{}]
                names = ", ".join(
                    f"{c.get('channelId')}:{c.get('channelName', '?')}"
                    for c in channels
                )
                print(f"{d.get('deviceId'):<20} bindId={d.get('bindId'):<8} "
                      f"channels[{names}]")
                if verbose:
                    print(json.dumps(d, indent=2, ensure_ascii=False))
            if found:
                print(f"\n(type={list_type})")
                return 0
            print(f"type={list_type}: no devices")
        except ImouError as exc:
            print(f"type={list_type}: {exc}")

    print("\nTrying listDeviceDetailsByPage ...")
    try:
        data = client.list_device_details_by_page()
        for d in data.get("deviceList", []):
            print(f"{d.get('deviceId'):<20} {d.get('deviceName', '')}  "
                  f"status={d.get('deviceStatus')}")
        if not data.get("deviceList"):
            print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    except ImouError as exc:
        print(exc)

    print(
        "\nNo devices returned. The credentials work (accessToken succeeded), "
        "so the camera is not bound to this developer account yet.\n"
        "Console > Device Management > add the device by SN + safety code, or "
        "register the developer account with the same email as your Imou Life app."
    )
    return 1


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]

    if cmd == "devices":
        return list_devices(verbose="-v" in args)

    elif cmd == "hls":
        if not args:
            print("usage: hls <deviceId> [streamId 0=HD 1=SD]")
            return 1
        stream_id = int(args[1]) if len(args) > 1 else 1
        print(client.hls_url(args[0], "0", stream_id))

    elif cmd == "set-callback":
        if not args:
            print("usage: set-callback <https url> [--skip-check]")
            return 1
        url = args[0]
        if not validate_callback_url(url):
            return 1
        if "--skip-check" not in args and not preflight(url):
            return 1
        print(f"Registering: {url}")
        print(json.dumps(client.set_message_callback(url), indent=2))
        print("Callback registered. Note: one URL per developer account.")

    elif cmd == "new-secret":
        import secrets
        print(secrets.token_urlsafe(32))

    elif cmd == "get-callback":
        print(json.dumps(client.get_message_callback(), indent=2))

    elif cmd == "disable-callback":
        current = client.get_message_callback().get("callbackUrl", "http://localhost")
        print(json.dumps(client.set_message_callback(current, status="off"), indent=2))

    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))