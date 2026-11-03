from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from scripts.qq_companion import desktop_webui as desktop


def test_browser_uses_separate_local_profile_with_background_mode_disabled(tmp_path, monkeypatch):
    browser = tmp_path / "Browser 中文" / "browser.exe"
    monkeypatch.setattr(desktop, "find_browser", lambda: browser)
    directory = tmp_path / "astrbot chat 中文"
    command = desktop.browser_command(directory)
    assert command[0] == str(browser)
    assert f"--user-data-dir={directory / 'webui-profile'}" in command
    assert "--disable-background-mode" in command
    assert "--disable-sync" in command
    assert f"--app={desktop.WEB_URL}" in command


@pytest.fixture
def session(tmp_path, monkeypatch):
    ui = desktop.DesktopWebUI(tmp_path, tmp_path / "stop.requested")
    observed = NS(visible=True, launched=[], stopped=[], now=100)

    def launch(argv, **kwargs):
        observed.launched.append(argv)
        return NS(stop=lambda: observed.stopped.append(argv))

    monkeypatch.setattr(desktop, "OwnedProcess", launch)
    monkeypatch.setattr(desktop, "browser_command", lambda path: [str(path / "browser")])
    monkeypatch.setattr(desktop, "has_browser_window", lambda _: observed.visible)
    monkeypatch.setattr(desktop.time, "monotonic", lambda: observed.now)
    return ui, observed


def request(ui, request_id="first", **identity):
    desktop.write_json(ui.directory / "desktop.request.json", {
        **ui.identity, "request_id": request_id, **identity,
    })


def test_unmanaged_terminal_run_does_not_stop_without_a_window(session):
    ui, observed = session
    observed.visible = False
    for _ in range(3):
        observed.now += 1000
        assert ui.step()
    assert not ui.stop_flag.exists()
    assert not observed.launched


def test_close_last_window_stops_even_if_browser_stays_in_background(session):
    ui, observed = session
    request(ui)
    assert ui.step()
    assert desktop.read_json(ui.directory / "desktop.response.json")["status"] == "ready"
    observed.visible = False
    assert ui.step()
    observed.now += desktop.CLOSE_GRACE
    assert not ui.step()
    assert ui.stop_flag.exists()


def test_reopen_and_remaining_window_keep_services_alive(session):
    ui, observed = session
    request(ui)
    ui.step()
    observed.visible = False
    ui.step()
    observed.now += 1
    request(ui, "second")
    ui.step()
    observed.visible = True
    observed.now += 10
    assert ui.step()
    observed.now += 100
    assert ui.step()  # Refresh, minimize or close just one window: another still exists.
    assert len(observed.launched) == 2
    assert not ui.stop_flag.exists()


@pytest.mark.parametrize("identity", [{"supervisor_pid": -1}, {"supervisor_created_at": -1}])
def test_stale_supervisor_request_is_ignored(session, identity):
    ui, observed = session
    request(ui, **identity)
    assert ui.step()
    assert not observed.launched


def test_failed_browser_reports_error_and_requests_stack_cleanup(session, monkeypatch):
    ui, observed = session
    request(ui)
    observed.visible = False
    ui.step()
    observed.now += desktop.WINDOW_TIMEOUT
    ui.run()
    assert ui.stop_flag.exists()
    assert desktop.read_json(ui.directory / "desktop.response.json")["status"] == "error"
    assert observed.stopped == observed.launched


def test_browser_launch_failure_also_requests_cleanup(session, monkeypatch):
    ui, _ = session
    request(ui)

    def fail(*args, **kwargs):
        raise OSError("cannot start browser")

    monkeypatch.setattr(desktop, "OwnedProcess", fail)
    ui.run()
    assert ui.stop_flag.exists()
    assert "cannot start browser" in desktop.read_json(ui.directory / "desktop.response.json")["message"]
