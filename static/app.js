(() => {
  'use strict';

  const params = new URLSearchParams(window.location.search);
  const tokenFromUrl = params.get('token');
  if (tokenFromUrl) {
    sessionStorage.setItem('zcode_exporter_token', tokenFromUrl);
    history.replaceState({}, '', window.location.pathname);
  }
  const TOKEN = sessionStorage.getItem('zcode_exporter_token') || '';

  const state = {
    token: TOKEN,
    sources: [],
    source: 'zcode',
    sessions: [],
    total: 0,
    query: '',
    sort: 'updated',
    current: null,
    messages: [],
    offset: 0,
    limit: 20,
    totalMessages: 0,
    selected: new Set(),
    job: null,
    pollTimer: null,
    outputDir: '',
    preview: {
      sessionId: null,
      offset: 0,
      limit: 30,
      total: 0,
      pages: 1,
      page: 1,
    },
  };

  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function fmtNum(value) {
    return new Intl.NumberFormat('zh-CN').format(Number(value || 0));
  }

  function fmtBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB'];
    let v = bytes;
    let i = -1;
    do {
      v /= 1024;
      i += 1;
    } while (v >= 1024 && i < units.length - 1);
    return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtTime(ms) {
    if (!ms) return '-';
    try {
      return new Date(Number(ms)).toLocaleString('zh-CN', { hour12: false });
    } catch {
      return '-';
    }
  }

  function showToast(message, type = '') {
    const el = $('toast');
    el.textContent = message;
    el.className = `toast ${type}`.trim();
    el.classList.remove('hidden');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => el.classList.add('hidden'), 4200);
  }

  function setStatus(text, kind = '') {
    $('status-text').textContent = text;
    $('status-dot').className = `dot ${kind}`.trim();
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ 'X-Auth-Token': state.token }, options.headers || {});
    const response = await fetch(path, Object.assign({}, options, { headers }));
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    if (!response.ok) {
      if (response.status === 401) {
        setStatus('认证失败', 'bad');
        throw new Error('认证失败：请使用启动时打印的带 token 的 URL 打开页面。');
      }
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data;
  }

  async function loadSources() {
    try {
      const data = await api('/api/sources');
      state.sources = data.sources || [];
      state.outputDir = data.default_output_dir || '';
      $('output-dir').value = state.outputDir;
      renderSourceTabs();
      const available = state.sources.find((s) => s.exists && !s.error);
      if (!available) {
        setStatus('未找到数据库', 'bad');
        $('session-list').innerHTML = '<div class="empty">未找到 ZCode / ZCodium 数据库。<br>可用 --zcode-db / --zcodium-db 指定。</div>';
        return;
      }
      if (!state.sources.some((s) => s.id === state.source && s.exists && !s.error)) {
        state.source = available.id;
      }
      setStatus('已连接', 'ok');
      await loadSessions();
    } catch (error) {
      setStatus('连接失败', 'bad');
      showToast(error.message, 'error');
    }
  }

  function renderSourceTabs() {
    const container = $('source-tabs');
    container.textContent = '';
    for (const source of state.sources) {
      const button = document.createElement('button');
      button.className = `source-tab${source.id === state.source ? ' active' : ''}`;
      button.textContent = `${source.label} (${source.session_count})`;
      button.disabled = !source.exists || Boolean(source.error);
      button.title = source.error || source.db_path;
      button.addEventListener('click', () => switchSource(source.id));
      container.appendChild(button);
    }
  }

  async function switchSource(sourceId) {
    if (state.source === sourceId) return;
    const source = state.sources.find((item) => item.id === sourceId);
    if (!source || !source.exists || source.error) {
      showToast(source?.error || '数据库不可用', 'error');
      return;
    }
    state.source = sourceId;
    state.selected.clear();
    state.current = null;
    state.messages = [];
    state.offset = 0;
    renderSourceTabs();
    renderSessionHeader();
    $('message-view').innerHTML = '';
    $('pager').classList.add('hidden');
    await loadSessions();
  }

  async function loadSessions() {
    $('session-list').innerHTML = '<div class="empty">正在加载…</div>';
    try {
      const query = new URLSearchParams({
        source: state.source,
        q: state.query,
        sort: state.sort,
        limit: '1000',
      });
      const data = await api(`/api/sessions?${query}`);
      state.sessions = data.sessions || [];
      state.total = data.total || 0;
      $('session-count').textContent = `${state.total} 个`;
      renderSessions();
      if (state.current) {
        const still = state.sessions.find((item) => item.id === state.current.id);
        if (still) {
          state.current = still;
          renderSessionHeader();
        }
      }
    } catch (error) {
      $('session-list').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      showToast(error.message, 'error');
    }
  }

  function renderSessions() {
    const container = $('session-list');
    container.textContent = '';
    if (state.sessions.length === 0) {
      container.innerHTML = '<div class="empty">没有匹配的会话</div>';
      return;
    }

    for (const session of state.sessions) {
      const item = document.createElement('div');
      item.className = `session-item${state.current?.id === session.id ? ' active' : ''}`;
      item.dataset.id = session.id;

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = state.selected.has(session.id);
      checkbox.addEventListener('click', (event) => {
        event.stopPropagation();
        if (checkbox.checked) state.selected.add(session.id);
        else state.selected.delete(session.id);
      });

      const content = document.createElement('div');
      const title = document.createElement('div');
      title.className = 'session-title';
      title.textContent = session.title || '(untitled)';
      const dir = document.createElement('div');
      dir.className = 'session-dir';
      dir.textContent = session.directory || '-';
      const badges = document.createElement('div');
      badges.className = 'session-badges';
      badges.innerHTML = [
        `<span class="badge cyan">${fmtNum(session.message_count)} 消息</span>`,
        `<span class="badge">${fmtNum(session.part_chars)} 字符</span>`,
        `<span class="badge">${fmtNum(session.estimated_tokens)} tokens</span>`,
        session.tool_count ? `<span class="badge green">${fmtNum(session.tool_count)} 工具</span>` : '',
        session.task_type === 'subagent_child' ? '<span class="badge amber">子代理</span>' : '',
      ].join('');
      content.append(title, dir, badges);
      item.append(checkbox, content);
      item.addEventListener('click', () => selectSession(session.id));
      container.appendChild(item);
    }
  }

  async function selectSession(sessionId) {
    state.offset = 0;
    history.replaceState({}, '', `#session=${encodeURIComponent(sessionId)}`);
    try {
      const data = await api(`/api/session?source=${encodeURIComponent(state.source)}&id=${encodeURIComponent(sessionId)}`);
      state.current = data.session;
      renderSessionHeader();
      renderSessions();
      await loadMessages(0);
    } catch (error) {
      showToast(error.message, 'error');
    }
  }

  function renderSessionHeader() {
    const container = $('session-header');
    const session = state.current;
    if (!session) {
      container.className = 'session-header empty';
      container.innerHTML = '<div>选择一个会话查看消息</div>';
      return;
    }
    container.className = 'session-header';
    const partTypes = (session.part_types || [])
      .map((item) => `<span class="badge">${escapeHtml(item.type || 'unknown')}: ${fmtNum(item.count)}</span>`)
      .join('');
    container.innerHTML = `
      <h2>${escapeHtml(session.title || '(untitled)')}</h2>
      <div class="session-sub">
        ${escapeHtml(session.id)}<br>
        工作区：${escapeHtml(session.directory || '-')} · 类型：${escapeHtml(session.task_type || '-')}<br>
        创建：${fmtTime(session.time_created)} · 更新：${fmtTime(session.time_updated)}
      </div>
      <div class="session-stats">
        <span class="badge cyan">${fmtNum(session.message_count)} 消息</span>
        <span class="badge">${fmtNum(session.part_count)} parts</span>
        <span class="badge">${fmtNum(session.part_chars)} 字符</span>
        <span class="badge">约 ${fmtNum(session.estimated_tokens)} tokens</span>
        <span class="badge green">${fmtNum(session.tool_count)} 工具调用</span>
        <span class="badge amber">${fmtNum(session.reasoning_count)} 推理段</span>
        ${partTypes}
      </div>
    `;
  }

  async function loadMessages(offset) {
    if (!state.current) return;
    const safeOffset = Math.max(0, offset);
    $('message-view').innerHTML = '<div class="empty">正在加载消息…</div>';
    try {
      const query = new URLSearchParams({
        source: state.source,
        id: state.current.id,
        offset: String(safeOffset),
        limit: String(state.limit),
        max_chars: '8000',
      });
      const data = await api(`/api/messages?${query}`);
      state.offset = safeOffset;
      state.messages = data.messages || [];
      state.totalMessages = data.total || 0;
      renderMessages();
      renderPager();
    } catch (error) {
      $('message-view').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      showToast(error.message, 'error');
    }
  }

  function renderMessages() {
    const container = $('message-view');
    container.textContent = '';
    if (state.messages.length === 0) {
      container.innerHTML = '<div class="empty">没有消息</div>';
      return;
    }
    for (const message of state.messages) {
      container.appendChild(renderMessage(message));
    }
    container.scrollTop = 0;
  }

  function renderMessage(message) {
    const article = document.createElement('article');
    article.className = `message ${message.role || 'unknown'}`;

    const head = document.createElement('div');
    head.className = 'message-head';
    head.innerHTML = `
      <span class="seq">#${String(message.sequence ?? '').padStart(6, '0')}</span>
      <span class="role ${escapeHtml(message.role || 'unknown')}">${escapeHtml(message.role || 'unknown')}</span>
      <span class="time">${fmtTime(message.time_created)}</span>
    `;
    article.appendChild(head);

    const meta = [];
    if (message.modelID) meta.push(`${message.providerID || ''}/${message.modelID}`);
    if (message.mode) meta.push(`mode=${message.mode}`);
    if (message.agent) meta.push(`agent=${message.agent}`);
    if (message.tokens) {
      meta.push(`tokens=${message.tokens.input || 0}/${message.tokens.output || 0}/${message.tokens.reasoning || 0}`);
    }
    if (message.finish) meta.push(`finish=${message.finish}`);
    if (meta.length) {
      const metaLine = document.createElement('div');
      metaLine.className = 'meta-line';
      metaLine.textContent = meta.join(' · ');
      article.appendChild(metaLine);
    }

    if (message.summary) {
      const box = document.createElement('div');
      box.className = 'summary-box';
      const title = document.createElement('div');
      title.className = 'summary-title';
      title.textContent = message.summary.title || 'Summary';
      const pre = document.createElement('pre');
      pre.textContent = message.summary.body || '';
      box.append(title, pre);
      article.appendChild(box);
    }

    for (const part of message.parts || []) {
      article.appendChild(renderPart(part));
    }
    return article;
  }

  function renderPart(part) {
    const wrapper = document.createElement('div');
    wrapper.className = `part ${part.type || 'unknown'}`;

    if (part.type === 'text') {
      const pre = document.createElement('pre');
      pre.className = 'text';
      pre.textContent = part.text || '';
      wrapper.appendChild(pre);
      addLoadFull(wrapper, part, 'text');
      return wrapper;
    }

    if (part.type === 'reasoning') {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `推理过程 · ${fmtNum(part.full_length || 0)} 字符`;
      const pre = document.createElement('pre');
      pre.textContent = part.text || '';
      details.append(summary, pre);
      wrapper.appendChild(details);
      addLoadFull(details, part, 'text');
      return wrapper;
    }

    if (part.type === 'tool') {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.innerHTML = `工具：${escapeHtml(part.tool || 'unknown')} <span class="badge ${part.status === 'completed' ? 'green' : part.status === 'error' ? 'red' : ''}">${escapeHtml(part.status || '-')}</span>`;
      details.appendChild(summary);

      if (part.input) {
        const label = document.createElement('div');
        label.className = 'label';
        label.textContent = '输入';
        const pre = document.createElement('pre');
        pre.textContent = part.input;
        details.append(label, pre);
      }
      if (part.output) {
        const label = document.createElement('div');
        label.className = 'label';
        label.textContent = '输出';
        const pre = document.createElement('pre');
        pre.textContent = part.output;
        pre.dataset.field = 'output';
        details.append(label, pre);
      }
      if (part.error) {
        const label = document.createElement('div');
        label.className = 'label';
        label.textContent = '错误';
        const pre = document.createElement('pre');
        pre.textContent = part.error;
        details.append(label, pre);
      }
      if (part.metadata_keys && part.metadata_keys.length) {
        const label = document.createElement('div');
        label.className = 'label';
        label.textContent = `元数据键：${part.metadata_keys.join(', ')}`;
        details.appendChild(label);
      }
      wrapper.appendChild(details);
      addLoadFull(details, part, 'output');
      return wrapper;
    }

    if (part.type === 'file') {
      const card = document.createElement('div');
      card.className = 'file-card';
      const name = document.createElement('div');
      name.className = 'file-name';
      name.textContent = part.filename || '(file)';
      const meta = document.createElement('div');
      meta.className = 'file-meta';
      meta.textContent = `${part.mime || '-'} · ${part.sizeBytes ? fmtBytes(part.sizeBytes) : ''} ${part.url || ''}`;
      card.append(name, meta);
      if (part.preview) {
        const pre = document.createElement('pre');
        pre.textContent = part.preview;
        card.appendChild(pre);
      }
      wrapper.appendChild(card);
      addLoadFull(wrapper, part, 'preview');
      return wrapper;
    }

    if (part.type === 'timeline') {
      wrapper.className = 'part marker';
      wrapper.textContent = `[模型切换] ${part.from || '-'} → ${part.to || '-'}`;
      return wrapper;
    }

    if (part.type === 'compaction') {
      wrapper.className = 'part marker';
      wrapper.textContent = `[压缩] trigger=${part.trigger || '-'} reason=${part.compactReason || '-'} pre_tokens=${part.preCompactTokenCount || '-'} status=${part.status || '-'}`;
      return wrapper;
    }

    if (part.type === 'step-finish') {
      wrapper.className = 'part marker';
      wrapper.textContent = `[step] finish reason=${part.reason || '-'} tokens=${JSON.stringify(part.tokens || {})}`;
      return wrapper;
    }

    if (part.type === 'step-start') {
      wrapper.className = 'part marker';
      wrapper.textContent = '[step] start';
      return wrapper;
    }

    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = `part: ${part.type || 'unknown'}`;
    const pre = document.createElement('pre');
    pre.textContent = part.raw || JSON.stringify(part, null, 2);
    details.append(summary, pre);
    wrapper.appendChild(details);
    return wrapper;
  }

  function addLoadFull(container, part, field) {
    if (!part.truncated) return;
    const button = document.createElement('button');
    button.className = 'link';
    button.textContent = '加载完整内容';
    button.addEventListener('click', () => loadFullPart(button, part.id, field));
    container.appendChild(button);
  }

  async function loadFullPart(button, partId, field) {
    if (!partId) return;
    button.disabled = true;
    button.textContent = '加载中…';
    try {
      const data = await api(`/api/part?source=${encodeURIComponent(state.source)}&id=${encodeURIComponent(partId)}&max_chars=2000000`);
      const part = data.part;
      const value = field === 'summary' ? (part.text || '') : (part[field] || part.text || part.raw || '');
      const scope = button.parentElement;
      const pre = field === 'output'
        ? (scope.querySelector('pre[data-field="output"]') || scope.querySelector('pre'))
        : scope.querySelector('pre');
      if (pre) pre.textContent = value;
      button.remove();
    } catch (error) {
      button.disabled = false;
      button.textContent = '加载完整内容';
      showToast(error.message, 'error');
    }
  }

  function renderPager() {
    const pager = $('pager');
    pager.classList.remove('hidden');
    const pages = Math.max(1, Math.ceil(state.totalMessages / state.limit));
    const page = Math.floor(state.offset / state.limit) + 1;
    $('page-info').textContent = `第 ${page} / ${pages} 页 · 共 ${fmtNum(state.totalMessages)} 条消息`;
    $('prev-page').disabled = state.offset <= 0;
    $('next-page').disabled = state.offset + state.limit >= state.totalMessages;
  }

  function collectExportOptions() {
    return {
      source: state.source,
      format: $('export-format').value,
      output_dir: $('output-dir').value.trim(),
      split_mb: Number($('split-mb').value || 0),
      utf8_bom: $('utf8-bom').checked,
      include_reasoning: $('opt-reasoning').checked,
      include_tool_input: $('opt-tool-input').checked,
      include_tool_output: $('opt-tool-output').checked,
      include_tool_metadata: $('opt-tool-metadata').checked,
      include_file_preview: $('opt-file-preview').checked,
      include_step_events: $('opt-step-events').checked,
      include_timeline: $('opt-timeline').checked,
      include_compaction: $('opt-compaction').checked,
      include_summary: $('opt-summary').checked,
      include_message_meta: $('opt-message-meta').checked,
      include_binary: $('opt-binary').checked,
      user_label: $('user-label').value.trim() || 'USER',
      agent_label: $('agent-label').value.trim() || 'ASSISTANT',
    };
  }

  async function startExport(scope) {
    if (!state.token) {
      showToast('缺少访问 token，请用启动时打印的 URL 打开。', 'error');
      return;
    }
    let ids = [];
    if (scope === 'current') {
      if (!state.current) return showToast('请先选择一个会话', 'error');
      ids = [state.current.id];
    } else if (scope === 'selected') {
      ids = Array.from(state.selected);
      if (!ids.length) return showToast('请先勾选会话', 'error');
    } else {
      ids = state.sessions.map((item) => item.id);
      if (!ids.length) return showToast('当前来源没有会话', 'error');
    }

    const payload = { ...collectExportOptions(), session_ids: ids };

    try {
      const data = await api('/api/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      state.job = data.job;
      renderJob(state.job);
      showToast('导出任务已启动', 'ok');
      pollJob(state.job.id);
    } catch (error) {
      showToast(error.message, 'error');
    }
  }

  async function previewExport(offset = 0, showLoading = true) {
    if (!state.current) return showToast('请先选择一个会话', 'error');
    const button = $('preview-export');
    const original = button.textContent;
    if (showLoading) {
      button.disabled = true;
      button.textContent = '预览中…';
    }
    try {
      const payload = {
        ...collectExportOptions(),
        session_id: state.current.id,
        preview_messages: state.preview.limit,
        preview_chars: 200000,
        preview_offset: Math.max(0, Number(offset) || 0),
      };
      const data = await api('/api/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      state.preview = {
        sessionId: state.current.id,
        offset: data.offset_messages,
        limit: data.limit_messages,
        total: data.total_messages,
        pages: data.pages,
        page: data.page,
      };
      const formatLabel = data.format === 'raw' ? '原始 JSONL' : '可读 TXT';
      $('preview-meta').textContent =
        `格式：${formatLabel} · 第 ${data.page} / ${data.pages} 页 · 本页 ${fmtNum(data.shown_messages)} 条 / 共 ${fmtNum(data.total_messages)} 条 · ${fmtBytes(data.bytes)}` +
        (data.truncated ? ' · 本页预览已截断' : '');
      $('preview-page-info').textContent = `第 ${data.page} / ${data.pages} 页`;
      $('preview-prev').disabled = data.page <= 1;
      $('preview-next').disabled = data.page >= data.pages;
      $('preview-jump').max = String(data.pages);
      $('preview-text').textContent = data.text || '(本页没有消息)';
      $('preview-modal').classList.remove('hidden');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      if (showLoading) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  function previewPrev() {
    previewExport(Math.max(0, state.preview.offset - state.preview.limit), false);
  }

  function previewNext() {
    previewExport(state.preview.offset + state.preview.limit, false);
  }

  function previewJump() {
    const page = Number($('preview-jump').value);
    if (!Number.isFinite(page) || page < 1 || page > state.preview.pages) {
      showToast(`请输入 1 - ${state.preview.pages} 之间的页码`, 'error');
      return;
    }
    previewExport((page - 1) * state.preview.limit, false);
  }

  function closePreview() {
    $('preview-modal').classList.add('hidden');
  }

  async function copyPreview() {
    const text = $('preview-text').textContent || '';
    try {
      await navigator.clipboard.writeText(text);
      showToast('预览内容已复制', 'ok');
    } catch {
      showToast('复制失败，请手动选择文本', 'error');
    }
  }

  function confirmPreview() {
    closePreview();
    startExport('current');
  }

  function pollJob(jobId) {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      try {
        const data = await api(`/api/job?id=${encodeURIComponent(jobId)}`);
        state.job = data.job;
        renderJob(state.job);
        if (['done', 'error', 'cancelled'].includes(state.job.status)) {
          clearInterval(state.pollTimer);
          if (state.job.status === 'done') showToast('导出完成', 'ok');
          if (state.job.status === 'error') showToast(`导出失败：${state.job.error}`, 'error');
        }
      } catch (error) {
        clearInterval(state.pollTimer);
        showToast(error.message, 'error');
      }
    }, 700);
  }

  function renderJob(job) {
    if (!job) return;
    $('job-card').classList.remove('hidden');
    const statusMap = {
      queued: '排队中',
      running: '导出中',
      done: '已完成',
      error: '失败',
      cancelled: '已取消',
    };
    $('job-status').textContent = statusMap[job.status] || job.status;
    $('job-id').textContent = job.id;

    const progress = job.progress || {};
    const total = Number(progress.message_total || 0);
    const done = Number(progress.message_done || 0);
    const value = job.status === 'done' ? 100 : total > 0 ? Math.min(100, (done / total) * 100) : 0;
    $('job-progress').value = value;

    const parts = [
      `会话 ${progress.session_index || 0}/${progress.session_total || 0}`,
      `消息 ${fmtNum(done)}/${fmtNum(total)}`,
      `已写入 ${fmtBytes(progress.bytes_written || 0)}`,
    ];
    if (progress.current_file) parts.push(`文件：${progress.current_file}`);
    if (progress.current_session) parts.push(`当前：${progress.current_session}`);
    if (job.error) parts.push(`错误：${job.error}`);
    $('job-meta').textContent = parts.join(' · ');

    $('job-cancel').classList.toggle('hidden', !['queued', 'running'].includes(job.status));
    $('job-download').classList.toggle('hidden', !(job.status === 'done' && job.download_path));
    $('job-open').classList.toggle('hidden', !job.output_dir);
  }

  async function cancelJob() {
    if (!state.job) return;
    try {
      await api('/api/job/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: state.job.id }),
      });
      showToast('已请求取消', 'ok');
    } catch (error) {
      showToast(error.message, 'error');
    }
  }

  function downloadJob() {
    if (!state.job?.download_path) return;
    const url = `/api/job/download?id=${encodeURIComponent(state.job.id)}&token=${encodeURIComponent(state.token)}`;
    window.location.href = url;
  }

  async function openJobDir() {
    if (!state.job?.output_dir) return;
    try {
      await api('/api/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: state.job.output_dir }),
      });
    } catch (error) {
      showToast(error.message, 'error');
    }
  }

  function bindEvents() {
    $('session-search').addEventListener('input', () => {
      clearTimeout(bindEvents.searchTimer);
      bindEvents.searchTimer = setTimeout(() => {
        state.query = $('session-search').value.trim();
        loadSessions();
      }, 280);
    });

    $('session-sort').addEventListener('change', () => {
      state.sort = $('session-sort').value;
      loadSessions();
    });

    $('refresh-btn').addEventListener('click', () => loadSources());

    $('prev-page').addEventListener('click', () => loadMessages(Math.max(0, state.offset - state.limit)));
    $('next-page').addEventListener('click', () => loadMessages(state.offset + state.limit));
    $('jump-btn').addEventListener('click', () => {
      const value = Number($('jump-seq').value);
      if (Number.isFinite(value) && value >= 0) loadMessages(value);
    });
    $('jump-seq').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') $('jump-btn').click();
    });

    $('export-current').addEventListener('click', () => startExport('current'));
    $('export-selected').addEventListener('click', () => startExport('selected'));
    $('export-all').addEventListener('click', () => startExport('all'));
    $('preview-export').addEventListener('click', () => previewExport(0, true));
    $('reset-labels').addEventListener('click', () => {
      $('user-label').value = 'USER';
      $('agent-label').value = 'ASSISTANT';
    });
    $('preview-prev').addEventListener('click', previewPrev);
    $('preview-next').addEventListener('click', previewNext);
    $('preview-jump-btn').addEventListener('click', previewJump);
    $('preview-jump').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') previewJump();
    });
    $('preview-close').addEventListener('click', closePreview);
    $('preview-copy').addEventListener('click', copyPreview);
    $('preview-confirm').addEventListener('click', confirmPreview);
    $('preview-modal').addEventListener('click', (event) => {
      if (event.target === $('preview-modal')) closePreview();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('preview-modal').classList.contains('hidden')) {
        closePreview();
      }
    });
    $('job-cancel').addEventListener('click', cancelJob);
    $('job-download').addEventListener('click', downloadJob);
    $('job-open').addEventListener('click', openJobDir);
  }

  async function init() {
    if (!state.token) {
      setStatus('缺少 token', 'bad');
      $('session-list').innerHTML = '<div class="empty">缺少访问 token。<br>请关闭窗口，使用启动脚本打印的完整 URL 重新打开。</div>';
      return;
    }
    bindEvents();
    await loadSources();

    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const sessionId = hash.get('session');
    if (sessionId && state.sessions.some((item) => item.id === sessionId)) {
      await selectSession(sessionId);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
