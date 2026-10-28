from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_onebot_configuration_preserves_existing_and_pairs_token():
    configure = load("qq_configure", "scripts/qq_companion/configure.py")
    astr = {"platform": [{"id": "other", "type": "other"}], "platform_settings": {}, "dashboard": {"other": 1}}
    snow = {"networks": {"httpServers": [{"host": "127.0.0.1", "port": 3000, "accessToken": "local-secret"}],
                         "wsClients": [{"name": "unrelated", "url": "ws://127.0.0.1:1234"}]}}
    astr, snow = configure.configure(astr, snow)
    astr, snow = configure.configure(astr, snow)
    paired = [p for p in astr["platform"] if p["id"] == "snowluma"]
    clients = [c for c in snow["networks"]["wsClients"] if c["name"] == "astrbot-chat"]
    assert len(paired) == len(clients) == 1
    assert paired[0]["ws_reverse_token"] == clients[0]["accessToken"] == "local-secret"
    assert paired[0]["ws_reverse_host"] == "127.0.0.1"
    assert clients[0]["reportSelfMessage"] is False
    assert astr["platform"][0]["id"] == "other"
    assert snow["networks"]["wsClients"][0]["name"] == "unrelated"


def test_inbound_guard_deduplicates_across_restarts_and_separates_accounts(tmp_path):
    guard_class = load("qq_guard", "integrations/qq_astrbot/plugin/guard.py").InboundGuard
    db = tmp_path / "seen.db"
    first = guard_class(db)
    assert first.should_process("bot-a", "message-1", "friend-a", kind="FriendMessage", has_image=True, now=100000)
    assert not first.should_process("bot-a", "message-1", "friend-a", now=100001)
    assert not first.should_process("bot-a", "message-2", "bot-a", now=100002)
    first.close()
    second = guard_class(db)
    assert not second.should_process("bot-a", "message-1", "friend-a", now=100003)
    assert second.should_process("bot-b", "message-1", "friend-a", now=100003)
    assert second.db.execute("SELECT kind, has_image FROM inbound_event WHERE account_id = ?", ("bot-a",)).fetchall() == [("FriendMessage", 1)]
    second.close()


def test_snowluma_non_loopback_server_is_rejected():
    configure = load("qq_configure2", "scripts/qq_companion/configure.py")
    snow = {"networks": {"httpServers": [{"host": "0.0.0.0", "port": 3000}], "wsClients": []}}
    try:
        configure.configure({"platform": []}, snow)
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback HTTP server was accepted")


def test_supervisor_only_stops_process_with_matching_creation_time():
    supervisor = load("qq_supervisor", "scripts/qq_companion/supervisor.py")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import psutil

        created_at = psutil.Process(process.pid).create_time()
        supervisor.stop_tree(process.pid, created_at + 100)
        assert process.poll() is None
        supervisor.stop_tree(process.pid, created_at)
        process.wait(timeout=10)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
