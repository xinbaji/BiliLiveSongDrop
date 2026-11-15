/* B 站点歌机 · 主流程 */
(function (global) {
  'use strict';

  const BS = global.BS;
  const U = BS.ui;
  const $ = function (id) { return document.getElementById(id); };

  const audio = $('audio');
  let currentKey = null;
  let lastReport = 0;
  let loginPollTimer = null;
  let loginProvider = null;
  let selectedTheme = 'glass';
  let previewTimer = null;
  let editingRule = -1;
  let lastQueueLen = -1;

  /* ── 播放 ───────────────────────────────────────────────── */

  // 只在 track_change / idle 时调用。状态广播（state/pause/resume）绝不能走到这里，
  // 否则 playing → resume → state → 重置到 0s → playing 会形成死循环，歌永远播不出去。
  function applyTrack(player) {
    const track = player && player.track;
    if (!track) {
      audio.pause();
      audio.removeAttribute('src');
      currentKey = null;
      U.renderPlayer(player);
      U.updateProgress(0, 0);
      return;
    }

    U.renderPlayer(player);

    if (track.key === currentKey) {
      // 同一个 key 又收到 track_change，说明引擎要求重播（单曲循环）
      audio.currentTime = 0;
      safePlay();
      return;
    }

    currentKey = track.key;
    audio.src = BS.state.base + '/api/audio/' + encodeURIComponent(track.key);
    audio.load();
    safePlay();
  }

  function safePlay() {
    const p = audio.play();
    if (p && p.catch) {
      p.catch(function (err) {
        if (err && err.name === 'NotAllowedError') {
          U.toast('浏览器拦截了自动播放，点一下播放键', 'err');
        }
      });
    }
  }

  audio.addEventListener('playing', function () {
    BS.post('/api/player/resume').catch(function () {});
  });

  audio.addEventListener('pause', function () {
    if (audio.ended) return;
    BS.post('/api/player/pause').catch(function () {});
  });

  audio.addEventListener('ended', function () {
    BS.post('/api/player/ended').catch(function () {});
  });

  audio.addEventListener('error', function () {
    if (!currentKey) return;
    U.toast('这首歌加载失败了，正在跳下一首', 'err');
    BS.post('/api/player/ended').catch(function () {});
  });

  audio.addEventListener('timeupdate', function () {
    const dur = (audio.duration || 0) * 1000;
    U.updateProgress(audio.currentTime * 1000, dur);
    const now = Date.now();
    if (now - lastReport > 1000) {
      lastReport = now;
      BS.sendPosition(audio.currentTime * 1000, dur);
    }
  });

  /* ── 播放控制 ───────────────────────────────────────────── */

  $('btn-toggle').addEventListener('click', async function () {
    if (!audio.src) {
      await BS.post('/api/player/next').catch(function (e) { U.toast(e.message, 'err'); });
      return;
    }
    if (audio.paused) {
      await BS.post('/api/player/resume').catch(function () {});
      safePlay();
    } else {
      audio.pause();
      await BS.post('/api/player/pause').catch(function () {});
    }
  });

  $('btn-next').addEventListener('click', function () {
    currentKey = null;
    BS.post('/api/player/next').catch(function (e) { U.toast(e.message, 'err'); });
  });

  $('btn-prev').addEventListener('click', function () {
    currentKey = null;
    BS.post('/api/player/prev').catch(function (e) { U.toast(e.message, 'err'); });
  });

  const MODE_ORDER = ['sequential', 'random', 'repeat_all', 'repeat_one'];

  $('btn-mode').addEventListener('click', function () {
    const cur = (BS.state.player && BS.state.player.mode) || 'sequential';
    const next = MODE_ORDER[(MODE_ORDER.indexOf(cur) + 1) % MODE_ORDER.length];
    BS.post('/api/player/mode', { mode: next }).catch(function (e) { U.toast(e.message, 'err'); });
  });

  $('volume').addEventListener('input', function (e) {
    audio.volume = Number(e.target.value) / 100;
  });

  $('volume').addEventListener('change', function (e) {
    BS.post('/api/player/volume', { volume: Number(e.target.value) / 100 })
      .catch(function () {});
  });

  $('progress').addEventListener('click', function (e) {
    if (!audio.duration) return;
    const rect = this.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    audio.currentTime = pct * audio.duration;
    BS.post('/api/player/seek', { position: pct * audio.duration * 1000 })
      .catch(function () {});
  });

  /* ── 视图与歌单 ─────────────────────────────────────────── */

  $('btn-new-playlist').addEventListener('click', async function () {
    const name = global.prompt('歌单名称');
    if (!name) return;
    try {
      const pl = await BS.post('/api/playlists', { name: name });
      await BS.refreshPlaylists();
      U.setView(String(pl.id));
      U.toast('歌单已创建', 'ok');
    } catch (e) { U.toast(e.message, 'err'); }
  });

  $('btn-play-view').addEventListener('click', async function () {
    const view = BS.state.view;
    try {
      if (view === 'queue') {
        await BS.post('/api/player/next');
        return;
      }
      const songs = await BS.get('/api/playlists/' + view + '/songs');
      if (!songs.length) { U.toast('这个歌单是空的', 'err'); return; }
      await BS.post('/api/player/play', { song_id: songs[0].id, playlist_id: Number(view) });
      U.toast('开始播放：' + songs[0].title, 'ok');
    } catch (e) { U.toast(e.message, 'err'); }
  });

  $('btn-set-idle').addEventListener('click', async function () {
    const view = BS.state.view;
    const cfg = BS.state.config;
    if (!cfg || view === 'queue') return;
    cfg.idle_playlist_id = String(cfg.idle_playlist_id) === String(view) ? 0 : Number(view);
    try {
      BS.state.config = await BS.put('/api/config', cfg);
      U.renderPlaylists(BS.state.playlists, BS.state.config.idle_playlist_id, BS.state.view);
      U.toast(cfg.idle_playlist_id ? '已设为空闲歌单' : '已取消空闲歌单', 'ok');
    } catch (e) { U.toast(e.message, 'err'); }
  });

  $('btn-clear-view').addEventListener('click', async function () {
    const view = BS.state.view;
    if (view === 'queue') {
      if (!confirm('清空整个点歌队列？')) return;
      try {
        const r = await BS.post('/api/queue/clear');
        await BS.refreshQueue();
        U.renderQueue([]);
        U.renderPlaylists(BS.state.playlists, (BS.state.config || {}).idle_playlist_id, 'queue');
        U.toast('已清空 ' + r.removed + ' 首', 'ok');
      } catch (e) { U.toast(e.message, 'err'); }
      return;
    }
    if (!confirm('把这个歌单里的歌全部移除？（歌单本身保留）')) return;
    try {
      const r = await BS.post('/api/playlists/' + view + '/clear');
      await BS.refreshPlaylists();
      U.renderPlaylist(Number(view), []);
      U.toast('已移除 ' + r.removed + ' 首', 'ok');
    } catch (e) { U.toast(e.message, 'err'); }
  });

  /* ── 导入 ───────────────────────────────────────────────── */

  async function doImport() {
    const raw = $('import-input').value.trim();
    const view = BS.state.view;
    if (!raw) return;
    if (view === 'queue') {
      U.toast('点歌队列不能直接导入，先建一个歌单', 'err');
      return;
    }

    $('btn-import').disabled = true;
    $('btn-import').textContent = '处理中';
    try {
      const r = await BS.post('/api/playlists/' + view + '/import', { input: raw });
      if (r.kind === 'keyword') {
        if (!r.candidates.length) {
          U.toast('没搜到这首歌', 'err');
          return;
        }
        U.pickCandidate(r.candidates, async function (c) {
          try {
            const added = await BS.post('/api/playlists/' + view + '/add_song', {
              source: c.source, source_id: c.source_id, extra: c.extra || '',
              title: c.title, artists: c.artists, cover_url: c.cover_url,
              duration: c.duration,
            });
            $('modal-pick').classList.remove('open');
            // added=false 表示歌单里已经有了，别谎报成功
            U.toast(added.added ? ('已加入：' + c.title) : '这首歌已经在歌单里了',
                    added.added ? 'ok' : 'err');
            await BS.refreshPlaylists();
            U.loadView();
          } catch (e) { U.toast(e.message, 'err'); }
        });
        return;
      }
      $('import-input').value = '';
      U.toast('导入了 ' + r.added + ' 首' + (r.source_name ? ' · ' + r.source_name : ''), 'ok');
      await BS.refreshPlaylists();
      U.loadView();
    } catch (e) {
      U.toast(e.message, 'err');
    } finally {
      $('btn-import').disabled = false;
      $('btn-import').textContent = '导入';
    }
  }

  $('btn-import').addEventListener('click', doImport);
  $('import-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') doImport();
  });

  /* ── 日志 ───────────────────────────────────────────────── */

  $('btn-clear-log').addEventListener('click', function () {
    $('log-list').innerHTML = '';
    $('log-empty').style.display = 'flex';
  });

  /* ── 直播间信息 ─────────────────────────────────────────── */

  let roomSeq = 0;

  async function loadRoom() {
    const cfg = BS.state.config || {};
    if (!cfg.room_id) {
      U.renderRoom(null);
      return;
    }
    const seq = ++roomSeq;
    try {
      const info = await BS.get('/api/room/info?room_id=' + cfg.room_id);
      if (seq !== roomSeq) return;
      U.renderRoom(info);
    } catch (e) {
      if (seq !== roomSeq) return;
      U.renderRoom(null);
      $('room-meta').textContent = e.message;
    }
  }

  /* ── 设置 ───────────────────────────────────────────────── */

  function collectOverlay() {
    const prev = (BS.state.config && BS.state.config.overlay) || {};
    return {
      theme: selectedTheme,
      show_cover: $('ov-cover').checked,
      show_requester: $('ov-requester').checked,
      show_time: $('ov-time').checked,
      card_background: $('ov-bg').checked,
      show_next: !!prev.show_next,
      card_width: prev.card_width || 420,
      scale: Number($('ov-scale').value) || 3,
      corner_radius: prev.corner_radius != null ? prev.corner_radius : 18,
      blur: prev.blur != null ? prev.blur : 18,
      opacity: prev.opacity != null ? prev.opacity : 0.62,
      accent: prev.accent || '',
    };
  }

  function collectReply() {
    const prev = (BS.state.config && BS.state.config.danmaku_reply) || {};
    return {
      enabled: $('rp-enabled').checked,
      rules: prev.rules || [],
      announce_enqueue: $('rp-announce-enqueue').checked,
      announce_playing: $('rp-announce-playing').checked,
      enqueue_template: $('rp-enqueue-tpl').value.trim() || prev.enqueue_template,
      playing_template: $('rp-playing-tpl').value.trim() || prev.playing_template,
      min_interval: prev.min_interval || 4,
    };
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(pushPreview, 220);
  }

  async function pushPreview() {
    if (!BS.state.config) return;
    BS.state.config.overlay = collectOverlay();
    try {
      BS.state.config = await BS.put('/api/config', BS.state.config);
    } catch (e) { /* 预览失败不打扰 */ }
  }

  /* ── 设置页 ─────────────────────────────────────────────── */

  const REPLY_VARS = '可用变量：{user} {title} {artist} {source} {position}';

  function openSettings() {
    const cfg = BS.state.config || {};
    const ov = cfg.overlay || {};
    const rp = cfg.danmaku_reply || {};

    $('cfg-room').value = cfg.room_id || '';
    $('cfg-prefix').value = cfg.command_prefix || '点歌';
    $('cfg-preferred').value = cfg.prefer_source || 'netease';
    $('cfg-port').value = cfg.port || 9527;
    $('cfg-danmaku-enabled').checked = cfg.danmaku_enabled !== false;
    $('cfg-prefetch').checked = cfg.prefetch_enabled !== false;

    selectedTheme = ov.theme || 'glass';
    paintThemes();

    $('ov-cover').checked = ov.show_cover !== false;
    $('ov-requester').checked = ov.show_requester !== false;
    $('ov-time').checked = ov.show_time !== false;
    $('ov-bg').checked = ov.card_background !== false;
    $('ov-scale').value = String(ov.scale || 3);

    $('rp-enabled').checked = !!rp.enabled;
    $('rp-announce-enqueue').checked = !!rp.announce_enqueue;
    $('rp-announce-playing').checked = !!rp.announce_playing;
    $('rp-enqueue-tpl').value = rp.enqueue_template || '';
    $('rp-playing-tpl').value = rp.playing_template || '';

    editingRule = -1;
    $('rule-keyword').value = '';
    $('rule-reply').value = '';
    $('btn-rule-add').textContent = '添加';
    paintRules();

    $('page-settings').classList.add('open');
    refreshReplyStatus();
    loadAbout();
    BS.get('/api/cache/stats').then(function (s) {
      $('cache-stat').textContent = s.files + ' 个文件，共 ' +
        (s.bytes / 1024 / 1024).toFixed(1) + ' MB';
    }).catch(function () {});
  }

  function closeSettings() {
    $('page-settings').classList.remove('open');
  }

  async function refreshReplyStatus() {
    const el = $('reply-status');
    try {
      const st = await BS.get('/api/reply/stats');
      if (!st.enabled) {
        el.className = 'reply-status';
        el.textContent = '未开启。开启后会用一个独立队列发送，最小间隔 ' +
          (st.config.min_interval || 4) + ' 秒。';
      } else if (!st.bili_logged_in) {
        el.className = 'reply-status is-warn';
        el.textContent = '已开启，但 B 站未登录，发不出去。';
      } else if (!st.room_id) {
        el.className = 'reply-status is-warn';
        el.textContent = '已开启，但还没设置直播间房间号。';
      } else {
        el.className = 'reply-status is-ok';
        el.textContent = '运行中 · 已发送 ' + st.sent + ' 条，队列中 ' +
          (st.queued || 0) + ' 条' + (st.last_error ? (' · 最近错误：' + st.last_error) : '');
      }
    } catch (e) {
      el.className = 'reply-status is-err';
      el.textContent = '取状态失败：' + e.message;
    }
  }

  // 主题网格用统一的点击回调
  function paintThemes() {
    U.renderThemeGrid(selectedTheme, function (id) {
      selectedTheme = id;
      paintThemes();
      schedulePreview();
    });
  }

  ['ov-cover', 'ov-requester', 'ov-time', 'ov-bg', 'ov-scale'].forEach(function (id) {
    $(id).addEventListener('change', schedulePreview);
  });

  $('btn-settings').addEventListener('click', openSettings);
  $('btn-settings-back').addEventListener('click', closeSettings);

  $('btn-settings-save').addEventListener('click', async function () {
    const cfg = BS.state.config || {};
    const oldPort = cfg.port;
    cfg.room_id = Number($('cfg-room').value) || 0;
    cfg.command_prefix = $('cfg-prefix').value.trim() || '点歌';
    cfg.prefer_source = $('cfg-preferred').value;
    cfg.port = Number($('cfg-port').value) || oldPort;
    cfg.danmaku_enabled = $('cfg-danmaku-enabled').checked;
    cfg.prefetch_enabled = $('cfg-prefetch').checked;
    cfg.overlay = collectOverlay();
    cfg.danmaku_reply = collectReply();

    try {
      BS.state.config = await BS.put('/api/config', cfg);
      const hint = $('save-hint');
      hint.textContent = '已保存';
      hint.classList.add('show');
      setTimeout(function () { hint.classList.remove('show'); }, 1600);
      if (cfg.port !== oldPort && cfg.port !== 0) {
        U.toast('端口改为 ' + cfg.port + '，点「重启服务」后生效');
      }
      await BS.refreshPlaylists();
      loadRoom();
      U.loadView();
      refreshReplyStatus();
    } catch (e) {
      U.toast(e.message, 'err');
    }
  });

  /* ── 关于 / 更新 ─────────────────────────────────────────── */

  let updateInfo = null;

  async function loadAbout() {
    try {
      const a = await BS.get('/api/about');
      $('about-version').textContent = 'v' + a.version;
      $('about-app-name').textContent = a.app_name;
      $('about-author').textContent = a.author;
      $('about-author').href = a.author_url;
      $('about-repo').textContent = a.repo;
      $('about-repo').href = a.repo_url;
      $('btn-github').onclick = function () { openExternal(a.repo_url); };
      $('about-hint').textContent = a.self_update
        ? '会从 GitHub 下载最新 zip 并自动覆盖重启。'
        : '源码运行不支持在线更新；打包版会从 GitHub 下载最新 zip 并自动覆盖重启。';
    } catch (e) { /* 打不开就算了，不影响别的 */ }
  }

  function openExternal(url) {
    if (global.pywebview && global.pywebview.api && global.pywebview.api.open_url) {
      global.pywebview.api.open_url(url);
      return;
    }
    global.open(url, '_blank');
  }

  function setUpdateStatus(text, cls) {
    const el = $('update-status');
    if (!text) { el.hidden = true; return; }
    el.hidden = false;
    el.className = 'update-status' + (cls ? ' ' + cls : '');
    el.textContent = text;
  }

  $('btn-update-check').addEventListener('click', async function () {
    const btn = $('btn-update-check');
    btn.disabled = true;
    btn.textContent = '检查中…';
    setUpdateStatus('正在查询 GitHub 最新版本…');
    try {
      const r = await BS.get('/api/update/check');
      if (!r.ok) {
        setUpdateStatus('检查失败：' + r.error, 'is-err');
        return;
      }
      updateInfo = r;
      if (!r.latest) {
        setUpdateStatus('GitHub 上还没有可用的 release。', 'is-warn');
        $('btn-update-apply').hidden = true;
        return;
      }
      if (!r.update_available) {
        setUpdateStatus('已经是最新版本 v' + r.current + '，远程最新 v' + r.latest + '。', 'is-ok');
        $('btn-update-apply').hidden = true;
        return;
      }
      setUpdateStatus(
        '发现新版本 v' + r.latest + '（当前 v' + r.current + '）\n\n' +
        (r.notes || '').slice(0, 400),
        'is-ok'
      );
      $('btn-update-apply').hidden = !r.asset_found;
      if (!r.asset_found) {
        setUpdateStatus('发现新版本 v' + r.latest + '，但 release 里没有安装包，请到 GitHub 手动下载。', 'is-warn');
      }
    } catch (e) {
      setUpdateStatus('检查失败：' + e.message, 'is-err');
    } finally {
      btn.disabled = false;
      btn.textContent = '检查更新';
    }
  });

  $('btn-update-apply').addEventListener('click', async function () {
    if (!updateInfo || !updateInfo.download_url) return;
    const btn = $('btn-update-apply');
    btn.disabled = true;
    btn.textContent = '下载中…';
    setUpdateStatus('正在下载更新包，视网速可能要几十秒，请勿关闭程序…');
    // 下载完成后后端会自动退出进程、由 updater 脚本接管。
    // 请求挂住或被掐断都正常，前端只负责把界面变成等待状态。
    try {
      await BS.post('/api/update/apply', { download_url: updateInfo.download_url });
    } catch (e) {
      // 进程退出导致连接断开，属预期
    }
    setUpdateStatus('更新包已就绪，程序即将退出并自动安装…', 'is-ok');
  });

  /* ── 关键词规则 ─────────────────────────────────────────── */

  function paintRules() {
    const rules = ((BS.state.config || {}).danmaku_reply || {}).rules || [];
    U.renderRules(rules, function (index) {
      const r = rules[index] || {};
      editingRule = index;
      $('rule-keyword').value = r.keyword || '';
      $('rule-reply').value = r.reply || '';
      $('btn-rule-add').textContent = '保存修改';
      $('rule-keyword').focus();
    }, async function (index) {
      const cfg = BS.state.config;
      cfg.danmaku_reply.rules.splice(index, 1);
      await saveRules();
    });
  }

  async function saveRules() {
    const cfg = BS.state.config;
    cfg.danmaku_reply = collectReply();
    try {
      BS.state.config = await BS.put('/api/config', cfg);
      paintRules();
      // 开关状态可能在这次保存里变了，状态条要跟着刷新，
      // 否则勾上「开启」后还一直显示「未开启」
      refreshReplyStatus();
    } catch (e) { U.toast(e.message, 'err'); }
  }

  $('btn-rule-add').addEventListener('click', async function () {
    const kw = $('rule-keyword').value.trim();
    const rp = $('rule-reply').value.trim();
    if (!kw || !rp) {
      U.toast('关键词和回复内容都要填', 'err');
      return;
    }
    const cfg = BS.state.config;
    const rules = cfg.danmaku_reply.rules || (cfg.danmaku_reply.rules = []);

    if (editingRule >= 0 && editingRule < rules.length) {
      rules[editingRule] = { keyword: kw, reply: rp };
    } else {
      const dup = rules.filter(function (r) { return r.keyword === kw; })[0];
      if (dup) {
        U.toast('已经有一条同样的关键词了', 'err');
        return;
      }
      rules.push({ keyword: kw, reply: rp });
    }

    editingRule = -1;
    $('rule-keyword').value = '';
    $('rule-reply').value = '';
    $('btn-rule-add').textContent = '添加';
    await saveRules();
    U.toast('规则已保存', 'ok');
  });

  $('btn-reply-test').addEventListener('click', async function () {
    try {
      const r = await BS.post('/api/reply/test', { text: '点歌机测试弹幕' });
      U.toast('已发送：' + r.sent, 'ok');
      refreshReplyStatus();
    } catch (e) { U.toast(e.message, 'err'); }
  });

  /* ── 服务重启 ───────────────────────────────────────────── */

  $('btn-restart').addEventListener('click', async function () {
    // 先把当前页面上的值写进配置，再重启，否则改了半天端口还是旧的
    $('btn-settings-save').click();
    await new Promise(function (r) { setTimeout(r, 500); });

    try {
      await BS.post('/api/system/restart');
    } catch (e) { /* 进程正在退出，连接被掐断是正常的 */ }

    U.toast('正在重启服务…', 'ok');
    const base = BS.state.base;

    // 轮询直到新进程起来。进程重启期间请求会连续失败，属预期。
    let tries = 0;
    const timer = setInterval(async function () {
      tries += 1;
      try {
        const res = await fetch(base + '/healthz', { cache: 'no-store' });
        const data = await res.json();
        if (data && data.app === 'bili-singer') {
          clearInterval(timer);
          U.toast('服务已重启', 'ok');
          location.reload();
        }
      } catch (e) { /* 还没起来，继续等 */ }
      if (tries > 60) {
        clearInterval(timer);
        U.toast('重启后没等到服务，请手动启动', 'err');
      }
    }, 800);
  });

  /* ── 收藏弹窗 ───────────────────────────────────────────── */

  $('btn-collect-close').addEventListener('click', function () {
    $('modal-collect').classList.remove('open');
  });

  /* ── 搜索卡片 ───────────────────────────────────────────── */

  let searchScope = 'all';
  let searchBusy = false;

  document.querySelectorAll('.scope-chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      document.querySelectorAll('.scope-chip').forEach(function (c) {
        c.classList.toggle('is-active', c === chip);
      });
      searchScope = chip.dataset.scope;
      if ($('search-input').value.trim()) doSearch();
    });
  });

  // 歌单链接走「自动建歌单」这条独立路径，其余走搜索
  function isPlaylistLink(text) {
    if (!text) return false;
    if (/music\.163\.com/.test(text) && /playlist|discover\/toplist|\/dj/.test(text)) return true;
    if (/bilibili\.com/.test(text) && /(favlist|media_id|fid=|fid%3D|\/ml\d)/.test(text)) return true;
    return false;
  }

  async function doSearch() {
    const q = $('search-input').value.trim();
    if (!q || searchBusy) return;

    if (isPlaylistLink(q)) {
      searchBusy = true;
      const hint = $('search-hint');
      hint.textContent = '正在按歌单导入，链接里歌曲多的话要等一会儿…';
      try {
        const r = await BS.post('/api/playlists/import_remote', { input: q });
        hint.textContent = '已创建歌单「' + r.playlist.name + '」，导入 ' +
          r.imported + ' / ' + r.total + ' 首。之后可以点歌单页的「刷新」做差异更新。';
        U.renderSearch([]);
        $('search-input').value = '';
        await BS.refreshPlaylists();
        U.setView(String(r.playlist.id));
        U.toast('已创建歌单：' + r.playlist.name, 'ok');
      } catch (e) {
        hint.textContent = '';
        U.toast(e.message, 'err');
      } finally {
        searchBusy = false;
      }
      return;
    }

    searchBusy = true;
    $('search-hint').textContent = '搜索中…';
    try {
      const data = await BS.get('/api/search?q=' + encodeURIComponent(q) +
                                '&source=' + searchScope);
      const items = (data.netease || []).concat(data.bilibili || []);
      U.renderSearch(items);
      $('search-hint').textContent = items.length
        ? ('找到 ' + items.length + ' 条，点右侧按钮加入队列或歌单')
        : '没搜到，换个关键词试试';
    } catch (e) {
      $('search-hint').textContent = '';
      U.toast(e.message, 'err');
    } finally {
      searchBusy = false;
    }
  }

  $('btn-search').addEventListener('click', doSearch);
  $('search-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') doSearch();
  });

  /* ── 歌单刷新 ───────────────────────────────────────────── */

  $('btn-refresh-pl').addEventListener('click', async function () {
    const view = BS.state.view;
    if (!view || view === 'queue') return;
    const btn = $('btn-refresh-pl');
    btn.disabled = true;
    btn.textContent = '刷新中';
    try {
      const r = await BS.post('/api/playlists/' + view + '/refresh');
      U.toast('刷新完成：新增 ' + r.added + ' 首，移除 ' + r.removed +
              ' 首，远程共 ' + r.total + ' 首', 'ok');
      await BS.refreshPlaylists();
      U.loadView();
    } catch (e) {
      U.toast(e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '刷新';
    }
  });

  /* ── 账号登录 / 登出 ─────────────────────────────────────── */

  $('btn-login-netease').addEventListener('click', function () { openLogin('netease'); });
  $('btn-login-bilibili').addEventListener('click', function () { openLogin('bilibili'); });

  ['netease', 'bilibili'].forEach(function (provider) {
    $('btn-logout-' + provider).addEventListener('click', async function () {
      try {
        await BS.post('/api/auth/' + provider + '/logout');
        const auth = await BS.get('/api/auth/status');
        BS.state.auth = auth;
        U.renderStatus();
        U.toast('已退出登录', 'ok');
      } catch (e) { U.toast(e.message, 'err'); }
    });
  });

  $('btn-cache-clear').addEventListener('click', async function () {
    try {
      const r = await BS.post('/api/cache/clear');
      U.toast('已清掉 ' + (r.removed / 1024 / 1024).toFixed(1) + ' MB 缓存', 'ok');
      BS.get('/api/cache/stats').then(function (s) {
        $('cache-stat').textContent = s.files + ' 个文件，共 ' +
          (s.bytes / 1024 / 1024).toFixed(1) + ' MB';
      }).catch(function () {});
    } catch (e) { U.toast(e.message, 'err'); }
  });

  /* ── 叠加层 ─────────────────────────────────────────────── */

  function overlayUrl(preview) {
    return BS.state.base + '/overlay' + (preview ? '?preview=1' : '');
  }

  $('btn-overlay-preview').addEventListener('click', function () {
    const url = overlayUrl(true);
    if (global.pywebview && global.pywebview.api && global.pywebview.api.open_url) {
      global.pywebview.api.open_url(url);
      return;
    }
    global.open(url, '_blank');
  });

  $('btn-port').addEventListener('click', function () {
    const url = overlayUrl();
    if (global.navigator.clipboard) {
      global.navigator.clipboard.writeText(url).then(function () {
        U.toast('叠加层地址已复制：' + url, 'ok');
      }).catch(function () { U.toast('叠加层地址：' + url); });
    } else {
      U.toast('叠加层地址：' + url);
    }
  });

  /* ── 登录 ───────────────────────────────────────────────── */

  function openLogin(provider) {
    loginProvider = provider;
    $('login-title').textContent = provider === 'netease' ? '扫码登录网易云音乐' : '扫码登录 B 站';
    $('qr-image').removeAttribute('src');
    $('qr-status').textContent = '正在获取二维码…';
    $('modal-login').classList.add('open');
    startQrLogin(provider);
  }

  async function startQrLogin(provider) {
    clearInterval(loginPollTimer);
    try {
      const qr = await BS.post('/api/auth/' + provider + '/qr');
      $('qr-image').src = qr.image;
      $('qr-status').textContent = '用手机 App 扫码';
      loginPollTimer = setInterval(function () { pollQr(provider, qr.key); }, 2000);
    } catch (e) {
      $('qr-status').textContent = '获取失败：' + e.message;
    }
  }

  async function pollQr(provider, key) {
    try {
      const r = await BS.get('/api/auth/' + provider + '/qr/poll?key=' + encodeURIComponent(key));
      const code = r.code;
      if (provider === 'netease') {
        if (code === 801) $('qr-status').textContent = '等待扫码…';
        else if (code === 802) $('qr-status').textContent = '已扫码，请在手机上确认';
        else if (code === 800) { $('qr-status').textContent = '二维码已过期'; clearInterval(loginPollTimer); }
        else if (code === 803) finishLogin();
      } else {
        if (code === 86101) $('qr-status').textContent = '等待扫码…';
        else if (code === 86090) $('qr-status').textContent = '已扫码，请在手机上确认';
        else if (code === 86038) { $('qr-status').textContent = '二维码已过期'; clearInterval(loginPollTimer); }
        else if (code === 0) finishLogin();
      }
    } catch (e) { /* 轮询失败不打断 */ }
  }

  async function finishLogin() {
    clearInterval(loginPollTimer);
    $('qr-status').textContent = '登录成功';
    U.toast('登录成功', 'ok');
    setTimeout(function () { $('modal-login').classList.remove('open'); }, 700);
    const s = await BS.get('/api/auth/status').catch(function () { return null; });
    if (s) { BS.state.auth = s; U.renderStatus(); }
  }

  $('btn-login-close').addEventListener('click', function () {
    clearInterval(loginPollTimer);
    $('modal-login').classList.remove('open');
  });

  $('btn-login-netease').addEventListener('click', function () { openLogin('netease'); });
  $('btn-login-bilibili').addEventListener('click', function () { openLogin('bilibili'); });

  // 直接点顶栏的登录状态就能登录
  $('pill-netease').addEventListener('click', function () {
    openLogin('netease');
  });
  $('pill-bilibili').addEventListener('click', function () {
    openLogin('bilibili');
  });
  $('pill-danmaku').addEventListener('click', function () {
    openSettings();
    $('cfg-room').focus();
  });

  $('btn-login-mode').addEventListener('click', async function () {
    clearInterval(loginPollTimer);
    if (!global.pywebview || !global.pywebview.api || !global.pywebview.api.web_login) {
      U.toast('网页登录只在桌面窗口里可用', 'err');
      return;
    }
    $('qr-status').textContent = '已在弹出的窗口里登录，完成后自动关闭';
    try {
      const cookie = await global.pywebview.api.web_login(loginProvider);
      if (!cookie) {
        $('qr-status').textContent = '没有拿到登录凭据，可能没登录成功';
        return;
      }
      await BS.post('/api/auth/' + loginProvider + '/web_login', { cookie: cookie });
      finishLogin();
    } catch (e) {
      $('qr-status').textContent = '失败：' + e.message;
    }
  });

  /* ── 键盘 ───────────────────────────────────────────────── */

  document.addEventListener('keydown', function (e) {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select';

    if (e.key === ' ' && !typing) {
      e.preventDefault();
      $('btn-toggle').click();
      return;
    }
    if (e.ctrlKey && e.key === 'ArrowRight') { e.preventDefault(); $('btn-next').click(); return; }
    if (e.ctrlKey && e.key === 'ArrowLeft') { e.preventDefault(); $('btn-prev').click(); return; }
    if (e.ctrlKey && (e.key === 'f' || e.key === 'F')) {
      e.preventDefault();
      $('search-input').focus();
      return;
    }
    if (e.key === 'Escape') {
      // 设置页是最上层，优先关它；否则关最上层的弹窗
      const page = $('page-settings');
      if (page.classList.contains('open')) {
        page.classList.remove('open');
        return;
      }
      ['modal-login', 'modal-pick', 'modal-collect'].forEach(function (id) {
        $(id).classList.remove('open');
      });
    }
  });

  /* ── 订阅 ───────────────────────────────────────────────── */

  BS.on(function (type, payload) {
    if (type === 'player') {
      // 只有切歌和转入空闲才动音频元素；其余状态变化只刷新界面
      if (payload.event === 'track_change' || payload.event === 'idle') {
        applyTrack(payload);
      } else {
        U.renderPlayer(payload);
      }
      if (payload.status === 'idle') U.updateProgress(0, 0);
      // 切歌时队列长度可能不变（一首从 pending 变 playing，总数一样），
      // 但内容一定变了，必须重拉，否则「▶ 正在播放」标记不会跟着走。
      const queueDirty =
        payload.event === 'track_change' ||
        payload.event === 'idle' ||
        (payload.queue_length !== undefined && payload.queue_length !== lastQueueLen);
      if (queueDirty) {
        lastQueueLen = payload.queue_length;
        BS.refreshQueue().then(function () {
          if (BS.state.view === 'queue') U.renderQueue(BS.state.queue);
          U.renderPlaylists(BS.state.playlists, (BS.state.config || {}).idle_playlist_id, BS.state.view);
        }).catch(function () {});
      }
    }
    if (type === 'auth') {
      BS.get('/api/auth/status').then(function (s) {
        BS.state.auth = s;
        U.renderStatus();
      });
    }
    if (type === 'log') U.appendLog(payload);
    if (type === 'config') loadRoom();
  });

  /* ── 轮询兜底 ───────────────────────────────────────────── */

  setInterval(function () {
    if (BS.state.wsConnected) return;
    BS.get('/api/danmaku/status').then(function (d) {
      BS.state.danmaku = d;
      U.renderStatus();
    }).catch(function () {});
    BS.refreshQueue().then(function () {
      if (BS.state.view === 'queue') U.renderQueue(BS.state.queue);
    }).catch(function () {});
  }, 5000);

  /* ── 启动 ───────────────────────────────────────────────── */

  async function boot() {
    await BS.resolveBase();
    try {
      await BS.loadInitial();
    } catch (e) {
      U.toast('连不上后端服务：' + e.message, 'err');
      return;
    }

    const st = BS.state;
    audio.volume = (st.config && st.config.volume) || 0.8;
    $('volume').value = Math.round(audio.volume * 100);

    st.view = 'queue';
    U.renderPlayer(st.player);
    U.renderPlaylists(st.playlists, st.config && st.config.idle_playlist_id, 'queue');
    U.setView('queue');
    U.renderStatus();
    U.appendLog({ user: '系统', text: '就绪，等待点歌', kind: 'ok' });

    loadRoom();
    BS.connect();
    applyTrack(st.player);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
