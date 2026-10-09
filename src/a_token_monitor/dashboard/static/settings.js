  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  const formatTime = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    return new Date(Number(seconds) * 1000).toLocaleString();
  };
  const formatDataSize = (value) => {
    const amount = Math.max(0, Number(value || 0));
    if (amount < 1024) return `${Math.round(amount)} B`;
    if (amount < 1024 * 1024) return `${(amount / 1024).toFixed(amount >= 10240 ? 0 : 1)} KiB`;
    if (amount < 1024 * 1024 * 1024) return `${(amount / 1024 / 1024).toFixed(amount >= 10 * 1024 * 1024 ? 1 : 2)} MiB`;
    return `${(amount / 1024 / 1024 / 1024).toFixed(2)} GiB`;
  };
  // 扫描目录：展示每个 provider 的生效目录，支持在线添加、移除和恢复默认。
  const scanDirsSourceLabel = (source) => ({ web: 'Web 配置', cli: '命令行', auto: '自动探测' })[source] || source || '未知';
  const scanDirsSourceClass = (source) => source === 'web' ? 'ok' : source === 'cli' ? 'warn' : 'other';
  const renderScanDirs = (payload) => {
    const container = document.getElementById('scan-dirs-content');
    const countLabel = document.getElementById('scan-dirs-count');
    if (!container) return;
    if (!payload || payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">该运行模式不支持在线管理扫描目录</span><span class="empty-hint">请以 daemon 或 service 模式运行。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const providers = Array.isArray(payload.providers) ? payload.providers : [];
    const dirCount = providers.reduce((total, provider) => total + (Array.isArray(provider.directories) ? provider.directories.length : 0), 0);
    if (countLabel) countLabel.textContent = `${providers.length} 个来源 · ${dirCount} 个目录`;
    const cards = providers.map((provider) => {
      const directories = Array.isArray(provider.directories) ? provider.directories : [];
      const rows = directories.map((directory) => {
        const statusPill = !directory.ok
          ? '<span class="pill danger">不可读或不存在</span>'
          : directory.structure_ok
            ? '<span class="pill ok">正常</span>'
            : '<span class="pill warn">结构存疑</span>';
        const notes = [...(directory.errors || []), ...(directory.warnings || [])];
        const note = notes.length ? `<div class="cell-sub">${notes.map(escapeHtml).join('；')}</div>` : '';
        return `<tr>
          <td><span class="mono">${escapeHtml(directory.path)}</span>${note}</td>
          <td>${statusPill}</td>
          <td><button class="btn mini danger" type="button" data-scan-dirs-remove="${escapeHtml(provider.key)}" data-scan-dirs-path="${escapeHtml(directory.path)}">移除</button></td>
        </tr>`;
      }).join('');
      const table = directories.length
        ? `<div class="table-wrap"><table class="tight"><thead><tr><th>目录</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<div class="empty-state"><span class="empty-title">当前没有生效的扫描目录</span></div>';
      const disabled = provider.enabled ? '' : ' <span class="pill other">已禁用</span>';
      const reset = Array.isArray(provider.override_dirs)
        ? `<button class="btn mini warn" type="button" data-scan-dirs-reset="${escapeHtml(provider.key)}">恢复默认</button>`
        : '';
      return `<article class="mini-card">
        <div class="mini-card-head">
          <div><div class="mini-card-title">${escapeHtml(provider.name)}${disabled}</div><div class="mini-card-path">默认 ${escapeHtml(provider.default_dir)} · 也可用 ${escapeHtml(provider.cli_option)} 指定</div></div>
          <span class="pill ${scanDirsSourceClass(provider.source)}">${escapeHtml(scanDirsSourceLabel(provider.source))}</span>
        </div>
        ${table}
        <div class="criteria-row">
          <label class="field wide">新增目录<input type="text" data-scan-dirs-input="${escapeHtml(provider.key)}" placeholder="例如 ~/.codex-work"></label>
          <div class="toolbar-actions">
            <button class="btn mini primary" type="button" data-scan-dirs-add="${escapeHtml(provider.key)}">添加</button>
            ${reset}
          </div>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = cards ? `<div class="card-grid">${cards}</div>` : '<div class="empty-state"><span class="empty-title">没有已知的 provider</span></div>';
    container.querySelectorAll('[data-scan-dirs-add]').forEach((button) => {
      button.addEventListener('click', () => {
        const provider = button.dataset.scanDirsAdd || '';
        const input = container.querySelector(`[data-scan-dirs-input="${provider}"]`);
        const path = input ? String(input.value || '').trim() : '';
        if (!path) {
          window.alert('请先填写要添加的目录路径。');
          return;
        }
        mutateScanDirs({ action: 'add', provider, path });
      });
    });
    container.querySelectorAll('[data-scan-dirs-remove]').forEach((button) => {
      button.addEventListener('click', () => {
        const path = button.dataset.scanDirsPath || '';
        if (!window.confirm(`确认从扫描目录中移除？\n${path}`)) return;
        mutateScanDirs({ action: 'remove', provider: button.dataset.scanDirsRemove || '', path, confirm: true });
      });
    });
    container.querySelectorAll('[data-scan-dirs-reset]').forEach((button) => {
      button.addEventListener('click', () => {
        if (!window.confirm('确认恢复该 provider 的默认扫描目录？Web 配置将被清除。')) return;
        mutateScanDirs({ action: 'reset', provider: button.dataset.scanDirsReset || '', confirm: true });
      });
    });
  };
  const refreshScanDirs = async () => {
    const container = document.getElementById('scan-dirs-content');
    const button = document.getElementById('scan-dirs-refresh-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch('/api/scan-dirs', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderScanDirs(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">读取扫描目录失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  const mutateScanDirs = async (body) => {
    const container = document.getElementById('scan-dirs-content');
    try {
      const response = await fetch('/api/scan-dirs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      renderScanDirs(payload);
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  // 历史数据：保留期配置、索引占用、清理预览与手动清理。
  const historySourceLabel = (source) => ({ web: 'Web 配置', cli: '配置默认' })[source] || source || '未知';
  const historySourceClass = (source) => source === 'web' ? 'ok' : 'other';
  const historyKindLabel = { usage: '用量历史', sessions: '会话历史', alerts: '告警历史' };
  const historyDeletedTotal = (deleted) => ['usage', 'sessions', 'alerts']
    .reduce((sum, key) => sum + Number((deleted || {})[key] || 0), 0);
  const renderHistory = (payload) => {
    const container = document.getElementById('history-content');
    const countLabel = document.getElementById('history-count');
    if (!container) return;
    if (!payload || payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">该运行模式不支持在线管理历史数据</span><span class="empty-hint">请以 daemon 或 service 模式运行。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const retention = payload.retention || {};
    const days = payload.retention_days || {};
    const dbs = Array.isArray(payload.dbs) ? payload.dbs : [];
    const totalBytes = dbs.reduce((sum, item) => sum + Number(item.bytes || 0), 0);
    if (countLabel) countLabel.textContent = `索引共 ${formatDataSize(totalBytes)}`;
    const retentionField = (key, label) => {
      const entry = retention[key] || {};
      const value = entry.value ?? days[key] ?? '';
      const badge = entry.source
        ? ` <span class="pill ${historySourceClass(entry.source)}">${escapeHtml(historySourceLabel(entry.source))}</span>`
        : '';
      return `<label class="field wide">${escapeHtml(label)}${badge}<input type="number" min="1" max="3650" step="1" data-history-days="${key}" value="${escapeHtml(String(value))}"></label>`;
    };
    const dbRows = dbs.map((item) => `<tr><td>${escapeHtml(item.label)}</td><td>${escapeHtml(formatDataSize(item.bytes))}</td></tr>`).join('');
    const lastCleanup = payload.last_cleanup;
    const lastCleanupLine = lastCleanup
      ? `上次清理：${escapeHtml(formatTime(lastCleanup.observed_at))} · 删除 ${historyDeletedTotal(lastCleanup.deleted)} 行 · 释放 ${escapeHtml(formatDataSize(lastCleanup.freed_bytes || 0))}${Array.isArray(lastCleanup.errors) && lastCleanup.errors.length ? ' · 存在部分失败' : ''}`
      : '尚未执行过清理';
    container.innerHTML = `
      <div class="account-subtitle"><span>保留天数</span></div>
      <div class="criteria-row">
        ${retentionField('usage_days', '用量历史保留天数')}
        ${retentionField('session_days', '会话历史保留天数')}
        ${retentionField('alert_days', '告警历史保留天数')}
        <div class="toolbar-actions">
          <button class="btn mini primary" type="button" id="history-save-button">保存</button>
          <button class="btn mini warn" type="button" id="history-reset-button">恢复默认</button>
        </div>
      </div>
      <div class="account-subtitle"><span>索引与状态数据占用</span></div>
      <div class="table-wrap"><table class="tight"><thead><tr><th>数据</th><th>占用</th></tr></thead><tbody>${dbRows || '<tr><td colspan="2">暂无数据</td></tr>'}</tbody></table></div>
      <div class="criteria-row">
        <div class="toolbar-actions">
          <button class="btn mini" type="button" id="history-preview-button">预览将清理的数据</button>
          <button class="btn mini danger" type="button" id="history-cleanup-button">立即清理</button>
        </div>
      </div>
      <div class="usage-note" id="history-last-cleanup">${lastCleanupLine}</div>`;
    document.getElementById('history-save-button')?.addEventListener('click', () => {
      const body = { action: 'set-retention' };
      [['usage_days', '[data-history-days="usage_days"]'], ['session_days', '[data-history-days="session_days"]'], ['alert_days', '[data-history-days="alert_days"]']].forEach(([key, selector]) => {
        const input = container.querySelector(selector);
        const raw = input ? String(input.value || '').trim() : '';
        const value = Number(raw);
        if (raw !== '' && Number.isFinite(value)) body[key] = value;
      });
      if (body.usage_days === undefined && body.session_days === undefined && body.alert_days === undefined) {
        window.alert('请填写要保存的保留天数。');
        return;
      }
      mutateHistory(body);
    });
    document.getElementById('history-reset-button')?.addEventListener('click', () => {
      if (!window.confirm('确认恢复默认保留天数？Web 配置将被清除。')) return;
      mutateHistory({ action: 'reset-retention', confirm: true });
    });
    document.getElementById('history-preview-button')?.addEventListener('click', refreshHistoryPreview);
    document.getElementById('history-cleanup-button')?.addEventListener('click', () => {
      if (!window.confirm('确认立即清理过期历史数据？删除不可撤销，活动会话和额度恢复记录会保留。')) return;
      mutateHistory({ action: 'cleanup', confirm: true });
    });
  };
  const renderHistoryPreview = (preview) => {
    const container = document.getElementById('history-preview-content');
    if (!container) return;
    const kinds = preview && Array.isArray(preview.kinds) ? preview.kinds : [];
    if (!kinds.length) {
      container.innerHTML = '';
      return;
    }
    const rows = kinds.map((kind) => `<tr>
      <td>${escapeHtml(historyKindLabel[kind.kind] || kind.kind)}</td>
      <td>${escapeHtml(formatTime(kind.cutoff))}</td>
      <td>${Number(kind.rows_to_delete || 0).toLocaleString('zh-CN')} / ${Number(kind.total_rows || 0).toLocaleString('zh-CN')}</td>
      <td>${escapeHtml(formatDataSize(kind.db_bytes))}</td>
      <td>${escapeHtml(formatDataSize(kind.estimated_free_bytes))}（预计）</td>
    </tr>`).join('');
    container.innerHTML = `
      <div class="account-subtitle"><span>将清理的数据（预计）</span></div>
      <div class="table-wrap"><table class="tight"><thead><tr><th>类别</th><th>截止时间</th><th>将删行数 / 总行数</th><th>当前占用</th><th>预计释放</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="usage-note">预计释放合计 ${escapeHtml(formatDataSize(preview.estimated_free_bytes || 0))}，为按行数比例的估算值；实际释放以清理结果为准。</div>`;
  };
  const renderHistoryCleanupResult = (result, errorMessage) => {
    const container = document.getElementById('history-result');
    if (!container) return;
    const deleted = result && result.deleted && typeof result.deleted === 'object' ? result.deleted : {};
    const breakdown = Object.keys(deleted)
      .map((key) => `${escapeHtml(historyKindLabel[key] || key)} ${Number(deleted[key] || 0)} 行`)
      .join(' · ');
    const errors = result && Array.isArray(result.errors) ? result.errors : [];
    const title = errorMessage ? '清理部分失败' : '清理完成';
    container.innerHTML = `<div class="empty-state"><span class="empty-title">${title}：删除 ${historyDeletedTotal(deleted)} 行，释放 ${escapeHtml(formatDataSize(result ? result.freed_bytes || 0 : 0))}</span>${breakdown ? `<span class="empty-hint">${breakdown}</span>` : ''}${errorMessage ? `<span class="empty-hint">${escapeHtml(errorMessage)}</span>` : ''}${errors.length ? `<span class="empty-hint">${errors.map(escapeHtml).join('；')}</span>` : ''}</div>`;
  };
  const refreshHistory = async () => {
    const container = document.getElementById('history-content');
    try {
      const response = await fetch('/api/history', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderHistory(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">读取历史数据状态失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  const refreshHistoryPreview = async () => {
    const container = document.getElementById('history-preview-content');
    try {
      const response = await fetch('/api/history?preview=1', { cache: 'no-store' });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      renderHistoryPreview(payload.preview);
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">预览失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  const mutateHistory = async (body) => {
    const resultBox = document.getElementById('history-result');
    try {
      const response = await fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error(payload.message || `HTTP ${response.status}`);
        error.payload = payload;
        throw error;
      }
      await refreshHistory();
      if (body.action === 'cleanup') renderHistoryCleanupResult(payload.result, null);
      if (body.action !== 'cleanup' && resultBox) resultBox.innerHTML = '';
    } catch (error) {
      if (body.action === 'cleanup') {
        renderHistoryCleanupResult(error.payload ? error.payload.result : null, error.message);
      } else if (resultBox) {
        resultBox.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
      }
    }
  };
  document.getElementById('scan-dirs-refresh-button')?.addEventListener('click', () => {
    refreshScanDirs();
    refreshHistory();
  });
__THEME_SCRIPT__
  refreshScanDirs();
  refreshHistory();
