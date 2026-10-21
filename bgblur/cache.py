"""サーバーに残した画像のキャッシュ.

受け取った画像は一時的にディスクへ保存しておく. 画面の
「全てクリア」ボタンでまとめて削除できるようにし, 保存数と経過時間から
古いものは自動で片付ける.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


class ImageCache:
    """受け取った画像のキャッシュ (ディスクへ書き出すだけ)."""

    def __init__(
        self,
        directory: Path,
        keep: int = 30,
        max_age: float = 3600 * 6,
    ) -> None:
        self.directory = Path(directory)
        self.keep = keep
        self.max_age = max_age
        self._lock = threading.Lock()
        self._seq = 0

    # --- 保存 ---------------------------------------------------------------
    def put(self, data: bytes) -> Path | None:
        """画像を保存する. 保存できなくても処理は続行する."""
        if not data:
            return None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._seq += 1
                name = f"{int(time.time() * 1000):013d}-{self._seq:06d}.img"
                path = self.directory / name
                path.write_bytes(data)
        except OSError:
            return None
        self.purge()
        return path

    # --- 参照 ---------------------------------------------------------------
    def files(self) -> list[Path]:
        try:
            return sorted(p for p in self.directory.iterdir() if p.is_file())
        except OSError:
            return []

    def count(self) -> int:
        return len(self.files())

    def find(self, name: str) -> Path | None:
        """保存した画像の名前からパスを取り出す."""
        if not name:
            return None
        # ディレクトリを抜けた名前や区切り文字を含む名前は受け付けない.
        if "/" in name or "\\" in name or name in (".", ".."):
            return None
        path = self.directory / name
        try:
            if path.is_file():
                return path
        except OSError:
            return None
        return None

    # --- 削除 ---------------------------------------------------------------
    def purge(self) -> int:
        """古いものと保存数を超えたものを削除する."""
        removed = 0
        now = time.time()
        for path in self.files():
            try:
                if now - path.stat().st_mtime > self.max_age:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        files = self.files()
        overflow = files[: max(0, len(files) - self.keep)] if self.keep else files
        for path in overflow:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    def clear(self) -> int:
        """キャッシュを全部消して, 消した数を返す."""
        removed = 0
        for path in self.files():
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed
