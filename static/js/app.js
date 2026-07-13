let currentEntity = null;
    let currentDialogName = '';
    let allDialogs = [];
    let videos = [];
    let filteredVideos = [];
    let evtSource = null;
    let statusTimer = null;
    let isConnected = false;
    let selectedIds = new Set();
    const TASK_ID_SEP = ':';
    const TASK_ID_DOM_SANITIZER = /[^a-zA-Z0-9:_-]/g;
    const MAX_CONCURRENT_HINT = 2;
    let lastQueueSnapshot = null;
    let reconnectTimer = null;
    let pollGeneration = 0;
    let dismissedTaskIds = new Set();
    let completedTasksNotified = new Set();
    let activeMobilePanel = 'dialogs';
    let lastTaskSnapshot = {};
    let filesPage = 1;
    let historyPage = 1;

    // Request notification permission
    if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") {
      Notification.requestPermission();
    }

    function makeTaskId(entityId, msgId) {
      return `${entityId}${TASK_ID_SEP}${msgId}`;
    }

    function videoEntityId(video) {
      return video && video.entity_id != null ? video.entity_id : currentEntity?.entity_id;
    }

    function makeSelectionKey(entityId, msgId) {
      return makeTaskId(entityId, msgId);
    }

    function selectionKeyForVideo(video) {
      return makeSelectionKey(videoEntityId(video), video.id);
    }

    function domIdForTask(taskId) {
      return `dl-${String(taskId).replace(TASK_ID_DOM_SANITIZER, '_')}`;
    }


    function isMobileLayout() {
      return window.innerWidth <= 900;
    }

    function setMobilePanel(panel) {
      activeMobilePanel = panel;
      if (!isMobileLayout()) return;
      document.querySelectorAll('.panel[data-panel]').forEach(el => {
        const active = el.dataset.panel === panel;
        el.classList.toggle('mobile-active', active);
        el.classList.toggle('mobile-hidden', !active);
      });
      document.querySelectorAll('#mobileNav button').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.panel === panel);
      });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function syncMobileLayout() {
      if (isMobileLayout()) {
        setMobilePanel(activeMobilePanel || 'dialogs');
      } else {
        document.querySelectorAll('.panel[data-panel]').forEach(el => {
          el.classList.remove('mobile-active', 'mobile-hidden');
        });
        const fab = document.getElementById('mobileTaskFab');
        if (fab) fab.classList.remove('visible', 'has-alert');
      }
    }

    function applyDownloadsPanelWidth(width) {
      const panel = document.querySelector('.panel-downloads');
      if (!panel || isMobileLayout()) return;
      const minWidth = 280;
      const maxWidth = Math.max(minWidth, Math.floor(window.innerWidth * 0.7));
      const nextWidth = Math.min(maxWidth, Math.max(minWidth, Number(width) || 320));
      panel.style.width = nextWidth + 'px';
      localStorage.setItem('downloadsPanelWidth', String(nextWidth));
    }

    function initDownloadsResizer() {
      const panel = document.querySelector('.panel-downloads');
      const handle = document.getElementById('downloadsResizer');
      if (!panel || !handle) return;
      applyDownloadsPanelWidth(localStorage.getItem('downloadsPanelWidth') || 320);

      handle.addEventListener('pointerdown', event => {
        if (isMobileLayout()) return;
        event.preventDefault();
        handle.classList.add('dragging');
        handle.setPointerCapture(event.pointerId);
      });
      handle.addEventListener('pointermove', event => {
        if (!handle.classList.contains('dragging')) return;
        applyDownloadsPanelWidth(window.innerWidth - event.clientX);
      });
      const stopDragging = event => {
        handle.classList.remove('dragging');
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      };
      handle.addEventListener('pointerup', stopDragging);
      handle.addEventListener('pointercancel', stopDragging);
      handle.addEventListener('dblclick', () => applyDownloadsPanelWidth(320));
    }

    function updateMobileTaskFab(queueData = null, tasks = null) {
      const fab = document.getElementById('mobileTaskFab');
      if (!fab) return;
      const badge = document.getElementById('mobileTaskFabBadge');
      const meta = document.getElementById('mobileTaskFabMeta');
      if (tasks) lastTaskSnapshot = tasks;
      const snapshot = tasks || lastTaskSnapshot || {};
      const items = Object.values(snapshot);
      const queue = queueData || lastQueueSnapshot || { active: 0, queued: 0, max: MAX_CONCURRENT_HINT };
      const active = queue.active ?? items.filter(item => item.status === 'downloading').length;
      const queued = queue.queued ?? items.filter(item => item.status === 'queued' || item.status === 'waiting').length;
      const issues = items.filter(item => item.status === 'error' || item.status === 'cancelled').length;
      const total = active + queued + issues;
      badge.textContent = String(total || items.length || 0);
      meta.textContent = active ? `下载中 ${active}` : queued ? `排队 ${queued}` : issues ? `异常 ${issues}` : '空闲';
      fab.classList.toggle('has-alert', issues > 0);
      fab.classList.toggle('visible', isMobileLayout() && (activeMobilePanel !== 'downloads' || total > 0));
    }

    function setTaskMeta(el, taskId, entityId, msgId, dialogName) {
      if (!el) return;
      el.dataset.taskId = taskId;
      if (entityId !== undefined && entityId !== null) el.dataset.entityId = entityId;
      if (msgId !== undefined && msgId !== null) el.dataset.msgId = msgId;
      if (dialogName) el.dataset.dialogName = dialogName;
    }

    function actionContainerFor(el) {
      if (!el) return null;
      const actionId = el.dataset.actionsId;
      if (actionId) return document.getElementById(actionId);
      return el.querySelector('.dl-actions');
    }

    // 连接状态检查
    function checkStatus() {
      fetch('/api/status').then(r => r.json()).then(data => {
        refreshHealth();
        const el = document.getElementById('connStatus');
        if (data.connected && !data.error) {
          el.className = 'conn-status conn-ok';
          el.innerHTML = '<span class="dot"></span> ' + esc(data.user);
          const tgBtn = document.getElementById('tgLoginBtn');
          if (tgBtn) tgBtn.style.display = 'none';
          if (!isConnected) {
            isConnected = true;
            loadDialogs();  // 连接成功后自动加载对话
          }
        } else {
          el.className = 'conn-status conn-err';
          el.innerHTML = '<span class="dot"></span> ' + esc(data.error || '未连接');
          isConnected = false;
          document.getElementById('dialogList').innerHTML = '<div class="empty">' + esc(data.error || 'Telegram 未连接') + '</div>';
          maybeShowTgLogin();
        }
      }).catch(() => {
        const el = document.getElementById('connStatus');
        el.className = 'conn-status conn-err';
        el.innerHTML = '<span class="dot"></span> 服务不可用';
        isConnected = false;
      });
    }

    // 启动时立即检查，之后每5秒检查一次
    checkStatus();
    statusTimer = setInterval(checkStatus, 5000);
    refreshHealth();

    // 登出入口：仅在启用了鉴权且当前非本地会话登录时显示
    function refreshAuthUi() {
      fetch('/api/auth/status').then(r => r.json()).then(data => {
        const btn = document.getElementById('logoutBtn');
        if (btn) btn.style.display = (data.auth_required && !data.local) ? '' : 'none';
      }).catch(() => {});
    }
    function doLogout() {
      fetch('/api/logout', { method: 'POST' })
        .then(() => { window.location = '/login'; })
        .catch(() => { window.location = '/login'; });
    }
    window.doLogout = doLogout;
    refreshAuthUi();

    // ── Telegram 网页登录向导 ──────────────────────────────────────────
    function maybeShowTgLogin() {
      fetch('/api/tg/login/status').then(r => r.json()).then(s => {
        const btn = document.getElementById('tgLoginBtn');
        if (btn) btn.style.display = s.needs_login ? '' : 'none';
      }).catch(() => {});
    }
    window.maybeShowTgLogin = maybeShowTgLogin;

    function tgStep(step) {
      ['Phone', 'Code', 'Password'].forEach(s => {
        const el = document.getElementById('tgStep' + s);
        if (el) el.style.display = (s === step) ? '' : 'none';
      });
    }
    function tgMsg(text, ok) {
      const el = document.getElementById('tgLoginMsg');
      if (el) { el.textContent = text || ''; el.style.color = ok ? '#4caf50' : '#ff6b6b'; }
    }
    function openTgLogin() {
      tgStep('Phone');
      tgMsg('');
      const m = document.getElementById('tgLoginModal');
      if (m) m.classList.remove('hidden');
    }
    function closeTgLogin(evt) {
      if (evt && evt.target && evt.target.id !== 'tgLoginModal' && !evt.target.classList.contains('modal-close')) return;
      const m = document.getElementById('tgLoginModal');
      if (m) m.classList.add('hidden');
    }
    async function tgPost(url, body, btnId, busyText) {
      const btn = btnId && document.getElementById(btnId);
      const label = btn && btn.textContent;
      if (btn) { btn.disabled = true; btn.textContent = busyText; }
      try {
        const resp = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        return { ok: resp.ok, data };
      } catch (e) {
        return { ok: false, data: { error: '网络错误，请重试' } };
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = label; }
      }
    }
    async function tgSendCode() {
      const phone = document.getElementById('tgPhone').value.trim();
      if (!phone) { tgMsg('请输入手机号'); return; }
      tgMsg('发送中...', true);
      const { ok, data } = await tgPost('/api/tg/login/send_code', { phone }, 'tgSendCodeBtn', '发送中...');
      if (ok && data.code_needed) { tgStep('Code'); tgMsg('验证码已发送，请查收', true); }
      else tgMsg(data.error || '发送失败');
    }
    async function tgSignIn() {
      const code = document.getElementById('tgCode').value.trim();
      if (!code) { tgMsg('请输入验证码'); return; }
      tgMsg('登录中...', true);
      const { ok, data } = await tgPost('/api/tg/login/sign_in', { code }, 'tgSignInBtn', '登录中...');
      if (ok && data.password_needed) { tgStep('Password'); tgMsg('该账号开启了两步验证，请输入密码', true); }
      else if (ok && data.ok) tgLoginDone(data.user);
      else tgMsg(data.error || '登录失败');
    }
    async function tgSubmitPassword() {
      const password = document.getElementById('tgPassword').value;
      if (!password) { tgMsg('请输入两步验证密码'); return; }
      tgMsg('验证中...', true);
      const { ok, data } = await tgPost('/api/tg/login/password', { password }, 'tgPasswordBtn', '验证中...');
      if (ok && data.ok) tgLoginDone(data.user);
      else tgMsg(data.error || '验证失败');
    }
    function tgLoginDone(user) {
      tgMsg('登录成功：' + (user || ''), true);
      setTimeout(() => {
        closeTgLogin();
        checkStatus();
        if (typeof loadDialogs === 'function') loadDialogs({ forceRefresh: true });
      }, 800);
    }
    window.openTgLogin = openTgLogin;
    window.closeTgLogin = closeTgLogin;
    window.tgSendCode = tgSendCode;
    window.tgSignIn = tgSignIn;
    window.tgSubmitPassword = tgSubmitPassword;

    restoreDownloadTasks();
    syncMobileLayout();
    initDownloadsResizer();
    window.addEventListener('resize', () => {
      syncMobileLayout();
      applyDownloadsPanelWidth(localStorage.getItem('downloadsPanelWidth') || 320);
    });

    let dialogsRetryTimer = null;

    function scheduleDialogsRetry(delay = 1500) {
      if (dialogsRetryTimer) clearTimeout(dialogsRetryTimer);
      dialogsRetryTimer = setTimeout(() => {
        dialogsRetryTimer = null;
        loadDialogs({ background: true });
      }, delay);
    }

    function loadDialogs(options = {}) {
      const background = !!options.background;
      if (!background) {
        document.getElementById('dialogList').innerHTML = '<div class="loading"><div class="spinner"></div>加载中...</div>';
      }

      const url = options.forceRefresh ? '/api/dialogs?refresh=true' : '/api/dialogs';
      fetch(url).then(async r => {
        const data = await r.json();
        return { ok: r.ok, status: r.status, data };
      }).then(({ ok, status, data }) => {
        const dialogs = Array.isArray(data) ? data : (Array.isArray(data.dialogs) ? data.dialogs : []);

        if (dialogs.length > 0) {
          allDialogs = dialogs;
          filterDialogs();
          if (data.loading) scheduleDialogsRetry();
          return;
        }

        if (data.loading || status === 202) {
          document.getElementById('dialogList').innerHTML = '<div class="loading"><div class="spinner"></div>正在后台同步对话列表...</div>';
          scheduleDialogsRetry();
          return;
        }

        if (!ok || data.error) {
          throw new Error(data.error || '加载失败，请检查连接');
        }

        allDialogs = [];
        renderDialogs([]);
      }).catch(e => {
        document.getElementById('dialogList').innerHTML = '<div class="empty">' + esc(e.message || '加载失败，请检查连接') + '</div>';
      });
    }

    let currentDialogTab = '全部';
    function switchDialogTab(tab, el) {
      document.querySelectorAll('.d-tab').forEach(t => t.classList.remove('active'));
      el.classList.add('active');
      currentDialogTab = tab;
      filterDialogs();
    }

    function filterDialogs() {
      const q = document.getElementById('dialogSearch').value.toLowerCase();
      const filtered = allDialogs.filter(d => {
        const matchName = d.name.toLowerCase().includes(q);
        const matchTab = currentDialogTab === '全部' || d.type === currentDialogTab || (currentDialogTab === '频道' && d.is_channel) || (currentDialogTab === '群组' && d.is_group) || (currentDialogTab === '私聊' && !d.is_channel && !d.is_group && !d.is_saved);
        return matchName && matchTab;
      });
      renderDialogs(filtered);
    }

    function renderDialogs(data) {
      const html = data.map(d => {
        const bc = d.type === '频道' ? 'badge-channel' : d.type === '群组' ? 'badge-group' : 'badge-private';
        const active = currentEntity && currentEntity.dialog_index === d.index ? 'active' : '';
        return `<div class="dialog-item ${active}" onclick="selectDialog(${d.index}, this)">
      <div class="dialog-name">${esc(d.name)}</div>
      <div class="dialog-type"><span class="badge ${bc}">${d.type}</span></div>
    </div>`;
      }).join('');
      document.getElementById('dialogList').innerHTML = html || '<div class="empty">未找到对话</div>';
    }
    function selectDialog(index, el) {
      const d = allDialogs.find(x => x.index === index);
      if (!d) return;
      document.querySelectorAll('.dialog-item').forEach(item => item.classList.remove('active'));
      if (el) el.classList.add('active');
      currentEntity = { dialog_index: index, entity_id: d.id, source: 'dialog' };
      currentDialogName = d.name;
      scanVideos(false);
      if (window.innerWidth <= 900) {
        setMobilePanel('videos');
      }
    }

    function doSearch() {
      const q = document.getElementById('searchInput').value.trim();
      if (!q) return;
      showVideoListSkeleton('正在查找对话并解析链接...');
      fetch('/api/search?q=' + encodeURIComponent(q)).then(r => r.json()).then(data => {
        if (data.error) { alert(data.error); return; }
        currentEntity = { entity_id: data.id, source: 'search' };
        currentDialogName = data.name;
        if (isMobileLayout()) setMobilePanel('videos');
        scanVideos(false);
      }).catch(e => alert('搜索失败'));
    }

    let scanningReplies = false;
    function scanVideos(forceRefresh) {
      if (!currentEntity) return;
      const limit = document.getElementById('scanLimit').value || 100;
      const ir = document.getElementById('includeReplies').checked;
      const replyPostLimit = document.getElementById('replyPostLimit')?.value || 50;
      document.getElementById('videoTitle').textContent = currentDialogName + ' - 扫描中...';
      showVideoListSkeleton('正在扫描主页视频...');
      document.getElementById('videoToolbar').style.display = 'none';
      document.getElementById('filterBar').style.display = 'none';
      scanningReplies = false; selectedIds.clear(); updateSelectedCount();

      const params = new URLSearchParams({ ...currentEntity, limit, include_replies: ir, reply_post_limit: replyPostLimit, refresh: forceRefresh ? 'true' : 'false' });
      fetch('/api/videos?' + params).then(r => r.json()).then(data => {
        if (data.error) {
          document.getElementById('videoTitle').textContent = currentDialogName;
          document.getElementById('videoList').innerHTML = '<div class="empty">' + esc(data.error) + '</div>';
          return;
        }
        if (currentEntity && data.entity_id != null) {
          currentEntity.entity_id = data.entity_id;
        }
        videos = data.videos;
        const cacheTag = data.cached ? '<span class="badge badge-cache">缓存</span>' : '';
        updateVideoTitle(cacheTag);

        if (videos.length > 0) {
          document.getElementById('filterBar').style.display = 'flex';
          document.getElementById('videoToolbar').style.display = 'flex';
          document.getElementById('filterText').value = '';
          document.getElementById('sortBy').value = 'default';
          document.getElementById('filterSizeMin').value = ''; // Clear new filter input
          applyFilter();
        } else if (!data.posts_with_replies || data.posts_with_replies.length === 0) {
          document.getElementById('videoList').innerHTML = '<div class="empty">未找到视频</div>';
        }

        if (ir && data.posts_with_replies && data.posts_with_replies.length > 0) {
          scanRepliesSequentially(data.posts_with_replies, data.entity_id || currentEntity.entity_id, cacheTag, forceRefresh);
        }
      }).catch(e => {
        document.getElementById('videoList').innerHTML = '<div class="empty">扫描失败，请检查连接</div>';
      });
    }

    function updateVideoTitle(extra = '') {
      document.getElementById('videoTitle').innerHTML = `${esc(currentDialogName)} (${videos.length} 个视频) ${extra}`;
    }

    function mergeVideos(newItems) {
      const merged = new Map(videos.map(v => [selectionKeyForVideo(v), v]));
      (newItems || []).forEach(v => merged.set(selectionKeyForVideo(v), v));
      videos = [...merged.values()];
    }

    function searchChannelVideos() {
      if (!currentEntity) { alert('请先选择或搜索频道'); return; }
      const keyword = document.getElementById('filterText').value.trim();
      if (!keyword) { alert('请输入要搜索的文件名或关键词'); return; }

      const scanLimitEl = document.getElementById('scanLimit');
      const configuredLimit = parseInt(scanLimitEl?.value || '100', 10) || 100;
      const scanLimit = Math.min(Math.max(configuredLimit, 1000), 5000);
      const params = new URLSearchParams({
        ...currentEntity,
        q: keyword,
        limit: '300',
        scan_limit: String(scanLimit),
        include_comments: 'true',
        comment_post_limit: '80',
        comment_limit: '100',
      });

      document.getElementById('filterBar').style.display = 'flex';
      document.getElementById('videoToolbar').style.display = 'none';
      document.getElementById('videoTitle').textContent = currentDialogName + ' - 频道搜索中...';
      showVideoListSkeleton('正在搜索 Telegram 索引、文件名、标签和评论...');

      fetch('/api/video_search?' + params).then(r => r.json()).then(data => {
        if (data.error) {
          document.getElementById('videoTitle').textContent = currentDialogName;
          document.getElementById('videoList').innerHTML = '<div class="empty">' + esc(data.error) + '</div>';
          return;
        }
        if (currentEntity && data.entity_id != null) currentEntity.entity_id = data.entity_id;

        const results = Array.isArray(data.videos) ? data.videos : [];
        mergeVideos(results);
        filteredVideos = results;
        document.getElementById('totalCount').textContent = filteredVideos.length;
        document.getElementById('selectAll').checked = false;
        document.getElementById('filterBar').style.display = 'flex';
        document.getElementById('videoToolbar').style.display = 'flex';
        updateVideoTitle(`<span class="badge badge-cache">频道搜索 ${results.length} 个 · 主帖 ${data.scanned || 0} 条 · 评论 ${data.comments_scanned || 0} 条</span>`);
        renderVideoList();
        updateSelectedCount();
      }).catch(() => {
        document.getElementById('videoTitle').textContent = currentDialogName;
        document.getElementById('videoList').innerHTML = '<div class="empty">频道搜索失败，请检查连接</div>';
      });
    }

    async function scanRepliesSequentially(posts, entityId, cacheTag, forceRefresh = false) {
      scanningReplies = true;
      let count = 0;
      const total = posts.length;
      const replyLimit = 100;

      const statusToast = document.createElement('span');
      statusToast.style = "font-size:11px; color:var(--accent); margin-left:10px;";
      document.getElementById('videoTitle').appendChild(statusToast);

      // 分批处理，避免请求过多
      for (let i = 0; i < posts.length; i += 2) {
        if (!scanningReplies) break;
        const chunk = posts.slice(i, i + 2);
        count += chunk.length;
        statusToast.textContent = `(正在扫描评论区: ${count}/${total})`;

        try {
          const results = await Promise.all(chunk.map(post =>
            fetch(`/api/replies?entity_id=${entityId}&post_id=${post.id}&limit=${replyLimit}&refresh=${forceRefresh ? 'true' : 'false'}`).then(r => r.json()).catch(() => ({ videos: [] }))
          ));

          let newVideosFound = false;
          results.forEach(res => {
            if (res.videos && res.videos.length > 0) {
              res.videos.forEach(nv => {
                const newKey = selectionKeyForVideo(nv);
                if (!videos.find(v => selectionKeyForVideo(v) === newKey)) {
                  videos.push(nv);
                  newVideosFound = true;
                }
              });
            }
          });

          if (newVideosFound) {
            document.getElementById('filterBar').style.display = 'flex';
            document.getElementById('videoToolbar').style.display = 'flex';
            updateVideoTitle(cacheTag);
            document.getElementById('videoTitle').appendChild(statusToast);
            applyFilter();
          }
        } catch (e) { }
      }
      if (scanningReplies) {
        statusToast.textContent = '(评论区扫描完成)';
        setTimeout(() => { if (statusToast.parentNode) statusToast.remove(); }, 3000);
      }
    }

    function applyFilter() {
      const keyword = document.getElementById('filterText').value.toLowerCase();
      const sortBy = document.getElementById('sortBy').value;
      const sizeMinText = document.getElementById('filterSizeMin').value;
      const sizeMin = sizeMinText ? parseFloat(sizeMinText) * 1024 * 1024 : 0;

      filteredVideos = videos.filter(v => {
        const contextHaystack = [v.filename, v.text_excerpt || '', v.parent_text_excerpt || ''].join(' ').toLowerCase();
        const matchKw = !keyword || contextHaystack.includes(keyword);
        const matchSz = !sizeMin || v.size >= sizeMin;
        return matchKw && matchSz;
      });

      if (sortBy === 'size_desc') filteredVideos.sort((a, b) => b.size - a.size);
      else if (sortBy === 'size_asc') filteredVideos.sort((a, b) => a.size - b.size);
      else if (sortBy === 'duration_desc') filteredVideos.sort((a, b) => b.duration - a.duration);
      else if (sortBy === 'duration_asc') filteredVideos.sort((a, b) => a.duration - b.duration);
      else if (sortBy === 'date_desc') filteredVideos.sort((a, b) => b.date > a.date ? 1 : -1);
      else if (sortBy === 'date_asc') filteredVideos.sort((a, b) => a.date > b.date ? 1 : -1);
      else if (sortBy === 'name_asc') filteredVideos.sort((a, b) => a.filename.localeCompare(b.filename));

      document.getElementById('totalCount').textContent = filteredVideos.length;
      document.getElementById('selectAll').checked = false;
      renderVideoList();
      updateSelectedCount();
    }

    let renderedCount = 0;
    const CHUNK_SIZE = 100;
    let listObserver = null;

    function renderVideoList() {
      if (listObserver) {
        listObserver.disconnect();
        listObserver = null;
      }
      const container = document.getElementById('videoList');
      container.innerHTML = '';
      renderedCount = 0;

      if (filteredVideos.length === 0) {
        container.innerHTML = '<div class="empty">无匹配视频</div>';
        return;
      }
      renderNextChunk();
    }

    function renderNextChunk() {
      const container = document.getElementById('videoList');
      const oldSentinel = document.getElementById('lazyRenderSentinel');
      if (oldSentinel) oldSentinel.remove();

      const nextBatch = filteredVideos.slice(renderedCount, renderedCount + CHUNK_SIZE);
      if (nextBatch.length === 0) return;

      const html = nextBatch.map(v => {
        const entityId = videoEntityId(v);
        const entityParam = entityId != null ? `?entity=${encodeURIComponent(entityId)}` : '';
        const thumb = v.has_thumb
          ? `<img class="video-thumb" src="/api/thumb/${v.id}${entityParam}" loading="lazy" onclick="previewByThumb(${v.id})" onerror="this.outerHTML='<div class=\'video-thumb-placeholder\'>&#9654;</div>'">`
          : '<div class="video-thumb-placeholder">&#9654;</div>';
        const rb = v.source && v.source !== '主消息' ? '<span class="badge badge-reply">' + esc(v.source) + '</span>' : '';
        const textExcerpt = (v.text_excerpt || '').trim();
        const parentExcerpt = (v.parent_text_excerpt || '').trim();
        const contextBlocks = [];
        if (textExcerpt) {
          contextBlocks.push(`
        <div class="video-context-block">
          <div class="video-context-label">消息摘要</div>
          <div class="video-context-text">${esc(textExcerpt)}</div>
        </div>`);
        }
        if (parentExcerpt) {
          contextBlocks.push(`
        <div class="video-context-block parent">
          <div class="video-context-label">主帖摘要</div>
          <div class="video-context-text">${esc(parentExcerpt)}</div>
        </div>`);
        }
        const selectionKey = selectionKeyForVideo(v);
        const isChecked = selectedIds.has(selectionKey) ? 'checked' : '';
        return `<div class="video-item">
      <input type="checkbox" class="vcb" data-id="${v.id}" data-entity-id="${entityId ?? ''}" data-size="${v.size}" ${isChecked} onchange="handleCbChange(this)">
      ${thumb}
      <div class="video-info">
        <div class="video-name" title="${esc(v.filename)}">${esc(v.filename)}${rb}</div>
        ${contextBlocks.join('')}
        <div class="video-meta"><span>${v.size_fmt}</span><span>${v.duration_fmt}</span><span>${v.date}</span></div>
        <div class="video-actions">
          <button class="btn btn-sm btn-outline" data-play-key="${selectionKey}" onclick="playOnlineVideoFromButton(this, event)">在线播放</button>
        </div>
      </div>
    </div>`;
      }).join('');

      const tempDiv = document.createElement('div');
      tempDiv.innerHTML = html;
      const fragment = document.createDocumentFragment();
      while (tempDiv.firstChild) {
        fragment.appendChild(tempDiv.firstChild);
      }
      container.appendChild(fragment);

      renderedCount += nextBatch.length;

      if (renderedCount < filteredVideos.length) {
        const sentinel = document.createElement('div');
        sentinel.id = 'lazyRenderSentinel';
        sentinel.style.height = '40px';
        sentinel.style.margin = '10px 0';
        sentinel.style.display = 'flex';
        sentinel.style.justify = 'center';
        sentinel.style.alignItems = 'center';
        sentinel.innerHTML = '<div class="spinner" style="width: 16px; height: 16px; border-width: 2px;"></div> <span style="font-size: 11px; color: var(--dim); margin-left: 6px;">正在滚动加载更多视频...</span>';
        container.appendChild(sentinel);

        listObserver = new IntersectionObserver(entries => {
          if (entries[0].isIntersecting) {
            listObserver.disconnect();
            listObserver = null;
            renderNextChunk();
          }
        }, { rootMargin: '200px' });
        listObserver.observe(sentinel);
      }
    }

    function handleCbChange(cb) {
      const id = parseInt(cb.dataset.id);
      const entityId = cb.dataset.entityId;
      const key = makeSelectionKey(entityId, id);
      if (cb.checked) selectedIds.add(key);
      else selectedIds.delete(key);
      updateSelectedCount();
    }

    function toggleSelectAll() {
      const checked = document.getElementById('selectAll').checked;
      filteredVideos.forEach(v => {
        const key = selectionKeyForVideo(v);
        if (checked) selectedIds.add(key);
        else selectedIds.delete(key);
      });
      renderVideoList();
      updateSelectedCount();
    }

    function invertSelection() {
      filteredVideos.forEach(v => {
        const key = selectionKeyForVideo(v);
        if (selectedIds.has(key)) selectedIds.delete(key);
        else selectedIds.add(key);
      });
      renderVideoList();
      updateSelectedCount();
    }

    function updateSelectedCount() {
      const count = selectedIds.size;
      let totalBytes = 0;
      videos.forEach(v => {
        if (selectedIds.has(selectionKeyForVideo(v))) totalBytes += v.size;
      });
      document.getElementById('selectedCount').textContent = count;
      document.getElementById('selectedInfo').textContent = count ? `已选 ${fmtSize(totalBytes)}` : '';
      const floatEl = document.getElementById('selectionFloat');
      const floatInfo = document.getElementById('selectionFloatInfo');
      if (floatEl && floatInfo) {
        floatInfo.innerHTML = count
          ? `已选 <strong>${count}</strong> 个 · ${fmtSize(totalBytes)}`
          : '已选 0 个';
        floatEl.classList.toggle('visible', count > 0);
      }
    }

    function clearVideoSelection() {
      selectedIds.clear();
      const selectAll = document.getElementById('selectAll');
      if (selectAll) selectAll.checked = false;
      renderVideoList();
      updateSelectedCount();
    }

    function fmtSize(b) {
      if (typeof b !== 'number') b = Number(b) || 0;
      if (!Number.isFinite(b) || b < 0) return '0B';
      for (const u of ['B', 'KB', 'MB', 'GB']) { if (b < 1024) return b.toFixed(1) + u; b /= 1024; }
      return b.toFixed(1) + 'TB';
    }

    function formatEta(seconds) {
      if (!Number.isFinite(seconds) || seconds <= 0) return '';
      const total = Math.round(seconds);
      const mins = Math.floor(total / 60);
      const secs = total % 60;
      if (mins >= 60) {
        const hours = Math.floor(mins / 60);
        const remMins = mins % 60;
        return `${hours}h ${remMins}m`;
      }
      if (mins > 0) {
        return `${mins}m ${secs.toString().padStart(2, '0')}s`;
      }
      return `${secs}s`;
    }

    function updateQueueSummary(queueData = null, tasks = null) {
      const el = document.getElementById('queueSummary');
      if (!el) return;
      if (!queueData) {
        queueData = lastQueueSnapshot || { active: 0, queued: 0, max: MAX_CONCURRENT_HINT };
      } else {
        lastQueueSnapshot = queueData;
      }
      if (tasks) lastTaskSnapshot = tasks;
      const totalSpeed = tasks ? Object.values(tasks).reduce((sum, info) => sum + (info.speed_bps || 0), 0) : 0;
      const speedText = totalSpeed ? fmtSize(totalSpeed) + '/s' : '0B/s';
      const active = queueData.active ?? 0;
      const queued = queueData.queued ?? 0;
      const max = queueData.max ?? MAX_CONCURRENT_HINT;
      el.innerHTML = `并发 <strong>${active}</strong>/${max} · 排队 <strong>${queued}</strong> · 总速率 <strong>${speedText}</strong>`;
      updateMobileTaskFab(queueData, tasks || lastTaskSnapshot);
    }

    function downloadSelected() {
      if (!selectedIds.size) { alert('请先选择视频'); return; }
      const selected = videos.filter(v => selectedIds.has(selectionKeyForVideo(v)));
      if (!selected.length) { alert('未找到已选视频'); return; }
      const groups = new Map();
      selected.forEach(v => {
        const entityId = videoEntityId(v);
        if (entityId == null) return;
        const taskId = makeTaskId(entityId, v.id);
        dismissedTaskIds.delete(taskId);
        addDownloadItem(taskId, v.filename, v.size_fmt, entityId, v.id, currentDialogName);
        if (!groups.has(entityId)) groups.set(entityId, []);
        groups.get(entityId).push(v.id);
      });
      if (!groups.size) { alert('缺少视频所属实体 ID，请重新扫描后重试'); return; }
      switchTab('downloads', document.querySelector('.tab'));
      if (isMobileLayout()) setMobilePanel('downloads');
      Promise.all([...groups.entries()].map(([entityId, msgIds]) =>
        fetch('/api/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message_ids: msgIds, dialog_name: currentDialogName, entity_id: entityId }),
        }).then(r => r.json())
      )).then(results => {
        const failed = results.find(data => data.error);
        if (failed) { alert(failed.error); return; }
        startProgressPolling();
        selectedIds.clear();
        const selectAll = document.getElementById('selectAll');
        if (selectAll) selectAll.checked = false;
        renderVideoList();
        updateSelectedCount();
      });
    }

    function addDownloadItem(taskId, filename, size = '', entityId = '', msgId = '', dialogName = '') {
      const container = document.getElementById('tabDownloads');
      if (container.querySelector('.empty')) container.innerHTML = '';
      const domId = domIdForTask(taskId);
      const actionDomId = `dla-${domId}`;
      const safeSize = size || '';
      let el = document.getElementById(domId);
      if (!el) {
        container.insertAdjacentHTML('afterbegin', `
      <div class="dl-item" id="${domId}">
        <div class="dl-top">
          <input class="task-select" type="checkbox" data-task-id="${taskId}">
          <div class="dl-name" title="${esc(filename)}">${esc(filename)}</div>
          <div class="dl-actions" id="${actionDomId}">
            <button class="btn-sm btn-danger" onclick="cancelDownload('${taskId}')">取消</button>
          </div>
        </div>
        <div class="dl-bar"><div class="dl-bar-fill" style="width:0%"></div></div>
        <div class="dl-status">
          <span class="dl-pct">等待中</span>
          <span class="dl-speed"></span>
          <span class="dl-size">${safeSize}</span>
          <span class="dl-downloader"></span>
        </div>
      </div>
    `);
        el = document.getElementById(domId);
      }
      if (el) {
        const actionEl = el.querySelector('.dl-actions');
        if (actionEl) actionEl.id = actionDomId;
        el.dataset.actionsId = actionDomId;
      }
      setTaskMeta(el, taskId, entityId, msgId, dialogName);
    }

    function cancelDownload(taskId) {
      fetch('/api/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId }),
      });
    }

    function retryDownload(taskId) {
      dismissedTaskIds.delete(taskId);
      const domId = domIdForTask(taskId);
      const el = document.getElementById(domId);
      if (el) {
        el.classList.remove('dl-error', 'dl-cancelled');
        el.querySelector('.dl-bar-fill').style.width = '0%';
        el.querySelector('.dl-pct').textContent = '等待中';
        el.querySelector('.dl-speed').textContent = '';
        const actionEl = actionContainerFor(el);
        if (actionEl) {
          actionEl.innerHTML = `<button class="btn-sm btn-danger" onclick="cancelDownload('${taskId}')">取消</button>`;
        }
      }
      const dialogName = el?.dataset.dialogName || currentDialogName;
      const entityId = el?.dataset.entityId || currentEntity?.entity_id || null;
      fetch('/api/retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, dialog_name: dialogName, entity_id: entityId }),
      }).then(async response => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.error || '重试失败');
        }
        if (payload.error) {
          throw new Error(payload.error);
        }
        startProgressPolling();
      }).catch(err => {
        restoreDownloadTasks();
        alert(err?.message || '重试失败，请稍后再试');
      });
    }

    function clearFailedTasks() {
      const terminalEls = [...document.querySelectorAll('#tabDownloads .dl-item.dl-error, #tabDownloads .dl-item.dl-cancelled')];
      const taskIds = terminalEls.map(el => el.dataset.taskId).filter(Boolean);
      taskIds.forEach(id => dismissedTaskIds.add(id));
      fetch('/api/clear_tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: taskIds }) }).catch(() => { });
      terminalEls.forEach(el => el.remove());
      const container = document.getElementById('tabDownloads');
      if (container && !container.querySelector('.dl-item')) container.innerHTML = '<div class="empty">暂无下载任务</div>';
    }

    function retryAllIncomplete() {
      fetch('/api/retry_all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(async response => {
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.error) throw new Error(payload.error || '继续失败');
          restoreDownloadTasks();
          startProgressPolling();
        })
        .catch(err => alert(err?.message || '继续失败，请稍后再试'));
    }

    function queueAction(taskId, action) {
      return fetch('/api/queue_action', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, action })
      }).then(async response => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.error) throw new Error(payload.error || '操作失败');
        restoreDownloadTasks();
        startProgressPolling();
        return payload;
      });
    }

    function selectedTaskIds() {
      return [...document.querySelectorAll('#tabDownloads .task-select:checked')].map(el => el.dataset.taskId);
    }

    function toggleAllTasks(checked) {
      document.querySelectorAll('#tabDownloads .task-select').forEach(el => { el.checked = checked; });
    }

    function bulkTaskAction(action) {
      const ids = selectedTaskIds();
      if (!ids.length) { alert('请先选择任务'); return; }
      Promise.allSettled(ids.map(id => queueAction(id, action))).then(results => {
        const failed = results.find(item => item.status === 'rejected');
        if (failed) alert(failed.reason?.message || '部分任务操作失败');
        restoreDownloadTasks();
      });
    }

    function isTerminalStatus(status) {
      return ['done', 'skipped', 'error', 'cancelled'].includes(status);
    }

    function applyDownloadStatus(taskId, info) {
      if (!taskId) return;
      if (dismissedTaskIds.has(taskId)) return;

      addDownloadItem(taskId, info.filename || 'unknown', info.total || '', info.entity_id, info.msg_id, info.dialog_name);

      const domId = domIdForTask(taskId);
      const el = document.getElementById(domId);
      if (!el) return;
      setTaskMeta(el, taskId, info.entity_id, info.msg_id, info.dialog_name || el.dataset.dialogName);

      const barEl = el.querySelector('.dl-bar-fill');
      if (barEl) barEl.style.width = (info.progress || 0) + '%';
      const pctEl = el.querySelector('.dl-pct');
      const spdEl = el.querySelector('.dl-speed');
      const szEl = el.querySelector('.dl-size');
      const downloaderEl = el.querySelector('.dl-downloader');
      const actEl = actionContainerFor(el);
      if (!pctEl || !spdEl || !szEl) return;

      const downloadedLabel = info.downloaded || (info.downloaded_bytes != null ? fmtSize(info.downloaded_bytes) : '');
      const totalLabel = info.total || (info.total_bytes ? fmtSize(info.total_bytes) : '');
      const etaSeconds = info.speed_bps && info.total_bytes ? Math.max(0, (info.total_bytes - (info.downloaded_bytes || 0)) / (info.speed_bps || 1)) : 0;
      const speedParts = [];
      if (info.speed) speedParts.push(info.speed);
      const etaText = formatEta(etaSeconds);
      if (etaText) speedParts.push('ETA ' + etaText);
      spdEl.textContent = speedParts.join(' · ');

      const sizeDisplay = downloadedLabel ? `${downloadedLabel}${totalLabel ? ' / ' + totalLabel : ''}` : totalLabel;
      if (sizeDisplay) szEl.textContent = sizeDisplay;
      if (downloaderEl) {
        const downloader = info.downloader || '';
        downloaderEl.textContent = downloader ? `下载器: ${downloader}` : '';
      }

      el.classList.remove('dl-done', 'dl-skipped', 'dl-error', 'dl-cancelled');

      if (info.status === 'done') {
        pctEl.textContent = '完成';
        spdEl.textContent = '';
        if (totalLabel) szEl.textContent = totalLabel;
        el.classList.add('dl-done');
        if (actEl) actEl.innerHTML = '';
        if (!completedTasksNotified.has(taskId)) {
          completedTasksNotified.add(taskId);
          if ("Notification" in window && Notification.permission === "granted") {
            new Notification("✅ 下载完成", { body: info.filename || '未知文件', icon: 'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🚀</text></svg>' });
          }
        }
        setTimeout(() => {
          dismissedTaskIds.add(taskId);
          const item = document.getElementById(domId);
          if (item && item.classList.contains('dl-done')) {
            item.remove();
            const ct = document.getElementById('tabDownloads');
            if (ct && !ct.querySelector('.dl-item')) ct.innerHTML = '<div class="empty">暂无下载任务</div>';
          }
        }, 8000);
      } else if (info.status === 'skipped') {
        pctEl.textContent = '已跳过';
        spdEl.textContent = '';
        if (totalLabel) szEl.textContent = totalLabel;
        el.classList.add('dl-skipped');
        if (actEl) actEl.innerHTML = '';
        setTimeout(() => {
          dismissedTaskIds.add(taskId);
          const item = document.getElementById(domId);
          if (item && item.classList.contains('dl-skipped')) {
            item.remove();
            const ct = document.getElementById('tabDownloads');
            if (ct && !ct.querySelector('.dl-item')) ct.innerHTML = '<div class="empty">暂无下载任务</div>';
          }
        }, 8000);
      } else if (info.status === 'error') {
        pctEl.textContent = '错误: ' + (info.error || '未知错误');
        spdEl.textContent = '';
        el.classList.add('dl-error');
        if (actEl) actEl.innerHTML = `<button class=\"btn-sm btn-retry\" onclick=\"retryDownload('${taskId}')\">重试</button>`;
      } else if (info.status === 'cancelled') {
        pctEl.textContent = '已取消';
        spdEl.textContent = '';
        el.classList.add('dl-cancelled');
        if (actEl) actEl.innerHTML = `<button class=\"btn-sm btn-retry\" onclick=\"retryDownload('${taskId}')\">重试</button>`;
      } else if (info.status === 'paused') {
        pctEl.textContent = '已暂停';
        spdEl.textContent = '';
        if (actEl) actEl.innerHTML = `<button class=\"btn-sm btn-retry\" onclick=\"queueAction('${taskId}','resume')\">继续</button><button class=\"btn-sm btn-danger\" onclick=\"queueAction('${taskId}','delete')\">删除</button>`;
      } else if (info.status === 'queued') {
        const pos = info.queue_position ? ` (#${info.queue_position})` : '';
        pctEl.textContent = '排队中' + pos;
        if (actEl) actEl.innerHTML = `<button class=\"btn-sm btn-outline\" onclick=\"queueAction('${taskId}','top')\">置顶</button><button class=\"btn-sm btn-outline\" onclick=\"queueAction('${taskId}','up')\">↑</button><button class=\"btn-sm btn-outline\" onclick=\"queueAction('${taskId}','down')\">↓</button><button class=\"btn-sm btn-outline\" onclick=\"queueAction('${taskId}','pause')\">暂停</button><button class=\"btn-sm btn-danger\" onclick=\"queueAction('${taskId}','delete')\">删除</button>`;
      } else if (info.status === 'downloading') {
        pctEl.textContent = (info.progress || 0) + '%';
        if (actEl) actEl.innerHTML = `<button class=\"btn-sm btn-danger\" onclick=\"cancelDownload('${taskId}')\">取消</button>`;
      } else {
        pctEl.textContent = '等待中';
        if (actEl) actEl.innerHTML = `<button class=\"btn-sm btn-danger\" onclick=\"cancelDownload('${taskId}')\">取消</button>`;
      }
    }


    function restoreDownloadTasks() {
      fetch('/api/download_status').then(r => r.json()).then(payload => {
        const tasks = payload.tasks || {};
        updateQueueSummary(payload.queue || null, tasks);
        const entries = Object.entries(tasks);
        if (!entries.length) return;

        entries.sort((a, b) => {
          const mb = parseInt((b[1] && b[1].msg_id) || 0, 10);
          const ma = parseInt((a[1] && a[1].msg_id) || 0, 10);
          return mb - ma;
        });
        entries.forEach(([taskId, info]) => applyDownloadStatus(taskId, info));

        const hasRunning = entries.some(([, info]) => !isTerminalStatus(info.status));
        if (hasRunning) startProgressPolling();
      }).catch(() => { });
    }

    function startProgressPolling() {
      // 关闭旧 SSE（若有）
      if (evtSource) { evtSource.close(); evtSource = null; }
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

      // 用轮询代替 SSE；pollGeneration 防止多个轮询周期并发
      const gen = ++pollGeneration;
      function doPoll() {
        if (gen !== pollGeneration) return;
        fetch('/api/download_status')
          .then(r => r.json())
          .then(payload => {
            if (gen !== pollGeneration) return;
            const tasks = payload.tasks || {};
            Object.entries(tasks).forEach(([tid, info]) => applyDownloadStatus(tid, info));
            updateQueueSummary(payload.queue || null, tasks);
            const hasActive = Object.values(tasks).some(t => !isTerminalStatus(t.status));
            if (hasActive) {
              reconnectTimer = setTimeout(doPoll, 1000);
            } else if (Object.keys(tasks).length > 0) {
              loadFiles();
            }
          })
          .catch(() => { if (gen === pollGeneration) reconnectTimer = setTimeout(doPoll, 2000); });
      }
      doPoll();
    }

    function updateHealthSummary(data) {
      const el = document.getElementById('healthSummary');
      if (!el || !data) return;
      const tg = data.telegram || {};
      const relay = data.relay || {};
      const proxy = data.proxy || {};
      const tdl = data.tdl || {};
      const parts = [
        `Telegram <span class="${tg.connected ? 'health-ok' : 'health-bad'}">${tg.connected ? esc(tg.user || '已连接') : esc(tg.error || '未连接')}</span>`,
        `代理 <span class="${proxy.ok ? 'health-ok' : 'health-bad'}">${proxy.enabled ? esc(proxy.label || '') : '未启用'}${proxy.latency_ms ? ' ' + proxy.latency_ms + 'ms' : ''}</span>`,
        `Relay <span class="${relay.connected ? 'health-ok' : 'health-bad'}">${relay.connected ? '已连接' : esc(relay.error || '未连接')}</span>`,
        `tdl <span class="${tdl.ok ? 'health-ok' : 'health-bad'}">${esc(tdl.version || tdl.error || '未知')}</span>`,
        `恢复文件 <strong>${data.resume_files ?? 0}</strong>`,
        `持久任务 <strong>${data.tasks_persisted ?? 0}</strong>`,
      ];
      el.innerHTML = parts.join(' · ');
    }

    function refreshHealth() {
      fetch('/api/health')
        .then(r => r.json())
        .then(updateHealthSummary)
        .catch(() => {
          const el = document.getElementById('healthSummary');
          if (el) el.innerHTML = '<span class="health-bad">健康检查不可用</span>';
        });
    }

    function updateProxySwitcher(data) {
      const select = document.getElementById('proxyTypeSelect');
      const status = document.getElementById('proxyTypeStatus');
      if (!select || !status || !data) return;
      const current = data.configured_proxy_type || data.proxy_type || data.active_proxy_type || 'http';
      select.value = current;
      if (data.restart_required) {
        status.innerHTML = `<span class="health-bad">已保存 ${esc(data.configured_label || current)}，重启后生效</span>`;
      } else {
        status.innerHTML = `<span class="health-ok">当前 ${esc(data.label || current)}</span>`;
      }
    }

    function loadProxySettings() {
      fetch('/api/settings/proxy')
        .then(r => r.json())
        .then(updateProxySwitcher)
        .catch(() => {
          const status = document.getElementById('proxyTypeStatus');
          if (status) status.innerHTML = '<span class="health-bad">代理设置加载失败</span>';
        });
    }

    function saveProxyType() {
      const select = document.getElementById('proxyTypeSelect');
      const status = document.getElementById('proxyTypeStatus');
      const btn = document.getElementById('proxyTypeSaveBtn');
      if (!select) return;
      if (btn) btn.disabled = true;
      if (status) status.textContent = '保存中...';
      fetch('/api/settings/proxy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ proxy_type: select.value })
      })
        .then(async r => {
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.error || '保存失败');
          return data;
        })
        .then(data => {
          updateProxySwitcher(data);
          refreshHealth();
        })
        .catch(err => {
          if (status) status.innerHTML = `<span class="health-bad">${esc(err.message || String(err))}</span>`;
        })
        .finally(() => {
          if (btn) btn.disabled = false;
        });
    }

    loadProxySettings();

    /* Preview */
    function videoBySelectionKey(key) {
      return videos.find(v => selectionKeyForVideo(v) === key)
        || filteredVideos.find(v => selectionKeyForVideo(v) === key);
    }

    function playOnlineVideo(key, event) {
      if (event) event.stopPropagation();
      const v = videoBySelectionKey(key);
      if (!v) { alert('未找到视频信息，请刷新列表后重试'); return; }
      const entityId = videoEntityId(v);
      if (entityId == null) { alert('缺少视频所属对话 ID，请重新扫描后重试'); return; }
      const url = `/api/online-play-url?entity_id=${encodeURIComponent(entityId)}&msg_id=${encodeURIComponent(v.id)}&filename=${encodeURIComponent(v.filename || '')}`;
      fetch(url)
        .then(async response => {
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.error) throw new Error(payload.error || '获取播放链接失败');
          openPreview(payload.url, payload.filename || v.filename || '在线播放');
        })
        .catch(err => alert(err?.message || '获取播放链接失败'));
    }

    function playOnlineVideoFromButton(button, event) {
      playOnlineVideo(button?.dataset?.playKey || '', event);
    }

    function previewByThumb(msgId) {
      fetch('/api/files?per_page=500').then(r => r.json()).then(payload => {
        const filesList = Array.isArray(payload) ? payload : (payload.files || []);
        const v = videos.find(x => x.id === msgId);
        if (!v) return;
        const f = filesList.find(x => x.filename === v.filename);
        if (f) {
          openPreview('/api/stream/' + encodeURIComponent(f.folder) + '/' + encodeURIComponent(f.filename), v.filename);
        } else {
          alert('该视频尚未下载，请先下载后再预览');
        }
      });
    }

    function previewFile(folder, filename) {
      openPreview('/api/stream/' + encodeURIComponent(folder) + '/' + encodeURIComponent(filename), filename);
    }

    function openPreview(url, title) {
      const modal = document.getElementById('previewModal');
      const video = document.getElementById('previewVideo');
      const titleEl = document.getElementById('previewTitle');
      video.dataset.closing = '';
      video.onerror = () => {
        if (video.dataset.closing === '1') return;
        alert('视频无法播放，可能文件为 0B、格式不受浏览器支持，或文件仍未下载完整');
      };
      video.src = url;
      titleEl.textContent = title;
      modal.classList.remove('hidden');
    }

    function closePreview(e) {
      if (e && e.target !== e.currentTarget) return;
      const modal = document.getElementById('previewModal');
      const video = document.getElementById('previewVideo');
      video.dataset.closing = '1';
      video.onerror = null;
      video.pause();
      video.removeAttribute('src');
      video.src = '';
      video.load();
      modal.classList.add('hidden');
    }

    document.addEventListener('keydown', e => { if (e.key === 'Escape') closePreview(); });

    /* Tabs */
    function switchTab(tab, el) {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      el.classList.add('active');
      document.getElementById('tabDownloads').style.display = tab === 'downloads' ? '' : 'none';
      document.getElementById('tabFiles').style.display = tab === 'files' ? '' : 'none';
      document.getElementById('tabHistory').style.display = tab === 'history' ? '' : 'none';
      document.getElementById('tabRecovery').style.display = tab === 'recovery' ? '' : 'none';
      document.getElementById('taskToolbar').style.display = tab === 'downloads' ? 'flex' : 'none';
      if (isMobileLayout()) setMobilePanel('downloads');
      if (tab === 'files') loadFiles();
      if (tab === 'history') loadHistory(1);
      if (tab === 'recovery') loadRecovery();
    }

    function loadHistory(page = 1) {
      const q = document.getElementById('historySearch').value || '';
      const status = document.getElementById('historyStatus').value || '';
      fetch(`/api/history?page=${page}&per_page=30&q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}`)
        .then(r => r.json()).then(payload => {
          historyPage = payload.page || 1;
          const items = payload.items || [];
          let html = items.map(info => `<div class="file-item">
            <div class="file-info"><div class="fname">${esc(info.filename || info.task_id || 'unknown')}</div>
            <div class="meta">${esc(info.dialog_name || '')} · ${esc(info.status || '')} · ${esc(info.downloader || '')} · ${info.final_bytes != null ? esc(fmtSize(info.final_bytes)) : ''}${info.integrity ? ' · 校验 ' + esc(info.integrity) : ''}</div></div>
          </div>`).join('');
          if (!html) html = '<div class="empty">暂无历史记录</div>';
          const pages = Math.max(1, Math.ceil((payload.total || 0) / 30));
          if (pages > 1) html += `<div class="queue-summary"><button class="btn btn-sm btn-outline" ${historyPage <= 1 ? 'disabled' : ''} onclick="loadHistory(${historyPage - 1})">上一页</button> 第 ${historyPage}/${pages} 页 <button class="btn btn-sm btn-outline" ${historyPage >= pages ? 'disabled' : ''} onclick="loadHistory(${historyPage + 1})">下一页</button></div>`;
          document.getElementById('historyList').innerHTML = html;
        });
    }

    function loadRecovery() {
      fetch('/api/recovery_candidates').then(r => r.json()).then(payload => {
        const items = payload.candidates || [];
        document.getElementById('recoveryList').innerHTML = items.length ? items.map(item => `<div class="file-item">
          <input class="recover-select" type="checkbox" data-task-id="${esc(item.task_id)}">
          <div class="file-info"><div class="fname">${esc(item.filename || item.task_id)}</div><div class="meta">${esc(item.task_id)} · ${esc(item.error || '')}</div></div>
        </div>`).join('') : '<div class="empty">没有可从日志恢复的失败任务</div>';
      });
    }

    function toggleRecovery(checked) {
      document.querySelectorAll('.recover-select').forEach(el => { el.checked = checked; });
    }

    function recoverSelected() {
      const taskIds = [...document.querySelectorAll('.recover-select:checked')].map(el => el.dataset.taskId);
      if (!taskIds.length) { alert('请先选择日志任务'); return; }
      fetch('/api/recover_candidates', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: taskIds })
      }).then(r => r.json()).then(payload => {
        if (Object.keys(payload.errors || {}).length) alert('部分任务恢复失败，请检查任务状态');
        restoreDownloadTasks();
        startProgressPolling();
        loadRecovery();
      });
    }

    function loadFiles(page = 1) {
      fetch('/api/files?page=' + encodeURIComponent(page) + '&per_page=100').then(r => r.json()).then(payload => {
        const data = Array.isArray(payload) ? payload : (payload.files || []);
        filesPage = Array.isArray(payload) ? 1 : (payload.page || 1);
        if (!data.length) { document.getElementById('tabFiles').innerHTML = '<div class="empty">暂无已下载文件</div>'; return; }
        let html = data.map(f => {
          const dlUrl = '/api/file/' + encodeURIComponent(f.folder) + '/' + encodeURIComponent(f.filename);
          const playable = f.playable !== false;
          const playReason = f.play_block_reason || '';
          const statusText = !playable && playReason ? ` · ${esc(playReason)}` : '';
          const playButton = playable
            ? `<button type="button" class="btn-dl" onclick="previewFileFromItem(this.closest('.file-item'))">播放</button>`
            : `<button type="button" class="btn-dl" disabled title="${esc(playReason)}">下载中</button>`;
          return `<div class="file-item" data-folder="${esc(f.folder)}" data-filename="${esc(f.filename)}" data-playable="${playable ? '1' : '0'}" data-play-reason="${esc(playReason)}" oncontextmenu="showFileContextMenu(event,this)">
        <div class="file-info">
          <div class="fname" title="${esc(f.folder)}/${esc(f.filename)}" onclick="previewFileFromItem(this.closest('.file-item'))"><span class="folder">${esc(f.folder)}/</span> ${esc(f.filename)}</div>
          <div class="meta">${f.size} · ${f.modified}${statusText}</div>
        </div>
        <div class="file-btns">
          <button type="button" class="btn-dl" onclick="openFolderFromItem(this.closest('.file-item'))">目录</button>
          ${playButton}
          <a class="btn-dl" href="${dlUrl}" download="${esc(f.filename)}">下载</a>
          <button type="button" class="btn-dl btn-dl-danger" onclick="deleteFileFromItem(this.closest('.file-item'))">删除</button>
        </div>
      </div>`;
        }).join('');
        const pages = Array.isArray(payload) ? 1 : (payload.pages || 1);
        const total = Array.isArray(payload) ? data.length : (payload.total || data.length);
        if (pages > 1) {
          html += `<div class="queue-summary" style="display:flex;justify-content:center;gap:10px;align-items:center;">
            <button class="btn btn-sm btn-outline" ${filesPage <= 1 ? 'disabled' : ''} onclick="loadFiles(${filesPage - 1})">上一页</button>
            <span>第 ${filesPage}/${pages} 页 · 共 ${total} 个文件</span>
            <button class="btn btn-sm btn-outline" ${filesPage >= pages ? 'disabled' : ''} onclick="loadFiles(${filesPage + 1})">下一页</button>
          </div>`;
        }
        document.getElementById('tabFiles').innerHTML = html;
      });
    }

    let ctxMenuTarget = null;
    document.addEventListener('click', () => {
      const menu = document.getElementById('contextMenu');
      if (menu) menu.style.display = 'none';
    });

    function fileDataFromItem(item) {
      return {
        folder: item?.dataset?.folder || '',
        filename: item?.dataset?.filename || '',
      };
    }

    function previewFileFromItem(item) {
      const { folder, filename } = fileDataFromItem(item);
      if (item?.dataset?.playable === '0') {
        alert(item.dataset.playReason || '文件仍未下载完整，完成后才能播放');
        return;
      }
      if (folder && filename) previewFile(folder, filename);
    }

    function openFolderFromItem(item) {
      const { folder } = fileDataFromItem(item);
      if (folder) openFolder(folder);
    }

    function deleteFileFromItem(item) {
      const { folder, filename } = fileDataFromItem(item);
      if (folder && filename) deleteFile(folder, filename);
    }

    function showFileContextMenu(e, item) {
      const { folder, filename } = fileDataFromItem(item);
      showContextMenu(e, folder, filename);
    }

    function showContextMenu(e, folder, filename) {
      e.preventDefault();
      ctxMenuTarget = { folder, filename };
      const menu = document.getElementById('contextMenu');
      menu.style.display = 'block';
      let left = e.pageX;
      let top = e.pageY;
      if (left + menu.offsetWidth > window.innerWidth) left -= menu.offsetWidth;
      if (top + menu.offsetHeight > window.innerHeight) top -= menu.offsetHeight;
      menu.style.left = left + 'px';
      menu.style.top = top + 'px';
    }

    function openFolder(folder) {
      fetch('/api/open-folder', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder })
      }).then(r => r.json()).then(res => {
        if (res.ok) return;
        if (res.path) {
          window.prompt(res.error || '服务器目录路径', res.path);
          return;
        }
        if (res.error) alert(res.error);
      }).catch(e => alert(e));
    }

    function deleteFile(folder, filename) {
      if (!confirm('确定要删除这个媒体文件吗？此操作不可恢复。\\n' + filename)) return;
      fetch('/api/delete-file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder, filename })
      }).then(r => r.json()).then(res => {
        if (res.error) alert(res.error);
        else loadFiles(filesPage);
      }).catch(e => alert(e));
    }

    function cmdRenameFile() {
      if (!ctxMenuTarget) return;
      const newName = prompt('输入新的文件名:', ctxMenuTarget.filename);
      if (newName && newName !== ctxMenuTarget.filename) {
        fetch('/api/rename-file', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder: ctxMenuTarget.folder, old_name: ctxMenuTarget.filename, new_name: newName })
        }).then(r => r.json()).then(res => {
          if (res.error) alert(res.error);
          else loadFiles();
        });
      }
    }

    function cmdDeleteFile() {
      if (!ctxMenuTarget) return;
      deleteFile(ctxMenuTarget.folder, ctxMenuTarget.filename);
    }

    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

    function showVideoListSkeleton(text = '正在加载...') {
      const container = document.getElementById('videoList');
      let html = `<div style="padding: 10px; font-size: 13px; color: var(--accent); display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
        <div class="spinner" style="width: 16px; height: 16px; border-width: 2px;"></div> ${esc(text)}
      </div>`;
      for (let i = 0; i < 5; i++) {
        html += `<div class="skeleton-item">
          <div class="skeleton-checkbox"></div>
          <div class="skeleton-thumb"></div>
          <div class="skeleton-info">
            <div class="skeleton-text-lg"></div>
            <div class="skeleton-text-md"></div>
            <div class="skeleton-text-sm"></div>
          </div>
        </div>`;
      }
      container.innerHTML = html;
    }