(() => {
  if (!localStorage.getItem('locale')) {
    const language = (navigator.language || 'fr').split('-')[0];
    localStorage.setItem('locale', ['fr','en','de','es','it'].includes(language) ? language : 'fr');
  }
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

  // Owner rule 2026-10-31: a Beta badge sits next to the Atlas name and logo until version 1.0.
  // Shown by default; hidden only once atlas-version.json reports a major version of 1 or more.
  let beta = true;
  fetch('/static/atlas/atlas-version.json', {cache:'no-cache', credentials:'same-origin'})
    .then(response => response.ok ? response.json() : null)
    .then(data => {
      const major = /^(\d+)\./.exec(data?.version || '');
      beta = !major || Number(major[1]) < 1;
      refresh();
    })
    .catch(() => {});

  function markBeta(anchor) {
    const label = (localStorage.getItem('locale') || '').startsWith('fr') ? 'Bêta' : 'Beta';
    const next = anchor.nextElementSibling;
    const badge = next && next.classList.contains('atlas-beta') ? next : null;
    if (!beta) { if (badge) badge.remove(); return; }
    if (badge) { if (badge.textContent !== label) badge.textContent = label; return; }
    const created = document.createElement('span');
    created.className = 'atlas-beta';
    created.textContent = label;
    anchor.after(created);
  }

  let accountName = '';
  async function loadAccountName() {
    try {
      const response = await fetch('/api/v1/auths/', {credentials:'same-origin'});
      if (response.ok) {
        const account = await response.json();
        accountName = typeof account.name === 'string' ? account.name : '';
        refresh();
      }
    } catch { /* The host owns sign-in and account error handling. */ }
  }

  function refresh() {
    document.querySelectorAll('img').forEach(replaceLogo);
    document.querySelectorAll('#sidebar-webui-name').forEach(markBeta);
    if (location.pathname.startsWith('/auth')) {
      document.querySelectorAll('img[data-atlas-brand=logotype], img[data-atlas-brand=symbol]').forEach(markBeta);
    }
    const title = document.title.replace(/Atlas \(Open WebUI\)|Open WebUI/g,'Atlas');
    if (document.title !== title) document.title = title;
    for (const container of document.querySelectorAll('nav, header, [role=dialog], #sidebar, #pinned-menu-items-list, #sidebar-webui-name')) {
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode;
        if (accountName && text.textContent.trim() === 'User' && container.matches('#sidebar, nav')) text.textContent = accountName;
        if (text.textContent.includes('Atlas (Open WebUI)')) text.textContent = text.textContent.replaceAll('Atlas (Open WebUI)','Atlas');
      }
    }
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
  loadAccountName();
  refresh();
})();
