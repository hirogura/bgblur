"""設定値 (環境変数で上書きできる)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    host: str = os.environ.get("BGBLUR_HOST", "0.0.0.0")
    port: int = _env_int("BGBLUR_PORT", 3371)
    max_upload_bytes: int = _env_int("BGBLUR_MAX_UPLOAD", 40 * 1024 * 1024)
    # 背景セグメンテーションに使う rembg のモデル名.
    # u2net (既定) / u2netp / isnet-general-use / bria-rmbg など.
    segment_model: str = os.environ.get("BGBLUR_MODEL", "u2net")
    segment_max_side: int = _env_int("BGBLUR_SEGMENT_MAX_SIDE", 1600)
    refine_iterations: int = _env_int("BGBLUR_REFINE_ITERATIONS", 3)
    cache_keep: int = _env_int("BGBLUR_CACHE_KEEP", 30)
    cache_max_age: int = _env_int("BGBLUR_CACHE_MAX_AGE", 3600 * 6)
    admin_token: str = os.environ.get("BGBLUR_ADMIN_TOKEN", "")
    repo_root: Path = REPO_ROOT
    model_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("BGBLUR_MODEL_DIR", "") or (REPO_ROOT / "models")
        )
    )

    @property
    def model_path(self) -> Path:
        from .segmenter import model_path

        return model_path(self.model_dir, self.segment_model)

    @property
    def cache_dir(self) -> Path:
        """受け取った画像を置いておく場所."""
        return self.repo_root / ".cache" / "images"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"


SETTINGS = Settings()
