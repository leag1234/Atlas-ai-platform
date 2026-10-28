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
    const url = new URL(image.src, location.href);
    const path = url.pathname;
    // Keep the native HTTP avatar URL: Open WebUI validates model image URLs.
    // CSS paints the appropriate local SVG over the raster response in both sizes.
    if (url.origin === location.origin && path === '/api/v1/models/model/profile/image' && /^atlas(-|$)/.test(url.searchParams.get('id') || '')) {
      image.dataset.atlasBrand = 'model';
      return;
    }
    if (image.dataset.atlasBrand === 'model') delete image.dataset.atlasBrand;
    const logotype = path === lightLogotype || path === darkLogotype || image.dataset.atlasBrand === 'logotype';
    if (!logotype && !sourcePaths.has(path) && image.dataset.atlasBrand !== 'symbol') return;
    image.dataset.atlasBrand = logotype ? 'logotype' : 'symbol';
    const source = logotype ? (isDark() ? darkLogotype : lightLogotype) : (isDark() ? dark : light);
    if (image.getAttribute('src') !== source) image.src = source;
    image.alt = 'Atlas';
  }

  function refresh() {
    document.querySelectorAll('img').forEach(replaceLogo);
  }

  new MutationObserver(refresh).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class', 'src'],
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
