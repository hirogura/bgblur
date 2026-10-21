"""アップデートと再起動のテスト."""

from __future__ import annotations

import time

from bgblur.service import REPO_URL, ServiceManager


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_repo_url_points_to_github():
    assert REPO_URL == "https://github.com/hirogura/bgblur.git"


def test_status_starts_idle(tmp_path):
    manager = ServiceManager(tmp_path, restart_hook=lambda: None)
    status = manager.status()
    assert status["state"] == "idle"
    assert status["steps"] == []
    assert manager.status(include_log=False).get("log") is None


def test_restart_calls_the_hook(tmp_path):
    called = []
    manager = ServiceManager(tmp_path, restart_hook=lambda: called.append(True))
    result = manager.restart()
    assert result["ok"] is True
    assert wait_for(lambda: bool(called))
    assert manager.status()["state"] == "restarting"


def test_update_fails_outside_a_git_repository(tmp_path):
    manager = ServiceManager(tmp_path, restart_hook=lambda: None)
    manager.start_update()
    assert wait_for(lambda: manager.status()["state"] in ("error", "done"))
    assert manager.status()["state"] == "error"
    assert "アップデート失敗" in manager.status()["message"]


def test_update_status_is_saved_to_disk(tmp_path):
    manager = ServiceManager(tmp_path, restart_hook=lambda: None)
    manager.start_update()
    assert wait_for(lambda: manager.status()["state"] in ("error", "done"))
    state_file = tmp_path / ".state" / "service.json"
    assert state_file.is_file()
    assert "state" in state_file.read_text(encoding="utf-8")
