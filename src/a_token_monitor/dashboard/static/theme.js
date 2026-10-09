  // 主题：跟随系统 / 白天 / 夜间，选择存 localStorage，切换不需要刷新页面。
  const THEME_STORAGE_KEY = 'a-token-monitor-theme';
  const THEME_MODES = ['system', 'light', 'dark'];
  const THEME_LABELS = { system: '跟随系统', light: '白天', dark: '夜间' };
  const themeQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  const readThemeMode = () => {
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      return THEME_MODES.includes(stored) ? stored : 'system';
    } catch (error) {
      return 'system';
    }
  };
  const resolvedTheme = (mode) => (mode === 'system'
    ? (themeQuery && themeQuery.matches ? 'light' : 'dark')
    : mode);
  const applyTheme = () => {
    const mode = readThemeMode();
    const root = document.documentElement;
    root.dataset.theme = resolvedTheme(mode);
    root.dataset.themeMode = mode;
    const label = document.getElementById('theme-label');
    if (label) label.textContent = THEME_LABELS[mode];
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.title = `主题：${THEME_LABELS[mode]}（点击切换）`;
      toggle.setAttribute('aria-label', `切换主题，当前${THEME_LABELS[mode]}`);
    }
  };
  const cycleTheme = () => {
    const mode = readThemeMode();
    const next = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch (error) {
      // 隐私模式下写不了 localStorage：本次仍然生效，只是不记忆。
    }
    applyTheme();
  };
  document.getElementById('theme-toggle')?.addEventListener('click', cycleTheme);
  if (themeQuery && themeQuery.addEventListener) {
    themeQuery.addEventListener('change', () => {
      if (readThemeMode() === 'system') applyTheme();
    });
  }
  applyTheme();
