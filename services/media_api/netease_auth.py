"""Operator-only NetEase QR login and Windows user-bound session storage.

No password is collected. Credentials never enter the bot's prompts or API
responses. Only the NetEase host receives the session Cookie.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import tempfile
import time

import requests

ROOT = Path(__file__).resolve().parents[2]
MAGIC = b"AstrbotChat-NetEase-DPAPI-v1\n"


class LoginError(ValueError):
    """A safe operator error; never includes a Cookie or upstream response."""


def session_path() -> Path:
    state = Path(os.environ.get("ASTRBOT_CHAT_STATE_DIR", str(ROOT / "storage/qq_companion")))
    return state / "auth" / "netease.dpapi"


def _cookie(value: str, *, require_login: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 32768 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise LoginError("netease_cookie_invalid")
    parsed = SimpleCookie()
    try:
        parsed.load(value)
    except CookieError:
        raise LoginError("netease_cookie_invalid") from None
    if not parsed or (require_login and not (parsed.get("MUSIC_U") and parsed["MUSIC_U"].value)):
        raise LoginError("netease_login_cookie_missing")
    return "; ".join(f"{key}={item.value}" for key, item in parsed.items())


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise LoginError("netease_secure_store_requires_windows")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # Both APIs have seven pointer/integer arguments with the same ABI. The
    # unused description and entropy parameters are deliberately null.
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = _Blob(len(data), buffer), _Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; no LOCAL_MACHINE flag, so the current
        # Windows user is required to decrypt this file after restart.
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise LoginError("netease_session_decrypt_failed" if decrypt else "netease_session_encrypt_failed")
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if result.pbData:
            ctypes.memset(result.pbData, 0, result.cbData)
            kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def save_session(cookie: str, path: Path | None = None) -> None:
    cookie = _cookie(cookie, require_login=True)
    payload = json.dumps({"version": 1, "cookie": cookie, "saved_at": int(time.time())}).encode("utf-8")
    encrypted = MAGIC + _dpapi(payload)
    target = path or session_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="netease-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encrypted)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def load_session(path: Path | None = None) -> str:
    target = path or session_path()
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return ""
    if len(data) > 65536 or not data.startswith(MAGIC):
        raise LoginError("netease_session_invalid")
    try:
        payload = json.loads(_dpapi(data[len(MAGIC):], decrypt=True))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise LoginError("netease_session_invalid")
        return _cookie(payload.get("cookie"), require_login=True)
    except (UnicodeError, ValueError, TypeError):
        raise LoginError("netease_session_invalid_or_unreadable") from None


def resolve_cookie() -> str:
    override = os.environ.get("ASTRBOT_CHAT_NETEASE_COOKIE") or os.environ.get("ALYSIA_NETEASE_COOKIE")
    return _cookie(override) if override else load_session()


class NetEaseLogin:
    """Direct HTTPS to NetEase, without a third-party login proxy."""

    def __init__(self, cookie: str = "", session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": "Mozilla/5.0 AstrbotChatSong/1.0",
                                  "Referer": "https://music.163.com/"})
        if cookie:
            parsed = SimpleCookie()
            parsed.load(_cookie(cookie))
            for key, item in parsed.items():
                self.http.cookies.set(key, item.value, domain="music.163.com", path="/", secure=True)

    def close(self) -> None:
        self.http.close()

    def _post(self, route: str, payload: dict, *, allowed=(200,)) -> dict:
        from .song_source import weapi_form

        csrf = next((item.value for item in self.http.cookies if item.name == "__csrf"), "")
        try:
            response = self.http.post("https://music.163.com/weapi/" + route,
                data=weapi_form({**payload, "csrf_token": csrf}),
                timeout=(5, 15), allow_redirects=False)
            try:
                if response.is_redirect:
                    raise LoginError("netease_login_redirect_refused")
                response.raise_for_status()
                data = response.json()
            finally:
                response.close()
        except (requests.RequestException, ValueError):
            raise LoginError("netease_login_request_failed") from None
        if not isinstance(data, dict) or data.get("code") not in allowed:
            code = data.get("code") if isinstance(data, dict) else None
            label = str(code) if type(code) is int else "invalid"
            stage = {"login/qrcode/unikey": "qr_key", "login/qrcode/client/login": "qr_poll",
                     "w/nuser/account/get": "account", "nuser/account/get": "account"}.get(route, "request")
            raise LoginError(f"netease_{stage}_rejected_{label}")
        return data

    def qr_key(self) -> str:
        key = self._post("login/qrcode/unikey", {"type": 3}).get("unikey", "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", key):
            raise LoginError("netease_qr_key_invalid")
        return key

    def poll(self, key: str) -> int:
        # QR authorization is identified by its key. Do not mix anonymous or
        # previous-login cookies into it (NetEase can reject confirmation with
        # code 502 in that case). The successful response supplies new cookies.
        self.http.cookies.clear()
        return self._post("login/qrcode/client/login", {"key": key, "type": 3},
                          allowed=(800, 801, 802, 803))["code"]

    def cookie(self) -> str:
        values = {item.name: item.value for item in self.http.cookies
                  if item.domain.lstrip(".") in {"music.163.com", "163.com"}}
        return _cookie("; ".join(f"{key}={value}" for key, value in values.items()), require_login=True)

    def account(self) -> dict:
        data = self._post("w/nuser/account/get", {})
        account, profile = data.get("account"), data.get("profile")
        if not isinstance(account, dict) or not account.get("id") or not isinstance(profile, dict):
            return {"logged_in": False}
        return {"logged_in": True, "nickname": str(profile.get("nickname") or "")[:80],
                "vip_type": profile.get("vipType", 0)}
