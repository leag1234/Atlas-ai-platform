(() => {
  const sourcePath = '/static/favicon.png';
  const light = '/static/atlas/brand/svg/atlas-symbol.svg';
  const dark = '/static/atlas/brand/svg/atlas-symbol-dark.svg';

  function isDark() {
    return document.documentElement.classList.contains('dark');
  }

  function replaceLogo(image) {
    const path = new URL(image.src, location.href).pathname;
    if (path !== sourcePath && image.dataset.atlasBrand !== 'symbol') return;
    image.dataset.atlasBrand = 'symbol';
    image.src = isDark() ? dark : light;
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
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', refresh);
  refresh();
})();
