  // 语言：点一下切到另一种语言。界面文案由服务端按语言渲染，所以这里写 cookie +
  // localStorage 后整页重载；cookie 让后续的接口请求也带上同一种语言。
  const LANGUAGE_STORAGE_KEY = 'a-token-monitor-language';
  const LANGUAGE_COOKIE = 'a-token-monitor-language';
  const currentLanguage = document.documentElement.lang === 'en' ? 'en' : 'zh';
  const writeLanguageCookie = (language) => {
    const maxAge = 60 * 60 * 24 * 365;
    document.cookie = `${LANGUAGE_COOKIE}=${language}; path=/; max-age=${maxAge}; SameSite=Lax`;
  };
  const switchLanguage = () => {
    const next = currentLanguage === 'en' ? 'zh' : 'en';
    writeLanguageCookie(next);
    try {
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
    } catch (error) {
      // 隐私模式下写不了 localStorage：cookie 仍然生效。
    }
    window.location.reload();
  };
  const languageToggle = document.getElementById('language-toggle');
  if (languageToggle) {
    const label = document.getElementById('language-label');
    if (label) label.textContent = currentLanguage === 'en' ? '中' : 'EN';
    languageToggle.title = currentLanguage === 'en'
      ? '切换到中文（Chinese）'
      : '切换到英文（English）';
    languageToggle.setAttribute('aria-label', languageToggle.title);
    languageToggle.addEventListener('click', switchLanguage);
  }
