"""Scan with the NetEase app, then retain a user-bound encrypted session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.media_api.netease_auth import (LoginError, NetEaseLogin, resolve_cookie,
                                             save_session, session_path, _dpapi)


def run(args) -> int:
    resume = getattr(args, "resume", False)
    if not args.force and not resume:
        cookie = resolve_cookie()
        if cookie:
            client = NetEaseLogin(cookie)
            try:
                account = client.account()
                if account["logged_in"]:
                    if not args.status:
                        save_session(cookie)
                    print(json.dumps({"status": "logged_in" if args.status else "saved", **account},
                                     ensure_ascii=True), flush=True)
                    return 0
            finally:
                client.close()
        if args.status:
            print(json.dumps({"status": "login_required", "logged_in": False}), flush=True)
            return 2

    import qrcode  # Already available in the AstrBot environment; no model loads.

    folder = session_path().parent / "login"
    folder.mkdir(parents=True, exist_ok=True)
    qr = folder / "qr.png"
    pending = folder / "pending.dpapi"
    client = NetEaseLogin()
    try:
        if resume:
            try:
                data = json.loads(_dpapi(pending.read_bytes(), decrypt=True))
                if not 0 <= time.time() - data["created_at"] <= 600:
                    raise LoginError("netease_pending_qr_expired")
                key = data["key"]
            except (ValueError, KeyError, TypeError):
                raise LoginError("netease_pending_qr_unavailable") from None
        else:
            key = client.qr_key()
            # Keep only the short-lived QR key, encrypted, so an operator can
            # resume a failed poll without asking the user to authorize again.
            pending.write_bytes(_dpapi(json.dumps({"created_at": time.time(), "key": key}).encode()))
        url = "https://music.163.com/login?" + urllib.parse.urlencode({"codekey": key})
        qrcode.make(url).save(qr)
        print(json.dumps({"status": "scan_required", "qr_image": str(qr),
                          "timeout_seconds": args.timeout}), flush=True)
        deadline = time.monotonic() + args.timeout
        previous, failures = None, 0
        while time.monotonic() < deadline:
            try:
                code = client.poll(key)
                failures = 0
            except LoginError:
                failures += 1
                if failures >= 3:
                    raise
                time.sleep(3)
                continue
            if code != previous:
                print(json.dumps({"status": {800: "qr_expired", 801: "waiting_for_scan",
                    802: "confirm_on_phone", 803: "authorized"}[code]}), flush=True)
                previous = code
            if code == 800:
                pending.unlink(missing_ok=True)
                return 2
            if code == 803:
                # Validate through a fresh client with the exact Cookie that
                # future service processes will read, before persisting it.
                cookie = client.cookie()
                verifier = NetEaseLogin(cookie)
                try:
                    account = verifier.account()
                finally:
                    verifier.close()
                if not account["logged_in"]:
                    raise LoginError("netease_session_verification_failed")
                save_session(cookie)
                pending.unlink(missing_ok=True)
                print(json.dumps({"status": "saved", **account,
                                  "session_file": str(session_path())}), flush=True)
                return 0
            time.sleep(3)
        print(json.dumps({"status": "qr_timeout"}), flush=True)
        pending.unlink(missing_ok=True)
        return 2
    finally:
        client.close()
        qr.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Check session without starting a QR login")
    mode.add_argument("--force", action="store_true", help="Create a new QR login, ignoring any old session")
    mode.add_argument("--resume", action="store_true", help="Resume the most recent QR poll after a transient error")
    parser.add_argument("--timeout", type=int, default=240, choices=range(30, 601), metavar="30..600")
    args = parser.parse_args()
    try:
        return run(args)
    except LoginError as exc:
        # No exception repr or traceback: upstream/cookie data must not appear
        # in terminal history or logs, even on a failed login.
        print(json.dumps({"status": "error", "error": str(exc)}), flush=True)
        return 1
    except OSError:
        print(json.dumps({"status": "error", "error": "netease_session_storage_failed"}), flush=True)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled"}), flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
