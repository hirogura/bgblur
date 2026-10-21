"""背景セグメンテーションと, 筆を基準にしたマスクの作り直しのテスト."""

from __future__ import annotations

import base64

import pytest

from bgblur.config import SETTINGS
from bgblur.segmenter import (
    ModelMissingError,
    Segmenter,
    decode_base64_mask,
    encode_mask_png,
)


def make_png(color=(255, 255, 255), size=(48, 32)):
    """テスト用の小さな画像を作る."""
    import cv2
    import numpy as np

    img = np.full((size[1], size[0], 3), color, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def make_scene(size=(300, 200)):
    """背景の上に赤い円を置いた画像."""
    import cv2
    import numpy as np

    img = np.zeros((size[1], size[0], 3), np.uint8)
    cv2.rectangle(img, (0, 0), (size[0], size[1]), (30, 60, 90), -1)
    cv2.circle(img, (size[0] // 2, size[1] // 2), 50, (10, 10, 220), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def has_model() -> bool:
    return SETTINGS.model_path.is_file()


def test_segment_rejects_garbage(tmp_path):
    segmenter = Segmenter(tmp_path)
    with pytest.raises(ValueError):
        segmenter.segment("これは画像ではありません".encode())


def test_segment_needs_the_model(tmp_path):
    segmenter = Segmenter(tmp_path, model_name="u2net")
    assert not segmenter.model_exists()
    with pytest.raises(ModelMissingError):
        segmenter.segment(make_png())


def test_unknown_model_is_reported(tmp_path):
    segmenter = Segmenter(tmp_path, model_name="no-such-model")
    with pytest.raises(ModelMissingError):
        segmenter.ensure_model(allow_download=False)


@pytest.mark.skipif(not has_model(), reason="背景セグメンテーションのモデルが未ダウンロードです")
def test_segment_finds_the_object(tmp_path):
    segmenter = Segmenter(SETTINGS.model_dir, model_name=SETTINGS.segment_model)
    result = segmenter.segment(make_scene())
    assert result["width"] == 300 and result["height"] == 200
    mask = decode_base64_mask(result["mask"])
    assert mask.shape == (200, 300)
    # 円の中は前景, 四隅は背景であること.
    assert mask[100, 150] > 127
    assert mask[5, 5] < 127


@pytest.mark.skipif(not has_model(), reason="背景セグメンテーションのモデルが未ダウンロードです")
def test_refine_uses_the_painted_area(tmp_path):
    import cv2
    import numpy as np

    segmenter = Segmenter(SETTINGS.model_dir, model_name=SETTINGS.segment_model)
    data = make_scene()
    auto = segmenter.segment(data)

    strokes = np.zeros((200, 300), dtype=np.uint8)
    cv2.rectangle(strokes, (5, 5), (60, 60), 255, -1)  # 背景の左上を前景として塗る
    ok, buf = cv2.imencode(".png", strokes)
    assert ok

    refined = segmenter.refine(
        data,
        mask=auto["mask"],
        foreground=base64.b64encode(buf.tobytes()).decode("ascii"),
    )
    mask = decode_base64_mask(refined["mask"])
    assert mask.shape == (200, 300)
    # 塗った場所は前景になる.
    assert mask[20, 20] > 127


def test_refine_needs_an_image(tmp_path):
    segmenter = Segmenter(tmp_path)
    with pytest.raises(ValueError):
        segmenter.refine("これは画像ではありません".encode())


def test_encode_mask_png_keeps_the_mask_in_alpha():
    import cv2
    import numpy as np

    mask = np.zeros((4, 4), dtype=np.uint8)
    mask[1, 1] = 200
    decoded = decode_base64_mask(
        base64.b64encode(encode_mask_png(mask)).decode("ascii")
    )
    assert decoded[1, 1] == 200
    assert decoded[0, 0] == 0
