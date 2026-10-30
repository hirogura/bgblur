/* bgblur フロントエンド */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const tokenMeta = document.querySelector('meta[name="admin-token"]');
  const ADMIN_TOKEN = tokenMeta ? tokenMeta.content.trim() : '';

  const MAX_PREVIEW_W = 1280;   // 画面上の表示幅の上限
  const MAX_PREVIEW_H = 900;    // 画面上の表示高さの上限
  const SEGMENT_MAX_SIDE = 1600; // セグメンテーションに送る画像の長辺の上限
  const UNDO_LIMIT = 40;        // 筆画の履歴保持数
  const DEPTH_BANDS = 8;        // 深度ぼかしの段階数 (多いほど滑らかで遅い)
  const EDGE_RAMP = 0.12;       // 輪郭のしきい値付近のなじませ幅

  const state = {
    images: [],      // {id, name, bitmap, width, height, mask, strokes, undo}
    currentId: null,
    mode: 'fg',      // fg (前景を塗る) / bg (背景を塗る)
    blur: 40,        // ぼかしの強さ (%)
    depth: true,     // 深度ぼかし (前景から離れるほど強くぼかす)
    threshold: 50,   // 輪郭のしきい値 (%) 前景マスクを前景とみなす境界
    trim: false,     // トリミング (前景だけ残して背景を透過)
    brush: 40,       // 筆の太さ (元の画像ピクセル)
    hide: false,     // 元画像と比較する
    drawing: false,
    pointerId: null,
    cursor: null,    // マウスの位置 (元の画像ピクセル)
    seq: 0,
  };

  const scratch = {};  // 再利用用の作業用キャンバス

  // ---------- 補助 ----------
  function withToken(extra) {
    const headers = Object.assign({}, extra || {});
    if (ADMIN_TOKEN) headers['X-Bgblur-Token'] = ADMIN_TOKEN;
    return headers;
  }

  async function api(path, options = {}) {
    const opts = Object.assign({}, options);
    opts.headers = withToken(opts.headers);
    opts.cache = 'no-store';
    const res = await fetch(path, opts);
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { error: text }; }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  let toastTimer = null;
  function toast(message, kind) {
    const el = $('toast');
    el.textContent = message;
    el.classList.remove('is-err', 'is-ok');
    if (kind === 'err') el.classList.add('is-err');
    if (kind === 'ok') el.classList.add('is-ok');
    el.classList.add('is-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('is-show'), 3600);
  }

  function current() {
    return state.images.find((img) => img.id === state.currentId) || null;
  }

  function baseName(name) {
    return (name || 'image').replace(/\.[^.]+$/, '') || 'image';
  }

  function base64ToBlob(b64, type) {
    const bin = atob(b64 || '');
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: type || 'image/png' });
  }

  async function bitmapFromMask(b64) {
    if (!b64) return null;
    try {
      return await createImageBitmap(base64ToBlob(b64));
    } catch (_) {
      return null;
    }
  }

  // 自動検出のソフトなマスクに輪郭のしきい値を適用したマスクを作る.
  // しきい値より濃い部分は前景 (不透明), 薄い部分は背景 (透過) になる.
  function applyMaskThreshold(source, pct) {
    if (!source) return null;
    const canvas = document.createElement('canvas');
    canvas.width = source.width;
    canvas.height = source.height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(source, 0, 0);
    const lo = (pct / 100) - EDGE_RAMP / 2;
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const px = data.data;
    for (let i = 3; i < px.length; i += 4) {
      const a = px[i] / 255;
      const v = Math.max(0, Math.min(1, (a - lo) / EDGE_RAMP));
      px[i] = Math.round(v * 255);
    }
    ctx.putImageData(data, 0, 0);
    return canvas;
  }

  // ---------- 画像の読み込み ----------
  async function loadFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) => /^image\//.test(f.type));
    if (!files.length) {
      toast('画像ファイルを選んでください', 'err');
      return;
    }
    for (const file of files) {
      try {
        const bitmap = await createImageBitmap(file);
        const img = {
          id: ++state.seq,
          name: file.name || 'image',
          bitmap: bitmap,
          width: bitmap.width,
          height: bitmap.height,
          mask: null,       // 自動検出のマスク (アルファを持つ PNG)
          serverId: null,   // サーバーに保存した画像の名前
          strokes: [],
          undo: [],
        };
        state.images.push(img);
        await segmentImage(img);
      } catch (err) {
        toast(`${file.name || '画像'}: ${err.message}`, 'err');
      }
    }
    if (state.images.length) {
      selectImage(state.images[state.images.length - 1].id);
      renderThumbs();
    }
  }

  function selectImage(id) {
    state.currentId = id;
    const img = current();
    if (!img) return;
    const canvas = $('canvas');
    const overlay = $('overlay');
    const cursor = $('cursor');
    const scale = previewScale(img);
    canvas.width = Math.max(1, Math.round(img.width * scale));
    canvas.height = Math.max(1, Math.round(img.height * scale));
    overlay.width = canvas.width;
    overlay.height = canvas.height;
    cursor.width = canvas.width;
    cursor.height = canvas.height;
    state.cursor = null;
    $('empty-hint').classList.add('is-hidden');
    render();
    renderThumbs();
    renderSegmentHint();
  }

  function previewScale(img) {
    return Math.min(1, MAX_PREVIEW_W / img.width, MAX_PREVIEW_H / img.height);
  }

  // ---------- 背景セグメンテーション ----------
  async function segmentImage(img) {
    const scale = Math.min(1, SEGMENT_MAX_SIDE / Math.max(img.width, img.height));
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(img.bitmap, 0, 0, w, h);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.92));
    if (!blob) throw new Error('画像を変換できませんでした');

    const data = await api('/api/segment', {
      method: 'POST',
      body: blob,
      headers: { 'Content-Type': 'image/jpeg' },
    });

    img.serverId = data.id || null;
    img.mask = data.mask || null;
    img.maskSoftBitmap = await bitmapFromMask(img.mask);
    img.maskBitmap = applyMaskThreshold(img.maskSoftBitmap, state.threshold);
    img.strokes = [];
    img.undo = [];
    return data;
  }

  async function runSegment(img) {
    if (!img) {
      toast('先に写真を選んでください', 'err');
      return;
    }
    const button = $('btn-segment');
    button.classList.add('is-busy');
    try {
      await segmentImage(img);
      render();
      renderSegmentHint();
      toast('背景を再検出しました', 'ok');
    } catch (err) {
      toast(`背景の検出に失敗しました: ${err.message}`, 'err');
    } finally {
      button.classList.remove('is-busy');
    }
  }

  // ---------- 筆を基準にマスクを作り直す ----------
  function strokeCanvas(img, mode) {
    const canvas = document.createElement('canvas');
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = '#fff';
    ctx.fillStyle = '#fff';
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (const stroke of img.strokes) {
      if (stroke.mode !== mode) continue;
      const points = stroke.points;
      if (!points.length) continue;
      ctx.beginPath();
      ctx.lineWidth = Math.max(1, stroke.size);
      ctx.moveTo(points[0].x, points[0].y);
      if (points.length === 1) {
        ctx.lineTo(points[0].x + 0.01, points[0].y);
      } else {
        for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i].x, points[i].y);
      }
      ctx.stroke();
    }
    return canvas;
  }

  async function runRefine(img) {
    if (!img) {
      toast('先に写真を選んでください', 'err');
      return;
    }
    if (!img.strokes.length) {
      toast('先に筆で前景や背景を塗ってください', 'err');
      return;
    }
    const button = $('btn-refine');
    button.classList.add('is-busy');
    try {
      const payload = { id: img.serverId, mask: img.mask, threshold: state.threshold };
      const fg = strokeCanvas(img, 'fg');
      const bg = strokeCanvas(img, 'bg');
      if (hasStroke(img, 'fg')) payload.foreground = fg.toDataURL('image/png');
      if (hasStroke(img, 'bg')) payload.background = bg.toDataURL('image/png');

      const data = await api('/api/refine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      img.mask = data.mask || null;
      img.maskSoftBitmap = await bitmapFromMask(img.mask);
      img.maskBitmap = applyMaskThreshold(img.maskSoftBitmap, state.threshold);
      render();
      renderSegmentHint();
      toast('筆の塗りを反映しました', 'ok');
    } catch (err) {
      toast(`再検出に失敗しました: ${err.message}`, 'err');
    } finally {
      button.classList.remove('is-busy');
    }
  }

  function hasStroke(img, mode) {
    return img.strokes.some((s) => s.mode === mode && s.points.length);
  }

  // ---------- 描画 ----------
  function scratchCanvas(key, w, h) {
    let canvas = scratch[key];
    if (!canvas) {
      canvas = document.createElement('canvas');
      scratch[key] = canvas;
    }
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    return canvas;
  }

  function blurRadius(img) {
    // 強さ (%) を画像の短辺に対するぼかし半径 (px) に変換する.
    const base = Math.min(img.width, img.height);
    return Math.max(0.5, (state.blur / 100) * base * 0.06);
  }

  // 前景マスク (自動検出の結果 + 筆で塗った部分) を w x h のキャンバスにする.
  function buildMask(img, w, h) {
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    const scale = w / img.width;
    ctx.clearRect(0, 0, w, h);
    if (img.maskBitmap) ctx.drawImage(img.maskBitmap, 0, 0, w, h);

    ctx.strokeStyle = '#fff';
    ctx.fillStyle = '#fff';
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (const stroke of img.strokes) {
      const points = stroke.points;
      if (!points.length) continue;
      ctx.save();
      // 背景を塗ったところだけは前景から差し引く.
      ctx.globalCompositeOperation = stroke.mode === 'bg' ? 'destination-out' : 'source-over';
      ctx.beginPath();
      ctx.lineWidth = Math.max(1, stroke.size * scale);
      ctx.moveTo(points[0].x * scale, points[0].y * scale);
      if (points.length === 1) {
        ctx.lineTo(points[0].x * scale + 0.01, points[0].y * scale);
      } else {
        for (let i = 1; i < points.length; i += 1) {
          ctx.lineTo(points[i].x * scale, points[i].y * scale);
        }
      }
      ctx.stroke();
      ctx.restore();
    }
    return canvas;
  }

  // 画像と鏡像を敷き詰める. (ox, oy) がオリジナルの左上. 端でぼかしが
  // 薄くならないように, 余白にも内容を伸ばす.
  function blitMirrored(ctx, source, sw, sh, ox, oy) {
    const put = (dx, dy, fx, fy) => {
      ctx.save();
      ctx.translate(dx, dy);
      ctx.scale(fx, fy);
      ctx.drawImage(source, 0, 0, sw, sh);
      ctx.restore();
    };
    put(ox, oy, 1, 1);
    put(ox, oy, -1, 1);
    put(ox + 2 * sw, oy, -1, 1);
    put(ox, oy, 1, -1);
    put(ox, oy + 2 * sh, 1, -1);
    put(ox, oy, -1, -1);
    put(ox + 2 * sw, oy, -1, -1);
    put(ox, oy + 2 * sh, -1, -1);
    put(ox + 2 * sw, oy + 2 * sh, -1, -1);
  }

  // ぼかした画像を描く. 鏡像の余白を足してからぼかすので, 端が暗くならず,
  // 内容も元の位置に戻る (違う半径の層を重ねてもずれない).
  function drawBlurred(ctx, bitmap, w, h, radius) {
    if (radius <= 0.01) {
      ctx.drawImage(bitmap, 0, 0, w, h);
      return;
    }
    const pad = Math.max(2, Math.ceil(radius * 3));
    const lw = Math.max(1, Math.round(w + pad * 2));
    const lh = Math.max(1, Math.round(h + pad * 2));
    const prep = scratchCanvas('blur-prep', lw, lh);
    const pctx = prep.getContext('2d');
    pctx.globalCompositeOperation = 'source-over';
    pctx.clearRect(0, 0, lw, lh);
    blitMirrored(pctx, bitmap, w, h, pad, pad);

    const layer = scratchCanvas('blur', lw, lh);
    const lctx = layer.getContext('2d');
    lctx.globalCompositeOperation = 'source-over';
    lctx.clearRect(0, 0, lw, lh);
    lctx.filter = `blur(${radius}px)`;
    lctx.drawImage(prep, 0, 0);
    lctx.filter = 'none';
    ctx.drawImage(layer, -pad, -pad);
  }

  // 前景マスクから「前景からの近さ」の地図 (w x h, 不透明グレースケール) を作る.
  // マスクをぼかすと境界で 0.5, 内側で 1 になるので 2 倍して 0-1 に伸ばす.
  // (前景と境界付近で 1, 離れるほど 0 に下がる.)
  function buildDepthMap(img, w, h, spread) {
    // ぼかしが画像の外を参照しても減光しないよう, 鏡像でパディングする.
    const pad = Math.max(2, Math.ceil(spread * 2));
    const lw = Math.max(1, w + pad * 2);
    const lh = Math.max(1, h + pad * 2);
    const gray = scratchCanvas('depth-gray', lw, lh);
    const gctx = gray.getContext('2d');
    gctx.globalCompositeOperation = 'source-over';
    gctx.fillStyle = '#000';
    gctx.fillRect(0, 0, lw, lh);
    blitMirrored(gctx, buildMask(img, w, h), w, h, pad, pad);

    const full = scratchCanvas('depth-full', lw, lh);
    const fctx = full.getContext('2d');
    fctx.globalCompositeOperation = 'source-over';
    fctx.clearRect(0, 0, lw, lh);
    fctx.filter = `blur(${spread}px) brightness(2)`;
    fctx.drawImage(gray, 0, 0);
    fctx.filter = 'none';

    const near = scratchCanvas('depth-near', w, h);
    const nctx = near.getContext('2d');
    nctx.globalCompositeOperation = 'source-over';
    nctx.clearRect(0, 0, w, h);
    nctx.drawImage(full, -pad, -pad);
    return near;
  }

  // 近さの地図を「しきい値より近い領域だけ残すマスク」に変えるフィルタ.
  function nearMaskFilter(threshold, ramp) {
    const contrast = 1 + (2 * threshold) / ramp;
    return `brightness(${1 / (ramp * contrast)}) contrast(${contrast})`;
  }

  // 深度ぼかし: 全力のぼかしを敷き, 前景に近い領域ほど弱いぼかしで上書きする.
  // 段階ごとのマスクは近さの地図をしきい値で刻んで作る.
  function drawDepthBlurred(ctx, img, w, h, radius) {
    const bands = DEPTH_BANDS;
    // ぼかし (%) を上げるほど最大半径も, 前景から最大ぼけに達するまでの
    // 距離も伸ばす. 100% なら等方的なぼかしの 2 倍まで強まり, 遠景ほど
    // 大きな差がつく.
    const p = state.blur / 100;
    const maxRadius = radius * (1 + p);
    // 前景の境界からこの距離でぼかしが最大強度になる (短辺比)
    const spread = Math.max(8, Math.min(w, h) * (0.12 + 0.18 * p));
    const near = buildDepthMap(img, w, h, spread);

    drawBlurred(ctx, img.bitmap, w, h, maxRadius);

    const layer = scratchCanvas('depth-layer', w, h);
    const mask = scratchCanvas('depth-mask', w, h);
    for (let k = bands - 1; k >= 1; k -= 1) {
      const lctx = layer.getContext('2d');
      lctx.globalCompositeOperation = 'source-over';
      lctx.clearRect(0, 0, w, h);
      drawBlurred(lctx, img.bitmap, w, h, (maxRadius * (k - 0.5)) / bands);

      const mctx = mask.getContext('2d');
      mctx.globalCompositeOperation = 'source-over';
      mctx.clearRect(0, 0, w, h);
      mctx.filter = nearMaskFilter((bands - k) / bands, 1 / bands);
      mctx.drawImage(near, 0, 0);
      mctx.filter = 'none';

      lctx.globalCompositeOperation = 'destination-in';
      lctx.drawImage(mask, 0, 0);
      lctx.globalCompositeOperation = 'source-over';
      ctx.drawImage(layer, 0, 0);
    }
  }

  // 背景の描画. 深度ぼかしが ON なら前景から離れるほど強くぼかす.
  function drawBackground(ctx, img, w, h, radius) {
    if (state.depth && radius >= 1) {
      drawDepthBlurred(ctx, img, w, h, radius);
      return;
    }
    drawBlurred(ctx, img.bitmap, w, h, radius);
  }

  // 前景だけを描く (マスクの外 = 背景は透過のまま).
  function drawForeground(ctx, img, w, h) {
    const fg = scratchCanvas('fg', w, h);
    const fctx = fg.getContext('2d');
    fctx.globalCompositeOperation = 'source-over';
    fctx.clearRect(0, 0, w, h);
    fctx.drawImage(img.bitmap, 0, 0, w, h);
    fctx.globalCompositeOperation = 'destination-in';
    fctx.drawImage(buildMask(img, w, h), 0, 0);
    fctx.globalCompositeOperation = 'source-over';
    ctx.drawImage(fg, 0, 0);
  }

  function render() {
    const img = current();
    const canvas = $('canvas');
    const overlay = $('overlay');
    const cursor = $('cursor');
    if (!img) {
      canvas.width = 0;
      canvas.height = 0;
      overlay.width = 0;
      overlay.height = 0;
      cursor.width = 0;
      cursor.height = 0;
      return;
    }
    const w = canvas.width;
    const h = canvas.height;
    const scale = w / img.width;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);

    if (state.hide) {
      ctx.drawImage(img.bitmap, 0, 0, w, h);
    } else if (state.trim) {
      // トリミング = 前景だけ (背景は透過)
      drawForeground(ctx, img, w, h);
    } else {
      // 背景 = ぼかした画像 (深度ぼかしなら離れるほど強い)
      drawBackground(ctx, img, w, h, blurRadius(img) * scale);

      // 前景 = マスクの所だけ元の画像
      drawForeground(ctx, img, w, h);
    }

    drawOverlay(img, scale);
  }

  function drawOverlay(img, scale) {
    const overlay = $('overlay');
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0, 0, overlay.width, overlay.height);
  }

  // ---------- 筆のカーソル ----------
  function drawCursor() {
    const canvas = $('cursor');
    const img = current();
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!img || !state.cursor) return;

    const scale = canvas.width / img.width;
    const x = state.cursor.x * scale;
    const y = state.cursor.y * scale;
    const r = Math.max(2, (state.brush / 2) * scale);  // 筆の太さと同じ大きさ
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = state.mode === 'bg' ? 'rgba(255, 120, 120, 0.16)' : 'rgba(120, 220, 255, 0.16)';
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.55)';
    ctx.stroke();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
    ctx.stroke();
  }

  // ---------- 筆ツール ----------
  function toImageCoords(event) {
    const canvas = $('canvas');
    const rect = canvas.getBoundingClientRect();
    const img = current();
    if (!img || !rect.width) return { x: 0, y: 0 };
    return {
      x: ((event.clientX - rect.left) / rect.width) * img.width,
      y: ((event.clientY - rect.top) / rect.height) * img.height,
    };
  }

  function pushUndo(img) {
    img.undo.push(JSON.stringify(img.strokes));
    if (img.undo.length > UNDO_LIMIT) img.undo.shift();
  }

  function undo(img) {
    if (!img || !img.undo.length) {
      toast('戻せる筆画がありません', 'err');
      return;
    }
    img.strokes = JSON.parse(img.undo.pop());
    render();
    toast('1 つ前の筆画に戻しました', 'ok');
  }

  function onPointerDown(event) {
    const img = current();
    if (!img) return;
    const point = toImageCoords(event);
    state.cursor = point;
    drawCursor();
    event.preventDefault();
    $('canvas').setPointerCapture(event.pointerId);
    state.drawing = true;
    state.pointerId = event.pointerId;
    pushUndo(img);
    img.strokes.push({ mode: state.mode, size: state.brush, points: [point] });
    render();
  }

  function onPointerMove(event) {
    const img = current();
    if (!img) return;
    const point = toImageCoords(event);
    state.cursor = point;
    if (!state.drawing || event.pointerId !== state.pointerId) {
      drawCursor();
      return;
    }
    const stroke = img.strokes[img.strokes.length - 1];
    if (!stroke) return;
    const last = stroke.points[stroke.points.length - 1];
    if (Math.hypot(point.x - last.x, point.y - last.y) < 2) return;
    stroke.points.push(point);
    render();
    drawCursor();  // ドラッグ中も円をカーソルに追従させる
  }

  function onPointerUp(event) {
    if (event.pointerId !== state.pointerId) return;
    state.drawing = false;
    state.pointerId = null;
  }

  function onPointerLeave() {
    if (state.drawing) return;  // ドラッグ中は消さない
    state.cursor = null;
    drawCursor();
  }

  // ---------- サムネイル ----------
  function renderThumbs() {
    const box = $('thumbs');
    box.textContent = '';
    for (const img of state.images) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'thumb' + (img.id === state.currentId ? ' is-active' : '');
      button.title = img.name;
      const thumb = document.createElement('canvas');
      const size = 52;
      const scale = Math.min(size / img.width, size / img.height);
      thumb.width = Math.max(1, Math.round(img.width * scale));
      thumb.height = Math.max(1, Math.round(img.height * scale));
      thumb.getContext('2d').drawImage(img.bitmap, 0, 0, thumb.width, thumb.height);
      button.append(thumb);
      button.addEventListener('click', () => selectImage(img.id));
      box.append(button);
    }
  }

  function renderSegmentHint() {
    const hint = $('segment-hint');
    const img = current();
    if (!img) {
      hint.textContent = '写真を追加すると自動で背景を検出します';
      return;
    }
    hint.textContent = img.mask
      ? '背景を検出しました. 筆で直したいときは「筆を反映して再検出」を押してください'
      : 'まだ背景を検出していません';
  }

  // ---------- 保存 ----------
  function renderFullSize(img) {
    const canvas = document.createElement('canvas');
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext('2d');
    if (state.hide) {
      ctx.drawImage(img.bitmap, 0, 0);
      return canvas;
    }
    if (state.trim) {
      // トリミング = 前景だけ (背景は透過)
      drawForeground(ctx, img, img.width, img.height);
      return canvas;
    }
    drawBackground(ctx, img, img.width, img.height, blurRadius(img));
    drawForeground(ctx, img, img.width, img.height);
    return canvas;
  }

  function save() {
    const img = current();
    if (!img) {
      toast('写真がありません', 'err');
      return;
    }
    const mime = $('format').value;
    const ext = mime === 'image/png' ? 'png' : 'jpg';
    const suffix = state.trim ? 'trim' : 'blur';
    const canvas = renderFullSize(img);
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          toast('保存できませんでした', 'err');
          return;
        }
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${baseName(img.name)}-${suffix}.${ext}`;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        if (state.trim && mime === 'image/jpeg') {
          toast('JPEG は透過を保持できません (背景は黒になります)');
        } else {
          toast('保存しました', 'ok');
        }
      },
      mime,
      0.92
    );
  }

  // ---------- アップデート / 再起動 ----------
  const modal = $('modal');
  $('modal-close').addEventListener('click', () => modal.classList.add('is-hidden'));

  function openModal(title, body) {
    $('modal-title').textContent = title;
    $('modal-body').textContent = body;
    modal.classList.remove('is-hidden');
  }

  async function waitForService(timeoutMs) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      try {
        const res = await fetch('/api/health', { cache: 'no-store' });
        if (res.ok) return true;
      } catch (_) { /* 再起動中 */ }
      await new Promise((r) => setTimeout(r, 800));
    }
    return false;
  }

  $('btn-update').addEventListener('click', async () => {
    const btn = $('btn-update');
    btn.classList.add('is-busy');
    openModal('アップデート', '開始しています...');
    try {
      await api('/api/update', { method: 'POST' });
      let last = '';
      for (;;) {
        let status;
        try {
          status = await api('/api/update/status');
        } catch (_) {
          await new Promise((r) => setTimeout(r, 1200));
          continue;
        }
        const lines = [];
        (status.steps || []).forEach((s) => {
          const mark = { running: '…', done: '✓', error: '×' }[s.state] || '·';
          lines.push(`${mark} ${s.name}${s.output ? ` — ${s.output}` : ''}`);
        });
        const text = lines.join('\n') + (status.message ? `\n\n${status.message}` : '');
        if (text !== last) {
          $('modal-body').textContent = text;
          last = text;
        }
        if (status.state === 'error') {
          toast(status.message || 'アップデートに失敗しました', 'err');
          break;
        }
        if (status.state === 'done' || status.state === 'idle') {
          $('modal-body').textContent += '\n\n再起動しています...';
          const back = await waitForService(120000);
          toast(back ? 'アップデートが完了しました' : '再起動に時間がかかっています', back ? 'ok' : '');
          break;
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.classList.remove('is-busy');
      setTimeout(() => modal.classList.add('is-hidden'), 1500);
    }
  });

  $('btn-restart').addEventListener('click', async () => {
    const btn = $('btn-restart');
    btn.classList.add('is-busy');
    try {
      await api('/api/restart', { method: 'POST' });
      toast('再起動しています...');
      const back = await waitForService(30000);
      if (back) {
        toast('再起動しました', 'ok');
        window.location.reload();
      } else {
        toast('再起動に時間がかかっています', '');
      }
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.classList.remove('is-busy');
    }
  });

  // ---------- 操作 ----------
  const dropzone = $('dropzone');
  const fileInput = $('file-input');

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', () => {
    loadFiles(fileInput.files);
    fileInput.value = '';
  });

  ['dragenter', 'dragover'].forEach((name) => {
    window.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add('is-over');
    });
  });
  ['dragleave', 'drop'].forEach((name) => {
    window.addEventListener(name, (event) => {
      event.preventDefault();
      if (name === 'dragleave' && event.relatedTarget) return;
      dropzone.classList.remove('is-over');
    });
  });
  window.addEventListener('drop', (event) => {
    event.preventDefault();
    if (event.dataTransfer && event.dataTransfer.files.length) {
      loadFiles(event.dataTransfer.files);
    }
  });

  const canvas = $('canvas');
  canvas.classList.add('is-brush');
  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('pointercancel', onPointerUp);
  canvas.addEventListener('pointerleave', onPointerLeave);

  const modes = {
    'mode-fg': {
      mode: 'fg',
      hint: 'ドラッグした場所が前景として残ります (背景がぼかされます)',
    },
    'mode-bg': {
      mode: 'bg',
      hint: 'ドラッグした場所は背景として扱われます (前景からは外れます)',
    },
  };
  Object.keys(modes).forEach((id) => {
    $(id).addEventListener('click', () => {
      Object.keys(modes).forEach((key) => $(key).classList.remove('is-active'));
      $(id).classList.add('is-active');
      state.mode = modes[id].mode;
      $('mode-hint').textContent = modes[id].hint;
      drawCursor();
    });
  });

  const blurSlider = $('blur-slider');
  blurSlider.addEventListener('input', () => {
    state.blur = Number(blurSlider.value);
    $('blur-value').textContent = `${state.blur}%`;
    render();
  });

  const brushSlider = $('brush-slider');
  brushSlider.addEventListener('input', () => {
    state.brush = Number(brushSlider.value);
    $('brush-value').textContent = `${state.brush}px`;
    drawCursor();
  });

  $('chk-hide').addEventListener('change', (event) => {
    state.hide = event.target.checked;
    render();
  });

  $('chk-depth').addEventListener('change', (event) => {
    state.depth = event.target.checked;
    if (state.depth) {
      state.trim = false;
      $('chk-trim').checked = false;
    }
    render();
  });

  // トリミング (前景だけ残す) は深度ぼかしと排他.
  $('chk-trim').addEventListener('change', (event) => {
    state.trim = event.target.checked;
    if (state.trim) {
      state.depth = false;
      $('chk-depth').checked = false;
    }
    render();
  });

  // 輪郭のしきい値. 保存しておいたソフトなマスクを即座に塗り直す.
  const edgeSlider = $('edge-slider');
  edgeSlider.addEventListener('input', () => {
    state.threshold = Number(edgeSlider.value);
    $('edge-value').textContent = `${state.threshold}%`;
    for (const img of state.images) {
      if (img.maskSoftBitmap) {
        img.maskBitmap = applyMaskThreshold(img.maskSoftBitmap, state.threshold);
      }
    }
    render();
  });

  $('btn-segment').addEventListener('click', () => runSegment(current()));
  $('btn-refine').addEventListener('click', () => runRefine(current()));
  $('btn-clear-mask').addEventListener('click', () => {
    const img = current();
    if (!img) return;
    img.mask = null;
    img.maskBitmap = null;
    img.maskSoftBitmap = null;
    img.strokes = [];
    img.undo = [];
    render();
    renderSegmentHint();
    toast('マスクを消しました');
  });

  $('btn-undo').addEventListener('click', () => undo(current()));
  $('btn-clear-strokes').addEventListener('click', () => {
    const img = current();
    if (!img || !img.strokes.length) return;
    pushUndo(img);
    img.strokes = [];
    render();
  });

  $('btn-clear-cache').addEventListener('click', async () => {
    const btn = $('btn-clear-cache');
    btn.classList.add('is-busy');
    try {
      const data = await api('/api/cache/clear', { method: 'POST' });
      const removed = data.removed || 0;
      toast(
        removed ? `サーバーのキャッシュ画像を ${removed} 件削除しました` : 'キャッシュ画像はありません',
        'ok'
      );
    } catch (err) {
      toast(`キャッシュの削除に失敗しました: ${err.message}`, 'err');
    } finally {
      btn.classList.remove('is-busy');
    }
  });

  $('btn-save').addEventListener('click', save);

  // ---------- 起動時 ----------
  (async () => {
    try {
      const info = await api('/api/info');
      if (info.version_label) $('version').textContent = info.version_label;
      document.title = `${info.name || 'bgblur'} ${info.version_label || ''}`.trim();
      if (info.repo) $('btn-update').title = `${info.repo} から最新版に更新します`;
      if (info.model && !info.model.ready) {
        toast('背景セグメンテーションのモデルをダウンロードしています (初回だけ少しかかります)');
      }
    } catch (_) { /* オフラインでも画面は使える */ }
  })();

  renderSegmentHint();
})();
