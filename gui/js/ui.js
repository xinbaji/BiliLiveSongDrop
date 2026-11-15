/* B 站点歌机 · 渲染层
 * 安全约定：所有来自网络 / 弹幕的文本一律走 textContent，绝不拼 innerHTML。
 * 观众可以发任意字符，拼 HTML 就是 XSS 入口。
 */
(function (global) {
  'use strict';

  const BS = global.BS;
  const U = {};

  const MODE_LABELS = {
    sequential: '顺序播放',
    random: '随机播放',
    repeat_all: '列表循环',
    repeat_one: '单曲循环',
  };
  const SOURCE_LABELS = { netease: '网易云', bilibili: 'B 站' };

  const $ = function (id) { return document.getElementById(id); };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function icon(path) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', path);
    svg.appendChild(p);
    return svg;
  }

  function smallBtn(path, title, onClick) {
    const btn = el('button', 'mini-btn');
    btn.title = title;
    btn.appendChild(icon(path));
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      onClick(e);
    });
    return btn;
  }

  /* ── 歌单侧栏 ───────────────────────────────────────────── */

  U.renderPlaylists = function (playlists, idleId, activeView) {
    const list = $('playlist-list');
    list.innerHTML = '';

    const queueCount = (BS.state.queue || []).length;
    list.appendChild(buildPlRow('queue', '点歌队列', queueCount, activeView === 'queue', true));

    if (playlists.length) list.appendChild(el('li', 'pl-sep'));

    playlists.forEach(function (pl) {
      const row = buildPlRow(String(pl.id), pl.name, pl.count, String(activeView) === String(pl.id), false);
      if (pl.id === idleId) {
        const flag = el('span', 'pl-flag', '空闲');
        row.insertBefore(flag, row.lastChild);
      }
      list.appendChild(row);
    });
  };

  function buildPlRow(id, name, count, active, isQueue) {
    const row = el('button', 'pl-item');
    row.type = 'button';
    if (active) row.classList.add('is-active');
    row.appendChild(el('span', 'pl-name', name));
    row.appendChild(el('span', 'pl-num', String(count)));
    row.addEventListener('click', function () {
      if (isQueue) U.setView('queue');
      else U.setView(id);
    });
    return row;
  }

  U.setView = function (view) {
    BS.state.view = view;
    const isQueue = view === 'queue';
    $('view-title').textContent = isQueue
      ? '点歌队列'
      : ((BS.state.playlists || []).filter(function (p) { return String(p.id) === String(view); })[0] || {}).name || '歌单';
    $('view-badge').hidden = !isQueue;
    $('import-row').style.display = isQueue ? 'none' : 'flex';
    $('btn-set-idle').style.display = isQueue ? 'none' : '';
    $('btn-clear-view').textContent = isQueue ? '清空队列' : '从歌单删空';

    // 「刷新」只对从远程歌单导入的歌单有意义：它要按远程列表比对差异。
    // 手动建的歌单没有远程来源，点了也无从比对。
    const current = (BS.state.playlists || []).filter(function (p) {
      return String(p.id) === String(view);
    })[0];
    $('btn-refresh-pl').hidden = isQueue || !(current && current.can_refresh);
    U.renderPlaylists(BS.state.playlists || [], (BS.state.config || {}).idle_playlist_id, view);
    U.loadView();
    BS.emit('view', view);
  };

  /* ── 曲目列表 ───────────────────────────────────────────── */

  U.loadView = async function () {
    const view = BS.state.view;
    if (!view) return;
    if (view === 'queue') {
      U.renderQueue(BS.state.queue || []);
      return;
    }
    try {
      const songs = await BS.get('/api/playlists/' + view + '/songs');
      U.renderPlaylist(Number(view), songs);
    } catch (e) {
      U.toast(e.message, 'err');
    }
  };

  U.renderQueue = function (items) {
    const rows = items.map(function (it) {
      return {
        songId: it.song.id,
        title: it.song.title,
        artists: it.song.artists,
        cover: it.song.cover_url,
        duration: it.song.duration,
        requester: it.requester,
        queueId: it.queue_id,
        playing: it.status === 'playing',
      };
    });
    paintTracks(rows, { view: 'queue' });
  };

  U.renderPlaylist = function (pid, songs) {
    const rows = songs.map(function (s) {
      return {
        songId: s.id,
        title: s.title,
        artists: s.artists,
        cover: s.cover_url,
        duration: s.duration,
        requester: '',
        queueId: 0,
        playing: false,
      };
    });
    paintTracks(rows, { view: String(pid) });
  };

  // 一次全量渲染上千条会卡死主线程，先渲染一屏多一点，剩下的滚动到底再补
  const RENDER_CHUNK = 120;

  function paintTracks(rows, ctx) {
    const list = $('track-list');
    const empty = $('track-empty');
    list.innerHTML = '';
    $('view-count').textContent = rows.length;

    if (!rows.length) {
      empty.style.display = 'flex';
      return;
    }
    empty.style.display = 'none';

    let rendered = 0;
    let sentinel = null;

    function renderChunk() {
      const end = Math.min(rendered + RENDER_CHUNK, rows.length);
      for (let index = rendered; index < end; index++) {
        list.insertBefore(buildTrackRow(rows[index], index, ctx), sentinel);
      }
      rendered = end;
      if (sentinel) sentinel.remove();
      sentinel = null;
      if (rendered < rows.length) {
        sentinel = el('li', 'list-more', '继续下滑加载剩余 ' + (rows.length - rendered) + ' 首');
        list.appendChild(sentinel);
        observer.observe(sentinel);
      }
    }

    const observer = new IntersectionObserver(function (entries) {
      if (entries.some(function (e) { return e.isIntersecting; })) {
        observer.disconnect();
        renderChunk();
      }
    }, { root: list, rootMargin: '300px' });

    renderChunk();
  }

  function buildTrackRow(r, index, ctx) {
      const item = el('li', 'track-item');
      if (r.playing) item.classList.add('is-playing');
      // 正在播的那首用播放符号代替序号，比只换底色更容易一眼认出
      item.appendChild(el('span', 't-num', r.playing ? '▶' : String(index + 1)));

      const cover = el('img', 't-cover');
      cover.alt = '';
      cover.loading = 'lazy';       // 1121 首同时发图片请求会直接把 WebView 打满
      cover.decoding = 'async';
      if (r.cover) cover.src = BS.coverUrl(r.cover);
      item.appendChild(cover);

      const body = el('div', 't-body');
      body.appendChild(el('div', 't-title', r.title || '未知曲目'));
      body.appendChild(el('div', 't-artist', r.artists || ''));
      item.appendChild(body);

      item.appendChild(el('span', 't-user', r.requester || ''));

      item.appendChild(el('span', 't-dur', BS.fmtTime(r.duration)));

      const actions = el('div', 't-actions');

      actions.appendChild(smallBtn(
        'M12 20.3l-1.2-1.1C6.5 15.4 4 13.1 4 10.3A3.9 3.9 0 017.9 6.4c1.5 0 2.9.7 3.9 1.9 1-1.2 2.4-1.9 3.9-1.9A3.9 3.9 0 0119.6 10.3c0 2.8-2.5 5.1-6.8 8.9l-1.2 1.1z',
        '收藏到歌单', function () { U.openCollect(r); }));
      actions.appendChild(smallBtn('M8 5v14l11-7z', '马上播这首', async function () {
        try {
          await BS.post('/api/player/play', {
            song_id: r.songId,
            playlist_id: ctx.view === 'queue' ? 0 : Number(ctx.view),
          });
          U.toast('正在播放：' + r.title, 'ok');
        } catch (e) { U.toast(e.message, 'err'); }
      }));

      if (ctx.view === 'queue') {
        actions.appendChild(smallBtn('M12 4l6 6h-4v8h-4v-8H6z', '置顶', async function () {
          try {
            await BS.post('/api/queue/' + r.queueId + '/top');
            await BS.refreshQueue();
            U.renderQueue(BS.state.queue);
            U.renderPlaylists(BS.state.playlists, (BS.state.config || {}).idle_playlist_id, BS.state.view);
          } catch (e) { U.toast(e.message, 'err'); }
        }));
        actions.appendChild(smallBtn('M6.4 5L5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z', '从队列移除', async function () {
          try {
            await BS.del('/api/queue/' + r.queueId);
            await BS.refreshQueue();
            U.renderQueue(BS.state.queue);
            U.renderPlaylists(BS.state.playlists, (BS.state.config || {}).idle_playlist_id, BS.state.view);
          } catch (e) { U.toast(e.message, 'err'); }
        }));
      } else {
        actions.appendChild(smallBtn('M6.4 5L5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z', '从歌单移除', async function () {
          try {
            await BS.del('/api/playlists/' + ctx.view + '/songs/' + r.songId);
            const songs = await BS.get('/api/playlists/' + ctx.view + '/songs');
            U.renderPlaylist(Number(ctx.view), songs);
            BS.refreshPlaylists();
          } catch (e) { U.toast(e.message, 'err'); }
        }));
      }

      item.appendChild(actions);
      return item;
  }

  /* ── 底部播放条 ─────────────────────────────────────────── */

  U.renderPlayer = function (player) {
    if (!player) return;
    const track = player.track;
    const cover = $('cover');
    const fallback = $('cover-fallback');

    if (!track) {
      cover.classList.remove('loaded');
      cover.removeAttribute('src');
      fallback.style.display = 'grid';
      $('title').textContent = '未在播放';
      $('artist').textContent = '等待点歌';
      $('tag-source').hidden = true;
      $('tag-requester').hidden = true;
    } else {
      if (track.cover_url) {
        if (cover.getAttribute('src') !== track.cover_url) {
          cover.onload = function () {
            cover.classList.add('loaded');
            fallback.style.display = 'none';
          };
          cover.onerror = function () {
            cover.classList.remove('loaded');
            fallback.style.display = 'grid';
          };
          cover.setAttribute('src', BS.coverUrl(track.cover_url));
        }
      } else {
        cover.classList.remove('loaded');
        cover.removeAttribute('src');
        fallback.style.display = 'grid';
      }
      $('title').textContent = track.title || '未知曲目';
      $('artist').textContent = track.artists || '未知歌手';

      const src = $('tag-source');
      src.hidden = false;
      src.textContent = (SOURCE_LABELS[track.source] || track.source) +
        (track.fallback ? ' · 已换源' : '');

      const req = $('tag-requester');
      if (track.requester) {
        req.hidden = false;
        req.textContent = track.requester;
      } else {
        req.hidden = true;
      }
    }

    const playing = player.status === 'playing' || player.status === 'loading';
    const iconHost = $('icon-toggle');
    iconHost.innerHTML = '';
    iconHost.appendChild(icon(playing ? 'M6 5h4v14H6zM14 5h4v14h-4z' : 'M8 5v14l11-7z'));

    $('btn-mode').textContent = MODE_LABELS[player.mode] || '顺序播放';
    $('time-duration').textContent = BS.fmtTime(player.duration);
  };

  U.updateProgress = function (positionMs, durationMs) {
    const duration = durationMs || 0;
    const pct = duration > 0 ? Math.min(100, (positionMs / duration) * 100) : 0;
    $('progress-fill').style.width = pct + '%';
    $('progress-thumb').style.left = pct + '%';
    $('time-current').textContent = BS.fmtTime(positionMs);
    if (duration > 0) $('time-duration').textContent = BS.fmtTime(duration);
  };

  /* ── 状态灯 ─────────────────────────────────────────────── */

  U.renderStatus = function () {
    const auth = BS.state.auth || {};
    const dm = BS.state.danmaku || {};

    const dmPill = $('pill-danmaku');
    const dmOk = dm.state === 'authed' || dm.state === 'connected';
    dmPill.className = 'pill ' + (dmOk ? 'is-ok' : (dm.state === 'error' ? 'is-err' : ''));
    dmPill.lastChild.textContent = dmOk ? '弹幕已连接'
      : (dm.state === 'error' ? '弹幕异常' : '弹幕未连接');

    const ne = auth.netease || {};
    const bl = auth.bilibili || {};

    const nPill = $('pill-netease');
    nPill.className = 'pill ' + (ne.logged_in ? 'is-ok' : '');
    nPill.lastChild.textContent = ne.logged_in
      ? ('网易云 ' + (ne.account || '已登录')).slice(0, 16)
      : '网易云未登录';

    const bPill = $('pill-bilibili');
    bPill.className = 'pill ' + (bl.logged_in ? 'is-ok' : '');
    bPill.lastChild.textContent = bl.logged_in
      ? ('B 站 ' + (bl.account || '已登录')).slice(0, 16)
      : 'B 站未登录';

    renderAccount('netease', ne);
    renderAccount('bilibili', bl);
  };

  function renderAccount(provider, info) {
    const loginBtn = $('btn-login-' + provider);
    const logoutBtn = $('btn-logout-' + provider);
    const nameEl = $('acct-' + provider);
    const idEl = $('acct-' + provider + '-id');
    if (!loginBtn || !nameEl) return;

    const row = loginBtn.closest('.account-row');
    if (info && info.logged_in) {
      row.classList.add('is-on');
      nameEl.textContent = info.account || '已登录';
      idEl.textContent = info.uid ? ('UID ' + info.uid) : '已登录';
      loginBtn.hidden = true;
      logoutBtn.hidden = false;
    } else {
      row.classList.remove('is-on');
      nameEl.textContent = provider === 'netease' ? '网易云未登录' : 'B 站未登录';
      idEl.textContent = provider === 'netease'
        ? '未登录时多数歌只能拿到 128k，VIP 歌直接没有链接'
        : '登录后可读收藏夹、发提示弹幕';
      loginBtn.hidden = false;
      logoutBtn.hidden = true;
    }
  }

  /* ── 收藏到歌单 ─────────────────────────────────────────── */

  let collectingSongId = 0;

  U.openCollect = function (row) {
    collectingSongId = row.songId;
    $('collect-song-name').textContent =
      (row.title || '未知曲目') + (row.artists ? '  ·  ' + row.artists : '');

    const list = $('collect-list');
    list.innerHTML = '';
    const pls = BS.state.playlists || [];
    if (!pls.length) {
      list.appendChild(el('li', 'rule-empty', '还没有歌单，先在左侧新建一个'));
    }
    pls.forEach(function (pl) {
      const btn = el('button', 'collect-item');
      btn.type = 'button';
      btn.appendChild(el('span', null, pl.name));
      btn.appendChild(el('span', 'collect-count', String(pl.count)));
      btn.addEventListener('click', async function () {
        try {
          const res = await BS.post('/api/playlists/' + pl.id + '/add_by_id',
                                    { song_id: collectingSongId });
          $('modal-collect').classList.remove('open');
          U.toast(res.added ? ('已收藏到「' + pl.name + '」') : '这首歌已经在这个歌单里了',
                  res.added ? 'ok' : '');
          await BS.refreshPlaylists();
          if (String(BS.state.view) === String(pl.id)) U.loadView();
        } catch (e) { U.toast(e.message, 'err'); }
      });
      list.appendChild(btn);
    });
    $('modal-collect').classList.add('open');
  };

  /* ── 搜索结果 ───────────────────────────────────────────── */

  U.renderSearch = function (items) {
    const list = $('search-results');
    list.innerHTML = '';

    if (!items || !items.length) {
      list.classList.remove('has-items');
      return;
    }

    items.forEach(function (s) {
      const li = el('li', 'sr-item');

      const cover = el('img', 'sr-cover');
      cover.alt = '';
      cover.loading = 'lazy';
      if (s.cover_url) cover.src = BS.coverUrl(s.cover_url);
      li.appendChild(cover);

      const body = el('div', 'sr-body');
      body.appendChild(el('div', 'sr-title', s.title || '未知曲目'));
      body.appendChild(el('div', 'sr-sub',
        (s.artists || '') + (s.duration ? '  ' + BS.fmtTime(s.duration) : '')));
      li.appendChild(body);

      const src = el('span', 'sr-src ' + (s.source === 'netease' ? 'ncm' : 'bili'),
                     s.source === 'netease' ? '网易云' : 'B站');
      li.appendChild(src);

      const acts = el('div', 'sr-actions');
      acts.appendChild(smallBtn('M8 5v14l11-7z', '加入点歌队列', async function () {
        try {
          await BS.post('/api/queue/add', {
            source: s.source, source_id: s.source_id, extra: s.extra || '',
            requester: '主播',
          });
          await BS.refreshQueue();
          U.renderQueue(BS.state.queue);
          U.toast('已加入队列：' + s.title, 'ok');
        } catch (e) { U.toast(e.message, 'err'); }
      }));
      acts.appendChild(smallBtn('M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z', '加入当前歌单', async function () {
        if (BS.state.view === 'queue') {
          U.toast('先在左侧选一个歌单再加', 'err');
          return;
        }
        try {
          const res = await BS.post('/api/playlists/' + BS.state.view + '/add_song', {
            source: s.source, source_id: s.source_id, extra: s.extra || '',
            title: s.title, artists: s.artists, cover_url: s.cover_url,
            duration: s.duration,
          });
          U.toast(res.added ? '已加入歌单' : '这首歌已经在歌单里了', res.added ? 'ok' : '');
          await BS.refreshPlaylists();
          U.loadView();
        } catch (e) { U.toast(e.message, 'err'); }
      }));
      li.appendChild(acts);

      list.appendChild(li);
    });

    list.classList.add('has-items');
  };

  /* ── 关键词规则 ─────────────────────────────────────────── */

  U.renderRules = function (rules, onEdit, onDelete) {
    const list = $('rule-list');
    list.innerHTML = '';

    if (!rules || !rules.length) {
      list.appendChild(el('li', 'rule-empty', '还没有规则。关键词命中就自动回一条弹幕。'));
      return;
    }

    rules.forEach(function (r, index) {
      const li = el('li', 'rule-item');
      li.appendChild(el('span', 'rule-kw', r.keyword || ''));
      li.appendChild(el('span', 'rule-rp', r.reply || ''));

      const acts = el('div', 'rule-actions');
      acts.appendChild(smallBtn(
        'M4 16.5V20h3.5L17 10.5 13.5 7 4 16.5zM19.7 7.8a1 1 0 000-1.4l-2.1-2.1a1 1 0 00-1.4 0L14.6 6l3.5 3.5 1.6-1.7z',
        '编辑', function () { onEdit(index); }));
      acts.appendChild(smallBtn('M6.4 5L5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z', '删除', function () { onDelete(index); }));
      li.appendChild(acts);

      list.appendChild(li);
    });
  };

  /* ── 直播间卡片 ─────────────────────────────────────────── */

  U.renderRoom = function (info) {
    const wrap = document.querySelector('.room-cover-wrap');
    const cover = $('room-cover');
    const badge = $('live-badge');

    const card = document.getElementById('room-card');
    if (!info) {
      wrap.classList.add('is-empty');
      if (card) card.classList.add('is-empty');
      $('room-title').textContent = '还没设置直播间';
      $('room-meta').textContent = '在设置里填房间号';
      badge.hidden = true;
      return;
    }
    if (card) card.classList.remove('is-empty');
    wrap.classList.remove('is-empty');
    // 优先用静态封面：keyframe 是实时截帧，带时效，过期就是一片灰
    const img = info.cover || info.keyframe;
    if (img) {
      cover.onerror = function () { wrap.classList.add('is-empty'); };
      cover.onload = function () { wrap.classList.remove('is-empty'); };
      const proxied = BS.coverUrl(img);
      if (cover.getAttribute('src') !== proxied) cover.src = proxied;
    } else {
      wrap.classList.add('is-empty');
    }
    $('room-title').textContent = info.title || '未命名直播间';
    const bits = [];
    if (info.anchor) bits.push(info.anchor);
    if (info.area) bits.push(info.area);
    if (info.online) bits.push('人气 ' + formatCount(info.online));
    $('room-meta').textContent = bits.join(' · ') || ('房间 ' + info.room_id);
    badge.hidden = info.live_status !== 1;
  };

  function formatCount(n) {
    if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
    return String(n);
  }

  /* ── 日志 ───────────────────────────────────────────────── */

  const MAX_LOG = 300;

  U.appendLog = function (entry) {
    const list = $('log-list');
    const empty = $('log-empty');
    if (empty) empty.style.display = 'none';

    const row = el('li', 'log-row');
    if (entry.kind) row.classList.add('is-' + entry.kind);

    const now = new Date();
    const hh = String(now.getHours()).padStart(2, '0');
    const mm = String(now.getMinutes()).padStart(2, '0');
    row.appendChild(el('span', 'log-time', hh + ':' + mm));

    const body = el('div', 'log-body');
    if (entry.user) {
      row.appendChild(el('span', 'log-user', entry.user));
    }
    body.appendChild(el('span', 'log-text', entry.text || ''));
    row.appendChild(body);

    list.appendChild(row);
    while (list.children.length > MAX_LOG) list.removeChild(list.firstChild);
    list.scrollTop = list.scrollHeight;
  };

  /* ── Toast ──────────────────────────────────────────────── */

  U.toast = function (message, type) {
    const stack = $('toast-stack');
    const node = el('div', 'toast', message);
    if (type === 'err') node.classList.add('is-err');
    if (type === 'ok') node.classList.add('is-ok');
    stack.appendChild(node);
    setTimeout(function () {
      node.style.opacity = '0';
      node.style.transition = 'opacity .2s';
      setTimeout(function () { node.remove(); }, 220);
    }, 2800);
  };

  /* ── 主题网格 ───────────────────────────────────────────── */

  U.THEMES = [
    { id: 'glass', name: '玻璃' },
    { id: 'minimal', name: '极简' },
    { id: 'neon', name: '霓虹' },
    { id: 'vinyl', name: '唱片' },
    { id: 'tape', name: '磁带' },
    { id: 'text', name: '纯文字' },
  ];

  U.renderThemeGrid = function (current, onPick) {
    const grid = $('theme-grid');
    grid.innerHTML = '';
    U.THEMES.forEach(function (t) {
      const btn = el('button', 'theme-swatch' + (t.id === current ? ' is-active' : ''));
      btn.type = 'button';
      btn.dataset.theme = t.id;
      btn.appendChild(el('span', 'name', t.name));
      btn.addEventListener('click', function () { onPick(t.id); });
      grid.appendChild(btn);
    });
  };

  /* ── 弹窗选择候选 ───────────────────────────────────────── */

  U.pickCandidate = function (candidates, onPick) {
    const list = $('pick-list');
    list.innerHTML = '';
    $('modal-pick').classList.add('open');

    const close = function () { $('modal-pick').classList.remove('open'); };
    $('btn-pick-close').onclick = close;

    candidates.forEach(function (c, i) {
      const item = el('li', 'track-item');
      item.appendChild(el('span', 't-num', String(i + 1)));

      const cover = el('img', 't-cover');
      cover.alt = '';
      if (c.cover_url) cover.src = BS.coverUrl(c.cover_url);
      item.appendChild(cover);

      const body = el('div', 't-body');
      body.appendChild(el('div', 't-title', c.title || ''));
      body.appendChild(el('div', 't-artist',
        (c.artists || '') + ' · ' + (SOURCE_LABELS[c.source] || c.source)));
      item.appendChild(body);

      item.appendChild(el('span', 't-dur', BS.fmtTime(c.duration)));

      const actions = el('div', 't-actions');
      const add = el('button', 'mini-btn');
      add.title = '加入歌单';
      add.appendChild(icon('M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z'));
      add.addEventListener('click', function () { onPick(c); });
      actions.appendChild(add);
      item.appendChild(actions);

      list.appendChild(item);
    });
  };

  BS.ui = U;
})(window);
