"""bgblur の HTTP サーバー.

標準ライブラリだけで動く. 静的なページと小さな JSON API だけ.

    GET  /                   画面 (bgblur)
    GET  /static/*           静的なファイル (HTML / CSS / JS)
    GET  /api/info           バージョンなどの情報
    GET  /api/health         ヘルスチェック
    GET  /api/update/status  アップデートの進行状況
    POST /api/segment        画像から前景 (背景以外) を Segment
    POST /api/refine         筆で塗った領域を基準にマスクを作り直す
    POST /api/cache/clear    サーバーに残ったキャッシュ画像を削除
    POST /api/restart        サービス再起動
    POST /api/update         GitHub からのアップデート
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, VERSION_LABEL, __version__
from .cache import ImageCache
from .config import SETTINGS, Settings
from .segmenter import MODEL_NAME, ModelMissingError, Segmenter
from .service import REPO_URL, ServiceManager, make_restart_hook

CHUNK = 64 * 1024


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class App:
    """アプリケーションの状態 (セグメンテーションエンジンと操作)."""

    def __init__(
        self,
        settings: Settings = SETTINGS,
        args: list[str] | None = None,
        warm_up: bool = True,
    ) -> None:
        self.settings = settings
        self.segmenter = Segmenter(
            settings.model_dir,
            model_name=settings.segment_model,
            max_side=settings.segment_max_side,
            refine_iterations=settings.refine_iterations,
        )
        self.service = ServiceManager(
            settings.repo_root, restart_hook=make_restart_hook(args)
        )
        self.cache = ImageCache(
            settings.cache_dir,
            keep=settings.cache_keep,
            max_age=settings.cache_max_age,
        )
        self.started_at = time.time()
        if warm_up:
            threading.Thread(
                target=self._warm_up, name="bgblur-warmup", daemon=True
            ).start()

    def _warm_up(self) -> None:
        """起動時に背景セグメンテーションのモデルを準備しておく."""
        try:
            path = self.segmenter.ensure_model()
            print(f"背景セグメンテーションのモデル: {path}", flush=True)
        except Exception as exc:  # noqa: BLE001   モデルが無いでも画面は動く
            print(f"モデルを準備できませんでした: {exc}", flush=True)

    def info(self) -> dict:
        update = self.service.status(include_log=False)
        return {
            "name": APP_NAME,
            "version": __version__,
            "version_label": VERSION_LABEL,
            "uptime": round(time.time() - self.started_at, 1),
            "repo": REPO_URL,
            "model": {
                "name": self.settings.segment_model or MODEL_NAME,
                "ready": self.segmenter.model_exists(),
                "path": str(self.settings.model_path),
            },
            "cache": {"count": self.cache.count(), "dir": str(self.cache.directory)},
            "update": update,
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"{APP_NAME}/{__version__}"
        protocol_version = "HTTP/1.1"

        # --- ヘルパ ---------------------------------------------------------
        def log_message(self, fmt, *args):  # 静かにする
            # リクエストの解析に失敗した handler には self.path が無い.
            path = getattr(self, "path", "") or ""
            if path.startswith("/api/"):
                print(f"{self.address_string()} {fmt % args}", flush=True)

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, data: dict):
            self._send(code, json.dumps(data, ensure_ascii=False).encode())

        def _error(self, code: int, message: str):
            self._json(code, {"error": message})

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > app.settings.max_upload_bytes:
                raise ValueError("ファイルが大きすぎます")
            data = bytearray()
            while len(data) < length:
                chunk = self.rfile.read(min(CHUNK, length - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            return bytes(data)

        def _json_body(self) -> dict:
            data = self._body()
            if not data:
                return {}
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("JSON を解読できませんでした") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON の形式が違います")
            return payload

        def _authorized(self) -> bool:
            """破壊的な操作 (再起動 / アップデート) の認可.

            BGBLUR_ADMIN_TOKEN が設定されていれば一致が必要. 未設定なら
            (自分専用のサーバーのため) 許可する.
            """
            token = app.settings.admin_token
            if not token:
                return True
            given = self.headers.get("X-Bgblur-Token", "")
            return given == token

        def _static(self, name: str):
            root = app.settings.static_dir.resolve()
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                self._error(404, "not found")
                return
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in (
                "application/javascript",
                "image/svg+xml",
            ):
                ctype += "; charset=utf-8"
            data = path.read_bytes()
            if name == "index.html":
                data = data.replace(b"__VERSION__", VERSION_LABEL.encode())
                # 管理トークンは HTML 属性に埋め込むので安全な文字だけ残す
                safe = "".join(
                    ch
                    for ch in app.settings.admin_token
                    if ch.isalnum() or ch in "._-"
                )
                data = data.replace(b"__ADMIN_TOKEN__", safe.encode())
            self._send(200, data, ctype)

        # --- ルーティング ----------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/") :])
            elif path == "/api/info":
                self._json(200, app.info())
            elif path == "/api/health":
                self._json(200, {"status": "ok", "version": __version__})
            elif path == "/api/update/status":
                self._json(200, app.service.status())
            else:
                self._error(404, "not found")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/segment":
                    data = self._body()
                    if not data:
                        self._error(400, "画像がありません")
                        return
                    saved = app.cache.put(data)  # サーバーに残す (「全てクリア」で消せる)
                    app.segmenter.ensure_model()  # 未ダウンロードならここで取得する
                    result = app.segmenter.segment(data)
                    if saved is not None:
                        result["id"] = saved.name
                    self._json(200, result)
                elif path == "/api/refine":
                    payload = self._json_body()
                    image = self._image_of(payload)
                    result = app.segmenter.refine(
                        image,
                        mask=payload.get("mask"),
                        foreground=payload.get("foreground"),
                        background=payload.get("background"),
                        threshold=payload.get("threshold"),
                    )
                    self._json(200, result)
                elif path == "/api/cache/clear":
                    if not self._authorized():
                        self._error(403, "不正なトークンです")
                        return
                    removed = app.cache.clear()
                    self._json(
                        200,
                        {
                            "ok": True,
                            "removed": removed,
                            "count": app.cache.count(),
                        },
                    )
                elif path == "/api/restart":
                    if not self._authorized():
                        self._error(403, "不正なトークンです")
                        return
                    self._json(200, app.service.restart())
                elif path == "/api/update":
                    if not self._authorized():
                        self._error(403, "不正なトークンです")
                        return
                    self._json(200, app.service.start_update())
                else:
                    self._error(404, "not found")
            except ModelMissingError as exc:
                self._error(503, f"セグメンテーションのモデルがありません: {exc}")
            except Exception as exc:  # noqa: BLE001  エラーを画面へ返す
                traceback.print_exc()
                self._error(400, str(exc))

        def _image_of(self, payload: dict) -> bytes:
            """refine 用に元画像を取り出す (キャッシュ or 埋め込み)."""
            name = str(payload.get("id") or "")
            path = app.cache.find(name)
            if path is not None:
                try:
                    return path.read_bytes()
                except OSError:
                    pass
            inline = payload.get("image")
            if isinstance(inline, str) and inline:
                import base64
                import binascii

                if inline.startswith("data:"):
                    _, _, inline = inline.partition(",")
                try:
                    return base64.b64decode(inline, validate=False)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("画像を読み込めませんでした") from exc
            raise ValueError("元画像が見つかりません")

    return Handler


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="bgblur", description="bgblur サーバー")
    ap.add_argument("--host", default=SETTINGS.host)
    ap.add_argument("--port", type=int, default=SETTINGS.port)
    args = ap.parse_args(argv)

    settings = SETTINGS
    settings.host = args.host
    settings.port = args.port

    app = App(settings, args=argv)
    httpd = Server((settings.host, settings.port), make_handler(app))
    print(
        f"{APP_NAME} {VERSION_LABEL} -> http://{settings.host}:{settings.port}/",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
