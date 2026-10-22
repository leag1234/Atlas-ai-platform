/* Keep HTTP Basic authentication at nginx and use the UI's HttpOnly session cookie. */
(() => {
  document.addEventListener('DOMContentLoaded', () => {
    const notice = document.createElement('div');
    notice.id = 'atlas-stable-notice';
    notice.textContent = 'Conversations are recorded to improve Atlas. Do not share sensitive information.';
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
