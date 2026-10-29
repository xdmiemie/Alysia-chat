from __future__ import annotations

import re
from threading import Thread

import pytest
import requests

from scripts.qq_companion import netease_cookie_import as importer
from services.media_api.netease_auth import LoginError


def test_import_verifies_before_persisting_and_rejects_header_injection(monkeypatch):
    saved = []
    class Login:
        def __init__(self, cookie):
            assert cookie == "MUSIC_U=token; __csrf=csrf"
        def account(self):
            return {"logged_in": False}
        def close(self):
            pass
    monkeypatch.setattr(importer, "NetEaseLogin", Login)
    monkeypatch.setattr(importer, "save_session", saved.append)
    with pytest.raises(LoginError, match="verification_failed"):
        importer.import_cookie("token", "csrf")
    assert saved == []
    with pytest.raises(LoginError, match="invalid"):
        importer.import_cookie("token\r\nInjected: yes")
    assert saved == []


def test_loopback_form_requires_host_origin_and_csrf_and_hides_values(monkeypatch, tmp_path):
    imports = []
    def record(music_u, csrf):
        imports.append((music_u, csrf))
        return {"logged_in": True}
    monkeypatch.setattr(importer, "import_cookie", record)
    monkeypatch.setattr(importer, "session_path", lambda: tmp_path / "session")
    server = importer.make_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = requests.Session()
    http.trust_env = False
    url = "http://" + server.expected_host
    try:
        page = http.get(url + "/", timeout=3)
        assert page.status_code == 200
        assert page.headers["Cache-Control"] == "no-store"
        # no-referrer suppresses Origin on real browser form POSTs. The page
        # must keep its same-origin Origin header so Edge can submit this form.
        assert page.headers["Referrer-Policy"] == "same-origin"
        assert 'type="password"' in page.text
        token = re.search(r'name="token" value="([^"]+)"', page.text)[1]
        data = {"token": token, "music_u": "private-token", "csrf": "private-csrf"}
        assert http.post(url + "/save", data=data, timeout=3).status_code == 403
        assert http.post(url + "/save", data=data, headers={"Origin": "null"}, timeout=3).status_code == 403
        assert http.post(url + "/save", data=data, headers={"Origin": "http://evil.example"}, timeout=3).status_code == 403
        assert http.get(url + "/", headers={"Host": "evil.example"}, timeout=3).status_code == 404
        headers = {"Origin": url}
        assert http.post(url + "/save", data={**data, "token": "wrong"}, headers=headers, timeout=3).status_code == 403
        assert not imports
        result = http.post(url + "/save", data=data, headers=headers, timeout=3)
        assert result.status_code == 200
        assert "private-token" not in result.text and "private-csrf" not in result.text
        assert imports == [("private-token", "private-csrf")]
        assert server.account == {"logged_in": True}
    finally:
        http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
