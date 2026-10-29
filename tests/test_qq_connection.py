"""Reject a WebUI-only startup and verify OneBot health without sending messages."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from scripts.qq_companion import configure, supervisor


CONFIG = {"networks": {"httpServers": [
    {"host": "127.0.0.1", "port": 3000, "accessToken": "local-test-secret"}]}}


@pytest.mark.parametrize("account,online,good,ready", [
    ("123", True, True, True), ("124", True, True, False),
    ("123", False, True, False), ("123", True, False, False),
])
def test_onebot_readiness_requires_selected_account_and_online_status(monkeypatch, account, online, good, ready):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            assert request.get_header("Authorization") == "Bearer local-test-secret"
            data = {"user_id": account} if request.full_url.endswith("get_login_info") else {"online": online, "good": good}
            return io.BytesIO(json.dumps({"retcode": 0, "data": data}).encode())
    monkeypatch.setattr(supervisor, "build_opener", lambda *args: Opener())
    assert supervisor.onebot_ready(CONFIG, "123") is ready
    assert calls == ["http://127.0.0.1:3000/get_login_info", "http://127.0.0.1:3000/get_status"]


@pytest.mark.parametrize("error", [URLError("not listening"), ValueError("invalid response")])
def test_webui_without_working_onebot_is_not_ready(monkeypatch, error):
    class Opener:
        def open(self, *args, **kwargs): raise error
    monkeypatch.setattr(supervisor, "build_opener", lambda *args: Opener())
    assert not supervisor.onebot_ready(CONFIG, "123")


def test_authentication_failure_does_not_log_token_or_wait_indefinitely(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError("http://127.0.0.1:3000/get_status", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(supervisor, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError, match="authentication failed") as exc:
        supervisor.onebot_ready(CONFIG, "123")
    assert "local-test-secret" not in str(exc.value)


def test_startup_reports_onebot_attachment_timeout_instead_of_success(tmp_path, monkeypatch):
    path = tmp_path / "onebot_123.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setattr(configure, "locate_snowluma_config", lambda *args: path)
    monkeypatch.setattr(supervisor, "STOP_FLAG", tmp_path / "stop")
    with pytest.raises(RuntimeError, match="WebUI started but OneBot is not ready"):
        supervisor.wait_onebot(SimpleNamespace(poll=lambda: None), timeout=0)
