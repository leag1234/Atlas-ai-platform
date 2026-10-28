/* Keep HTTP Basic authentication at nginx and use the UI's HttpOnly session cookie. */
(() => {
  const instanceNotice = (() => {
    const raw = document.currentScript?.dataset.instanceNotice || '';
    if (!raw.startsWith('{')) return raw;
    try {
      const texts = JSON.parse(raw);
      let stored = '';
      try { stored = localStorage.getItem('locale') || ''; } catch { stored = ''; }
      const language = (stored || navigator.language || 'en').slice(0, 2).toLowerCase();
      return texts[language] || texts.en || Object.values(texts)[0] || '';
    } catch { return ''; }
  })();
  document.addEventListener('DOMContentLoaded', () => {
    if (!instanceNotice) return;
    const notice = document.createElement('div');
    notice.id = 'atlas-stable-notice';
    notice.textContent = instanceNotice;
    notice.style.cssText = 'position:fixed;bottom:0;left:0;right:0;text-align:center;font:11px sans-serif;padding:3px;background:#eef2f6;color:#334155;z-index:100;pointer-events:none';
    document.body.appendChild(notice);
  });
  const originalFetch = window.fetch;
  window.fetch = (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin) return originalFetch(input, init);
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    if (/^Bearer /i.test(headers.get('Authorization') || '')) headers.delete('Authorization');
    return originalFetch(input, { ...init, headers, credentials: 'same-origin' });
  };
})();
