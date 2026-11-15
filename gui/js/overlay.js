/* OBS 叠加层 · 状态同步
 * 进度条不用后台每秒推送，而是「锚点 + 时间差」在本地插值，
 * 这样 WS 只在切歌/暂停时发包，长时间播放也不会累积消息。
 */
(function () {
  'use strict';

  const root = document.documentElement;
  const $ = function (id) { return document.getElementById(id); };

  let state = null;
  let ws = null;
  let reconnectTimer = null;

  const BASE = location.origin;
  // 叠加层在 OBS 里必须是透明背景，但直接开浏览器会白底白字看不见，
  // 加 ?preview=1 换成棋盘格底，方便主播确认效果
  const IS_PREVIEW = new URLSearchParams(location.search).has("preview");
  if (IS_PREVIEW) document.body.classList.add("is-preview");

  /* ── 主题 ───────────────────────────────────────────────── */

  // 预览时把整卡缩到视口内，同时在提示里写明真实像素尺寸，
  // 免得在 1250px 的浏览器窗口里看到 1260px 的卡片被裁掉一条边。
  function fitPreview() {
    if (!IS_PREVIEW) return;
    const card = $('card');
    const w = card.offsetWidth || 1;
    const h = card.offsetHeight || 1;
    const fit = Math.min(1, (innerWidth - 40) / w, (innerHeight - 72) / h);
    root.style.setProperty('--fit', String(fit));
    const tip = Math.round(fit * 100);
    document.body.dataset.tip =
      '叠加层预览 · 卡片原生 ' + Math.round(w) + ' x ' + Math.round(h) +
      ' px（预览缩放 ' + tip + '%）· 投到 OBS 是透明背景';
  }

  function applyConfig(cfg) {
    if (!cfg) return;
    const o = cfg.overlay || {};
    document.body.dataset.theme = o.theme || 'glass';
    document.body.dataset.cover = o.show_cover === false ? 'off' : 'on';
    document.body.dataset.requester = o.show_requester === false ? 'off' : 'on';
    document.body.dataset.time = o.show_time === false ? 'off' : 'on';
    document.body.dataset.bg = o.card_background === false ? 'off' : 'on';

    // --u 是所有尺寸的公共倍数：整卡按 3 倍原生排版，OBS 缩小显示才清晰
    const u = Number(o.scale) > 0 ? Number(o.scale) : 3;
    root.style.setProperty('--u', String(u));
    root.style.setProperty('--card-w', (o.card_width || 420) + 'px');
    root.style.setProperty('--radius', (o.corner_radius || 18) + 'px');
    root.style.setProperty('--blur', (o.blur || 18) + 'px');
    root.style.setProperty('--opacity', String(o.opacity != null ? o.opacity : 0.62));
    if (o.accent) root.style.setProperty('--accent', o.accent);
  }

  /* ── 渲染 ───────────────────────────────────────────────── */

  function fmtTime(ms) {
    if (!ms || ms < 0) return '0:00';
    const total = Math.floor(ms / 1000);
    return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
  }

  function render() {
    const card = $('card');
    const track = state && state.track;

    if (!track) {
      card.classList.add('is-idle');
      return;
    }
    card.classList.remove('is-idle');

    const cover = $('cover');
    if (track.cover_url) {
      const proxied = BASE + '/api/cover?u=' + encodeURIComponent(track.cover_url);
      if (cover.getAttribute('src') !== proxied) {
        cover.setAttribute('src', proxied);
      }
    } else if (cover.hasAttribute('src')) {
      cover.removeAttribute('src');
    }

    $('title').textContent = track.title || '未知曲目';
    $('artist').textContent = track.artists || '';

    const req = $('requester');
    if (track.requester) {
      req.textContent = '点歌 · ' + track.requester;
      req.hidden = false;
    } else {
      req.hidden = true;
    }

    const fb = $('fallback');
    fb.hidden = !track.fallback;

    $('time-duration').textContent = fmtTime(state.duration || track.duration);

    // 唱片主题暂停时停止旋转
    document.querySelector('.cover-wrap').classList.toggle(
      'is-paused', state.status !== 'playing'
    );
    fitPreview();
  }

  // 只更新时间文本，不再画进度条
  function tick() {
    if (!state || !state.track) return;
    let pos = state.position || 0;
    const dur = state.duration || (state.track && state.track.duration) || 0;
    if (state.status === 'playing' && state.anchor_ts) {
      pos += (Date.now() / 1000 - state.anchor_ts) * 1000;
    }
    if (dur > 0) pos = Math.min(pos, dur);
    pos = Math.max(0, pos);

    $('time-current').textContent = fmtTime(pos);
    $('time-duration').textContent = fmtTime(dur);
  }

  setInterval(tick, 250);

  /* ── 连接 ───────────────────────────────────────────────── */

  function connect() {
    try {
      ws = new WebSocket(BASE.replace(/^http/, 'ws') + '/ws');
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onmessage = function (evt) {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.event === 'config') {
        applyConfig(msg.config);
        return;
      }
      if (msg.status !== undefined) {
        state = msg;
        render();
        tick();
      }
    };

    ws.onclose = scheduleReconnect;
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  }

  async function boot() {
    try {
      const res = await fetch(BASE + '/api/config');
      const data = await res.json();
      if (data.ok) applyConfig(data.data);
    } catch (e) { /* 用默认主题 */ }
    fitPreview();
    connect();
    tick();
  }

  window.addEventListener('resize', fitPreview);

  boot();
})();
