"""HTTP API の確認."""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from bgblur import VERSION_LABEL
from bgblur.app import App, Server, make_handler
from bgblur.config import Settings


def make_app(tmp_path, **kwargs):
    settings = Settings(host="127.0.0.1", port=0, repo_root=tmp_path, **kwargs)
    return App(settings, warm_up=False)


def serve(app):
    httpd = Server(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def get(url):
    with urllib.request.urlopen(url, timeout=30) as res:
        return res.status, res.read().decode()


def post(url, data: bytes, headers: dict | None = None, allow_error: bool = False):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as exc:
        if not allow_error:
            raise
        return exc.code, exc.read().decode()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    return make_app(tmp_path_factory.mktemp("repo"))


@pytest.fixture(scope="module")
def server(app):
    httpd, url = serve(app)
    yield url
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def guarded_app(tmp_path_factory):
    return make_app(tmp_path_factory.mktemp("guarded"), admin_token="secret-token")


@pytest.fixture(scope="module")
def guarded_server(guarded_app):
    httpd, url = serve(guarded_app)
    yield url
    httpd.shutdown()
    httpd.server_close()


def test_index_has_version(server):
    status, body = get(f"{server}/")
    assert status == 200
    assert VERSION_LABEL in body
    assert "v.0.4.2" in body


def test_index_has_update_and_restart_buttons(server):
    status, body = get(f"{server}/")
    assert status == 200
    assert "アップデート" in body
    assert "再起動" in body
    assert "btn-update" in body
    assert "btn-restart" in body


def test_index_has_editor_controls(server):
    status, body = get(f"{server}/")
    assert status == 200
    assert "dropzone" in body
    assert "blur-slider" in body
    assert "brush-slider" in body
    assert "btn-refine" in body
    assert "btn-segment" in body
    assert "edge-slider" in body
    assert "輪郭のしきい値" in body
    assert "chk-depth" in body
    assert "chk-trim" in body
    assert "深度ぼかし" in body
    assert "トリミング" in body


def test_info(server):
    status, body = get(f"{server}/api/info")
    assert status == 200
    data = json.loads(body)
    assert data["name"] == "bgblur"
    assert data["version_label"] == VERSION_LABEL
    assert data["repo"].endswith("bgblur.git")
    assert data["model"]["name"]


def test_health(server):
    status, body = get(f"{server}/api/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_static_files_are_served(server):
    status, body = get(f"{server}/static/app.js")
    assert status == 200
    assert "bgblur" in body


def test_segment_rejects_empty_body(server):
    status, body = post(f"{server}/api/segment", b"", allow_error=True)
    assert status == 400
    assert "error" in json.loads(body)


def test_segment_rejects_garbage(server):
    status, body = post(
        f"{server}/api/segment", "これは画像ではありません".encode(), allow_error=True
    )
    assert status in (400, 503)
    assert "error" in json.loads(body)


def test_refine_needs_an_image(server):
    status, body = post(f"{server}/api/refine", b"{}", allow_error=True)
    assert status == 400
    assert "error" in json.loads(body)


def test_update_and_restart_need_the_token(guarded_server):
    status, body = post(f"{guarded_server}/api/restart", b"{}", allow_error=True)
    assert status == 403
    assert "トークン" in json.loads(body)["error"]

    status, body = post(f"{guarded_server}/api/update", b"{}", allow_error=True)
    assert status == 403
    assert "トークン" in json.loads(body)["error"]


def test_segment_caches_the_image_and_clear_removes_it(server, app):
    import cv2
    import numpy as np

    img = np.full((32, 48, 3), 255, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok

    status, body = post(
        f"{server}/api/segment", buf.tobytes(), {"Content-Type": "image/png"}
    )
    assert status in (200, 503)
    assert app.cache.count() >= 1

    status, body = post(f"{server}/api/cache/clear", b"{}")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["removed"] >= 1
    assert app.cache.count() == 0


def test_cache_clear_needs_the_token(guarded_server):
    status, body = post(f"{guarded_server}/api/cache/clear", b"{}", allow_error=True)
    assert status == 403
    assert "トークン" in json.loads(body)["error"]


def test_refine_uses_the_cached_image(server):
    """保存された画像と涂抹り情報からマスクを作り直せる."""
    import cv2
    import numpy as np

    img = np.zeros((120, 160, 3), dtype=np.uint8)
    img[:, :] = (40, 40, 40)
    cv2.circle(img, (80, 60), 30, (10, 10, 220), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok

    status, body = post(
        f"{server}/api/segment", buf.tobytes(), {"Content-Type": "image/png"}
    )
    assert status in (200, 503)
    if status != 200:
        pytest.skip("背景セグメンテーションのモデルが未ダウンロードです")
    data = json.loads(body)
    assert data["id"]
    assert data["mask"]

    strokes = np.zeros((120, 160), dtype=np.uint8)
    cv2.rectangle(strokes, (2, 2), (30, 30), 255, -1)
    ok, sbuf = cv2.imencode(".png", strokes)
    assert ok

    payload = json.dumps(
        {
            "id": data["id"],
            "mask": data["mask"],
            "foreground": base64.b64encode(sbuf.tobytes()).decode("ascii"),
        }
    ).encode("utf-8")
    status, body = post(
        f"{server}/api/refine", payload, {"Content-Type": "application/json"}
    )
    assert status == 200
    refined = json.loads(body)
    assert refined["width"] == 160 and refined["height"] == 120
    assert refined["mask"]
