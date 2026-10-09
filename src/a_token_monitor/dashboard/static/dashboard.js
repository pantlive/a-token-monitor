  const statusNames = {
    running: '运行中', limit_blocked: '额度受限',
    waiting_for_approval: '等待批准', discovered: '已发现',
    completed: '已完成', failed: '失败', orphaned: '已结束', unknown: '未知'
  };
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  const formatTime = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    return new Date(Number(seconds) * 1000).toLocaleString();
  };
  const formatRelativeTime = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const delta = Math.max(0, Date.now() / 1000 - Number(seconds));
    if (delta < 60) return `${Math.floor(delta)} 秒前`;
    if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
    if (delta < 86400) return `${(delta / 3600).toFixed(1)} 小时前`;
    return `${(delta / 86400).toFixed(1)} 天前`;
  };
  const formatDay = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const moment = new Date(Number(seconds) * 1000);
    const pad = (value) => String(value).padStart(2, '0');
    return `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`;
  };
  const formatReset = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const remaining = Math.round(Number(seconds) - Date.now() / 1000);
    const suffix = remaining > 0 ? `（${Math.ceil(remaining / 60)} 分钟后）` : '（已到时间）';
    return `${formatTime(seconds)} ${suffix}`;
  };
  const formatPercent = (value) => value === null || value === undefined ? '未知' : `${Number(value).toFixed(1)}%`;
  const formatNumber = (value) => Number(value || 0).toLocaleString('zh-CN');
  const formatTokens = (value) => {
    const amount = Number(value || 0);
    if (amount >= 1e9) return `${(amount / 1e9).toFixed(2)}B`;
    if (amount >= 1e6) return `${(amount / 1e6).toFixed(2)}M`;
    if (amount >= 1e3) return `${(amount / 1e3).toFixed(1)}K`;
    return String(amount);
  };
  const formatBytes = (value) => `${(Number(value || 0) / 1024 / 1024).toFixed(1)} MiB`;
  const formatDataSize = (value) => {
    const amount = Math.max(0, Number(value || 0));
    if (amount < 1024) return `${Math.round(amount)} B`;
    if (amount < 1024 * 1024) return `${(amount / 1024).toFixed(amount >= 10240 ? 0 : 1)} KiB`;
    if (amount < 1024 * 1024 * 1024) return `${(amount / 1024 / 1024).toFixed(amount >= 10 * 1024 * 1024 ? 1 : 2)} MiB`;
    return `${(amount / 1024 / 1024 / 1024).toFixed(2)} GiB`;
  };
  const formatCredits = (value) => value === null || value === undefined ? '不可反推' : `${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 4 })} credits`;
  const formatUsd = (value) => value === null || value === undefined ? '未知' : `$${Number(value).toFixed(6)}`;
  // 中文注释：汇总卡用同一套金额格式——≥$1 保留 2 位、小额保留 4 位，
  // 避免「$7.117772」这种六位小数和旁边「$113.97」两种风格并排。
  const formatUsdSummary = (value) => {
    if (value === null || value === undefined) return '未知';
    const number = Number(value);
    return `$${number.toFixed(Math.abs(number) >= 1 ? 2 : 4)}`;
  };
  const formatUsdCompact = (value) => value === null || value === undefined ? '未计价' : `$${Number(value).toFixed(2)}`;
  // 中文注释：头像显示订阅缩写，而不是账号 ID 的前两位（"3" / "5" 这种看不出含义）。
  // 规则：产品与套餐各取单词首字母，最多 3 个字符——Codex · Plus → CP、
  // Codex · Pro Lite → CPL、Grok · SuperGrok → GS、Command Code · GOAT → CCG、Kimi → K。
  const subscriptionInitials = (value) => {
    const words = String(value || '').split(/[^0-9A-Za-z\u4e00-\u9fff]+/).filter(Boolean);
    if (!words.length) return '?';
    const letters = [words[0][0], ...words.slice(1).map((word) => word[0])];
    return letters.join('').slice(0, 3).toUpperCase() || '?';
  };
  // 订阅类型：Codex 的 planType 是小写（plus / prolite），Grok 是 SuperGrok，
  // Command Code 是 GOAT；未收录的值只做首字母大写，形如模型名的原样保留。
  const PLAN_NAMES = {
    plus: 'Plus', pro: 'Pro', prolite: 'Pro Lite', probusiness: 'Pro Business',
    team: 'Team', business: 'Business', enterprise: 'Enterprise', free: 'Free',
    edu: 'Edu', max: 'Max', supergrok: 'SuperGrok',
    supergrokheavy: 'SuperGrok Heavy', goat: 'GOAT'
  };
  const planLabel = (value) => {
    const raw = String(value || '').trim();
    if (!raw) return '';
    if (raw.includes('/')) return raw;
    const mapped = PLAN_NAMES[raw.toLowerCase().replace(/[\s_-]+/g, '')];
    if (mapped) return mapped;
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  };
  const statusClass = (status) => ['running', 'limit_blocked', 'waiting_for_approval', 'failed', 'orphaned'].includes(status) ? status : 'other';
  // 额度窗口统一成固定行：5 小时 / 周 / 月永远都在、顺序固定，缺的周期显示「不适用」，
  // 这样不同订阅的卡片同构、同名行横向对齐；完全没有额度窗口的订阅只给一行说明。
  const QUOTA_PERIOD_LABELS = { five_hours: '5 小时', day: '日', week: '周', month: '月', other: '其它' };
  const QUOTA_PERIOD_ORDER = ['five_hours', 'day', 'week', 'month', 'other'];
  const QUOTA_FIXED_PERIODS = ['five_hours', 'week', 'month'];
  // 兜底：万一后端没带 period，就按和后端一致的规则现场判一次（时长优先、名字兜底）。
  const quotaPeriod = (window) => {
    if (window && window.period) return window.period;
    const minutes = Number(window && window.window_minutes);
    if (Number.isFinite(minutes) && minutes > 0) {
      if (minutes <= 360) return 'five_hours';
      if (minutes <= 2160) return 'day';
      if (minutes <= 14400) return 'week';
      return 'month';
    }
    const name = `${(window && window.name) || ''} ${(window && window.limit_id) || ''}`.toLowerCase();
    if (name.includes('hour') || name.includes('5h')) return 'five_hours';
    if (name.includes('day')) return 'day';
    if (name.includes('week')) return 'week';
    if (name.includes('month')) return 'month';
    return 'other';
  };
  const quotaPeriodLabel = (period) => QUOTA_PERIOD_LABELS[period] || QUOTA_PERIOD_LABELS.other;
  const quotaDuration = (window) => {
    if (window && window.duration_label) return window.duration_label;
    const minutes = Number(window && window.window_minutes);
    if (!Number.isFinite(minutes) || minutes <= 0) return '周期未知';
    if (minutes < 60) return `${Math.round(minutes)} 分钟`;
    if (minutes < 1440) return `${Math.round(minutes / 60)} 小时`;
    if (minutes < 14400) return `${Math.round(minutes / 1440)} 天`;
    return `${Math.round(minutes / 43200)} 个月`;
  };
  const quotaPercentText = (window) => (window.used_percent === null || window.used_percent === undefined
    ? '待采集'
    : formatPercent(window.used_percent));
  const quotaWindowTitle = (window) => `${window.limit_id}/${window.name} · ${quotaPercentText(window)} · ${quotaDuration(window)}`;
  const renderQuotaRow = (period, label, windows) => {
    if (!windows.length) {
      return `<div class="quota-row absent">
        <span class="quota-row-label">${escapeHtml(label)}</span>
        <div class="bar quota-bar"><span style="width:0"></span></div>
        <span class="quota-row-percent absent">不适用</span>
        <span class="quota-row-meta">该订阅没有${escapeHtml(label)}额度</span>
      </div>`;
    }
    // 同一周期有多条窗口（例如 Kimi 的 limit_month_total / limit_month_code）时取最紧的
    // 一条做主行，其余用角标提示，保证「一行一个周期」。
    const ordered = [...windows].sort((left, right) => (
      Number(right.used_percent === null || right.used_percent === undefined ? -1 : right.used_percent)
      - Number(left.used_percent === null || left.used_percent === undefined ? -1 : left.used_percent)
    ));
    const main = ordered[0];
    const rest = ordered.slice(1);
    const unknown = main.used_percent === null || main.used_percent === undefined;
    const percent = unknown ? 0 : Math.max(0, Math.min(100, Number(main.used_percent)));
    const level = main.is_exhausted ? 'danger' : percent >= 80 ? 'warn' : '';
    const stateText = main.is_exhausted ? '已耗尽' : unknown ? '未返回已用比例' : '可用';
    const badge = rest.length
      ? ` <span class="quota-badge" title="${escapeHtml(ordered.map((window) => quotaWindowTitle(window)).join('\n'))}">另有 ${rest.length} 条</span>`
      : '';
    const meta = unknown
      ? `${escapeHtml(stateText)} · ${escapeHtml(quotaDuration(main))}`
      : `重置 ${escapeHtml(formatReset(main.resets_at))} · ${escapeHtml(quotaDuration(main))}`;
    return `<div class="quota-row ${level || 'ok'}">
      <span class="quota-row-label">${escapeHtml(label)}</span>
      <div class="bar quota-bar"><span class="${level}" style="width:${percent}%"></span></div>
      <span class="quota-row-percent ${level}">${escapeHtml(quotaPercentText(main))}</span>
      <span class="quota-row-meta">${meta}${badge}</span>
    </div>`;
  };
  const renderQuotaRows = (quotas) => {
    const windows = (Array.isArray(quotas) ? quotas : []).flatMap((quota) => quota.windows || []);
    if (!windows.length) {
      return '<div class="quota-note">该订阅不提供额度窗口，这里只统计用量</div>';
    }
    const buckets = new Map();
    windows.forEach((window) => {
      const period = quotaPeriod(window);
      if (!buckets.has(period)) buckets.set(period, []);
      buckets.get(period).push(window);
    });
    // 固定三段永远渲染；额外的周期（如「日」或判不出的「其它」）按规范顺序插进去，
    // 宁可多一行也不丢信息。
    const rows = QUOTA_PERIOD_ORDER
      .filter((period) => QUOTA_FIXED_PERIODS.includes(period) || buckets.has(period))
      .map((period) => renderQuotaRow(period, quotaPeriodLabel(period), buckets.get(period) || []));
    return `<div class="quota-rows">${rows.join('')}</div>`;
  };
  // 活动会话整块默认折叠，点击按钮再展开；折叠状态在 5 秒自动刷新之间保持。
  const expandedSessionTables = new Set();
  const renderSessionTable = (sessions, accountKey) => {
    if (!sessions || sessions.length === 0) {
      return '<div class="empty-state">没有活动会话</div>';
    }
    const renderRow = (session) => {
      const active = session.active !== false;
      const status = escapeHtml(statusNames[session.status] || session.status || '未知');
      const cssStatus = active ? statusClass(session.status) : 'other';
      const pids = session.pids && session.pids.length ? session.pids.join(', ') : '无';
      const detail = session.last_error || '—';
      const usage = session.usage || {};
      const turns = usage.turns === undefined || usage.turns === null ? null : Number(usage.turns);
      const advice = usage.reminder
        ? `<div><span class="pill warn">建议开新会话</span></div>`
        : '';
      const usageCell = turns === null
        ? '<span class="muted">—</span>'
        : `${escapeHtml(String(turns))} 轮<div class="muted">上下文 ${escapeHtml(formatDataSize(usage.context_tokens || 0))} · 累计 ${escapeHtml(formatDataSize(usage.total_tokens || 0))}</div>${advice}`;
      const archive = session.archive || {};
      const archiveCell = !session.jsonl_path
        ? '<span class="muted">无 JSONL</span>'
        : archive.eligible
          ? `<button class="btn mini" type="button" data-archive-session="${escapeHtml(session.jsonl_path)}" title="把这个会话压缩归档（tar.gz，可恢复）">归档此会话</button>`
          : `<span class="muted" title="${escapeHtml(archive.reason || '当前不可归档')}">不可归档</span>`;
      return `<tr>
        <td><div class="session-id">${escapeHtml(session.session_id || session.thread_id)}</div><div class="muted">${escapeHtml(session.source)}</div></td>
        <td><span class="pill ${cssStatus}">${status}</span>${active ? '' : '<div class="cell-sub">已结束</div>'}</td>
        <td>${escapeHtml(pids)}<div class="muted">${session.process_backed ? '已绑定 JSONL' : '无进程证据'}</div></td>
        <td class="usage-number">${usageCell}</td>
        <td class="cwd">${escapeHtml(session.cwd || '未知')}</td>
        <td class="event">${escapeHtml(session.last_event_type || '未知')}<div class="muted">${escapeHtml(formatTime(session.last_event_at))}</div></td>
        <td class="error-text">${escapeHtml(detail)}</td>
        <td>${archiveCell}</td>
      </tr>`;
    };
    const expanded = expandedSessionTables.has(accountKey);
    return `<div class="session-collapsible"${expanded ? '' : ' style="display:none"'}><div class="table-wrap session-table"><table>
      <thead><tr><th>会话</th><th>状态</th><th>进程</th><th>轮数 / 上下文</th><th>工作目录</th><th>最近事件</th><th>说明</th><th>归档</th></tr></thead>
      <tbody>${sessions.map(renderRow).join('')}</tbody>
    </table></div></div>
    <button class="session-toggle" type="button" data-session-toggle="${escapeHtml(accountKey)}" aria-expanded="${expanded}">${expanded ? '收起会话列表' : `展开 ${sessions.length} 个会话（含最近结束）`}</button>`;
  };
  let selectedUsagePeriod = 'today';
  let selectedUsageModel = '';
  let selectedUsageProject = '';
  // 用量统计维度：账号 / 模型 / 项目共用同一份叶子聚合，只切换分组方式。
  const usageDimensions = [['account', '按账号'], ['model', '按模型'], ['project', '按项目']];
  const usageDimensionLabels = { account: '按账号', model: '按模型', project: '按项目' };
  let selectedUsageDimension = 'account';
  let selectedUsageAccount = '';
  // 中文注释：一个账号可能用了几十个模型，全部铺开会把行撑得非常高；
  // 默认只列用量最大的前几个，展开状态记录在 Set 里（重新渲染后保持）。
  const USAGE_MODEL_PREVIEW = 3;
  const expandedUsageModelLists = new Set();
  let latestState = null;
  let latestUsageState = null;
  let latestHealthState = null;
  let usagePollTimer = 0;
  let usagePollSuspended = false;
  let latestInsightsState = null;
  let insightsPollTimer = 0;
  let insightsPollSuspended = false;
  let insightsPeriodDays = '';
  let mainRefreshTimer = 0;
  // 低频分区默认折叠：状态记在 localStorage，展开时才加载明细。
  const COLLAPSED_SECTIONS_KEY = 'a-token-monitor-collapsed-sections';
  const DEFAULT_COLLAPSED = ['alert-history', 'usage-search', 'housekeeping'];
  const readCollapsedSections = () => {
    try {
      const stored = window.localStorage.getItem(COLLAPSED_SECTIONS_KEY);
      if (stored === null) return new Set(DEFAULT_COLLAPSED);
      const parsed = JSON.parse(stored);
      return new Set(Array.isArray(parsed) ? parsed : DEFAULT_COLLAPSED);
    } catch (error) {
      return new Set(DEFAULT_COLLAPSED);
    }
  };
  const collapsedSections = readCollapsedSections();
  const loadedSections = new Set();
  const sectionLoaders = {
    'alert-history': () => refreshAlertHistory(),
    'usage-search': () => refreshUsageSearch(),
    'housekeeping': () => refreshHousekeeping()
  };
  const persistCollapsedSections = () => {
    try {
      window.localStorage.setItem(COLLAPSED_SECTIONS_KEY, JSON.stringify([...collapsedSections]));
    } catch (error) {
      /* 隐私模式下忽略存储失败 */
    }
  };
  const applySectionState = (id) => {
    const section = document.getElementById(id);
    const toggle = document.querySelector(`[data-section-toggle="${id}"]`);
    if (!section) return;
    const collapsed = collapsedSections.has(id);
    section.classList.toggle('is-collapsed', collapsed);
    if (toggle) toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  };
  const setSectionCollapsed = (id, collapsed) => {
    if (collapsed) {
      collapsedSections.add(id);
    } else {
      collapsedSections.delete(id);
    }
    applySectionState(id);
    persistCollapsedSections();
    if (!collapsed && sectionLoaders[id] && !loadedSections.has(id)) {
      loadedSections.add(id);
      sectionLoaders[id]();
    }
  };
  const ensureSectionLoaded = (id) => {
    if (!collapsedSections.has(id) && sectionLoaders[id] && !loadedSections.has(id)) {
      loadedSections.add(id);
      sectionLoaders[id]();
    }
  };
  const navLinks = [...document.querySelectorAll('[data-nav-target]')];
  const updateActiveNav = () => {
    const current = ['housekeeping', 'usage-search', 'alert-history', 'insights', 'usage', 'accounts', 'traffic', 'overview'].find((id) => {
      const target = document.getElementById(id);
      return target && target.getBoundingClientRect().top <= 120;
    }) || 'overview';
    navLinks.forEach((link) => link.classList.toggle('active', link.dataset.navTarget === current));
  };
  window.addEventListener('scroll', updateActiveNav, { passive: true });
  updateActiveNav();
  const usagePeriodOrder = ['today', 'seven_days', 'month', 'year'];
  const usageTokenFields = [
    'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
    'output_tokens', 'reasoning_output_tokens', 'total_tokens'
  ];
  const distinctSorted = (values) => [...new Set(values.filter((value) => value))].sort();
  const sumNullable = (items, key) => {
    const values = items.map((item) => item[key]).filter((value) => value !== null && value !== undefined);
    return values.length ? values.reduce((sum, value) => sum + Number(value), 0) : null;
  };
  const sumUsageModels = (models) => {
    const summary = {};
    usageTokenFields.forEach((field) => {
      summary[field] = models.reduce((sum, model) => sum + Number(model[field] || 0), 0);
    });
    summary.models = models;
    summary.estimated_credits = sumNullable(models, 'estimated_credits');
    summary.estimated_cost_usd = sumNullable(models, 'estimated_cost_usd');
    summary.api_equivalent_cost_usd = sumNullable(models, 'api_equivalent_cost_usd');
    summary.cache_savings_usd = sumNullable(models, 'cache_savings_usd');
    summary.subscription_cost_usd = null;
    summary.unpriced_models = distinctSorted(models.filter((model) => (
      !model.api_pricing_known
    )).map((model) => model.model));
    return summary;
  };
  // 把当前筛选条件（账号 / 模型 / 项目）应用到账号列表上，并把模型级条目
  // 摊平成叶子：按账号、按模型、按项目三种维度都从同一份叶子聚合，
  // 保证任何维度的合计都等于账号合计，不会出现口径不一致。
  const usageLeafModels = (account) => {
    const leaves = [];
    const projects = Array.isArray(account.projects) && account.projects.length
      ? account.projects
      : [{ project: '', models: account.models || [] }];
    projects.forEach((project) => {
      if (selectedUsageProject && (project.project || '') !== selectedUsageProject) return;
      (project.models || []).forEach((model) => {
        if (selectedUsageModel && model.model !== selectedUsageModel) return;
        leaves.push({ model, project: project.project || '' });
      });
    });
    return leaves;
  };
  const usageAccountName = (account) => account.account || account.account_id || '未知账号';
  const usageAccountMatches = (account) => (
    !selectedUsageAccount || usageAccountName(account) === selectedUsageAccount
  );
  const usageGroups = (accounts, dimension) => {
    const groups = new Map();
    (accounts || []).forEach((account) => {
      const name = usageAccountName(account);
      usageLeafModels(account).forEach((leaf) => {
        const key = dimension === 'model'
          ? (leaf.model.model || '未知模型')
          : dimension === 'project'
            ? (leaf.project || '未知项目')
            : name;
        let group = groups.get(key);
        if (!group) {
          group = {
            key,
            models: [],
            accounts: new Map(),
            profiles: new Set(),
            products: new Set(),
            accountId: account.account_id || '',
          };
          groups.set(key, group);
        }
        if (!group.accountId && account.account_id) group.accountId = account.account_id;
        group.models.push(leaf.model);
        group.accounts.set(name, (group.accounts.get(name) || 0) + Number(leaf.model.total_tokens || 0));
        (account.profiles || []).forEach((profile) => group.profiles.add(profile));
        (account.products || []).forEach((product) => group.products.add(product));
      });
    });
    return [...groups.values()].map((group) => ({
      ...group,
      stats: sumUsageModels(group.models),
      accountList: [...group.accounts.entries()].sort((a, b) => b[1] - a[1]).map((item) => item[0]),
    }));
  };
  const usageTagLine = (values, fallback) => {
    const items = [...values].filter((item) => item);
    return items.length ? items.join(' · ') : fallback;
  };
  const usageAccountSubLabel = (group) => usageTagLine(
    group.products,
    usageTagLine(group.profiles, '未知 profile'),
  );
  // 中文注释：产品展示名与「账号与额度」卡片共用同一套映射（Codex / Grok / Kimi /
  // DeepSeek Harness / Command Code / Claude Code），避免两处叫法不一致。
  const accountProductLabel = (account) => {
    const profiles = (account && account.profiles) || [];
    const matches = (id) => account && (account.product === id || profiles.some((profile) => (typeof profile === 'string' ? profile : profile.name) === id));
    return matches('kimi') ? 'Kimi'
      : matches('grok') ? 'Grok'
        : matches('dsh') ? 'DeepSeek Harness'
          : matches('command-code') ? 'Command Code'
            : matches('claude') ? 'Claude Code'
              : 'Codex';
  };
  // 账号 → 产品 · 套餐：套餐取自 /api/state（与卡片同源、同一套展示名归一化）。
  const usageAccountSubscriptions = () => {
    const map = new Map();
    const accounts = (latestState && Array.isArray(latestState.accounts)) ? latestState.accounts : [];
    const quotas = (latestState && Array.isArray(latestState.quotas)) ? latestState.quotas : [];
    accounts.forEach((account) => {
      const plans = distinctSorted(
        quotas
          .filter((quota) => (quota.account || 'codex') === account.name)
          .map((quota) => planLabel(quota.plan_type))
          .concat([planLabel(account.plan_type)]),
      );
      map.set(account.name, { product: accountProductLabel(account), plan: plans.join(' / ') });
    });
    return map;
  };
  // 账号成本排行的标签：`Codex · Plus/账号 ID`，与卡片标题同源。
  const usageAccountRankingLabel = (group, subscriptions) => {
    const meta = (subscriptions || usageAccountSubscriptions()).get(group.key) || {};
    const product = meta.product || usageTagLine(group.products, '未知产品');
    const subscription = meta.plan ? `${product} · ${meta.plan}` : product;
    const accountId = group.accountId || group.key;
    const suffix = accountId && accountId.toLowerCase() !== product.toLowerCase()
      ? `<span class="muted">/${escapeHtml(accountId)}</span>`
      : '';
    return `${escapeHtml(subscription)}${suffix}`;
  };
  const usageContributorLabel = (group) => {
    const names = group.accountList || [];
    if (!names.length) return '';
    return names.length === 1 ? names[0] : `${names[0]} 等 ${names.length} 个账号`;
  };
  const usageSortByCost = (groups) => [...groups].sort((a, b) => {
    const left = Number(a.stats.estimated_cost_usd || 0);
    const right = Number(b.stats.estimated_cost_usd || 0);
    if (left !== right) return right - left;
    return Number(b.stats.total_tokens || 0) - Number(a.stats.total_tokens || 0);
  });
  const renderUsageRanking = (title, hint, groups, labelOf) => {
    const ranked = usageSortByCost(groups).filter((group) => Number(group.stats.estimated_cost_usd || 0) > 0);
    if (!ranked.length) return '';
    const total = ranked.reduce((sum, group) => sum + Number(group.stats.estimated_cost_usd || 0), 0);
    const max = Number(ranked[0].stats.estimated_cost_usd) || 1;
    const top = ranked.slice(0, 5);
    const items = top.map((group) => {
      const cost = Number(group.stats.estimated_cost_usd || 0);
      const share = total > 0 ? (cost / total) * 100 : 0;
      const width = Math.max(3, Math.round((cost / max) * 100));
      return `<div class="top-project"><div class="top-project-row"><span class="top-project-label">${labelOf(group)}</span><span class="top-project-value">${escapeHtml(formatUsdCompact(cost))} · ${share.toFixed(1)}%</span></div><div class="bar"><span style="width:${width}%"></span></div></div>`;
    }).join('');
    return `<div class="usage-top-projects"><div class="usage-trend-head"><span>${escapeHtml(title)} Top ${top.length}</span><span class="muted">${escapeHtml(hint)}</span></div>${items}</div>`;
  };
  const usageFilterControls = (periods) => {
    const models = distinctSorted(periods.flatMap((period) => (period.accounts || []).flatMap((account) => (account.models || []).map((model) => model.model))));
    const projects = distinctSorted(periods.flatMap((period) => (period.accounts || []).flatMap((account) => (account.projects || []).map((project) => project.project))));
    const accounts = distinctSorted(periods.flatMap((period) => (period.accounts || []).map((account) => account.account || account.account_id)));
    if (!models.includes(selectedUsageModel)) selectedUsageModel = '';
    if (!projects.includes(selectedUsageProject)) selectedUsageProject = '';
    if (!accounts.includes(selectedUsageAccount)) selectedUsageAccount = '';
    const modelOptions = [`<option value="">全部模型</option>`, ...models.map((model) => `<option value="${escapeHtml(model)}"${model === selectedUsageModel ? ' selected' : ''}>${escapeHtml(model)}</option>`)].join('');
    const projectOptions = [`<option value="">全部项目</option>`, ...projects.map((project) => `<option value="${escapeHtml(project)}"${project === selectedUsageProject ? ' selected' : ''}>${escapeHtml(project)}</option>`)].join('');
    const accountOptions = [`<option value="">全部账号</option>`, ...accounts.map((account) => `<option value="${escapeHtml(account)}"${account === selectedUsageAccount ? ' selected' : ''}>${escapeHtml(account)}</option>`)].join('');
    return `<div class="usage-filters">
      <label class="usage-filter">账号<select id="usage-account-filter">${accountOptions}</select></label>
      <label class="usage-filter">模型<select id="usage-model-filter">${modelOptions}</select></label>
      <label class="usage-filter">项目 / 工作目录<select id="usage-project-filter">${projectOptions}</select></label>
    </div>`;
  };
  const updateSpendFromUsage = (usage) => {
    if (!usage || (usage.indexing && usage.indexing.complete === false)) {
      document.getElementById('spend-count').textContent = '索引中';
      return;
    }
    const periods = usage && Array.isArray(usage.periods) ? usage.periods : [];
    const today = periods.find((period) => period.key === 'today');
    const todayCost = today ? sumNullable(today.accounts || [], 'estimated_cost_usd') : null;
    document.getElementById('spend-count').textContent = formatUsdCompact(todayCost);
  };
  const monthCost = (periods) => {
    const month = (periods || []).find((period) => period.key === 'month');
    return month ? sumNullable(month.accounts || [], 'estimated_cost_usd') : null;
  };
  const budgetUsd = () => {
    if (!latestState || latestState.budget_usd === null || latestState.budget_usd === undefined) return null;
    const value = Number(latestState.budget_usd);
    return value > 0 ? value : null;
  };
  // 配额耗尽 / 超 90% 与预算超 80% 的告警横幅，随 5 秒状态轮询刷新。
  // 折叠状态下也能看到关键结论，不必展开分区。
  const renderSectionSummaries = (state) => {
    const alertHistory = (state && state.alert_history) || {};
    const alertSummary = document.getElementById('alert-history-summary');
    if (alertSummary) {
      alertSummary.textContent = alertHistory.available
        ? `未读 ${formatNumber(alertHistory.unread)} 条 · 共 ${formatNumber(alertHistory.total)} 条 · 最近 ${formatTime(alertHistory.last_alert_at)}`
        : '告警历史暂不可用（监控进程未启用落盘）';
    }
    const usageIndex = (state && state.usage_index) || {};
    const usageSummary = document.getElementById('usage-search-summary');
    if (usageSummary) {
      usageSummary.textContent = usageIndex.available
        ? `索引 ${formatNumber(usageIndex.records)} 条记录 / ${formatNumber(usageIndex.sessions)} 个会话 · ${formatNumber(usageIndex.models)} 个模型 · ${formatNumber(usageIndex.accounts)} 个账号 · ${formatDay(usageIndex.first_at)} ~ ${formatDay(usageIndex.last_at)}`
        : '用量索引还是空的，先让 daemon 完成一次索引';
    }
    const housekeeping = (state && state.housekeeping) || {};
    const housekeepingSummary = document.getElementById('housekeeping-summary');
    if (housekeepingSummary) {
      const totals = housekeeping.totals || {};
      const preview = housekeeping.preview || {};
      housekeepingSummary.textContent = housekeeping.available
        ? `合计 ${formatDataSize(totals.bytes || 0)} · 会话文件 ${formatDataSize(totals.session_bytes || 0)}（${formatNumber(totals.session_files || 0)} 个）· ${formatNumber(preview.count || 0)} 个可归档 · ${formatNumber((housekeeping.reminders || []).length)} 条磁盘提醒`
        : '磁盘统计不可用（监控进程未启动扫描）';
    }
  };
  // 顶部关注区：把额度、流量、预算、磁盘和长会话提醒收敛成可跳转的紧凑行。
  const ALERT_VISIBLE_ROWS = 3;
  let expandedAlerts = false;
  const renderAlerts = () => {
    const container = document.getElementById('alert-list');
    if (!container) return;
    const alerts = [];
    const quotas = latestState && Array.isArray(latestState.quotas) ? latestState.quotas : [];
    quotas.forEach((quota) => {
      const account = quota.account || 'codex';
      (quota.windows || []).forEach((window) => {
        const label = `${account} · ${window.limit_id}/${window.name}`;
        const percent = Number(window.used_percent);
        if (window.is_exhausted) {
          alerts.push({
            level: 'danger',
            title: `${label} 额度已耗尽`,
            detail: `重置时间 ${formatReset(window.resets_at)}`,
            href: '#accounts',
            link: '查看额度'
          });
        } else if (window.used_percent !== null && window.used_percent !== undefined && !Number.isNaN(percent) && percent >= 90) {
          alerts.push({
            level: 'warn',
            title: `${label} 已使用 ${percent.toFixed(1)}%`,
            detail: '接近额度上限，注意剩余用量',
            href: '#accounts',
            link: '查看额度'
          });
        }
      });
    });
    const traffic = (latestState && latestState.traffic) || {};
    (traffic.alerts || []).forEach((alert) => {
      if (!alert || !alert.message) return;
      alerts.push({
        level: alert.level === 'danger' ? 'danger' : 'warn',
        title: alert.message,
        detail: `${alert.kind === 'burst' ? '突发窗口' : '累计窗口'} · 对端 ${alert.remote || '未知'}`,
        href: '#alert-history',
        link: '查看告警历史'
      });
    });
    const budget = budgetUsd();
    if (budget !== null && latestUsageState && latestUsageState.usage) {
      const spent = monthCost(latestUsageState.usage.periods);
      if (spent !== null) {
        const ratio = (spent / budget) * 100;
        if (ratio >= 100) {
          alerts.push({ level: 'danger', title: `本月 API 等价金额 ${formatUsdCompact(spent)} 已超预算`, detail: `月预算 ${formatUsdCompact(budget)}（${ratio.toFixed(0)}%）`, href: '#usage', link: '查看用量' });
        } else if (ratio >= 80) {
          alerts.push({ level: 'warn', title: `本月 API 等价金额 ${formatUsdCompact(spent)} 已达预算 ${ratio.toFixed(0)}%`, detail: `月预算 ${formatUsdCompact(budget)}`, href: '#usage', link: '查看用量' });
        }
      }
    }
    const housekeeping = (latestState && latestState.housekeeping) || {};
    (housekeeping.reminders || []).forEach((reminder) => {
      alerts.push({
        level: reminder.level === 'danger' ? 'danger' : 'warn',
        title: reminder.title || reminder.message,
        detail: reminder.detail || '',
        href: '#housekeeping',
        link: '查看磁盘与会话管理'
      });
    });
    const sessionAdvice = (latestState && latestState.session_advice) || {};
    (sessionAdvice.sessions || []).forEach((reminder) => {
      alerts.push({
        level: 'warn',
        title: reminder.title || reminder.message,
        detail: reminder.detail || '',
        href: '#accounts',
        link: '查看会话'
      });
    });
    if (alerts.length === 0) {
      container.innerHTML = '';
      return;
    }
    const visible = expandedAlerts ? alerts : alerts.slice(0, ALERT_VISIBLE_ROWS);
    const rows = visible.map((alert) => `<div class="alert-row ${alert.level}" role="status">
        <span class="alert-accent" aria-hidden="true"></span>
        <div class="alert-body">
          <div class="alert-title">${escapeHtml(alert.title)}</div>
          ${alert.detail ? `<div class="alert-detail">${escapeHtml(alert.detail)}</div>` : ''}
        </div>
        ${alert.href ? `<a class="alert-link" href="${escapeHtml(alert.href)}">${escapeHtml(alert.link || '查看')}</a>` : ''}
      </div>`).join('');
    const rest = alerts.length - visible.length;
    const toggle = alerts.length > ALERT_VISIBLE_ROWS
      ? `<button id="alert-toggle-button" class="btn mini" type="button">${rest > 0 ? `展开其余 ${rest} 条` : '收起'}</button>`
      : '';
    container.innerHTML = `<div class="alert-head"><span>需要关注</span><span class="section-count">${alerts.length} 条</span><span class="alert-head-spacer"></span>${toggle}</div>${rows}`;
    document.getElementById('alert-toggle-button')?.addEventListener('click', () => {
      expandedAlerts = !expandedAlerts;
      renderAlerts();
    });
  };
  const renderTrend = (daily) => {
    if (!Array.isArray(daily) || !daily.length) return '';
    const costs = daily.map((day) => Number(day && day.estimated_cost_usd) || 0);
    const maxCost = costs.reduce((max, value) => Math.max(max, value), 0);
    const total = costs.reduce((sum, value) => sum + value, 0);
    const todayKey = new Date().toLocaleDateString('sv-SE');
    const bars = daily.map((day, index) => {
      const cost = costs[index];
      const height = maxCost > 0 ? Math.max(3, Math.round((cost / maxCost) * 100)) : 3;
      const classes = ['trend-bar'];
      if (day.date === todayKey) classes.push('today');
      if (cost <= 0) classes.push('empty');
      const unpriced = day.has_unpriced ? '（含未定价模型）' : '';
      const title = `${day.date} · ${formatUsdCompact(day.estimated_cost_usd)} · ${formatNumber(day.total_tokens)} tokens${unpriced}`;
      return `<div class="${classes.join(' ')}" style="height:${height}%" title="${escapeHtml(title)}"></div>`;
    }).join('');
    return `<div class="usage-trend"><div class="usage-trend-head"><span>近 ${daily.length} 天 API 等价金额趋势</span><span class="muted">合计 ${escapeHtml(formatUsdCompact(total))}</span></div><div class="trend-chart">${bars}</div></div>`;
  };
  // Kimi booster 钱包返回的是真实扣费（分），与本地按公开 API 价的估算并排展示。
  const renderKimiReconciliation = (periods) => {
    if (!latestState || !Array.isArray(latestState.quotas)) return '';
    const month = (periods || []).find((period) => period.key === 'month');
    const monthAccounts = month && Array.isArray(month.accounts) ? month.accounts : [];
    const lines = latestState.quotas.filter((quota) => quota.product === 'kimi').map((quota) => {
      const meta = quota.metadata || {};
      if (meta.booster_monthly_used_cents === undefined) return '';
      const symbol = meta.booster_currency === 'CNY' ? '¥' : '$';
      const formatMoney = (cents) => `${symbol}${(Number(cents || 0) / 100).toFixed(2)}`;
      const limitEnabled = meta.booster_monthly_charge_limit_enabled === 'true' && Number(meta.booster_monthly_charge_limit_cents || 0) > 0;
      const limitText = limitEnabled ? `（限额 ${formatMoney(meta.booster_monthly_charge_limit_cents)}）` : '';
      const accountUsage = monthAccounts.find((account) => account.account === quota.account);
      const estimate = accountUsage && accountUsage.estimated_cost_usd !== null && accountUsage.estimated_cost_usd !== undefined
        ? ` · 本地 API 等价估算 ${formatUsdCompact(accountUsage.estimated_cost_usd)}`
        : '';
      return `<div class="usage-note">Kimi 对账 · ${escapeHtml(quota.account || 'kimi')}：本月 booster 真实扣费 ${escapeHtml(formatMoney(meta.booster_monthly_used_cents))}${escapeHtml(limitText)}，余额 ${escapeHtml(formatMoney(meta.booster_balance_cents))}${escapeHtml(estimate)}</div>`;
    }).filter((line) => line);
    return lines.join('');
  };
  // Command Code 订阅返回真实名额扣费（美元），与本地 API 等价估算并排展示。
  const renderCommandCodeReconciliation = (periods) => {
    if (!latestState || !Array.isArray(latestState.quotas)) return '';
    const month = (periods || []).find((period) => period.key === 'month');
    const monthAccounts = month && Array.isArray(month.accounts) ? month.accounts : [];
    const lines = latestState.quotas.filter((quota) => quota.product === 'command-code').map((quota) => {
      const meta = quota.metadata || {};
      if (meta.period_credits_spent === undefined) return '';
      const parts = [`本月订阅扣费 $${meta.period_credits_spent}`, `剩余名额 $${meta.monthly_credits_remaining || '0.00'}`];
      if (meta.period_requests !== undefined) parts.push(`${meta.period_requests} 次请求`);
      if (meta.days_left !== undefined) parts.push(`${meta.days_left} 天后重置`);
      const accountUsage = monthAccounts.find((account) => account.account === quota.account);
      const estimate = accountUsage && accountUsage.estimated_cost_usd !== null && accountUsage.estimated_cost_usd !== undefined
        ? ` · 本地 API 等价估算 ${formatUsdCompact(accountUsage.estimated_cost_usd)}`
        : '';
      return `<div class="usage-note">Command Code 对账 · ${escapeHtml(quota.account || 'command-code')}：${escapeHtml(parts.join('，'))}${escapeHtml(estimate)}</div>`;
    }).filter((line) => line);
    return lines.join('');
  };
  const renderUsage = (state) => {
    const container = document.getElementById('usage-content');
    const usage = state.usage || {};
    const periods = Array.isArray(usage.periods) ? usage.periods : [];
    const indexing = usage.indexing || {};
    const duplicateFiles = Number(indexing.deduplicated_files || 0);
    const duplicateNote = duplicateFiles > 0
      ? `已忽略 ${duplicateFiles} 份重复会话（${formatBytes(indexing.deduplicated_bytes)}）。`
      : '';
    if (indexing.complete === false) {
      const percent = Number(indexing.percent || 0).toFixed(2);
      const remainingBytes = Math.max(0, Number(indexing.total_bytes || 0) - Number(indexing.indexed_bytes || 0));
      const bytesPerSec = Number(indexing.bytes_per_sec || 0);
      const etaSeconds = bytesPerSec > 0 ? Math.ceil(remainingBytes / bytesPerSec) : 0;
      const eta = etaSeconds > 0 ? `预计剩余约 ${etaSeconds} 秒。` : '正在后台继续索引。';
      container.innerHTML = `<div class="empty-state"><strong>历史索引进行中（${escapeHtml(percent)}%）</strong><br>已确认 ${escapeHtml(formatBytes(indexing.indexed_bytes))} / ${escapeHtml(formatBytes(indexing.total_bytes))}。完成前不显示 token 和金额。待处理 ${escapeHtml(indexing.pending_files ?? 0)} 个文件。${escapeHtml(duplicateNote)}${escapeHtml(eta)} 页面会自动更新，无需连点刷新。</div>`;
      return;
    }
    if (periods.length === 0) {
      container.innerHTML = '<div class="empty-state">暂无可读取的 session JSONL 用量记录</div>';
      return;
    }
    const period = periods.find((item) => item.key === selectedUsagePeriod) || periods[0];
    selectedUsagePeriod = period.key;
    const tabs = usagePeriodOrder.map((key) => {
      const item = periods.find((candidate) => candidate.key === key);
      if (!item) return '';
      const selected = item.key === selectedUsagePeriod ? ' selected' : '';
      const pressed = item.key === selectedUsagePeriod ? 'true' : 'false';
      return `<button class="usage-tab${selected}" type="button" aria-pressed="${pressed}" data-usage-period="${escapeHtml(item.key)}">${escapeHtml(item.label)}</button>`;
    }).join('');
    const accounts = Array.isArray(period.accounts) ? period.accounts : [];
    const filterControls = usageFilterControls(periods);
    const note = usage.pricing && usage.pricing.note
      ? usage.pricing.note
      : '金额为估算值，不代表 Plus 实际扣款。';
    const indexingNote = duplicateNote ? `${duplicateNote} ` : '';
    const filteredAccounts = accounts.filter(usageAccountMatches);
    const dimensionTabs = usageDimensions.map(([key, label]) => {
      const selected = key === selectedUsageDimension;
      return `<button class="usage-tab${selected ? ' selected' : ''}" type="button" aria-pressed="${selected ? 'true' : 'false'}" data-usage-dimension="${escapeHtml(key)}">${escapeHtml(label)}</button>`;
    }).join('');
    // 三种维度共用同一份叶子聚合结果：合计始终等于账号合计，切换维度不会变化。
    const groups = usageSortByCost(usageGroups(filteredAccounts, selectedUsageDimension));
    const statsList = groups.map((group) => group.stats);
    const totalTokens = statsList.reduce((sum, stats) => sum + Number(stats.total_tokens || 0), 0);
    const totalCredits = sumNullable(statsList, 'estimated_credits');
    const totalUsd = sumNullable(statsList, 'estimated_cost_usd');
    const totalCacheSavings = sumNullable(statsList, 'cache_savings_usd');
    const savingsItem = totalCacheSavings !== null && totalCacheSavings > 0
      ? `<div class="usage-summary-item"><div class="usage-summary-label">缓存节省（等价）</div><div class="usage-summary-value" title="${escapeHtml(formatUsd(totalCacheSavings))}">≈ ${escapeHtml(formatUsdSummary(totalCacheSavings))}</div></div>`
      : '';
    const budget = budgetUsd();
    let budgetItem = '';
    if (budget !== null) {
      const monthSpent = monthCost(periods);
      const ratio = monthSpent !== null ? (monthSpent / budget) * 100 : 0;
      const level = ratio >= 100 ? 'danger' : ratio >= 80 ? 'warn' : '';
      budgetItem = `<div class="usage-summary-item"><div class="usage-summary-label">本月预算（已用 ${escapeHtml(ratio.toFixed(0))}%）</div><div class="usage-summary-value">${escapeHtml(formatUsdCompact(monthSpent))} / ${escapeHtml(formatUsdCompact(budget))}</div><div class="bar budget-bar"><span class="${level}" style="width:${Math.min(100, Math.max(0, ratio))}%"></span></div></div>`;
    }
    const summary = `<div class="usage-summary">
      <div class="usage-summary-item"><div class="usage-summary-label">匹配账号</div><div class="usage-summary-value">${formatNumber(filteredAccounts.length)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">总 token</div><div class="usage-summary-value" title="${escapeHtml(formatNumber(totalTokens))}">${formatNumber(totalTokens)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">API 等价金额</div><div class="usage-summary-value" title="${escapeHtml(formatUsd(totalUsd))}">${escapeHtml(formatUsdSummary(totalUsd))}</div></div>
      ${savingsItem}${budgetItem}
    </div>`;
    const dimensionHeaders = {
      account: '<tr><th>订阅 / 账号 ID</th><th>调用模型</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th><th>占比</th></tr>',
      model: '<tr><th>模型</th><th>使用账号</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th><th>占比</th></tr>',
      project: '<tr><th>项目 / 工作目录</th><th>账号</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th><th>占比</th></tr>',
    };
    const shareText = (stats) => {
      if (totalUsd === null || totalUsd <= 0) return '—';
      const cost = stats.estimated_cost_usd;
      if (cost === null || cost === undefined) return '—';
      return `${((Number(cost) / totalUsd) * 100).toFixed(1)}%`;
    };
    const groupsByAccount = usageGroups(filteredAccounts, 'account');
    const accountGroupByKey = new Map(groupsByAccount.map((group) => [group.key, group]));
    // 订阅（产品 · 套餐）从 /api/state 取一次，表格与排行共用同一份标签。
    const accountSubscriptions = usageAccountSubscriptions();
    const accountLabel = (group) => usageAccountRankingLabel(group, accountSubscriptions);
    const rows = groups.map((group) => {
      const stats = group.stats;
      const accountGroup = accountGroupByKey.get(group.key) || group;
      // 模型按 token 用量倒序，折叠时优先露出来的就是大头。
      const modelItems = (stats.models || []).slice().sort((left, right) => Number(right.total_tokens || 0) - Number(left.total_tokens || 0));
      const modelsExpanded = expandedUsageModelLists.has(group.key);
      const visibleModels = modelsExpanded ? modelItems : modelItems.slice(0, USAGE_MODEL_PREVIEW);
      const hiddenModels = modelItems.length - visibleModels.length;
      const models = visibleModels.map((model) => `<div><span class="usage-model">${escapeHtml(model.model)}</span> · ${formatNumber(model.total_tokens)} tokens</div>`).join('');
      const modelsToggle = modelItems.length > USAGE_MODEL_PREVIEW
        ? `<button class="chip-button usage-models-toggle" type="button" data-usage-models="${escapeHtml(group.key)}" aria-expanded="${modelsExpanded ? 'true' : 'false'}">${modelsExpanded ? '收起模型' : `另有 ${hiddenModels} 个模型`}</button>`
        : '';
      const profiles = usageTagLine(accountGroup.profiles, '未知 profile');
      const pricingWarning = stats.unpriced_models && stats.unpriced_models.length
        ? `<div class="muted">未定价（未计入金额）：${escapeHtml(stats.unpriced_models.join(', '))}</div>`
        : '';
      let labelCell;
      if (selectedUsageDimension === 'model') {
        labelCell = `<td><div>${escapeHtml(group.key)}</div><div class="muted">${escapeHtml(usageContributorLabel(group) || '未知账号')}</div></td>`;
      } else if (selectedUsageDimension === 'project') {
        labelCell = `<td><div>${escapeHtml(group.key)}</div><div class="muted">${escapeHtml(usageContributorLabel(group) || '未知账号')}</div></td>`;
      } else {
        // 中文注释：与账号成本排行同一份标签（产品 · 套餐/账号 ID），
        // 次要行保留 profile，用来区分同一账号下的多个登录目录。
        labelCell = `<td><div>${accountLabel(group)}</div><div class="muted">Profile：${escapeHtml(profiles)}</div></td>`;
      }
      const detailCell = selectedUsageDimension === 'account'
        ? `<td class="usage-models">${models || '<span class="muted">暂无模型</span>'}${modelsToggle}${pricingWarning}</td>`
        : `<td class="usage-models">${group.accountList.slice(0, 3).map((name) => `<div>${escapeHtml(name)}</div>`).join('')}${group.accountList.length > 3 ? `<div class="muted">等 ${group.accountList.length} 个账号</div>` : ''}${pricingWarning}</td>`;
      return `<tr>
        ${labelCell}
        ${detailCell}
        <td class="usage-number">${formatNumber(stats.input_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.cached_input_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.cache_write_input_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.output_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.reasoning_output_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.total_tokens)}</td>
        <td class="usage-number">${formatCredits(stats.estimated_credits)}</td>
        <td class="usage-number">${formatUsd(stats.estimated_cost_usd)}</td>
        <td class="usage-number">${escapeHtml(shareText(stats))}</td>
      </tr>`;
    }).join('');
    const totalsRow = groups.length > 1
      ? `<tfoot><tr>
        <td>合计（${formatNumber(groups.length)} 组）</td>
        <td>${formatNumber(filteredAccounts.length)} 个账号</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.input_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.cached_input_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.cache_write_input_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.output_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.reasoning_output_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(totalTokens)}</td>
        <td class="usage-number">${formatCredits(totalCredits)}</td>
        <td class="usage-number">${formatUsd(totalUsd)}</td>
        <td class="usage-number">${totalUsd && totalUsd > 0 ? '100.0%' : '—'}</td>
      </tr></tfoot>`
      : '';
    const accountRanking = renderUsageRanking(
      '账号成本排行',
      '当前筛选 · 占比为筛选内合计',
      groupsByAccount,
      accountLabel,
    );
    const projectLabel = (group) => {
      const sub = usageContributorLabel(group);
      return `${escapeHtml(group.key)}${sub ? `<span class="muted"> · ${escapeHtml(sub)}</span>` : ''}`;
    };
    const projectRanking = renderUsageRanking(
      '项目成本排行',
      '当前筛选 · 按 API 等价金额',
      usageGroups(filteredAccounts, 'project'),
      projectLabel,
    );
    // 中文注释：两个排行并排两列；只有一个有数据时（例如全是未定价模型）
    // 让剩下那个占满整行，避免半张空栏。
    const rankingBlocks = [accountRanking, projectRanking].filter(Boolean);
    const rankings = rankingBlocks.length
      ? `<div class="usage-rankings${rankingBlocks.length === 1 ? ' single' : ''}">${rankingBlocks.join('')}</div>`
      : '';
    container.innerHTML = `${renderTrend(usage.daily)}
      <div class="usage-tabs">${tabs}<span class="usage-tabs-divider" aria-hidden="true"></span>${dimensionTabs}</div>
      ${filterControls}${summary}${rankings}<div class="usage-note">${indexingNote}${escapeHtml(note)} 统计维度：${escapeHtml(usageDimensionLabels[selectedUsageDimension] || '按账号')} · 时间范围：${escapeHtml(formatTime(period.start_at))} 至 ${escapeHtml(formatTime(period.end_at))} · credits：${escapeHtml(formatCredits(totalCredits))}</div>${renderKimiReconciliation(periods)}${renderCommandCodeReconciliation(periods)}
      <div class="table-wrap usage-table"><table>
        <thead>${dimensionHeaders[selectedUsageDimension] || dimensionHeaders.account}</thead>
        <tbody>${rows || '<tr><td colspan="11" class="empty-state">这个时间范围没有匹配的账号、模型或项目</td></tr>'}</tbody>
        ${totalsRow}
      </table></div>`;
    container.querySelectorAll('[data-usage-period]').forEach((button) => {
      button.addEventListener('click', () => {
        selectedUsagePeriod = button.dataset.usagePeriod || 'today';
        renderUsage(state);
      });
    });
    container.querySelectorAll('[data-usage-models]').forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.dataset.usageModels || '';
        if (expandedUsageModelLists.has(key)) {
          expandedUsageModelLists.delete(key);
        } else {
          expandedUsageModelLists.add(key);
        }
        renderUsage(state);
      });
    });
    container.querySelectorAll('[data-usage-dimension]').forEach((button) => {
      button.addEventListener('click', () => {
        selectedUsageDimension = button.dataset.usageDimension || 'account';
        renderUsage(state);
      });
    });
    const accountFilter = document.getElementById('usage-account-filter');
    const modelFilter = document.getElementById('usage-model-filter');
    const projectFilter = document.getElementById('usage-project-filter');
    if (accountFilter) {
      accountFilter.addEventListener('change', () => {
        selectedUsageAccount = accountFilter.value;
        renderUsage(state);
      });
    }
    if (modelFilter) {
      modelFilter.addEventListener('change', () => {
        selectedUsageModel = modelFilter.value;
        renderUsage(state);
      });
    }
    if (projectFilter) {
      projectFilter.addEventListener('change', () => {
        selectedUsageProject = projectFilter.value;
        renderUsage(state);
      });
    }
  };
  const refreshUsage = async () => {
    const button = document.getElementById('usage-load-button');
    const container = document.getElementById('usage-content');
    if (usagePollTimer) {
      window.clearTimeout(usagePollTimer);
      usagePollTimer = 0;
    }
    if (button) {
      button.disabled = true;
      button.textContent = '正在统计…';
    }
    if (!latestUsageState) {
      container.innerHTML = '<div class="empty-state">正在启动后台用量索引…</div>';
    }
    const poll = async () => {
      try {
        const response = await fetch('/api/usage', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        latestUsageState = { usage: payload.usage || {} };
        renderUsage(latestUsageState);
        updateSpendFromUsage(latestUsageState.usage);
        renderAlerts();
        const indexing = latestUsageState.usage.indexing || {};
        if (indexing.complete === false) {
          if (button) {
            button.textContent = `索引中 ${Number(indexing.percent || 0).toFixed(1)}%`;
          }
          // 中文注释：标签页隐藏时暂停轮询，由 visibilitychange 恢复。
          if (document.hidden) {
            usagePollSuspended = true;
          } else {
            usagePollTimer = window.setTimeout(poll, 1000);
          }
          return;
        }
        if (button) {
          button.disabled = false;
          button.textContent = '刷新用量';
        }
      } catch (error) {
        container.innerHTML = `<div class="error" style="display:block">读取用量失败：${escapeHtml(error.message)}</div>`;
        if (button) {
          button.disabled = false;
          button.textContent = '刷新用量';
        }
      }
    };
    await poll();
  };
  const renderInsights = (payload) => {
    const container = document.getElementById('insights-content');
    const insights = (payload && payload.insights) || {};
    if (insights.ready !== true) {
      const indexing = insights.indexing || {};
      if (indexing.complete === false) {
        const percent = Number(indexing.percent || 0).toFixed(2);
        container.innerHTML = `<div class="empty-state"><strong>用量索引进行中（${escapeHtml(percent)}%）</strong><br>索引完成后才能生成习惯分析，页面会自动更新。</div>`;
      } else {
        container.innerHTML = '<div class="empty-state">用量索引尚未建立，请先在「用量与费用」区加载用量。</div>';
      }
      return;
    }
    if (insights.insufficient) {
      container.innerHTML = `<div class="empty-state">对话数量不足（当前 ${formatNumber(insights.conversation_count || 0)} 个，至少需要 3 个），再积累一些使用后再来分析。</div>`;
      return;
    }
    const hitRate = insights.cache_hit_rate;
    const windowDays = Number(insights.window_days || 0);
    // 中文注释：今天按本地日历日（后端 days=today 用同一个起点），与用量区一致。
    const windowLabel = insights.window_kind === 'today'
      ? '今天'
      : windowDays > 0 ? `近 ${windowDays} 天` : '全部历史';
    const summary = `<div class="usage-summary">
      <div class="usage-summary-item"><div class="usage-summary-label">分析对话数 · ${escapeHtml(windowLabel)}</div><div class="usage-summary-value">${formatNumber(insights.conversation_count)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">总 token</div><div class="usage-summary-value">${formatNumber(insights.total_tokens)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">整体缓存命中率</div><div class="usage-summary-value">${hitRate === null || hitRate === undefined ? '未知' : `${Number(hitRate).toFixed(1)}%`}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">预计可节省</div><div class="usage-summary-value">${escapeHtml(formatUsdCompact(insights.potential_savings_usd))}</div></div>
    </div>`;
    const hours = Array.isArray(insights.hour_histogram) ? insights.hour_histogram : [];
    const maxHour = hours.reduce((max, item) => Math.max(max, Number(item.total_tokens || 0)), 0);
    const hourBars = hours.map((item) => {
      const tokens = Number(item.total_tokens || 0);
      const height = maxHour > 0 ? Math.max(3, Math.round(tokens / maxHour * 100)) : 3;
      const classes = tokens > 0 ? 'trend-bar' : 'trend-bar empty';
      return `<div class="${classes}" style="height:${height}%" title="${escapeHtml(`${item.hour}:00 · ${formatNumber(tokens)} tokens`)}"></div>`;
    }).join('');
    const hoursPanel = `<div class="usage-trend"><div class="usage-trend-head"><span>活跃时段</span><span class="muted">按 token 加权 · 24 小时</span></div><div class="trend-chart">${hourBars}</div></div>`;
    const statRow = (label, valueText, width, hint, title) => `<div class="top-project"><div class="top-project-row"><span class="top-project-label"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(label)}${hint ? `<span class="muted"> · ${escapeHtml(hint)}</span>` : ''}</span><span class="top-project-value">${escapeHtml(valueText)}</span></div><div class="bar"><span style="width:${width}%"></span></div></div>`;
    const models = Array.isArray(insights.models) ? insights.models : [];
    const maxModelCost = models.reduce((max, item) => Math.max(max, Number(item.estimated_cost_usd || 0)), 0);
    const modelRows = models.map((item) => statRow(item.model, `${formatUsdCompact(item.estimated_cost_usd)} · ${formatNumber(item.total_tokens)} tokens`, maxModelCost > 0 ? Math.max(3, Math.round(Number(item.estimated_cost_usd || 0) / maxModelCost * 100)) : 3)).join('');
    const modelsPanel = `<div class="usage-trend"><div class="usage-trend-head"><span>模型成本分布</span><span class="muted">Top ${models.length}</span></div>${modelRows || '<div class="muted">暂无模型数据</div>'}</div>`;
    const buckets = Array.isArray(insights.size_buckets) ? insights.size_buckets : [];
    const maxBucketCount = buckets.reduce((max, item) => Math.max(max, Number(item.conversations || 0)), 0);
    const bucketRows = buckets.map((item) => statRow(item.label, `${formatNumber(item.conversations)} 个 · ${formatUsdCompact(item.estimated_cost_usd)}`, maxBucketCount > 0 ? Math.max(3, Math.round(Number(item.conversations || 0) / maxBucketCount * 100)) : 3)).join('');
    const bucketsPanel = `<div class="usage-trend"><div class="usage-trend-head"><span>对话规模分布</span><span class="muted">按轮数</span></div>${bucketRows}</div>`;
    const suggestions = Array.isArray(insights.suggestions) ? insights.suggestions : [];
    const suggestionCards = suggestions.length
      ? suggestions.map((item) => `<div class="alert-banner ${escapeHtml(item.level || 'info')}">${item.saving_usd !== null && item.saving_usd !== undefined ? `<span class="suggestion-saving">可省 ${escapeHtml(formatUsdCompact(item.saving_usd))}</span>` : ''}<strong>${escapeHtml(item.title)}</strong><div>${escapeHtml(item.detail)}</div></div>`).join('')
      : '<div class="empty-state">当前用量模式很健康，没有发现明显的浪费点。</div>';
    const observations = Array.isArray(insights.observations) ? insights.observations : [];
    const observationItems = observations.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
    const observationsPanel = observations.length
      ? `<div class="usage-trend"><div class="usage-trend-head"><span>习惯画像</span><span class="muted">${escapeHtml(windowLabel)}的已索引对话</span></div><ul class="observation-list">${observationItems}</ul></div>`
      : '';
    const topConversations = Array.isArray(insights.top_conversations) ? insights.top_conversations : [];
    const maxTopCost = Number((topConversations[0] || {}).estimated_cost_usd || 0);
    // 中文注释：最贵对话按「订阅（产品 · 套餐）· 账号 · 会话 · 项目路径」展示。
    // 订阅与账号卡片同源（/api/state + 同一套 planLabel 归一化），会话 ID 只显示
    // 前 12 位、完整 ID 放在悬停提示里，未定价提示仍然缀在最后。
    const topSubscriptions = usageAccountSubscriptions();
    const conversationTitle = (item) => {
      const meta = topSubscriptions.get(item.account) || {};
      const subscription = meta.plan ? `${meta.product} · ${meta.plan}` : (meta.product || '');
      return subscription || `${item.account || '未知账号'}`;
    };
    const conversationDetail = (item, fullSessionId = false) => {
      const sessionId = String(item.label || '').trim();
      return [
        item.account || '未知账号',
        sessionId ? `会话 ${fullSessionId ? sessionId : sessionId.slice(0, 12)}` : '',
        item.project || '',
      ].filter(Boolean).join(' · ');
    };
    const topRows = topConversations.map((item) => statRow(
      conversationTitle(item),
      `${formatUsdCompact(item.estimated_cost_usd)} · ${formatNumber(item.total_tokens)} tokens · ${formatNumber(item.turns)} 轮`,
      maxTopCost > 0 ? Math.max(3, Math.round(Number(item.estimated_cost_usd || 0) / maxTopCost * 100)) : 3,
      `${conversationDetail(item)}${item.has_unpriced ? ' · 含未定价模型' : ''}`,
      `${conversationTitle(item)} · ${conversationDetail(item, true)}`
    )).join('');
    container.innerHTML = `${summary}${observationsPanel}<div class="insights-grid">${hoursPanel}${modelsPanel}${bucketsPanel}</div><div class="usage-trend"><div class="usage-trend-head"><span>省 token 建议</span><span class="muted">${escapeHtml(windowLabel)} · 按用量规模估算</span></div><div class="alert-list">${suggestionCards}</div></div><div class="usage-trend"><div class="usage-trend-head"><span>最贵对话 Top ${topConversations.length}</span><span class="muted">按 API 等价金额</span></div>${topRows || '<div class="muted">暂无可计价对话</div>'}</div>`;
  };
  const refreshInsights = async () => {
    const button = document.getElementById('insights-load-button');
    const container = document.getElementById('insights-content');
    if (insightsPollTimer) {
      window.clearTimeout(insightsPollTimer);
      insightsPollTimer = 0;
    }
    if (button) {
      button.disabled = true;
      button.textContent = '正在分析…';
    }
    if (!latestInsightsState) {
      container.innerHTML = '<div class="empty-state">正在读取用量索引…</div>';
    }
    const poll = async () => {
      try {
        const insightsUrl = insightsPeriodDays ? `/api/insights?days=${insightsPeriodDays}` : '/api/insights';
        const response = await fetch(insightsUrl, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        latestInsightsState = payload;
        renderInsights(payload);
        const insights = payload.insights || {};
        const indexing = insights.indexing || {};
        if (insights.ready !== true && indexing.complete === false) {
          if (button) {
            button.textContent = `索引中 ${Number(indexing.percent || 0).toFixed(1)}%`;
          }
          if (document.hidden) {
            insightsPollSuspended = true;
          } else {
            insightsPollTimer = window.setTimeout(poll, 1000);
          }
          return;
        }
        if (button) {
          button.disabled = false;
          button.textContent = '重新分析';
        }
      } catch (error) {
        container.innerHTML = `<div class="error" style="display:block">读取习惯分析失败：${escapeHtml(error.message)}</div>`;
        if (button) {
          button.disabled = false;
          button.textContent = '重新分析';
        }
      }
    };
    await poll();
  };
  const renderTraffic = (state) => {
    const container = document.getElementById('traffic-content');
    const countLabel = document.getElementById('traffic-section-count');
    if (!container) return;
    const traffic = state.traffic || {};
    const processes = Array.isArray(traffic.processes) ? traffic.processes : [];
    const totals = traffic.totals || {};
    const thresholds = traffic.thresholds || {};
    if (countLabel) {
      countLabel.textContent = `${processes.length} 个进程 · 近 15 秒 ${formatDataSize(totals.burst_bytes || 0)}`;
    }
    if (traffic.source === 'unavailable') {
      const reason = traffic.reason || '无法读取内核 TCP 计数（INET_DIAG）';
      container.innerHTML = `<div class="empty-state">${escapeHtml(reason)}。异常流量监控暂不可用。</div>`;
      return;
    }
    if (processes.length === 0) {
      container.innerHTML = '<div class="empty-state">当前没有识别到 Codex / Grok / Kimi / DeepSeek Harness 等 code agent 进程。</div>';
      return;
    }
    const warnBytes = Number(thresholds.burst_warn_bytes || 0);
    const rank = { danger: 0, warn: 1 };
    const ordered = [...processes].sort((left, right) => {
      const leftRank = rank[left.alert_level] ?? 2;
      const rightRank = rank[right.alert_level] ?? 2;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return Number(right.burst_bytes || 0) - Number(left.burst_bytes || 0);
    });
    const rows = ordered.map((item) => {
      const level = item.alert_level === 'danger' ? 'danger' : item.alert_level === 'warn' ? 'warn' : 'ok';
      const status = item.alert_level === 'danger' ? '异常大上传' : item.alert_level === 'warn' ? '偏高' : '正常';
      const remotes = (item.connections || []).filter((conn) => !conn.loopback && !conn.service).slice(0, 3).map((conn) => conn.remote).join(' · ') || '无外连';
      return `<tr>
        <td><div>${escapeHtml(item.product_label || item.product)}</div><div class="muted">pid ${escapeHtml(item.pid)}${(item.pids || []).length > 1 ? ` · ${item.pids.length} 个进程` : ''}</div></td>
        <td class="cwd">${escapeHtml(item.cwd || '未知')}</td>
        <td class="usage-number">${escapeHtml(formatDataSize(item.burst_bytes || 0))}<div class="muted">本轮 ${escapeHtml(formatDataSize(item.external_upload_delta || 0))}</div></td>
        <td class="usage-number">${escapeHtml(formatDataSize(item.window_bytes || 0))}<div class="muted">监控累计 ${escapeHtml(formatDataSize(item.observed_external_bytes || 0))}</div></td>
        <td class="traffic-remote">${escapeHtml(remotes)}</td>
        <td><span class="pill ${level}">${status}</span>${warnBytes ? `<div class="muted">阈值 ${escapeHtml(formatDataSize(warnBytes))} / 15s</div>` : ''}</td>
      </tr>`;
    }).join('');
    const platformNote = traffic.source === 'process-only'
      ? `<div class="usage-note">${escapeHtml(traffic.reason || '当前平台没有内核 TCP 计数')}，字节列仅供参考。</div>`
      : '';
    container.innerHTML = `${platformNote}<div class="usage-note">只统计离开本机的 TCP 发送字节。回环和进程自己监听的 Web UI（例如 DeepSeek Harness :3080 推给浏览器的会话）不计入外发告警。首次看到一条连接时只记基线，避免把监控启动前的历史流量当成突发上传。</div>
      <div class="table-wrap traffic-table"><table>
        <thead><tr><th>Agent</th><th>工作目录</th><th>近 15 秒外发</th><th>近 5 分钟 / 累计</th><th>主要对端</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  };
  // 告警历史：读取落盘告警，支持筛选、已读和清理。
  let alertHistoryState = null;
  // 中文注释：每页固定 50 条，alertHistoryPage 是当前页码（0 起）。
  const ALERT_PAGE_SIZE = 50;
  let alertHistoryPage = 0;
  const alertFilterValue = (id) => {
    const element = document.getElementById(id);
    return element ? String(element.value || '') : '';
  };
  const alertQueryString = () => {
    const params = new URLSearchParams();
    const days = alertFilterValue('alert-range-filter');
    if (days) params.set('days', days);
    const level = alertFilterValue('alert-level-filter');
    if (level) params.set('level', level);
    const kind = alertFilterValue('alert-kind-filter');
    if (kind) params.set('kind', kind);
    const ack = alertFilterValue('alert-ack-filter');
    if (ack) params.set('ack', ack);
    const keyword = alertFilterValue('alert-keyword-filter').trim();
    if (keyword) params.set('q', keyword);
    params.set('limit', String(ALERT_PAGE_SIZE));
    params.set('offset', String(alertHistoryPage * ALERT_PAGE_SIZE));
    return params.toString();
  };
  // 中文注释：告警 → 会话上下文的展开状态与缓存；5 秒自动刷新重渲染时保持展开。
  const alertContextCache = new Map();
  const ALERT_CONTEXT_REASONS = {
    no_session: '告警时间窗内没有匹配该工作目录的会话文件（可能已清理或归档）',
    unsupported_product: '该 agent 的会话格式暂不支持明细提取',
    unreadable: '会话文件无法读取',
  };
  const ALERT_CONTEXT_KINDS = {
    user: '用户消息',
    tool: '工具调用',
    tool_output: '工具输出',
    search: '联网搜索',
    assistant: '助手消息',
    image: '图片输入',
  };
  const ALERT_ACTIVITY_BASIS = {
    record: '日志记录',
    parameters: '根据参数判断',
    tool: '根据调用类型判断',
    unknown: '证据不足',
  };
  const renderAlertContext = (context) => {
    if (!context || !context.found) {
      const reason = ALERT_CONTEXT_REASONS[(context && context.reason) || ''] || '未能定位会话';
      return `<div class="empty-state"><span class="empty-title">未找到可关联的会话</span><span class="empty-hint">${escapeHtml(reason)}</span></div>`;
    }
    const session = context.session || {};
    const totals = context.totals || {};
    const events = Array.isArray(context.events) ? context.events : [];
    const items = events.map((event) => {
      const kindLabel = ALERT_CONTEXT_KINDS[event.kind] || '事件';
      const size = Number(event.size || 0) > 0 ? ` <span class="muted">${escapeHtml(formatDataSize(event.size))}</span>` : '';
      // 中文注释：优先展示行为和对象，不把工具名称或命令当作执行目的。
      const activities = Array.isArray(event.activities) && event.activities.length ? event.activities : [{ summary: '用途无法判断', basis: 'unknown' }];
      const body = activities.map((activity) => {
        const result = activity.phase === 'result' ? ' · 执行结果' : '';
        const target = activity.target ? ` · ${escapeHtml(activity.target)}` : '';
        const basis = ALERT_ACTIVITY_BASIS[activity.basis] || ALERT_ACTIVITY_BASIS.unknown;
        return `<strong>${escapeHtml(activity.summary || '用途无法判断')}${result}</strong>${target} <span class="muted">${escapeHtml(basis)}</span>`;
      }).join('<br>');
      const detail = event.kind === 'user' || event.kind === 'search' ? escapeHtml(event.detail || '') : '';
      return `<div class="ctx-event"><span class="ctx-time mono">${escapeHtml(formatTime(event.t))}</span><span class="chip">${escapeHtml(kindLabel)}</span><span class="ctx-body">${body}${detail ? `<br>${detail}` : ''}${size}</span></div>`;
    }).join('');
    const truncated = context.truncated ? `（仅显示前 ${events.length} 条）` : '';
    const privacyNote = context.content_enabled ? '' : '<br>内容摘要已隐藏，行为类别仍可见。';
    const summaries = Array.isArray(context.activity_summary) ? context.activity_summary : [];
    const activityOverview = summaries.length ? `<div class="usage-note"><strong>涉及的行为：</strong>${summaries.map(escapeHtml).join(' · ')}</div>` : '';
    const statsNote = context.fallback
      ? '告警时间窗内该会话没有新事件；以下是告警发生前最近的活动，不能确认它们对应本次外发。'
      : `时间窗内 ${Number(totals.events || 0)} 条事件 · 本地输入记录 ${escapeHtml(formatDataSize(totals.input_bytes || 0))} · 工具输出 ${escapeHtml(formatDataSize(totals.output_bytes || 0))}${truncated}。行为来自本地日志，不能确认实际外发内容或上传成功。`;
    return `<div class="alert-context">
      <div class="usage-note">会话 <span class="mono">${escapeHtml(session.path || '')}</span><br>${statsNote}${privacyNote}</div>
      ${activityOverview}
      ${items || '<div class="empty-state"><span class="empty-hint">时间窗内没有提取到事件明细。</span></div>'}
    </div>`;
  };
  const renderAlertContextRow = (alert) => {
    const entry = alertContextCache.get(String(alert.id));
    if (!entry) return '';
    let body = '';
    if (entry.status === 'loading') {
      body = '<div class="empty-state"><span class="empty-title">正在读取会话上下文…</span></div>';
    } else if (entry.status === 'error') {
      body = `<div class="empty-state"><span class="empty-title">读取会话上下文失败：${escapeHtml(entry.message)}</span></div>`;
    } else {
      body = renderAlertContext(entry.data);
    }
    return `<tr class="alert-context-row"><td colspan="8">${body}</td></tr>`;
  };
  const toggleAlertContext = async (id) => {
    if (!id) return;
    if (alertContextCache.has(id)) {
      alertContextCache.delete(id);
      if (alertHistoryState) renderAlertHistory(alertHistoryState);
      return;
    }
    alertContextCache.set(id, { status: 'loading' });
    if (alertHistoryState) renderAlertHistory(alertHistoryState);
    try {
      const response = await fetch(`/api/alerts/context?id=${encodeURIComponent(id)}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      alertContextCache.set(id, { status: 'ok', data: payload.context || {} });
    } catch (error) {
      alertContextCache.set(id, { status: 'error', message: error.message });
    }
    if (alertHistoryState) renderAlertHistory(alertHistoryState);
  };
  // 告警历史：先给结论（未读/红色/最近），再给可筛选的明细表。
  const renderAlertStats = (stats, payload) => {
    const container = document.getElementById('alert-history-stats');
    if (!container) return;
    const scope = Number((payload.filters && payload.filters.days) || 0);
    const unread = Number(stats.unread || 0);
    container.innerHTML = [
      `<div class="stat"><div class="stat-label">未读告警</div><div class="stat-value ${unread > 0 ? 'warn' : 'ok'}">${escapeHtml(formatNumber(unread))}</div><div class="stat-foot">共 ${escapeHtml(formatNumber(stats.total || 0))} 条匹配记录</div></div>`,
      `<div class="stat"><div class="stat-label">红色告警</div><div class="stat-value ${Number(stats.danger || 0) > 0 ? 'danger' : ''}">${escapeHtml(formatNumber(stats.danger || 0))}</div><div class="stat-foot">黄色 ${escapeHtml(formatNumber(stats.warn || 0))} 条</div></div>`,
      `<div class="stat"><div class="stat-label">最近一次</div><div class="stat-value" style="font-size:15px">${escapeHtml(formatTime(stats.last_alert_at))}</div><div class="stat-foot">${scope ? `统计范围：近 ${escapeHtml(String(scope))} 天` : '统计范围：全部历史'}</div></div>`,
      `<div class="stat"><div class="stat-label">重复合并窗口</div><div class="stat-value">${escapeHtml(String(Number(payload.merge_window_seconds || 0)))} 秒</div><div class="stat-foot">同进程同规则告警合并为一条</div></div>`
    ].join('');
  };
  const renderAlertHistory = (payload) => {
    const container = document.getElementById('alert-history-content');
    const countLabel = document.getElementById('alert-history-count');
    if (!container) return;
    alertHistoryState = payload;
    if (payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">告警历史不可用</span><span class="empty-hint">当前监控进程未启用落盘，或数据库无法读取。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
    const stats = payload.stats || {};
    renderAlertStats(stats, payload);
    if (countLabel) {
      countLabel.textContent = `未读 ${Number(stats.unread || 0)} / 共 ${Number(stats.total || 0)} 条`;
    }
    const totalPages = Math.max(1, Math.ceil(Number(stats.total || 0) / ALERT_PAGE_SIZE));
    if (alerts.length === 0 && alertHistoryPage > 0 && Number(stats.total || 0) > 0) {
      // 中文注释：筛选变化或清理后旧页码可能超出范围，收敛到末页重拉。
      alertHistoryPage = totalPages - 1;
      refreshAlertHistory();
      return;
    }
    if (alerts.length === 0) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">当前筛选条件下没有历史告警</span><span class="empty-hint">放宽时间范围、级别或已读状态再试。</span></div>';
      return;
    }
    const rows = alerts.map((alert) => {
      const level = alert.level === 'danger' ? 'danger' : 'warn';
      const levelText = alert.level === 'danger' ? '异常大上传' : '偏高';
      const rule = alert.kind === 'burst' ? '突发窗口' : '累计窗口';
      const repeat = Number(alert.count || 1) > 1 ? `<span class="chip warn">合并 ${Number(alert.count)} 次</span> ` : '';
      const ackAction = alert.acknowledged ? 'unack' : 'ack';
      const ackLabel = alert.acknowledged ? '标为未读' : '标为已读';
      return `<tr class="${alert.acknowledged ? '' : 'row-unread'}">
        <td><div class="cell-main">${escapeHtml(formatTime(alert.last_seen_at))}</div><div class="cell-sub">首次 ${escapeHtml(formatTime(alert.first_seen_at))}</div></td>
        <td><span class="pill ${level}">${levelText}</span><div class="cell-sub">#${escapeHtml(alert.id)}</div></td>
        <td><div class="cell-main">${escapeHtml(alert.product_label || alert.product)}</div><div class="cell-sub">pid ${escapeHtml(alert.pid)}${alert.command ? ` · ${escapeHtml(alert.command)}` : ''}</div></td>
        <td><span class="truncate" title="${escapeHtml(alert.cwd || '')}">${escapeHtml(alert.cwd || '未知目录')}</span></td>
        <td><div class="cell-main">${escapeHtml(formatDataSize(alert.peak_bytes || alert.bytes || 0))}</div><div class="cell-sub">${repeat}${escapeHtml(rule)} · ${escapeHtml(String(Number(alert.window_seconds || 0)))} 秒</div></td>
        <td><span class="mono">${escapeHtml(alert.remote || '—')}</span></td>
        <td><span class="pill ${alert.acknowledged ? 'other' : 'warn'}">${alert.acknowledged ? '已读' : '未读'}</span></td>
        <td><div class="alert-row-actions"><button class="btn mini" type="button" data-alert-context="${escapeHtml(alert.id)}">上下文</button><button class="btn mini" type="button" data-alert-action="${ackAction}" data-alert-id="${escapeHtml(alert.id)}">${ackLabel}</button></div></td>
      </tr>${renderAlertContextRow(alert)}`;
    }).join('');
    const pager = totalPages > 1
      ? `<div class="table-actions"><button class="btn" type="button" data-alert-page="-1" ${alertHistoryPage === 0 ? 'disabled' : ''}>上一页</button><span class="muted">第 ${alertHistoryPage + 1} / ${totalPages} 页 · 共 ${escapeHtml(formatNumber(stats.total || 0))} 条</span><button class="btn" type="button" data-alert-page="1" ${alertHistoryPage >= totalPages - 1 ? 'disabled' : ''}>下一页</button></div>`
      : '';
    container.innerHTML = `<div class="table-wrap"><table class="tight alert-history-table">
        <thead><tr><th>时间</th><th>级别</th><th>Agent / 进程</th><th>工作目录</th><th>外发峰值 / 规则</th><th>主要对端</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>${pager}`;
    container.querySelectorAll('[data-alert-action]').forEach((button) => {
      button.addEventListener('click', () => mutateAlerts(button.dataset.alertAction, { ids: [Number(button.dataset.alertId)] }));
    });
    container.querySelectorAll('[data-alert-context]').forEach((button) => {
      button.addEventListener('click', () => toggleAlertContext(button.dataset.alertContext || ''));
    });
    container.querySelectorAll('[data-alert-page]').forEach((button) => {
      button.addEventListener('click', () => {
        alertHistoryPage += Number(button.dataset.alertPage || 0);
        refreshAlertHistory();
      });
    });
  };
  const refreshAlertHistory = async () => {
    const container = document.getElementById('alert-history-content');
    const button = document.getElementById('alert-history-load-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch(`/api/alerts?${alertQueryString()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderAlertHistory(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state">读取告警历史失败：${escapeHtml(error.message)}</div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  const mutateAlerts = async (action, body) => {
    try {
      const response = await fetch('/api/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...body })
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await response.json();
      await refreshAlertHistory();
      refresh();
    } catch (error) {
      const container = document.getElementById('alert-history-content');
      if (container) container.innerHTML = `<div class="empty-state">更新告警失败：${escapeHtml(error.message)}</div>`;
    }
  };
  const clearAlertHistory = async () => {
    const days = Number(alertFilterValue('alert-range-filter') || 0);
    const total = Number((alertHistoryState && alertHistoryState.stats && alertHistoryState.stats.total) || 0);
    if (total === 0) {
      window.alert('当前筛选条件下没有可清理的告警。');
      return;
    }
    const scope = days ? `早于近 ${days} 天的历史告警` : '全部历史告警';
    if (!window.confirm(`将删除${scope}（当前筛选条件内约 ${total} 条，含已读与未读）。此操作不可撤销，是否继续？`)) return;
    await mutateAlerts('clear', days ? { before: Date.now() / 1000 - days * 86400 } : { all: true });
  };
  // 用量检索：按日期、模型和会话查询已索引的 token 历史。
  let usageSearchState = null;
  let usageSearchGroup = 'session';
  // 中文注释：每页固定 30 行，usageSearchPage 是当前页码（0 起）。
  const usageSearchLimit = 30;
  let usageSearchPage = 0;
  const usageSearchValue = (id) => {
    const element = document.getElementById(id);
    return element ? String(element.value || '') : '';
  };
  const usageSearchQueryString = () => {
    const params = new URLSearchParams();
    const from = usageSearchValue('usage-search-from');
    const to = usageSearchValue('usage-search-to');
    if (from || to) {
      if (from) params.set('from', from);
      if (to) params.set('to', to);
    } else {
      params.set('days', usageSearchValue('usage-search-range') || '30');
    }
    const model = usageSearchValue('usage-search-model');
    if (model) params.set('model', model);
    const account = usageSearchValue('usage-search-account');
    if (account) params.set('account', account);
    const keyword = usageSearchValue('usage-search-keyword').trim();
    if (keyword) params.set('q', keyword);
    params.set('group', usageSearchGroup);
    params.set('sort', usageSearchValue('usage-search-sort') || 'recent');
    params.set('limit', String(usageSearchLimit));
    params.set('offset', String(usageSearchPage * usageSearchLimit));
    return params.toString();
  };
  const fillUsageSearchModels = (models) => {
    const select = document.getElementById('usage-search-model');
    if (!select) return;
    const current = select.value;
    const options = ['<option value="">全部模型</option>'].concat(
      (models || []).map((model) => `<option value="${escapeHtml(model)}"${model === current ? ' selected' : ''}>${escapeHtml(model)}</option>`)
    );
    select.innerHTML = options.join('');
    select.value = current;
  };
  const fillUsageSearchAccounts = (accounts) => {
    const select = document.getElementById('usage-search-account');
    if (!select) return;
    const current = select.value;
    const options = ['<option value="">全部账号</option>'].concat(
      (accounts || []).map((account) => `<option value="${escapeHtml(account)}"${account === current ? ' selected' : ''}>${escapeHtml(account)}</option>`)
    );
    select.innerHTML = options.join('');
    select.value = current;
  };
  const usageSearchNumber = (value) => Number(value || 0).toLocaleString('zh-CN');
  const renderUsageSearchStats = (search, facets) => {
    const container = document.getElementById('usage-search-stats');
    if (!container) return;
    const totals = (search && search.totals) || {};
    const usage = totals.usage || {};
    const cost = totals.cost_usd;
    const scope = facets.records
      ? `索引 ${formatDay(facets.first_at)} ~ ${formatDay(facets.last_at)}`
      : '索引为空';
    container.innerHTML = [
      `<div class="stat"><div class="stat-label">匹配行</div><div class="stat-value">${escapeHtml(formatNumber(search.matched_rows || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.records || 0))} 条原始记录</div></div>`,
      `<div class="stat"><div class="stat-label">涉及会话</div><div class="stat-value">${escapeHtml(formatNumber(totals.sessions || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.models || 0))} 个模型</div></div>`,
      `<div class="stat"><div class="stat-label">输入 token</div><div class="stat-value">${escapeHtml(formatTokens(usage.input_tokens || 0))}</div><div class="stat-foot">其中缓存 ${escapeHtml(formatTokens(usage.cached_input_tokens || 0))}</div></div>`,
      `<div class="stat"><div class="stat-label">输出 token</div><div class="stat-value">${escapeHtml(formatTokens(usage.output_tokens || 0))}</div><div class="stat-foot">推理 ${escapeHtml(formatTokens(usage.reasoning_output_tokens || 0))}</div></div>`,
      `<div class="stat"><div class="stat-label">合计 token</div><div class="stat-value">${escapeHtml(formatTokens(totals.total_tokens || 0))}</div><div class="stat-foot">${escapeHtml(scope)}</div></div>`,
      `<div class="stat"><div class="stat-label">API 等价金额</div><div class="stat-value ${cost === null || cost === undefined ? '' : 'ok'}" style="font-size:17px">${cost === null || cost === undefined ? '部分无单价' : escapeHtml(formatUsdCompact(cost))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.records || 0))} 条记录合计</div></div>`
    ].join('');
  };
  const renderUsageSearch = (payload) => {
    const container = document.getElementById('usage-search-content');
    const countLabel = document.getElementById('usage-search-count');
    if (!container) return;
    usageSearchState = payload;
    const search = payload.search || {};
    const facets = payload.facets || {};
    fillUsageSearchModels(facets.models);
    fillUsageSearchAccounts(facets.accounts);
    renderUsageSearchStats(search, facets);
    if (search.available === false || facets.available === false) {
      const scope = facets.records
        ? `索引覆盖 ${escapeHtml(formatDay(facets.first_at))} ~ ${escapeHtml(formatDay(facets.last_at))}`
        : '用量索引还是空的，先让 daemon 完成一次索引再检索。';
      container.innerHTML = `<div class="empty-state"><span class="empty-title">没有可检索的用量索引</span><span class="empty-hint">${scope}</span></div>`;
      if (countLabel) countLabel.textContent = '索引为空';
      return;
    }
    const rows = Array.isArray(search.rows) ? search.rows : [];
    const totals = search.totals || {};
    if (countLabel) {
      countLabel.textContent = `${formatNumber(search.matched_rows)} 行 · ${formatNumber(totals.total_tokens)} token`;
    }
    const totalPages = Math.max(1, Math.ceil(Number(search.matched_rows || 0) / usageSearchLimit));
    if (rows.length === 0 && usageSearchPage > 0 && Number(search.matched_rows || 0) > 0) {
      // 中文注释：筛选变化后旧页码可能超出范围，收敛到末页重拉。
      usageSearchPage = totalPages - 1;
      refreshUsageSearch();
      return;
    }
    if (rows.length === 0) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">没有符合条件的用量记录</span><span class="empty-hint">若刚产生用量，索引可能仍在写入，可稍后重试。</span></div>';
      return;
    }
    const group = search.group || usageSearchGroup;
    // 中文注释：时间/合计/金额三列对应服务端排序（recent/tokens/cost），点表头即切换。
    const currentSort = usageSearchValue('usage-search-sort') || 'recent';
    const usSortHeader = (label, key, numeric) => {
      const active = currentSort === key;
      const classes = `${numeric ? 'num ' : ''}sortable${active ? ' sorted' : ''}`;
      return `<th class="${classes}" data-us-sort="${key}">${label}${active ? ' ▼' : ''}</th>`;
    };
    const header = group === 'date'
      ? `<tr>${usSortHeader('日期', 'recent')}<th>会话 / 模型</th><th class="num">输入（缓存）</th><th class="num">输出</th>${usSortHeader('合计 token', 'tokens', true)}${usSortHeader('估算金额', 'cost', true)}<th class="num">记录</th></tr>`
      : group === 'model'
        ? `<tr><th>模型</th><th>会话</th><th class="num">输入（缓存）</th><th class="num">输出</th>${usSortHeader('合计 token', 'tokens', true)}${usSortHeader('估算金额', 'cost', true)}<th class="num">记录</th></tr>`
        : group === 'account'
          ? `<tr><th>账号</th><th>产品 / 模型</th><th class="num">输入（缓存）</th><th class="num">输出</th>${usSortHeader('合计 token', 'tokens', true)}${usSortHeader('估算金额', 'cost', true)}<th class="num">记录</th></tr>`
          : `<tr>${usSortHeader('时间', 'recent')}<th>会话</th><th>模型</th><th>项目 / 工作目录</th><th class="num">输入（缓存）</th><th class="num">输出</th>${usSortHeader('合计 token', 'tokens', true)}${usSortHeader('估算金额', 'cost', true)}<th class="num">记录</th></tr>`;
    const body = rows.map((row) => {
      const usage = row.usage || {};
      const cost = row.estimated_cost_usd === null || row.estimated_cost_usd === undefined
        ? '<span class="muted">未计价</span>'
        : escapeHtml(formatUsdCompact(row.estimated_cost_usd));
      const tokens = `${escapeHtml(formatNumber(usage.input_tokens))}<div class="cell-sub">缓存 ${escapeHtml(formatNumber(usage.cached_input_tokens))}</div>`;
      const output = `${escapeHtml(formatNumber(usage.output_tokens))}<div class="cell-sub">推理 ${escapeHtml(formatNumber(usage.reasoning_output_tokens))}</div>`;
      const total = `${escapeHtml(formatNumber(row.total_tokens))}`;
      if (group === 'date') {
        return `<tr>
          <td><div class="cell-main">${escapeHtml(row.date)}</div><div class="cell-sub">最近 ${escapeHtml(formatTime(row.last_at))}</div></td>
          <td><div class="cell-main">${escapeHtml(formatNumber(row.records))} 条记录</div><div class="cell-sub">${escapeHtml(String((row.models || []).length))} 个模型</div></td>
          <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
        </tr>`;
      }
      if (group === 'model') {
        return `<tr>
          <td><div class="cell-main">${escapeHtml((row.models || ['未知模型'])[0])}</div><div class="cell-sub">最近 ${escapeHtml(formatTime(row.last_at))}</div></td>
          <td>${escapeHtml(formatNumber(row.records))} 条<div class="cell-sub">${escapeHtml(String((row.models || []).length))} 个会话/模型组合</div></td>
          <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
        </tr>`;
      }
      if (group === 'account') {
        const products = (row.products || []).map((product) => `<span class="chip">${escapeHtml(product)}</span>`).join(' ');
        return `<tr>
          <td><div class="cell-main">${escapeHtml(row.account || '未知账号')}</div><div class="cell-sub">${escapeHtml(row.account_id || row.account_key || '未记录账号 ID')}</div></td>
          <td><div>${escapeHtml(formatNumber(row.records))} 条记录</div><div class="cell-sub">${products || escapeHtml(String((row.models || []).length)) + ' 个模型'}</div></td>
          <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
        </tr>`;
      }
      const sessionLabel = row.session_id || (row.session_path || '').split('/').pop() || '未知会话';
      // 中文注释：账号可能是很长的 account_id（UUID），截断单行显示，完整值放 title。
      const accountCell = row.account
        ? `<div class="cell-sub truncate" title="${escapeHtml(row.account)}">${escapeHtml(row.account)} · ${escapeHtml(formatNumber(row.records))} 条</div>`
        : `<div class="cell-sub">${escapeHtml(formatNumber(row.records))} 条</div>`;
      // 中文注释：模型列已显示主模型，项目列的模型 chip 只补充其余模型，避免重复。
      const extraModels = (row.models || []).filter((model) => model && model !== row.model);
      const extraChips = extraModels.map((model) => `<span class="chip">${escapeHtml(model)}</span>`).join(' ');
      return `<tr>
        <td><div class="cell-main">${escapeHtml(formatTime(row.last_at))}</div><div class="cell-sub">${escapeHtml(row.date)}</div></td>
        <td><button class="chip-button" type="button" data-usage-session="${escapeHtml(row.session_id || '')}" title="下钻到该会话">${escapeHtml(String(sessionLabel).slice(0, 12))}</button>${accountCell}</td>
        <td>${escapeHtml(row.model || '—')}</td>
        <td><span class="truncate" title="${escapeHtml(row.project || '')}">${escapeHtml(row.project || '未知目录')}</span>${extraChips ? `<div class="cell-sub">${extraChips}</div>` : ''}</td>
        <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
      </tr>`;
    }).join('');
    const pager = totalPages > 1
      ? `<div class="table-actions"><button class="btn" type="button" data-us-page="-1" ${usageSearchPage === 0 ? 'disabled' : ''}>上一页</button><span class="muted">第 ${usageSearchPage + 1} / ${totalPages} 页 · 共 ${escapeHtml(formatNumber(search.matched_rows || 0))} 行</span><button class="btn" type="button" data-us-page="1" ${usageSearchPage >= totalPages - 1 ? 'disabled' : ''}>下一页</button></div>`
      : '';
    container.innerHTML = `<div class="table-wrap"><table class="tight usage-table">
        <thead>${header}</thead>
        <tbody>${body}</tbody>
      </table></div>${pager}`;
    container.querySelectorAll('[data-usage-session]').forEach((button) => {
      button.addEventListener('click', () => {
        const value = button.dataset.usageSession || '';
        if (!value) return;
        const input = document.getElementById('usage-search-keyword');
        if (input) input.value = value;
        usageSearchPage = 0;
        refreshUsageSearch();
      });
    });
    container.querySelectorAll('[data-us-page]').forEach((button) => {
      button.addEventListener('click', () => {
        usageSearchPage += Number(button.dataset.usPage || 0);
        refreshUsageSearch();
      });
    });
    container.querySelectorAll('[data-us-sort]').forEach((header) => {
      header.addEventListener('click', () => {
        const select = document.getElementById('usage-search-sort');
        if (select) select.value = header.dataset.usSort || 'recent';
        usageSearchPage = 0;
        refreshUsageSearch();
      });
    });
  };
  const refreshUsageSearch = async () => {
    const container = document.getElementById('usage-search-content');
    const button = document.getElementById('usage-search-load-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch(`/api/usage/search?${usageSearchQueryString()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderUsageSearch(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state">用量检索失败：${escapeHtml(error.message)}</div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  // 磁盘与会话管理：目录占用、归档与清理。
  let housekeepingState = null;
  // 中文注释：项目卡片默认只看前 5 个，展开状态跨刷新保持。
  let housekeepingProjectsExpanded = false;
  // 中文注释：会话表格的当前页码；切换筛选条件时归零，刷新时按总页数收敛。
  let housekeepingTablePage = 0;
  // 中文注释：会话表格的列排序；空值表示保持后端「体积 × 闲置时长」的默认顺序。
  let housekeepingSortKey = '';
  let housekeepingSortDir = -1;
  const housekeepingValue = (id, fallback) => {
    const element = document.getElementById(id);
    if (!element) return fallback;
    const raw = String(element.value || '').trim();
    if (!raw) return fallback;
    const number = Number(raw);
    return Number.isFinite(number) ? number : fallback;
  };
  const housekeepingCriteria = () => {
    const projectElement = document.getElementById('housekeeping-project');
    const project = projectElement ? String(projectElement.value || '').trim() : '';
    return {
      days: Math.max(1, Math.round(housekeepingValue('housekeeping-days', 30))),
      min_size_mb: Math.max(0, housekeepingValue('housekeeping-min-size', 0)),
      project
    };
  };
  const renderHousekeepingStats = (summary, payload) => {
    const container = document.getElementById('housekeeping-stats');
    if (!container) return;
    const totals = summary.totals || {};
    const preview = (payload && payload.preview) || summary.preview || {};
    const archives = (payload && payload.archives) || [];
    const reminderCount = (summary.reminders || []).length;
    const singleWarn = (summary.thresholds || {}).single_warn_bytes || 0;
    container.innerHTML = [
      `<div class="stat"><div class="stat-label">目录合计</div><div class="stat-value">${escapeHtml(formatDataSize(totals.bytes || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.directories || 0))} 个目录 · ${escapeHtml(formatNumber(totals.files || 0))} 个文件</div></div>`,
      `<div class="stat"><div class="stat-label">会话文件</div><div class="stat-value">${escapeHtml(formatDataSize(totals.session_bytes || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.session_files || 0))} 个会话文件</div></div>`,
      `<div class="stat"><div class="stat-label">可归档 / 清理</div><div class="stat-value ${Number(preview.count || 0) > 0 ? 'warn' : 'ok'}">${escapeHtml(formatNumber(preview.count || 0))} 个</div><div class="stat-foot">约 ${escapeHtml(formatDataSize(preview.bytes || 0))} · 跳过活动 ${escapeHtml(formatNumber(preview.skipped_active || 0))} 个</div></div>`,
      `<div class="stat"><div class="stat-label">已有归档</div><div class="stat-value">${escapeHtml(formatNumber(archives.length))} 份</div><div class="stat-foot">${escapeHtml(summary.archive_dir ? String(summary.archive_dir).split('/').slice(-2).join('/') : '未配置归档目录')}</div></div>`,
      `<div class="stat"><div class="stat-label">单目录阈值</div><div class="stat-value ${reminderCount ? 'danger' : 'ok'}" style="font-size:17px">${escapeHtml(formatDataSize(singleWarn || 0))}</div><div class="stat-foot">${reminderCount ? `${escapeHtml(formatNumber(reminderCount))} 条磁盘提醒` : '当前未超阈值'}</div></div>`
    ].join('');
  };
  const renderHousekeeping = (state) => {
    const container = document.getElementById('housekeeping-content');
    const countLabel = document.getElementById('housekeeping-count');
    if (!container) return;
    const summary = (state && state.housekeeping) || {};
    const directories = Array.isArray(summary.directories) ? summary.directories : [];
    const totals = summary.totals || {};
    if (summary.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">磁盘统计不可用</span><span class="empty-hint">监控进程未启动磁盘扫描，或状态目录不可读。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const preview = summary.preview || {};
    if (countLabel) {
      countLabel.textContent = `合计 ${formatDataSize(totals.bytes || 0)} · ${Number(preview.count || 0)} 个可归档`;
    }
    renderHousekeepingStats(summary, housekeepingState);
    const reminderBanners = (summary.reminders || []).map((reminder) => (
      `<div class="alert-banner ${reminder.level === 'danger' ? 'danger' : 'warn'}">${escapeHtml(reminder.message || reminder.title || '')}</div>`
    )).join('');
    const cards = directories.map((item) => {
      const bytes = Number(item.bytes || 0);
      const sessionBytes = Number(item.session_bytes || 0);
      const sessionShare = bytes > 0 ? Math.min(100, Math.round(sessionBytes / bytes * 100)) : 0;
      const ratio = bytes > 0
        ? `<div class="ratio" title="会话文件占 ${sessionShare}%"><span class="ratio-sessions" style="width:${sessionShare}%"></span><span class="ratio-other" style="width:${100 - sessionShare}%"></span></div>`
        : '';
      const children = (item.top_children || []).slice(0, 4).map((child) => (
        `<span class="chip">${escapeHtml(child.name)} ${escapeHtml(formatDataSize(child.bytes || 0))}</span>`
      )).join('');
      return `<article class="mini-card">
        <div class="mini-card-head">
          <div><div class="mini-card-title">${escapeHtml(item.label || '未知目录')}</div><div class="mini-card-path" title="${escapeHtml(item.path || '')}">${escapeHtml(item.path || '')}</div></div>
          <span class="pill ${item.cleanable ? 'ok' : 'other'}">${item.cleanable ? '可归档' : '仅统计'}</span>
        </div>
        <div class="mini-card-metrics">
          <div><div class="metric-label">占用</div><div class="metric-value">${escapeHtml(formatDataSize(bytes))}</div></div>
          <div><div class="metric-label">会话文件</div><div class="metric-value">${escapeHtml(formatDataSize(sessionBytes))}<div class="cell-sub">${escapeHtml(formatNumber(item.session_files || 0))} 个</div></div></div>
          <div><div class="metric-label">文件总数</div><div class="metric-value">${escapeHtml(formatNumber(item.files || 0))}</div></div>
        </div>
        ${ratio}
        ${children ? `<div class="chip-list">${children}</div>` : ''}
      </article>`;
    }).join('');
    const scanNote = summary.observed_at
      ? `上次扫描 ${escapeHtml(formatTime(summary.observed_at))}`
      : '尚未完成扫描';
    // 中文注释：按项目筛选时提示因此被跳过的其他项目文件数。
    const skippedProjectNote = Number(preview.skipped_project || 0) > 0
      ? `<div class="usage-note">按项目筛选：另有 ${escapeHtml(formatNumber(preview.skipped_project))} 个文件因属于其他项目被跳过。</div>`
      : '';
    container.innerHTML = `${reminderBanners}
      <div class="usage-note">按当前条件（${escapeHtml(String(preview.criteria ? preview.criteria.older_than_days : 30))} 天前、非活动）可归档或清理 ${escapeHtml(formatNumber(preview.count || 0))} 个文件，约 ${escapeHtml(formatDataSize(preview.bytes || 0))}；过新跳过 ${escapeHtml(formatNumber(preview.skipped_recent || 0))} 个。</div>
      ${skippedProjectNote}
      ${cards ? `<div class="account-subtitle"><span>按目录统计</span><span class="muted">${scanNote}</span></div><div class="card-grid">${cards}</div>` : '<div class="empty-state"><span class="empty-title">没有需要统计的目录</span></div>'}`;
  };
  const renderHousekeepingActions = () => {
    const container = document.getElementById('housekeeping-actions');
    if (!container) return;
    if (!housekeepingState) {
      container.innerHTML = '';
      return;
    }
    const payload = housekeepingState;
    const preview = payload.preview || {};
    const files = Array.isArray(preview.files) ? preview.files : [];
    const archives = Array.isArray(payload.archives) ? payload.archives : [];
    // 中文注释：选中项目时展示该项目的全部会话（含状态与行级操作），
    // 否则只列出符合当前条件、可批量处理的会话。
    const projectSessions = Array.isArray(payload.project_sessions) ? payload.project_sessions : null;
    const selectedProject = housekeepingCriteria().project;
    const sessionButtons = (path) => `<button class="btn mini" type="button" data-hk-action="archive" data-hk-session="${escapeHtml(path)}">归档</button> <button class="btn mini danger" type="button" data-hk-action="clean" data-hk-session="${escapeHtml(path)}">清理</button>`;
    let tableTitle = '待处理会话';
    let tableMeta = `${escapeHtml(formatNumber(preview.count || 0))} 个 · ${escapeHtml(formatDataSize(preview.bytes || 0))}`;
    let table = '';
    // 中文注释：两个列表共用一个分页器；默认保持后端「体积 × 闲置时长」降序，
    // 点击表头后在前端按列排序（原始数组不动，取消排序可回到默认顺序）。
    const rawSource = projectSessions || files;
    const source = housekeepingSortKey
      ? [...rawSource].sort((left, right) => {
          let result = 0;
          if (housekeepingSortKey === 'modified') result = Number(left.modified_at || 0) - Number(right.modified_at || 0);
          else if (housekeepingSortKey === 'size') result = Number(left.size || 0) - Number(right.size || 0);
          else if (housekeepingSortKey === 'project') result = String(left.project || '').localeCompare(String(right.project || ''));
          else if (housekeepingSortKey === 'path') result = String(left.path || '').localeCompare(String(right.path || ''));
          return result * housekeepingSortDir;
        })
      : rawSource;
    // 中文注释：可排序表头；当前排序列带箭头，数字列保持右对齐。
    const hkSortHeader = (label, key, numeric) => {
      const active = housekeepingSortKey === key;
      const classes = `${numeric ? 'num ' : ''}sortable${active ? ' sorted' : ''}`;
      const arrow = active ? (housekeepingSortDir === 1 ? ' ▲' : ' ▼') : '';
      return `<th class="${classes}" data-hk-sort="${key}">${label}${arrow}</th>`;
    };
    const pageSize = 10;
    const maxPage = Math.max(0, Math.ceil(source.length / pageSize) - 1);
    housekeepingTablePage = Math.min(Math.max(0, housekeepingTablePage), maxPage);
    const pageItems = source.slice(housekeepingTablePage * pageSize, (housekeepingTablePage + 1) * pageSize);
    const pager = source.length > pageSize
      ? `<div class="table-actions"><button class="btn" type="button" data-hk-page="-1" ${housekeepingTablePage === 0 ? 'disabled' : ''}>上一页</button><span class="muted">第 ${housekeepingTablePage + 1} / ${maxPage + 1} 页 · 共 ${escapeHtml(formatNumber(source.length))} 个</span><button class="btn" type="button" data-hk-page="1" ${housekeepingTablePage >= maxPage ? 'disabled' : ''}>下一页</button></div>`
      : '';
    if (projectSessions) {
      tableTitle = `项目 ${selectedProject} 的会话`;
      tableMeta = `共 ${escapeHtml(formatNumber(projectSessions.length))} 个 · 符合当前条件 ${escapeHtml(formatNumber(preview.count || 0))} 个`;
      const projectRows = pageItems.map((item) => {
        const state = item.archive_state || {};
        const status = state.eligible
          ? '<span class="pill ok">可归档</span>'
          : `<span class="pill other">${escapeHtml(state.reason || '暂不可归档')}</span>`;
        return `<tr>
      <td><div class="cell-main">${escapeHtml(formatTime(item.modified_at))}</div><div class="cell-sub">${escapeHtml(String(item.session_id || '').slice(0, 12))}</div></td>
      <td><span class="truncate" title="${escapeHtml(item.path || '')}">${escapeHtml(item.path || '')}</span></td>
      <td class="num">${escapeHtml(formatDataSize(item.size || 0))}</td>
      <td>${status}</td>
      <td>${state.eligible ? sessionButtons(item.path || '') : ''}</td>
    </tr>`;
      }).join('');
      table = projectRows
        ? `<div class="table-wrap"><table class="tight"><thead><tr>${hkSortHeader('最后修改', 'modified')}${hkSortHeader('会话文件', 'path')}${hkSortHeader('大小', 'size', true)}<th>状态</th><th>操作</th></tr></thead><tbody>${projectRows}</tbody></table></div>${pager}`
        : '<div class="empty-state"><span class="empty-title">该项目下没有会话文件</span></div>';
    } else {
      const rows = pageItems.map((item) => `<tr>
      <td><div class="cell-main">${escapeHtml(formatTime(item.modified_at))}</div><div class="cell-sub">${escapeHtml(String(item.session_id || '').slice(0, 12))}</div></td>
      <td><span class="truncate" title="${escapeHtml(item.project || '')}">${escapeHtml(item.project || '未知项目')}</span></td>
      <td><span class="truncate" title="${escapeHtml(item.path || '')}">${escapeHtml(item.path || '')}</span></td>
      <td class="num">${escapeHtml(formatDataSize(item.size || 0))}</td>
      <td>${sessionButtons(item.path || '')}</td>
    </tr>`).join('');
      table = rows
        ? `<div class="table-wrap"><table class="tight"><thead><tr>${hkSortHeader('最后修改', 'modified')}${hkSortHeader('项目', 'project')}${hkSortHeader('会话文件', 'path')}${hkSortHeader('大小', 'size', true)}<th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>${pager}`
        : '<div class="empty-state"><span class="empty-title">当前条件下没有可处理的会话</span><span class="empty-hint">放宽保留天数或降低体积下限再试。</span></div>';
    }
    const archiveCards = archives.map((item) => `<article class="mini-card">
      <div class="mini-card-head">
        <div><div class="mini-card-title">${escapeHtml(String(item.archive || '').split('/').pop() || '')}</div><div class="mini-card-path">${escapeHtml(item.created_at ? formatTime(item.created_at) : '时间未知')}</div></div>
        <button class="btn mini" type="button" data-housekeeping-restore="${escapeHtml(String(item.archive || '').split('/').pop() || '')}">恢复到原路径</button>
      </div>
      <div class="mini-card-metrics">
        <div><div class="metric-label">文件数</div><div class="metric-value">${escapeHtml(item.count === null || item.count === undefined ? '—' : formatNumber(item.count))}</div></div>
        <div><div class="metric-label">归档大小</div><div class="metric-value">${escapeHtml(formatDataSize(item.bytes || 0))}</div></div>
      </div>
    </article>`).join('');
    container.innerHTML = `<div class="account-subtitle"><span>${escapeHtml(tableTitle)}</span><span class="muted">${tableMeta}</span></div>
      <div class="usage-note">归档会先打包 tar.gz 并写 manifest，校验通过后才删除原文件，可随时恢复；直接清理不可撤销。活动会话、10 分钟内改动过的文件会始终跳过。<br>列表按体积 × 闲置时长排序，最久未用且占用最大的排在最前。</div>
      ${table}
      ${archives.length ? `<div class="account-subtitle"><span>已有归档</span><span class="muted">${escapeHtml(formatNumber(archives.length))} 份</span></div><div class="card-grid compact-grid">${archiveCards}</div>` : ''}`;
    container.querySelectorAll('[data-hk-session]').forEach((button) => {
      button.addEventListener('click', () => {
        const path = button.dataset.hkSession || '';
        const action = button.dataset.hkAction || 'archive';
        if (!path) return;
        if (action === 'clean') {
          if (!window.confirm(`确认直接删除这个会话文件？删除不可撤销，建议先归档。\n${path}`)) return;
        } else if (!window.confirm(`确认把这个会话压缩归档（tar.gz）并删除原文件？可在「磁盘与会话管理」里恢复。\n${path}`)) return;
        manageSession(action, path);
      });
    });
    container.querySelectorAll('[data-hk-page]').forEach((button) => {
      button.addEventListener('click', () => {
        housekeepingTablePage += Number(button.dataset.hkPage || 0);
        renderHousekeepingActions();
      });
    });
    container.querySelectorAll('[data-hk-sort]').forEach((header) => {
      header.addEventListener('click', () => {
        const key = header.dataset.hkSort || '';
        if (housekeepingSortKey === key) {
          housekeepingSortDir = -housekeepingSortDir;
        } else {
          housekeepingSortKey = key;
          // 中文注释：时间/大小默认降序（最新、最大在前），文本列默认升序。
          housekeepingSortDir = key === 'project' || key === 'path' ? 1 : -1;
        }
        housekeepingTablePage = 0;
        renderHousekeepingActions();
      });
    });
    container.querySelectorAll('[data-housekeeping-restore]').forEach((button) => {
      button.addEventListener('click', () => {
        const name = button.dataset.housekeepingRestore || '';
        if (!window.confirm(`确认从归档 ${name} 恢复会话文件到原始路径？`)) return;
        mutateHousekeeping('restore', { archive: name });
      });
    });
  };
  // 中文注释：按项目聚合的归档入口 + 项目筛选下拉；响应里没有 projects 字段时保持现状。
  const renderHousekeepingProjects = (projects) => {
    if (!Array.isArray(projects)) return;
    const select = document.getElementById('housekeeping-project');
    if (select) {
      const current = select.value;
      select.innerHTML = '<option value="">全部项目</option>' + projects.map((item) => (
        `<option value="${escapeHtml(item.project)}">${escapeHtml(item.project)} — ${escapeHtml(formatNumber(item.files || 0))} 个文件 / ${escapeHtml(formatDataSize(item.bytes || 0))}</option>`
      )).join('');
      // 中文注释：刷新后保留选中值；项目已消失则回退「全部项目」。
      select.value = projects.some((item) => item.project === current) ? current : '';
    }
    const container = document.getElementById('housekeeping-projects');
    if (!container) return;
    if (!projects.length) {
      container.innerHTML = '';
      return;
    }
    // 中文注释：默认只展示占用最大的前 5 个项目，其余折叠。
    const visible = housekeepingProjectsExpanded ? projects : projects.slice(0, 5);
    const cards = visible.map((item) => `<article class="mini-card">
      <div class="mini-card-head">
        <div><div class="mini-card-title truncate" title="${escapeHtml(item.project)}">${escapeHtml(item.project)}</div><div class="mini-card-path">${escapeHtml(formatTime(item.oldest_at))} → ${escapeHtml(formatTime(item.newest_at))}</div></div>
      </div>
      <div class="mini-card-metrics">
        <div><div class="metric-label">会话文件</div><div class="metric-value">${escapeHtml(formatNumber(item.files || 0))}</div></div>
        <div><div class="metric-label">占用</div><div class="metric-value">${escapeHtml(formatDataSize(item.bytes || 0))}</div></div>
        ${item.selected_files !== undefined ? `<div><div class="metric-label">符合当前条件</div><div class="metric-value">${escapeHtml(formatNumber(item.selected_files))}<div class="cell-sub">${escapeHtml(formatDataSize(item.selected_bytes || 0))}</div></div></div>` : ''}
      </div>
      <div class="mini-card-actions"><button class="btn mini warn" type="button" data-housekeeping-project="${escapeHtml(item.project)}">压缩归档</button><button class="btn mini danger" type="button" data-housekeeping-project-clean="${escapeHtml(item.project)}">直接清理</button></div>
    </article>`).join('');
    const hiddenCount = projects.length - visible.length;
    const toggle = projects.length > 5
      ? `<div class="table-actions"><button id="housekeeping-projects-toggle" class="btn" type="button">${housekeepingProjectsExpanded ? '收起其余项目' : `展开其余 ${escapeHtml(formatNumber(hiddenCount))} 个项目`}</button></div>`
      : '';
    container.innerHTML = `<div class="account-subtitle"><span>按项目归档</span><span class="muted">${escapeHtml(formatNumber(projects.length))} 个项目</span></div><div class="card-grid compact-grid">${cards}</div>${toggle}`;
    container.querySelectorAll('[data-housekeeping-project]').forEach((button) => {
      button.addEventListener('click', () => {
        const project = button.dataset.housekeepingProject || '';
        if (!project) return;
        if (!window.confirm(housekeepingProjectAllConfirm('archive', project))) return;
        mutateHousekeeping('archive', { project, all_sessions: true });
      });
    });
    container.querySelectorAll('[data-housekeeping-project-clean]').forEach((button) => {
      button.addEventListener('click', () => {
        const project = button.dataset.housekeepingProjectClean || '';
        if (!project) return;
        if (!window.confirm(housekeepingProjectAllConfirm('clean', project))) return;
        mutateHousekeeping('clean', { project, all_sessions: true });
      });
    });
    document.getElementById('housekeeping-projects-toggle')?.addEventListener('click', () => {
      housekeepingProjectsExpanded = !housekeepingProjectsExpanded;
      renderHousekeepingProjects(projects);
    });
  };
  const refreshHousekeeping = async () => {
    const criteria = housekeepingCriteria();
    const button = document.getElementById('housekeeping-scan-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const projectQuery = criteria.project ? `&project=${encodeURIComponent(criteria.project)}` : '';
      const response = await fetch(`/api/housekeeping?days=${criteria.days}&min_size_mb=${criteria.min_size_mb}&refresh=1${projectQuery}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      housekeepingState = await response.json();
      renderHousekeepingProjects(housekeepingState.projects);
      if (latestState) renderHousekeeping(latestState);
      renderHousekeepingActions();
      refresh();
    } catch (error) {
      const container = document.getElementById('housekeeping-content');
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">磁盘扫描失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  const housekeepingNote = (text) => {
    const container = document.getElementById('housekeeping-actions');
    if (!container) return null;
    const note = document.createElement('div');
    note.className = 'alert-banner info action-result';
    note.id = 'housekeeping-task-note';
    const previous = document.getElementById('housekeeping-task-note');
    if (previous) {
      previous.replaceWith(note);
    } else {
      container.prepend(note);
    }
    note.textContent = text;
    return note;
  };
  const pollHousekeepingTask = async (taskId, action, report) => {
    const label = action === 'archive' ? '归档' : '清理';
    const show = report || housekeepingNote;
    const deadline = Date.now() + 30 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 800));
      let payload = null;
      try {
        const response = await fetch(`/api/housekeeping?task=${taskId}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
      } catch (error) {
        show(`${label}进度查询失败：${error.message}`, 'danger');
        return;
      }
      const task = payload.task;
      if (!task) {
        await refreshHousekeeping();
        show(`${label}任务已结束（任务记录已被清理）。`, 'info');
        return;
      }
      const progress = task.progress || {};
      if (task.state === 'running') {
        const phase = progress.phase === 'compress' ? '压缩'
          : progress.phase === 'verify' ? '校验归档'
            : progress.phase === 'delete' ? '删除原文件'
              : '准备文件清单';
        const percent = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;
        show(`${label}进行中：${phase} ${progress.done}/${progress.total}（${percent}%），已处理 ${formatDataSize(progress.bytes_done || 0)} / ${formatDataSize(progress.total_bytes || 0)}。`, 'info');
        continue;
      }
      if (task.state === 'failed') {
        await refreshHousekeeping();
        show(`${label}失败：${task.error || '未知原因'}`, 'danger');
        return;
      }
      const result = task.result || {};
      const detail = `${label}完成：${result.count || 0} 个文件，释放 ${formatDataSize(result.bytes || 0)}${result.archive ? `，归档 ${String(result.archive).split('/').pop()}` : ''}。`;
      await refreshHousekeeping();
      show(detail, 'info');
      return;
    }
    await refreshHousekeeping();
    show(`${label}仍在后台执行，可稍后刷新查看结果。`, 'info');
  };
  const mutateHousekeeping = async (action, body) => {
    const criteria = housekeepingCriteria();
    const container = document.getElementById('housekeeping-actions');
    const background = action === 'archive' || action === 'clean';
    try {
      const response = await fetch('/api/housekeeping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, days: criteria.days, min_size_mb: criteria.min_size_mb, project: criteria.project || undefined, confirm: true, async: background, ...body })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      if (background && payload.task) {
        housekeepingNote(`${action === 'archive' ? '归档' : '清理'}已提交到后台执行…`);
        await pollHousekeepingTask(payload.task.id, action);
        return;
      }
      const changed = (payload.result && payload.result.count) || 0;
      const freed = (payload.result && payload.result.bytes) || 0;
      housekeepingState = { ...payload };
      renderHousekeepingProjects(housekeepingState.projects);
      if (latestState) renderHousekeeping(latestState);
      renderHousekeepingActions();
      housekeepingNote(
        action === 'restore'
          ? `已恢复 ${payload.result.restored} 个会话文件到 ${payload.result.destination}。`
          : `已完成${action === 'archive' ? '归档' : '清理'}：${changed} 个文件，释放 ${formatDataSize(freed)}。`
      );
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  // 中文注释：归档/清理的确认文案按是否选中项目分整句，便于整句翻译（词序不同）。
  const housekeepingConfirm = (action, criteria) => {
    if (action === 'archive') {
      return criteria.project
        ? `确认把项目 ${criteria.project} 下、${criteria.days} 天前、非活动的会话压缩归档（tar.gz）并删除原文件？归档在后台执行，可在本页恢复。`
        : `确认把 ${criteria.days} 天前、非活动的会话压缩归档（tar.gz）并删除原文件？归档在后台执行，可在本页恢复。`;
    }
    return criteria.project
      ? `确认直接删除项目 ${criteria.project} 下、${criteria.days} 天前、非活动的会话文件？删除在后台执行且不可撤销，建议先归档。`
      : `确认直接删除 ${criteria.days} 天前、非活动的会话文件？删除在后台执行且不可撤销，建议先归档。`;
  };
  const requestHousekeepingArchive = () => {
    const criteria = housekeepingCriteria();
    if (!window.confirm(housekeepingConfirm('archive', criteria))) return;
    mutateHousekeeping('archive');
  };
  // 中文注释：项目卡片的一键操作覆盖该项目全部非活动会话，不看保留天数与体积下限。
  const housekeepingProjectAllConfirm = (action, project) => (
    action === 'archive'
      ? `确认把项目 ${project} 下的全部非活动会话压缩归档（tar.gz）并删除原文件？不看保留天数与体积下限；活动会话与 10 分钟内写入的文件仍会跳过。归档在后台执行，可在本页恢复。`
      : `确认直接删除项目 ${project} 下的全部非活动会话文件？不看保留天数与体积下限；活动会话与 10 分钟内写入的文件仍会跳过。删除在后台执行且不可撤销，建议先归档。`
  );
  // 中文注释：磁盘与会话管理表格里的行级归档/清理，进度提示写回本区域。
  const manageSession = async (action, path) => {
    housekeepingNote(action === 'archive' ? '正在归档会话…' : '正在清理会话…');
    try {
      const response = await fetch('/api/housekeeping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, session: path, confirm: true, async: true })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      if (payload.task) {
        await pollHousekeepingTask(payload.task.id, action);
        return;
      }
      await refreshHousekeeping();
    } catch (error) {
      housekeepingNote(action === 'archive' ? `归档该会话失败：${error.message}` : `清理该会话失败：${error.message}`);
    }
  };
  // 单会话归档的进度显示在页面顶部关注区，折叠的磁盘分区里看不到提示。
  const archiveNotice = (text, level) => {
    // 中文注释：单独一个容器，避免被每 5 秒的 renderAlerts 覆盖。
    const container = document.getElementById('session-notice');
    if (!container) return;
    let note = document.getElementById('session-archive-note');
    if (!note) {
      note = document.createElement('div');
      note.id = 'session-archive-note';
      container.append(note);
    }
    note.className = `alert-row ${level === 'danger' ? 'danger' : 'warn'}`;
    note.innerHTML = `<span class="alert-accent" aria-hidden="true"></span><div class="alert-body"><div class="alert-title">${escapeHtml(text)}</div><div class="alert-detail">来自活动会话表的「归档此会话」，归档文件可在磁盘与会话管理里恢复。</div></div><a class="alert-link" href="#housekeeping">查看磁盘与会话管理</a>`;
  };
  const archiveSession = async (path) => {
    archiveNotice('正在归档会话…', 'warn');
    try {
      const response = await fetch('/api/housekeeping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'archive', session: path, confirm: true, async: true })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      if (payload.task) {
        await pollHousekeepingTask(payload.task.id, 'archive', archiveNotice);
      }
      refresh();
    } catch (error) {
      archiveNotice(`归档该会话失败：${error.message}`, 'danger');
    }
  };
  const renderAccounts = (state) => {
    const container = document.getElementById('account-list');
    const quotas = Array.isArray(state.quotas) ? state.quotas : [];
    const sessions = Array.isArray(state.sessions) ? state.sessions : [];
    const configuredAccounts = Array.isArray(state.accounts) ? state.accounts : [];
    // 中文注释：本区展示的是额度窗口，所以没有任何额度窗口的订阅（DeepSeek Harness、
    // Claude Code 这类没有额度概念的 provider）整张卡片都不出现，计数也只算显示出来的
    // 订阅——否则会看到一排「0 个窗口」的空卡片。
    const accountsWithQuota = new Set(
      quotas
        .filter((quota) => (quota.windows || []).length > 0)
        .map((quota) => quota.account || 'codex')
    );
    const names = [...new Set([
      ...configuredAccounts.map((account) => account.name),
      ...quotas.map((quota) => quota.account || 'codex'),
      ...sessions.map((session) => session.account || 'codex')
    ])].filter((name) => accountsWithQuota.has(name));
    document.getElementById('quota-source').textContent = names.length ? `${names.length} 个账号` : '暂无额度订阅';
    document.getElementById('account-section-count').textContent = names.length ? `${names.length} 个账号` : '暂无额度订阅';
    if (names.length === 0) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">没有带额度窗口的订阅</span><span class="empty-hint">没有额度概念的 provider（例如 DeepSeek Harness、Claude Code）不在本区显示，它们的使用量仍计入「用量与成本估算」。</span></div>';
      return;
    }
    container.innerHTML = names.map((name) => {
      const account = configuredAccounts.find((item) => item.name === name) || {};
      const accountQuotas = quotas.filter((quota) => (quota.account || 'codex') === name);
      const accountSessions = sessions.filter((session) => (session.account || 'codex') === name);
      const accountCounts = account.counts || {};
      const profiles = (account.profiles || []).map((profile) => typeof profile === 'string' ? profile : profile.name).filter((profile) => profile).join(' · ');
      const accountId = account.account_id || name;
      const product = accountProductLabel(account);
      const plans = distinctSorted(accountQuotas.map((quota) => planLabel(quota.plan_type)).concat([planLabel(account.plan_type)]));
      const subscription = plans.length ? `${product} · ${plans.join(' / ')}` : product;
      const activeCount = accountCounts.active ?? accountSessions.length;
      const accountSource = accountQuotas.map((quota) => quota.source).filter((source) => source).join(' · ');
      return `<article class="account-block">
        <div class="account-heading">
          <div class="account-identity"><div class="account-avatar" aria-hidden="true" title="${escapeHtml(subscription)}">${escapeHtml(subscriptionInitials(subscription))}</div><div><div class="account-label">订阅</div><h3 class="account-title">${escapeHtml(subscription)}</h3><div class="account-meta">Account ID：<span class="mono">${escapeHtml(accountId)}</span>${profiles ? ` · Profile：${escapeHtml(profiles)}` : ''}${accountSource ? ` · 来源：${escapeHtml(accountSource)}` : ''}</div></div></div>
          <div class="account-side"><span class="account-activity">${escapeHtml(activeCount)} 个活动</span></div>
        </div>
        <div class="account-subtitle"><span>额度窗口</span><span class="muted">${accountQuotas.length} 个窗口</span></div>
        <div class="quota-rows-block">${renderQuotaRows(accountQuotas)}</div>
        <div class="account-subtitle"><span>活动会话</span><span class="muted">${activeCount} 个活动</span></div>
        ${renderSessionTable(accountSessions, name)}
      </article>`;
    }).join('');
    container.querySelectorAll('[data-archive-session]').forEach((button) => {
      button.addEventListener('click', () => {
        const path = button.dataset.archiveSession || '';
        if (!path) return;
        if (!window.confirm(`确认把这个会话压缩归档（tar.gz）并删除原文件？可在「磁盘与会话管理」里恢复。\n${path}`)) return;
        archiveSession(path);
      });
    });
    container.querySelectorAll('[data-session-toggle]').forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.dataset.sessionToggle || '';
        const wrap = button.previousElementSibling;
        const willExpand = !expandedSessionTables.has(key);
        if (willExpand) {
          expandedSessionTables.add(key);
        } else {
          expandedSessionTables.delete(key);
        }
        if (wrap) wrap.style.display = willExpand ? '' : 'none';
        button.setAttribute('aria-expanded', String(willExpand));
        const rows = wrap ? wrap.querySelectorAll('tbody tr').length : 0;
        button.textContent = willExpand ? '收起会话列表' : `展开 ${rows} 个活动会话`;
      });
    });
  };
  // 运行健康徽标：读取 state.health，可点击展开未正常组件的明细。
  const healthStatusMeta = {
    ok: { label: '正常', className: 'ok' },
    starting: { label: '启动中', className: 'starting' },
    degraded: { label: '部分降级', className: 'degraded' },
    failed: { label: '异常', className: 'failed' }
  };
  const healthComponentStatusLabel = { ok: '正常', starting: '启动中', degraded: '数据过期', failed: '失败' };
  const unhealthyComponents = (health) => {
    const components = health && Array.isArray(health.components) ? health.components : [];
    return components.filter((component) => component && component.status !== 'ok');
  };
  const renderHealthPanel = () => {
    const panel = document.getElementById('health-detail');
    if (!panel) return;
    const unhealthy = unhealthyComponents(latestHealthState);
    if (!unhealthy.length) {
      panel.innerHTML = '';
      panel.style.display = 'none';
      return;
    }
    const items = unhealthy.map((component) => {
      const statusLabel = healthComponentStatusLabel[component.status] || component.status || '未知';
      const successAt = component.last_success_at ? `${formatRelativeTime(component.last_success_at)}（${formatTime(component.last_success_at)}）` : '从未成功';
      const errorNote = component.last_error ? `<div class="cell-sub">${escapeHtml(component.last_error)}</div>` : '';
      return `<li><strong>${escapeHtml(component.label || component.key)}</strong> · ${escapeHtml(statusLabel)} · 上次成功 ${escapeHtml(successAt)}${errorNote}</li>`;
    }).join('');
    panel.innerHTML = `<div>以下组件未处于正常状态：</div><ul>${items}</ul>`;
  };
  const renderHealth = (state) => {
    const badge = document.getElementById('health-indicator');
    const panel = document.getElementById('health-detail');
    if (!badge) return;
    latestHealthState = state && state.health && typeof state.health === 'object' ? state.health : null;
    if (!latestHealthState || !latestHealthState.overall) {
      badge.style.display = 'none';
      badge.setAttribute('aria-expanded', 'false');
      if (panel) {
        panel.innerHTML = '';
        panel.style.display = 'none';
      }
      return;
    }
    const meta = healthStatusMeta[latestHealthState.overall] || healthStatusMeta.failed;
    badge.className = `health-badge ${meta.className}`;
    badge.innerHTML = `<span class="health-dot"></span><span>${escapeHtml(meta.label)}</span>`;
    badge.style.display = '';
    badge.title = `运行健康：${meta.label}（点击查看组件明细）`;
    if (panel && panel.style.display !== 'none') {
      renderHealthPanel();
      if (panel.innerHTML) {
        panel.style.display = 'block';
      }
    }
  };
  let refreshInFlight = false;
  const refresh = async () => {
    // 中文注释：慢请求期间不叠加轮询，避免旧响应覆盖新状态。
    if (refreshInFlight) return;
    refreshInFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    const refreshButton = document.getElementById('refresh-button');
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.classList.add('is-spinning');
    }
    try {
      const response = await fetch('/api/state', { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      latestState = state;
      const counts = state.counts || {};
      const accounts = Array.isArray(state.accounts) ? state.accounts : [];
      document.getElementById('active-count').textContent = counts.active ?? 0;
      document.getElementById('process-count').textContent = counts.process_backed ?? 0;
      document.getElementById('quota-window-count').textContent = Array.isArray(state.quotas) ? state.quotas.reduce((total, quota) => total + (Array.isArray(quota.windows) ? quota.windows.length : 0), 0) : 0;
      document.getElementById('accounts-online').textContent = accounts.length;
      if (!latestUsageState) document.getElementById('spend-count').textContent = '按需加载';
      const trafficTotals = (state.traffic && state.traffic.totals) || {};
      const uploadBurst = document.getElementById('upload-burst-count');
      const uploadAlerts = document.getElementById('upload-alert-count');
      if (uploadBurst) uploadBurst.textContent = formatDataSize(trafficTotals.burst_bytes || 0);
      if (uploadAlerts) {
        const alertHistory = state.alert_history || {};
        uploadAlerts.textContent = alertHistory.available ? (alertHistory.unread ?? 0) : (trafficTotals.alert_count ?? 0);
      }
      document.getElementById('service-sync').textContent = `已同步 ${formatTime(state.updated_at)}`;
      document.getElementById('service-state').textContent = '监控服务在线';
      renderHealth(state);
      document.querySelectorAll('.status-dot').forEach((dot) => dot.classList.remove('error'));
      renderAccounts(state);
      renderTraffic(state);
      renderHousekeeping(state);
      renderSectionSummaries(state);
      renderAlerts();
      if (alertHistoryState) refreshAlertHistory();
      document.getElementById('error').style.display = 'none';
    } catch (error) {
      const box = document.getElementById('error');
      box.textContent = `读取监控状态失败：${error.message}`;
      box.style.display = 'block';
      document.getElementById('service-state').textContent = '监控服务连接异常';
      document.getElementById('service-sync').textContent = '同步失败';
      document.querySelectorAll('.status-dot').forEach((dot) => dot.classList.add('error'));
    } finally {
      window.clearTimeout(timeout);
      refreshInFlight = false;
      if (refreshButton) {
        refreshButton.disabled = false;
        refreshButton.classList.remove('is-spinning');
      }
    }
  };
  document.getElementById('refresh-button')?.addEventListener('click', refresh);
  document.getElementById('health-indicator')?.addEventListener('click', () => {
    const badge = document.getElementById('health-indicator');
    const panel = document.getElementById('health-detail');
    if (!badge || !panel) return;
    const expanded = panel.style.display !== 'none';
    if (expanded) {
      panel.style.display = 'none';
      badge.setAttribute('aria-expanded', 'false');
      return;
    }
    renderHealthPanel();
    const visible = Boolean(panel.innerHTML);
    panel.style.display = visible ? 'block' : 'none';
    badge.setAttribute('aria-expanded', String(visible));
  });
  document.getElementById('usage-load-button')?.addEventListener('click', refreshUsage);
  document.getElementById('insights-load-button')?.addEventListener('click', refreshInsights);
  document.getElementById('alert-history-load-button')?.addEventListener('click', () => {
    alertHistoryPage = 0;
    refreshAlertHistory();
  });
  ['alert-range-filter', 'alert-level-filter', 'alert-kind-filter', 'alert-ack-filter'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', () => {
      alertHistoryPage = 0;
      refreshAlertHistory();
    });
  });
  document.getElementById('alert-keyword-filter')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      alertHistoryPage = 0;
      refreshAlertHistory();
    }
  });
  document.getElementById('alert-ack-all-button')?.addEventListener('click', () => mutateAlerts('ack', { all: true }));
  document.getElementById('alert-clear-button')?.addEventListener('click', clearAlertHistory);
  document.getElementById('usage-search-load-button')?.addEventListener('click', () => {
    usageSearchPage = 0;
    refreshUsageSearch();
  });
  document.getElementById('usage-search-refresh-button')?.addEventListener('click', () => {
    usageSearchPage = 0;
    refreshUsageSearch();
  });
  document.querySelectorAll('[data-usage-search-group]').forEach((tab) => {
    tab.addEventListener('click', () => {
      usageSearchGroup = tab.dataset.usageSearchGroup || 'session';
      document.querySelectorAll('[data-usage-search-group]').forEach((item) => item.classList.toggle('selected', item === tab));
      usageSearchPage = 0;
      refreshUsageSearch();
    });
  });
  ['usage-search-range', 'usage-search-model', 'usage-search-account', 'usage-search-sort', 'usage-search-from', 'usage-search-to'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', () => {
      usageSearchPage = 0;
      refreshUsageSearch();
    });
  });
  document.getElementById('usage-search-keyword')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      usageSearchPage = 0;
      refreshUsageSearch();
    }
  });
  document.getElementById('housekeeping-scan-button')?.addEventListener('click', refreshHousekeeping);
  document.getElementById('housekeeping-preview-button')?.addEventListener('click', refreshHousekeeping);
  document.getElementById('housekeeping-archive-button')?.addEventListener('click', requestHousekeepingArchive);
  document.getElementById('housekeeping-clean-button')?.addEventListener('click', () => {
    const criteria = housekeepingCriteria();
    if (!window.confirm(housekeepingConfirm('clean', criteria))) return;
    mutateHousekeeping('clean');
  });
  ['housekeeping-days', 'housekeeping-min-size', 'housekeeping-project'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', () => {
      housekeepingTablePage = 0;
      refreshHousekeeping();
    });
  });
  document.querySelectorAll('[data-insights-days]').forEach((tab) => {
    tab.addEventListener('click', () => {
      insightsPeriodDays = tab.dataset.insightsDays || '';
      document.querySelectorAll('[data-insights-days]').forEach((item) => item.classList.toggle('selected', item === tab));
      latestInsightsState = null;
      refreshInsights();
    });
  });
  DEFAULT_COLLAPSED.forEach(applySectionState);
  collapsedSections.forEach((id) => applySectionState(id));
  document.querySelectorAll('[data-section-toggle]').forEach((toggle) => {
    toggle.addEventListener('click', () => {
      const id = toggle.dataset.sectionToggle;
      setSectionCollapsed(id, !collapsedSections.has(id));
    });
  });
  navLinks.forEach((link) => {
    link.addEventListener('click', () => {
      const id = link.dataset.navTarget;
      if (collapsedSections.has(id)) setSectionCollapsed(id, false);
    });
  });
__THEME_SCRIPT__
  refresh();
  ['alert-history', 'usage-search', 'housekeeping'].forEach(ensureSectionLoaded);
  mainRefreshTimer = window.setInterval(refresh, 5000);
  // 中文注释：标签页隐藏时暂停自动刷新和索引轮询，回到前台立即补一次刷新。
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (mainRefreshTimer) {
        window.clearInterval(mainRefreshTimer);
        mainRefreshTimer = 0;
      }
      if (usagePollTimer) {
        window.clearTimeout(usagePollTimer);
        usagePollTimer = 0;
        usagePollSuspended = true;
      }
      if (insightsPollTimer) {
        window.clearTimeout(insightsPollTimer);
        insightsPollTimer = 0;
        insightsPollSuspended = true;
      }
      return;
    }
    if (!mainRefreshTimer) mainRefreshTimer = window.setInterval(refresh, 5000);
    refresh();
    if (usagePollSuspended) {
      usagePollSuspended = false;
      refreshUsage();
    }
    if (insightsPollSuspended) {
      insightsPollSuspended = false;
      refreshInsights();
    }
  });
