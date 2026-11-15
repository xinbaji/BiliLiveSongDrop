/* B 站点歌机 · 通信与状态
 * 注意：页面是 file:// 加载的，不能用 ES Module（会被 CORS 拦），
 * 所以这里用普通 script + 全局命名空间。
 */
(function (global) {
  'use strict';

  const BS = global.BS || (global.BS = {});

  BS.state = {
    base: '',
    player: null,
    queue: [],
    playlists: [],
    config: null,
    auth: { netease: {}, bilibili: {} },
    danmaku: { state: 'off', detail: '' },
    wsConnected: false,
    searchResults: null,
  };

  BS.listeners = [];
  BS.on = function (fn) { BS.listeners.push(fn); };
  BS.emit = function (type, payload) {
    BS.listeners.forEach(function (fn) {
      try { fn(type, payload); } catch (e) { console.error(e); }
    });
  };

  /* ── 端口发现 ─────────────────────────────────────────────
     窗口里加载的是后端自己的地址，同源即可用。
     但要先验一下 /healthz 的 app 标识：万一页面被别的 HTTP 服务器托管
     （比如 pywebview 的文件服务器），同源地址就是错的，必须改问原生桥。
  */
  function waitPywebview(timeout) {
    return new Promise(function (resolve) {
      if (global.pywebview && global.pywebview.api) return resolve(true);
      const timer = setTimeout(function () { resolve(false); }, timeout || 3000);
      global.addEventListener('pywebviewready', function () {
        clearTimeout(timer);
        resolve(true);
      }, { once: true });
    });
  }

  async function isOurBackend(origin) {
    try {
      const res = await fetch(origin + '/healthz', { cache: 'no-store' });
      const data = await res.json();
      return data && data.app === 'bili-singer';
    } catch (e) {
      return false;
    }
  }

  BS.resolveBase = async function () {
    const here = location.origin;
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      if (await isOurBackend(here)) {
        BS.state.base = here;
        return here;
      }
    }

    // 同源不是我们的服务，问原生桥要真实端口
    const hasBridge = await waitPywebview(2500);
    if (hasBridge) {
      try {
        const port = await global.pywebview.api.port();
        if (port) {
          const origin = 'http://127.0.0.1:' + port;
          if (await isOurBackend(origin)) {
            BS.state.base = origin;
            return origin;
          }
        }
      } catch (e) { /* 继续兜底 */ }
    }

    BS.state.base = 'http://127.0.0.1:9527';
    return BS.state.base;
  };

  /* ── HTTP ───────────────────────────────────────────────── */

  BS.request = async function (method, path, body) {
    const opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(BS.state.base + path, opts);
    let data;
    try {
      data = await res.json();
    } catch (e) {
      throw new Error('服务无响应（HTTP ' + res.status + '）');
    }
    if (!data.ok) throw new Error(data.error || ('请求失败 HTTP ' + res.status));
    return data.data;
  };

  BS.get = function (path) { return BS.request('GET', path); };
  BS.post = function (path, body) { return BS.request('POST', path, body || {}); };
  BS.del = function (path) { return BS.request('DELETE', path); };
  BS.put = function (path, body) { return BS.request('PUT', path, body || {}); };

  /* ── WebSocket ──────────────────────────────────────────── */

  let ws = null;
  let wsTimer = null;

  BS.connect = function () {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const url = BS.state.base.replace(/^http/, 'ws') + '/ws';
    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      BS.state.wsConnected = true;
      BS.emit('ws', { connected: true });
    };

    ws.onmessage = function (evt) {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.event && msg.status !== undefined) {
        BS.state.player = msg;
        BS.emit('player', msg);
      }
      if (msg.event === 'log') BS.emit('log', msg.entry);
      if (msg.event === 'auth') BS.emit('auth', msg);
      if (msg.event === 'config') {
        BS.state.config = msg.config;
        BS.emit('config', msg.config);
      }
    };

    ws.onclose = function () {
      BS.state.wsConnected = false;
      BS.emit('ws', { connected: false });
      scheduleReconnect();
    };

    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  };

  function scheduleReconnect() {
    clearTimeout(wsTimer);
    wsTimer = setTimeout(BS.connect, 2000);
  }

  BS.sendPosition = function (position, duration) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'position', position: position, duration: duration }));
    }
  };

  /* ── 首屏数据 ───────────────────────────────────────────── */

  BS.loadInitial = async function () {
    const data = await BS.get('/api/state');
    BS.state.player = data.player;
    BS.state.queue = data.queue;
    BS.state.playlists = data.playlists;
    BS.state.config = data.config;
    BS.state.auth = data.auth;
    BS.state.danmaku = data.danmaku;
    BS.emit('init', data);
  };

  BS.refreshQueue = async function () {
    BS.state.queue = await BS.get('/api/queue');
    BS.emit('queue', BS.state.queue);
  };

  BS.refreshPlaylists = async function () {
    BS.state.playlists = await BS.get('/api/playlists');
    BS.emit('playlists', BS.state.playlists);
  };

  /* ── 工具 ───────────────────────────────────────────────── */

  // 封面统一走后端代理：WebView2 直接访问 B 站图床在某些网络下会静默失败
  BS.coverUrl = function (url) {
    if (!url) return '';
    return BS.state.base + '/api/cover?u=' + encodeURIComponent(url);
  };

  BS.fmtTime = function (ms) {
    if (!ms || ms < 0) return '0:00';
    const total = Math.floor(ms / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m + ':' + String(s).padStart(2, '0');
  };
})(window);
