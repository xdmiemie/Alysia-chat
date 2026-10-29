from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import requests

from services.media_api.netease_auth import (LoginError, NetEaseLogin, load_session,
                                             resolve_cookie, save_session, session_path)
from services.media_api.song_source import NetEaseSource
from scripts.qq_companion import netease_login


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ASTRBOT_CHAT_NETEASE_COOKIE", raising=False)
    monkeypatch.delenv("ALYSIA_NETEASE_COOKIE", raising=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_encrypted_session_is_reused_by_a_fresh_process():
    cookie = "MUSIC_U=unit-test-secret-token; __csrf=test-csrf"
    save_session(cookie)
    assert cookie.encode() not in session_path().read_bytes()
    assert b"unit-test-secret-token" not in session_path().read_bytes()
    assert load_session() == cookie
    # The same path is used by the separate media API and song-worker Python
    # processes. A fresh process proves persistence beyond an in-memory cache.
    code = ("from services.media_api.song_source import NetEaseSource; "
            "assert NetEaseSource.from_environment().http.headers['Cookie'] == " + repr(cookie))
    interpreters = [sys.executable]
    model_python = Path(__file__).resolve().parents[1] / "runtime_env" / "python.exe"
    if model_python.is_file():
        interpreters.append(str(model_python))
    for interpreter in interpreters:
        result = subprocess.run([interpreter, "-c", code], capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert not result.stdout


def test_env_precedence_and_no_session(monkeypatch):
    assert resolve_cookie() == ""
    monkeypatch.setenv("ALYSIA_NETEASE_COOKIE", "MUSIC_U=legacy")
    assert resolve_cookie() == "MUSIC_U=legacy"
    monkeypatch.setenv("ASTRBOT_CHAT_NETEASE_COOKIE", "MUSIC_U=current")
    assert NetEaseSource.from_environment().http.headers["Cookie"] == "MUSIC_U=current"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_corrupted_store_is_not_silently_treated_as_anonymous():
    save_session("MUSIC_U=do-not-log-this")
    data = bytearray(session_path().read_bytes())
    data[-1] ^= 1
    session_path().write_bytes(data)
    with pytest.raises(LoginError) as failure:
        NetEaseSource.from_environment()
    assert "do-not-log-this" not in str(failure.value)


@pytest.mark.parametrize("cookie", ["MUSIC_A=anonymous", "MUSIC_U=", "MUSIC_U=secret\r\nX-Test: injected"])
def test_invalid_or_anonymous_cookie_cannot_replace_saved_login(cookie):
    with pytest.raises(LoginError):
        save_session(cookie)
    assert not session_path().exists()


def test_auth_requests_use_only_netease_and_do_not_follow_redirects(monkeypatch):
    http = requests.Session()
    client = NetEaseLogin("MUSIC_U=secret; __csrf=csrf", session=http)
    received = []
    class Response:
        is_redirect = False
        def raise_for_status(self):
            pass
        def json(self):
            return {"code": 802}
        def close(self):
            pass
    monkeypatch.setattr("services.media_api.song_source.weapi_form", lambda payload: payload)
    def post(url, **kwargs):
        received.append((url, kwargs))
        return Response()
    monkeypatch.setattr(http, "post", post)
    assert client.poll("key") == 802
    url, kwargs = received[0]
    assert url == "https://music.163.com/weapi/login/qrcode/client/login"
    assert kwargs["allow_redirects"] is False
    assert kwargs["data"]["csrf_token"] == ""
    assert "secret" not in repr(kwargs)
    assert all(c.domain == "music.163.com" and c.secure for c in http.cookies)


def test_account_status_excludes_tokens_and_rejects_anonymous(monkeypatch):
    client = NetEaseLogin()
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"code": 200, "account": None, "profile": None})
    assert client.account() == {"logged_in": False}
    monkeypatch.setattr(client, "_post", lambda *a, **k: {
        "code": 200, "account": {"id": 1, "token": "secret"},
        "profile": {"nickname": "test", "vipType": 11}})
    assert client.account() == {"logged_in": True, "nickname": "test", "vip_type": 11}


def test_qr_accepts_netease_uuid_keys(monkeypatch):
    client = NetEaseLogin()
    key = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setattr(client, "_post", lambda *a: {"code": 200, "unikey": key})
    assert client.qr_key() == key


class FakeLogin:
    valid = True
    def __init__(self, cookie=""):
        self.provided = cookie
    def qr_key(self):
        return "testkey"
    def poll(self, key):
        return 803
    def cookie(self):
        return "MUSIC_U=qr-unit-test-token"
    def account(self):
        assert self.provided == "MUSIC_U=qr-unit-test-token"
        return {"logged_in": self.valid}
    def close(self):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_authorized_qr_is_verified_saved_and_qr_removed(monkeypatch, capsys):
    monkeypatch.setattr(netease_login, "NetEaseLogin", FakeLogin)
    args = type("Args", (), {"force": True, "status": False, "timeout": 30})()
    assert netease_login.run(args) == 0
    assert load_session() == "MUSIC_U=qr-unit-test-token"
    assert not (session_path().parent / "login" / "qr.png").exists()
    assert "qr-unit-test-token" not in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_unverified_qr_does_not_persist_cookie(monkeypatch):
    monkeypatch.setattr(netease_login, "NetEaseLogin", FakeLogin)
    monkeypatch.setattr(FakeLogin, "valid", False)
    args = type("Args", (), {"force": True, "status": False, "timeout": 30})()
    with pytest.raises(LoginError, match="verification_failed"):
        netease_login.run(args)
    assert not session_path().exists()
    assert not (session_path().parent / "login" / "qr.png").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_poll_errors_have_a_fixed_retry_limit(monkeypatch):
    class FailedLogin(FakeLogin):
        calls = 0
        def poll(self, key):
            self.__class__.calls += 1
            raise LoginError("test_failure")
    monkeypatch.setattr(netease_login, "NetEaseLogin", FailedLogin)
    monkeypatch.setattr(netease_login.time, "sleep", lambda _: None)
    args = type("Args", (), {"force": True, "status": False, "timeout": 30})()
    with pytest.raises(LoginError):
        netease_login.run(args)
    assert FailedLogin.calls == 3
    assert not session_path().exists()


def test_status_does_not_start_a_login(monkeypatch, capsys):
    monkeypatch.setattr(netease_login, "NetEaseLogin", lambda *a: pytest.fail("No login should be started"))
    args = type("Args", (), {"force": False, "status": True})()
    assert netease_login.run(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "login_required"
