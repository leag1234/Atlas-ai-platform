(() => {
  const sourcePaths = new Set([
    '/favicon.png',
    '/static/favicon.png',
    '/static/favicon-96x96.png',
    '/static/favicon.svg',
    '/static/logo.png',
    '/static/splash.png',
    '/static/splash-dark.png'
  ]);
  const light = '/static/atlas/brand/svg/atlas-symbol.svg';
  const dark = '/static/atlas/brand/svg/atlas-symbol-dark.svg';
  const lightLogotype = '/static/atlas/brand/svg/atlas-logotype.svg';
  const darkLogotype = '/static/atlas/brand/svg/atlas-logotype-dark.svg';
  const systemDark = window.matchMedia('(prefers-color-scheme: dark)');

  function isDark() {
    const theme = localStorage.getItem('theme');
    if (theme === 'dark') return true;
    if (theme === 'light') return false;
    if (theme === 'system') return systemDark.matches;
    return document.documentElement.classList.contains('dark');
  }

  function replaceLogo(image) {
    const path = new URL(image.src, location.href).pathname;
    const logotype = path === lightLogotype || path === darkLogotype || image.dataset.atlasBrand === 'logotype';
    if (!logotype && !sourcePaths.has(path) && image.dataset.atlasBrand !== 'symbol') return;
    image.dataset.atlasBrand = logotype ? 'logotype' : 'symbol';
    image.src = logotype ? (isDark() ? darkLogotype : lightLogotype) : (isDark() ? dark : light);
    image.alt = 'Atlas';
  }

  function refresh() {
    document.querySelectorAll('img').forEach(replaceLogo);
  }

  new MutationObserver(refresh).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class'],
    childList: true,
    subtree: true
  });
  document.addEventListener('DOMContentLoaded', refresh, { once: true });
  window.addEventListener('atlas-theme-change', refresh);
  window.addEventListener('storage', event => {
    if (event.key === 'theme') refresh();
  });
  systemDark.addEventListener('change', refresh);
  refresh();
})();
