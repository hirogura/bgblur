# bgblur

- バージョン: v.0.1.0
- ポート: 3371

自分でホストする「商品写真の背景ぼかし」サービス. 写真をアップロードすると自動で
前景 (商品) をセグメンテーションして, 背景だけをぼかします. 自動検出が不正確なときは
筆ツールで前景や背景を塗ってから, 塗った領域を基準に背景検出し直せます.

## できること

| 機能 | 説明 |
| --- | --- |
| ドラッグ＆ドロップ / アップロード | 写真をウィンドウへドロップするか, クリックしてファイルを選びます. 複数まとめて読み込めます. |
| 背景の自動検出 | rembg (U2-Net) で前景を抽出し, 背景だけをぼかします. |
| 強さスライダー | ぼかしの強さをスライダーで調整します. 画面にはそのまま反映されます (リアルタイム). |
| 筆ツール | 前景 / 背景の好きな場所を塗って, 自動検出の結果を直します. 太さと同じ大きさの円がカーソル位置に表示されます. 1 歩戻しもできます. |
| 筆を反映して再検出 | 塗った領域を確定情報として OpenCV GrabCut を使い, 背景を検出し直します. |
| 元画像と比較 | 「元画像と比較する」にチェックを入れると, ぼかしを消した状態を見られます. |
| 保存 | PNG / JPEG で保存します. 保存は元の解像度で行われます. |
| 全てクリア | 「写真」の隣にあるボタンで, サーバーに残ったキャッシュ画像を削除します. |
| アップデート / 再起動 | 画面右上から GitHub からの更新と bgblur サービスの再起動ができます. |

## インストール

```shell
git clone https://github.com/hirogura/bgblur.git /opt/bgblur
cd /opt/bgblur
sudo ./scripts/install.sh
```

スクリプトが venv を作り, 依存関係と背景セグメンテーションのモデル (U2-Net) を用意して,
systemd のサービスとして登録・起動します.

- 待ち受けは `127.0.0.1:3371` (localhost のみ) です.
- `tailscale serve` がある環境では `https://<端末名>:3371/` でも公開します.
- LAN などに公開したい場合は `BGBLUR_HOST=0.0.0.0 sudo ./scripts/install.sh`
  のように環境変数を指定してください.

## 使い方

1. 写真をドロップ (またはクリックして選択) します.
2. 自動で前景が検出され, 背景がぼかされます.
3. 強さスライダーでぼかしの濃さを調整します.
4. 検出が不正確なら「前景を塗る」/「背景を塗る」で好きな部分を塗り,
   「筆を反映して再検出」を押します.
5. 「ぼかした写真を保存」で保存します.
6. サーバーに残った画像は「写真」の横の「全てクリア」で削除できます.

## 設定 (環境変数)

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `BGBLUR_HOST` | `127.0.0.1` | 待ち受けるアドレス. `0.0.0.0` で LAN にも公開します. |
| `BGBLUR_PORT` | `3371` | 待ち受けるポート. |
| `BGBLUR_ADMIN_TOKEN` | (なし) | 設定すると「再起動」「アップデート」にトークンが必要になります. |
| `BGBLUR_MAX_UPLOAD` | `41943040` | 1 回のアップロードで受け付ける最大バイト数 (40MB). |
| `BGBLUR_MODEL` | `u2net` | 背景セグメンテーションに使う rembg のモデル名. `u2netp` / `isnet-general-use` / `bria-rmbg` なども指定できます. |
| `BGBLUR_SEGMENT_MAX_SIDE` | `1600` | セグメンテーションに送る画像の長辺の上限 (px). |
| `BGBLUR_REFINE_ITERATIONS` | `3` | GrabCut (筆を反映して再検出) の反復回数. |
| `BGBLUR_CACHE_KEEP` | `30` | サーバーに残すキャッシュ画像の枚数の上限. |
| `BGBLUR_CACHE_MAX_AGE` | `21600` | キャッシュ画像を消すまでの時間 (秒). |
| `BGBLUR_MODEL_DIR` | `./models` | セグメンテーションのモデル (U2-Net) を保存する場所. |

## 内部 API

| メソッド | パス | 説明 |
| --- | --- | --- |
| `GET` | `/` | 画面. |
| `GET` | `/static/*` | CSS / JS. |
| `GET` | `/api/info` | バージョン, リポジトリ URL, モデルの有無. |
| `GET` | `/api/health` | ヘルスチェック. |
| `GET` | `/api/update/status` | アップデートの進行状況. |
| `POST` | `/api/segment` | 画像 (本体) を送ると前景マスク (アルファチャンネル付き PNG, base64) を返します. 送られた画像はキャッシュに残ります. |
| `POST` | `/api/refine` | JSON で `id` / `mask` / `foreground` / `background` を送り, 塗った領域を基準にマスクを作り直します. |
| `POST` | `/api/cache/clear` | サーバーに残ったキャッシュ画像を削除します. |
| `POST` | `/api/restart` | サービスを再起動します. |
| `POST` | `/api/update` | GitHub からアップデートして再起動します. |

## 開発

```shell
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m bgblur --port 3371
.venv/bin/python -m pytest tests -q
```

## 構成

```
bgblur/
  app.py       HTTP サーバーと API
  cache.py     サーバーに残す画像のキャッシュ
  config.py    設定 (環境変数)
  segmenter.py 背景セグメンテーション (rembg / U2-Net) と GrabCut によるマスク修正
  service.py   アップデート (git pull) と再起動
  static/      画面 (HTML / CSS / JS)
systemd/bgblur.service
scripts/install.sh
models/        セグメンテーションのモデル (実行時にダウンロード)
.cache/        受け取った画像のキャッシュ (「全てクリア」で削除)
```
