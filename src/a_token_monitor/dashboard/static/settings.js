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
  // 密钥脱敏：各 agent 的 hook / 代理方式、代理在线启停、上游、规则与测试。
  const redactModeLabel = { off: '关闭', hook: 'hook', proxy: '代理', both: 'hook + 代理' };
  const redactModeClass = (mode) => mode === 'off' ? 'other' : 'ok';
  const redactSourceLabel = (source) => ({ web: 'Web 配置', cli: '命令行' })[source] || source || '未知';
  const redactLines = (value) => (Array.isArray(value) ? value : []).join('\n');
  const redactSplit = (value) => String(value || '').split('\n').map((item) => item.trim()).filter(Boolean);
  const renderRedactMessages = (messages, error) => {
    const box = document.getElementById('redact-messages');
    if (!box) return;
    const items = (Array.isArray(messages) ? messages : []).filter(Boolean);
    if (error) {
      box.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error)}</span></div>`;
    } else if (items.length) {
      box.innerHTML = `<div class="usage-note">${items.map(escapeHtml).join('<br>')}</div>`;
    } else {
      box.innerHTML = '';
    }
  };
  const renderRedact = (payload) => {
    const container = document.getElementById('redact-content');
    const countLabel = document.getElementById('redact-count');
    if (!container) return;
    if (!payload || payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">该运行模式不支持在线管理脱敏</span><span class="empty-hint">请以 daemon 或 service 模式运行。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const writable = payload.writable !== false;
    const disabled = writable ? '' : ' disabled';
    const proxy = payload.proxy || {};
    const agents = Array.isArray(payload.agents) ? payload.agents : [];
    const config = payload.config || {};
    const active = agents.filter((agent) => agent.mode !== 'off').length;
    if (countLabel) countLabel.textContent = `${active} / ${agents.length} ${'个 agent 已启用'} · ${proxy.running ? '代理运行中' : '代理未运行'}`;
    const proxyPill = proxy.running
      ? '<span class="pill ok">运行中</span>'
      : proxy.enabled ? '<span class="pill danger">启动失败</span>' : '<span class="pill other">未运行</span>';
    const proxyError = proxy.error ? `<div class="cell-sub">${escapeHtml(proxy.error)}</div>` : '';
    const readOnlyNote = writable ? '' : '<div class="usage-note">Dashboard 开放到了非本机地址，这里只读；请在本机用命令行 a-token-monitor redact 修改。</div>';
    const agentRows = agents.map((agent) => {
      const options = ['off', 'hook', 'proxy', 'both'].map((mode) => `<option value="${mode}"${agent.mode === mode ? ' selected' : ''}>${escapeHtml(redactModeLabel[mode])}</option>`).join('');
      return `<tr>
        <td>${escapeHtml(agent.name)}</td>
        <td><span class="pill ${redactModeClass(agent.mode)}">${escapeHtml(redactModeLabel[agent.mode] || agent.mode)}</span></td>
        <td><span class="mono">${escapeHtml(agent.proxy ? agent.proxy_url : '—')}</span></td>
        <td><label class="field"><select data-redact-agent="${escapeHtml(agent.key)}"${disabled}>${options}</select></label></td>
      </tr>`;
    }).join('');
    const upstreamRows = (Array.isArray(payload.upstreams) ? payload.upstreams : []).map((item) => `<tr>
        <td><span class="mono">/${escapeHtml(item.name)}/</span></td>
        <td><span class="mono">${escapeHtml(item.url)}</span></td>
        <td>${item.builtin ? '<span class="pill other">内置</span>' : `<button class="btn mini danger" type="button" data-redact-upstream-remove="${escapeHtml(item.name)}"${disabled}>移除</button>`}</td>
      </tr>`).join('');
    const defaultOff = new Set(payload.default_disabled_rules || []);
    const ruleBoxes = (Array.isArray(payload.rules) ? payload.rules : []).map((rule) => {
      // 中文注释：默认关闭的规则（公网 IP、主目录用户名）复选框默认不勾选；enabled_rules
      // 里显式点名的规则覆盖默认状态。
      const enabled = (config.enabled_rules || []).includes(rule);
      const off = (config.disabled_rules || []).includes(rule);
      const on = enabled || (!off && !defaultOff.has(rule));
      const note = defaultOff.has(rule) ? ` <span class="pill other">${'默认关闭'}</span>` : '';
      return `<label class="field check"><input type="checkbox" data-redact-rule="${escapeHtml(rule)}"${on ? ' checked' : ''}${disabled}><span class="mono">${escapeHtml(rule)}</span>${note}</label>`;
    }).join('');
    const extra = (config.extra_patterns || []).map((item) => `${item.name}=${item.pattern}`).join('\n');
    const configError = payload.config_error ? `<div class="usage-note">${'现有规则配置有误'}: ${escapeHtml(payload.config_error)}</div>` : '';
    container.innerHTML = `
      ${readOnlyNote}
      <div class="account-subtitle"><span>本地代理</span>${proxyPill}</div>
      <div class="criteria-row">
        <label class="field check"><input type="checkbox" id="redact-proxy-enabled"${proxy.enabled ? ' checked' : ''}${disabled}><span>随 daemon 运行代理</span></label>
        <label class="field">端口<input type="number" min="1" max="65535" step="1" id="redact-proxy-port" value="${escapeHtml(String(proxy.port || 8790))}"${disabled}></label>
        <span class="pill other">${escapeHtml(redactSourceLabel(proxy.source))}</span>
        <div class="toolbar-actions">
          <button class="btn mini primary" type="button" id="redact-proxy-save"${disabled}>保存</button>
          <button class="btn mini warn" type="button" id="redact-proxy-reset"${disabled}>恢复命令行设置</button>
        </div>
      </div>
      ${proxyError}
      <div class="account-subtitle"><span>各 agent 的脱敏方式</span></div>
      <div class="table-wrap"><table class="tight"><thead><tr><th>Agent</th><th>当前</th><th>代理地址</th><th>切换</th></tr></thead><tbody>${agentRows}</tbody></table></div>
      <div class="usage-note">Codex 与 Grok 用订阅登录走代理，官方文档没有写明支持，请自行评估账号风险；拿不准就只用 hook。Codex 新装的 hook 要在 Codex 里运行 /hooks 审核信任后才会执行。</div>
      <div class="account-subtitle"><span>代理上游</span></div>
      <div class="table-wrap"><table class="tight"><thead><tr><th>路径前缀</th><th>上游地址</th><th>操作</th></tr></thead><tbody>${upstreamRows}</tbody></table></div>
      <div class="criteria-row">
        <label class="field">路径前缀<input type="text" id="redact-upstream-name" placeholder="例如 magpie"${disabled}></label>
        <label class="field wide">上游地址<input type="text" id="redact-upstream-url" placeholder="例如 http://127.0.0.1:3425/v1"${disabled}></label>
        <div class="toolbar-actions"><button class="btn mini primary" type="button" id="redact-upstream-add"${disabled}>添加</button></div>
      </div>
      <div class="usage-note">dsh 等没有 hook 的工具：在这里登记上游，再把它的 baseURL 改成 http://127.0.0.1:端口/路径前缀。</div>
      <div class="account-subtitle"><span>规则</span></div>
      ${configError}
      <div class="redact-rules">${ruleBoxes}</div>
      <div class="criteria-row">
        <label class="field check"><input type="checkbox" id="redact-scan-env"${config.scan_env !== false ? ' checked' : ''}${disabled}><span>匹配含 TOKEN / SECRET / API_KEY / PASSWORD 的环境变量值</span></label>
        <label class="field check"><input type="checkbox" id="redact-restore"${config.restore !== false ? ' checked' : ''}${disabled}><span>在模型回复和工具参数里还原占位符</span></label>
        <label class="field">提示词含密钥时<select id="redact-prompt-action"${disabled}><option value="block"${config.prompt_action !== 'warn' ? ' selected' : ''}>拦下</option><option value="warn"${config.prompt_action === 'warn' ? ' selected' : ''}>只提醒</option></select></label>
        <label class="field">对照表保留天数<input type="number" min="1" step="1" id="redact-ttl" value="${escapeHtml(String(config.vault_ttl_days ?? 30))}"${disabled}></label>
      </div>
      <div class="criteria-row">
        <label class="field wide">dotenv 文件（每行一个）<textarea id="redact-dotenv" placeholder="~/project/.env"${disabled}>${escapeHtml(redactLines(config.dotenv_files))}</textarea></label>
        <label class="field wide">固定密钥值（每行一个）<textarea id="redact-literals"${disabled}>${escapeHtml(redactLines(config.literal_values))}</textarea></label>
      </div>
      <div class="criteria-row">
        <label class="field wide">自定义规则（每行 名称=正则）<textarea id="redact-extra" placeholder="corp_id=CORP-[0-9]{8}"${disabled}>${escapeHtml(extra)}</textarea></label>
        <label class="field wide">白名单正则（每行一个）<textarea id="redact-allow"${disabled}>${escapeHtml(redactLines(config.allow_patterns))}</textarea></label>
      </div>
      <div class="criteria-row"><div class="toolbar-actions"><button class="btn mini primary" type="button" id="redact-config-save"${disabled}>保存规则</button></div></div>
      <div class="account-subtitle"><span>测试规则</span></div>
      <div class="criteria-row">
        <label class="field wide full">粘贴一段文本<textarea id="redact-scan-text" placeholder="例如 token=ghp_..."></textarea></label>
        <div class="toolbar-actions"><button class="btn mini" type="button" id="redact-scan-button">测试</button></div>
      </div>
      <div id="redact-scan-result"></div>`;
    container.querySelectorAll('[data-redact-agent]').forEach((select) => {
      select.addEventListener('change', () => {
        const agent = select.dataset.redactAgent || '';
        const mode = select.value;
        const name = (agents.find((item) => item.key === agent) || {}).name || agent;
        if (!window.confirm(`${name} → ${redactModeLabel[mode] || mode}\n${'确认修改脱敏方式？会改写这个 agent 的配置文件（改前自动备份）。'}`)) {
          refreshRedact();
          return;
        }
        mutateRedact({ action: 'set-agent', agent, mode });
      });
    });
    document.getElementById('redact-proxy-save')?.addEventListener('click', () => {
      const enabled = Boolean(document.getElementById('redact-proxy-enabled')?.checked);
      const port = Number(document.getElementById('redact-proxy-port')?.value || 0);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        window.alert('端口必须是 1 到 65535 的整数。');
        return;
      }
      mutateRedact({ action: 'set-proxy', enabled, port });
    });
    document.getElementById('redact-proxy-reset')?.addEventListener('click', () => {
      if (!window.confirm('确认恢复命令行的代理设置？Web 配置将被清除。')) return;
      mutateRedact({ action: 'reset-proxy', confirm: true });
    });
    document.getElementById('redact-upstream-add')?.addEventListener('click', () => {
      const name = String(document.getElementById('redact-upstream-name')?.value || '').trim();
      const url = String(document.getElementById('redact-upstream-url')?.value || '').trim();
      if (!name || !url) {
        window.alert('请填写路径前缀和上游地址。');
        return;
      }
      mutateRedact({ action: 'set-upstream', name, url });
    });
    container.querySelectorAll('[data-redact-upstream-remove]').forEach((button) => {
      button.addEventListener('click', () => {
        const name = button.dataset.redactUpstreamRemove || '';
        if (!window.confirm(`/${name}/\n${'确认删除这个上游？'}`)) return;
        mutateRedact({ action: 'set-upstream', name, url: null });
      });
    });
    document.getElementById('redact-config-save')?.addEventListener('click', () => {
      const extraPatterns = [];
      for (const line of redactSplit(document.getElementById('redact-extra')?.value)) {
        const index = line.indexOf('=');
        if (index <= 0) {
          window.alert(`${'自定义规则格式应为 名称=正则'}\n${line}`);
          return;
        }
        extraPatterns.push({ name: line.slice(0, index).trim(), pattern: line.slice(index + 1).trim() });
      }
      const boxes = [...container.querySelectorAll('[data-redact-rule]')];
      const checked = boxes.filter((box) => box.checked).map((box) => box.dataset.redactRule);
      const unchecked = boxes.filter((box) => !box.checked).map((box) => box.dataset.redactRule);
      // 中文注释：默认关闭的规则只有显式勾选才进 enabled_rules，否则从
      // disabled_rules 里去掉（保持默认关闭）；其余未勾选的就是 disabled。
      const enabledRules = checked.filter((rule) => defaultOff.has(rule));
      const disabledRules = unchecked.filter((rule) => !defaultOff.has(rule));
      mutateRedact({
        action: 'set-config',
        config: {
          disabled_rules: disabledRules,
          enabled_rules: enabledRules,
          extra_patterns: extraPatterns,
          allow_patterns: redactSplit(document.getElementById('redact-allow')?.value),
          literal_values: redactSplit(document.getElementById('redact-literals')?.value),
          dotenv_files: redactSplit(document.getElementById('redact-dotenv')?.value),
          scan_env: Boolean(document.getElementById('redact-scan-env')?.checked),
          restore: Boolean(document.getElementById('redact-restore')?.checked),
          prompt_action: document.getElementById('redact-prompt-action')?.value || 'block',
          vault_ttl_days: Number(document.getElementById('redact-ttl')?.value || 30)
        }
      });
    });
    document.getElementById('redact-scan-button')?.addEventListener('click', scanRedact);
  };
  const refreshRedact = async () => {
    const container = document.getElementById('redact-content');
    try {
      const response = await fetch('/api/redact', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderRedact(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">读取脱敏设置失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  const mutateRedact = async (body) => {
    try {
      const response = await fetch('/api/redact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      renderRedact(payload);
      renderRedactMessages(payload.messages, null);
    } catch (error) {
      renderRedactMessages(null, error.message);
      refreshRedact();
    }
  };
  const scanRedact = async () => {
    const box = document.getElementById('redact-scan-result');
    const text = String(document.getElementById('redact-scan-text')?.value || '');
    if (!box) return;
    try {
      const response = await fetch('/api/redact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'scan', text })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      const summary = payload.count ? `${'命中'} ${payload.count} · ${escapeHtml(payload.summary)}` : '没有命中';
      box.innerHTML = `<div class="usage-note">${summary}</div><pre class="redact-output">${escapeHtml(payload.redacted)}</pre>`;
    } catch (error) {
      box.innerHTML = `<div class="empty-state"><span class="empty-title">测试失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  document.getElementById('scan-dirs-refresh-button')?.addEventListener('click', () => {
    refreshScanDirs();
    refreshHistory();
    refreshRedact();
  });
__THEME_SCRIPT__
  refreshScanDirs();
  refreshHistory();
  refreshRedact();
