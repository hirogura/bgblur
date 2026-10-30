"""背景セグメンテーション (rembg / U2-Net) と, 筆で塗った領域を基準にした
マスクの作り直し (OpenCV GrabCut).

前景マスク (白 = 前景 / 黒 = 背景) を PNG で往復する. ブラウザ側で同じ
形のマスクを描けるように, 返り値は base64 入りの PNG.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import threading
from pathlib import Path

# 既定のモデル. rembg が対応している名前をそのまま使える.
MODEL_NAME = "u2net"

# 長さ 0 の写真は GrabCut に対応していないので使わない.
GRABCUT_MIN_SIDE = 8


class ModelMissingError(RuntimeError):
    """背景セグメンテーションのモデルが手元にない."""


def rembg_home(model_dir: Path) -> Path:
    """rembg がモデルを置くディレクトリ."""
    return Path(model_dir) / "rembg"


def model_path(model_dir: Path, model_name: str = MODEL_NAME) -> Path:
    """モデルファイルの置き場所."""
    return rembg_home(model_dir) / "models" / model_name / f"{model_name}.onnx"


def decode_image(data: bytes):
    """バイト列を cv2 の BGR 画像にする."""
    import cv2
    import numpy as np

    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("画像を読み込めませんでした")
    return image


def encode_mask_png(mask) -> bytes:
    """前景マスクを PNG にする.

    RGB は白で, アルファチャンネルにマスクの値 (0-255) を入れる.
    こうしておくとブラウザ側ではそのまま ``destination-in`` の合成に使える.
    """
    import cv2
    import numpy as np

    array = np.asarray(mask, dtype=np.uint8)
    rgba = np.dstack(
        [
            np.full_like(array, 255),
            np.full_like(array, 255),
            np.full_like(array, 255),
            array,
        ]
    )
    ok, buf = cv2.imencode(".png", rgba)
    if not ok:
        raise ValueError("PNG に変換できませんでした")
    return buf.tobytes()


def decode_base64_image(value: str | None):
    """base64 (data URL 付きでも可) を cv2 の画像にする."""
    import cv2
    import numpy as np

    if not value:
        return None
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    try:
        raw = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64 を解読できませんでした") from exc
    if not raw:
        return None
    buffer = np.frombuffer(raw, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("マスク画像を読み込めませんでした")
    return image


def decode_base64_mask(value: str | None):
    """base64 のマスクを 0-255 の 2 次元配列にして返す.

    アルファチャンネルを持つ PNG (bgblur が返す形式) があればそれを使い,
    グレースケールならそのまま使う.
    """
    import numpy as np

    image = decode_base64_image(value)
    if image is None:
        return None
    if image.ndim == 3:
        if image.shape[2] == 4:
            return np.asarray(image[:, :, 3], dtype=np.uint8)
        return np.asarray(image[:, :, 0], dtype=np.uint8)
    return np.asarray(image, dtype=np.uint8)


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Segmenter:
    """rembg を使って前景マスクを推測する."""

    def __init__(
        self,
        model_dir: Path,
        model_name: str = MODEL_NAME,
        max_side: int = 1600,
        refine_iterations: int = 3,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.model_name = model_name or MODEL_NAME
        self.max_side = max_side
        self.refine_iterations = refine_iterations
        self._lock = threading.RLock()
        self._session = None

    # --- モデル -------------------------------------------------------------
    def model_exists(self) -> bool:
        path = model_path(self.model_dir, self.model_name)
        return path.is_file() and path.stat().st_size > 0

    def _session_class(self):
        from rembg.sessions import sessions_class

        for session_class in sessions_class:
            try:
                if session_class.name() == self.model_name:
                    return session_class
            except Exception:  # noqa: BLE001  名前を持たないクラスは握りつぶす
                continue
        raise ModelMissingError(
            f"未知のモデルです: {self.model_name} "
            "(例: u2net / u2netp / isnet-general-use / bria-rmbg)"
        )

    def download_model(self) -> Path:
        """モデルを ``models/`` へダウンロードする."""
        os.environ["REMBG_HOME"] = str(rembg_home(self.model_dir))
        try:
            path = self._session_class().download_models()
        except Exception as exc:  # noqa: BLE001  ネットワーク等的エラー
            raise ModelMissingError(
                f"モデルのダウンロードに失敗しました: {exc}"
            ) from exc
        return Path(path)

    def ensure_model(self, allow_download: bool = True) -> Path:
        """モデルを用意してそのパスを返す. 無ければダウンロードする."""
        with self._lock:
            if self.model_exists():
                return model_path(self.model_dir, self.model_name)
            if not allow_download:
                raise ModelMissingError("背景セグメンテーションのモデルがありません")
            return self.download_model()

    def _load(self):
        """rembg のセッションを作る (初回のときだけ)."""
        if self._session is not None:
            return self._session
        with self._lock:
            if self._session is not None:
                return self._session
            if not self.model_exists():
                raise ModelMissingError("背景セグメンテーションのモデルがありません")
            os.environ["REMBG_HOME"] = str(rembg_home(self.model_dir))
            try:
                from rembg import new_session
            except ImportError as exc:  # pragma: no cover - 依存が無い環境
                raise ModelMissingError(
                    "rembg がインストールされていません (pip install rembg)"
                ) from exc
            try:
                self._session = new_session(self.model_name)
            except Exception as exc:  # noqa: BLE001
                raise ModelMissingError(
                    f"モデルをロードできませんでした: {exc}"
                ) from exc
            return self._session

    # --- セグメンテーション -------------------------------------------------
    def _resize_limit(self, image):
        """長辺が ``max_side`` を超えないように縮める."""
        import cv2

        height, width = image.shape[:2]
        scale = min(1.0, self.max_side / max(width, height))
        if scale >= 1.0:
            return image
        resized = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        return resized

    def segment(self, data: bytes) -> dict:
        """画像から前景マスクを推測する.

        戻り値は ``{"width", "height", "mask"}``. ``mask`` は base64 の PNG.
        """
        import numpy as np
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("画像を読み込めませんでした") from exc
        image = image.convert("RGB")

        width, height = image.size
        scale = min(1.0, self.max_side / max(width, height))
        if scale < 1.0:
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )

        session = self._load()
        from rembg import remove

        masks = remove(image, session=session, only_mask=True)
        if isinstance(masks, list):
            if not masks:
                raise ValueError("前景マスクを生成できませんでした")
            mask = masks[0]
        else:
            mask = masks
        mask = mask.convert("L")

        array = np.array(mask, dtype=np.uint8)
        return {
            "width": image.width,
            "height": image.height,
            "mask": to_base64(encode_mask_png(array)),
            "model": self.model_name,
        }

    # --- 筆を基準にしたマスクの作り直し -------------------------------------
    def refine(
        self,
        data: bytes,
        mask: str | None = None,
        foreground: str | None = None,
        background: str | None = None,
        threshold: float | None = None,
    ) -> dict:
        """筆で塗った領域を前景/背景の確定情報としてマスクを作り直す.

        ``mask`` は自動検出の結果 (省略可), ``foreground`` / ``background``
        は利用者が塗った領域. どちらも base64 の PNG. ``threshold`` は
        自動検出のマスクを「たぶん前景」とみなす輪郭のしきい値 (0-100%,
        省略時は既定の 50% 相当).
        """
        import cv2
        import numpy as np

        image = decode_image(data)
        height, width = image.shape[:2]
        small = self._resize_limit(image)
        sh, sw = small.shape[:2]
        if min(sh, sw) < GRABCUT_MIN_SIDE:
            raise ValueError("画像が小さすぎます")

        if threshold is None:
            cut = 128
        else:
            cut = int(round(255.0 * max(0.0, min(100.0, float(threshold))) / 100.0))
            cut = max(1, min(255, cut))

        gc_mask = np.full((sh, sw), cv2.GC_PR_BGD, dtype=np.uint8)

        prior = decode_base64_mask(mask)
        if prior is not None:
            prior = cv2.resize(prior, (sw, sh), interpolation=cv2.INTER_LINEAR)
            gc_mask[prior >= cut] = cv2.GC_PR_FGD

        fg = decode_base64_mask(foreground)
        if fg is not None:
            fg = cv2.resize(fg, (sw, sh), interpolation=cv2.INTER_LINEAR)
            gc_mask[fg > 0] = cv2.GC_FGD
            # 塗った領域の周辺は「たぶん前景」とし, GrabCut が広げられるようにする.
            grow = max(2, int(min(sh, sw) * 0.03))
            near = cv2.dilate((fg > 0).astype(np.uint8), np.ones((grow, grow), np.uint8))
            gc_mask[near > 0] = np.where(
                gc_mask[near > 0] == cv2.GC_BGD, cv2.GC_BGD, cv2.GC_PR_FGD
            )

        bg = decode_base64_mask(background)
        if bg is not None:
            bg = cv2.resize(bg, (sw, sh), interpolation=cv2.INTER_LINEAR)
            gc_mask[bg > 0] = cv2.GC_BGD

        has_hint = bool((gc_mask == cv2.GC_FGD).any() or (gc_mask == cv2.GC_BGD).any())
        if has_hint:
            bgd_model = np.zeros((1, 65), np.float64)
            fgd_model = np.zeros((1, 65), np.float64)
            cv2.grabCut(
                small,
                gc_mask,
                None,
                bgd_model,
                fgd_model,
                max(1, self.refine_iterations),
                cv2.GC_INIT_WITH_MASK,
            )

        result = np.where(
            (gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD), 255, 0
        ).astype(np.uint8)
        if (sh, sw) != (height, width):
            result = cv2.resize(result, (width, height), interpolation=cv2.INTER_LINEAR)

        return {
            "width": width,
            "height": height,
            "mask": to_base64(encode_mask_png(result)),
            "model": self.model_name,
        }
