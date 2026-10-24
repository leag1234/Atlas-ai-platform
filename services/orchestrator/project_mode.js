/* REQ-PROJ-08: canonical scoped state; no host transcript or Svelte stores. */
(() => {
  'use strict';
  if (window.atlasProjectMounted) return;
  window.atlasProjectMounted = true;
  const t = key => window.atlasProjectLocale.text(key);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const uuid = value => /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value || '');
  const owned = location.pathname === '/project-mode';
  let root, left, center, right, notice, identity = '', scope, lifecycle, stream, polling, generation = 0;
  let memberships = [], projects = [], conversations = [], files = [], folders = [], selected = new Set(), turns = [], events = new Map(), session = null, action = 'chat', sending = false;
  let transcript, activity, composer, input, send, stop, toggle, uploads, sessionLabel;
  const selections = () => new URLSearchParams(location.search);
  const base = () => {
    if (!scope || !uuid(scope.org) || !uuid(scope.project)) throw new Error(t('selectionError'));
    return `/api/atlas/orgs/${scope.org}/projects/${scope.project}`;
  };
  const storageKey = () => `atlas-project:${identity}`;
  const say = message => { if (notice) notice.textContent = message; };
  function failure(error) { if (error.name !== 'AbortError') say(error.message || t('failure')); }
  const guarded = fn => async event => { try { await fn(event); } catch (error) { failure(error); } };
  function button(key, fn, cls) { const b = node('button', t(key), cls); b.type = 'button'; b.addEventListener('click', guarded(fn)); return b; }
  function field(key, control) { const label = node('label', t(key)); label.append(control); control.setAttribute('aria-label', t(key)); return label; }
  async function request(path, options = {}) {
    const headers = new Headers(options.headers);
    if (options.method && options.method !== 'GET') headers.set('Idempotency-Key', crypto.randomUUID());
    if (options.body && !(options.body instanceof FormData)) { headers.set('Content-Type', 'application/json'); options.body = JSON.stringify(options.body); }
    const current = generation;
    const response = await fetch(path, { credentials: 'same-origin', signal: lifecycle?.signal, ...options, headers });
    if (current !== generation) throw new DOMException('Scope changed', 'AbortError');
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      const detail = `${t('failure')} (${response.status}): ${error.message || error.error || response.statusText}`;
      if (response.status === 401) { sessionStorage.removeItem(storageKey()); cleanup(); root?.remove(); }
      throw Object.assign(new Error(detail), { status: response.status });
    }
    return response;
  }
  const api = async (path, method = 'GET', body) => (await request(path, { method, body })).json();
  async function list(path) {
    const items = []; let cursor = null;
    do { const result = await api(path + (cursor ? (path.includes('?') ? '&' : '?') + 'cursor=' + encodeURIComponent(cursor) : '')); items.push(...result.items); cursor = result.next_cursor; } while (cursor);
    return items;
  }
  function saveRoute() {
    const url = new URL(location.href);
    for (const key of ['org', 'project', 'conversation']) scope?.[key] ? url.searchParams.set(key, scope[key]) : url.searchParams.delete(key);
    history.replaceState(null, '', url);
    if (identity && scope) sessionStorage.setItem(storageKey(), JSON.stringify(scope));
  }
  function cleanup() {
    generation++; lifecycle?.abort(); stream?.abort(); clearTimeout(polling);
    window.atlasSideView?.close(); document.body.classList.remove('atlas-project-open');
    document.querySelector('.atlas-app-root')?.removeAttribute('inert');
    events.clear(); turns = []; selected.clear(); session = null; sending = false;
  }
  function picker(key, values, selectedID, change) {
    const select = node('select'); select.append(new Option('—', ''));
    values.forEach(value => select.append(new Option(value.name || value.title || value.org_id, value.id || value.org_id)));
    select.value = selectedID || ''; select.addEventListener('change', guarded(() => change(select.value)));
    return field(key, select);
  }
  async function nameDialog(key, submit) {
    const dialog = node('dialog', null, 'atlas-project-dialog'), form = node('form'), name = node('input');
    name.required = true; name.maxLength = 120;
    const create = node('button', t('create')); create.type = 'submit';
    form.append(node('h2', t(key)), field('name', name), create, button('cancel', () => dialog.close())); dialog.append(form); root.append(dialog);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.addEventListener('submit', guarded(async event => { event.preventDefault(); create.disabled = true; try { await submit(name.value.trim()); dialog.close(); } finally { create.disabled = false; } }));
    dialog.showModal(); name.focus();
  }
  function navigation() {
    left.replaceChildren(node('h2', t('projects')));
    left.append(picker('organization', memberships, scope?.org, org => choose({ org })), picker('project', projects, scope?.project, project => choose({ org: scope.org, project })));
    const create = button('newProject', () => nameDialog('newProject', async name => {
      const project = await api(`/api/atlas/orgs/${scope.org}/projects`, 'POST', { name, instructions: '', default_action: 'chat' });
      await choose({ org: scope.org, project: project.id });
    })); create.disabled = !scope?.org; left.append(create, node('h3', t('conversations')));
    for (const conversation of conversations) {
      const b = button('newSession', () => choose({ ...scope, conversation: conversation.id })); b.textContent = conversation.title;
      b.dataset.conversation = conversation.id; b.setAttribute('aria-current', String(conversation.id === scope?.conversation)); left.append(b);
    }
    const fresh = button('newSession', () => nameDialog('newSession', async title => {
      const conversation = await api(base() + '/conversations', 'POST', { title }); await choose({ ...scope, conversation: conversation.id });
    })); fresh.disabled = !scope?.project; left.append(fresh, button('ordinary', () => { cleanup(); scope = null; sessionStorage.removeItem(storageKey()); saveRoute(); root.remove(); if (owned) location.href = '/'; }));
  }
  async function choose(value) {
    cleanup(); lifecycle = new AbortController(); const current = generation;
    scope = value; projects = []; conversations = []; files = []; folders = []; center.replaceChildren(); right.replaceChildren(); say(t('loading'));
    for (const key of ['org', 'project', 'conversation']) if (value[key] && !uuid(value[key])) throw new Error(t('selectionError'));
    if (value.org && !memberships.some(m => m.org_id === value.org)) throw new Error(t('selectionError'));
    if (value.org) projects = await list(`/api/atlas/orgs/${value.org}/projects`);
    if (current !== generation) return;
    if (value.project) {
      if (!projects.some(p => p.id === value.project)) throw new Error(t('selectionError'));
      const project = await api(base()); action = project.default_action;
      conversations = await list(base() + '/conversations');
      if (value.conversation && !conversations.some(c => c.id === value.conversation)) throw new Error(t('selectionError'));
      if (!value.conversation && conversations.length) value.conversation = conversations[0].id;
    }
    if (current !== generation) return;
    saveRoute(); navigation(); document.body.classList.add('atlas-project-open');
    document.querySelector('.atlas-app-root')?.setAttribute('inert', '');
    say('');
    if (value.project) await mountConversation(current);
  }
  async function mountConversation() { center.append(node('p', t('selected') + ': ' + projects.find(p => p.id === scope.project).name)); }
  async function boot() {
    if (/^\/(auth|admin)/.test(location.pathname)) return;
    try {
      const me = await fetch('/api/atlas/me', { credentials: 'same-origin' });
      if (!me.ok) { if (owned || selections().has('project')) throw new Error(`${t('failure')} (${me.status})`); return; }
      memberships = (await me.json()).memberships;
      const host = await fetch('/api/v1/auths/', { credentials: 'same-origin' });
      if (!host.ok) throw new Error(t('signIn'));
      identity = (await host.json()).id;
      if (!identity) throw new Error(t('signIn'));
      // Only selection IDs survive reload. Never persist prose, events or tokens.
      for (let i = sessionStorage.length - 1; i >= 0; i--) { const key = sessionStorage.key(i); if (key.startsWith('atlas-project:') && key !== storageKey()) sessionStorage.removeItem(key); }
      document.getElementById('atlas-project-entry')?.remove();
      const entry = button('projects', () => mount()); entry.id = 'atlas-project-entry'; document.body.append(entry);
      if (owned || selections().has('project') || sessionStorage.getItem(storageKey())) await mount();
    } catch (error) { showFailure(error); }
  }
  function showFailure(error) {
    cleanup(); root?.remove(); document.getElementById('atlas-project-failure')?.remove();
    const warning = node('aside', `${t('shellFailure')}: ${error.message}`, 'atlas-project-failure'); warning.id = 'atlas-project-failure'; warning.setAttribute('role', 'alert');
    const link = node('a', t('fallback')); link.href = '/project-mode' + location.search; warning.append(link); document.body.append(warning);
  }
  async function mount() {
    if (root?.isConnected) return;
    if (!owned && !document.querySelector('#chat-input')) {
      await new Promise(resolve => {
        const timer = setTimeout(() => { observer.disconnect(); resolve(); }, 10000);
        const observer = new MutationObserver(() => { if (document.querySelector('#chat-input')) { clearTimeout(timer); observer.disconnect(); resolve(); } });
        observer.observe(document.body, { childList: true, subtree: true });
      });
      if (!document.querySelector('#chat-input')) { showFailure(new Error(t('shellFailure'))); return; }
    }
    root = node('section', null, 'atlas-project'); root.id = 'atlas-project'; root.setAttribute('aria-label', t('projects'));
    const header = node('header'), title = node('strong', 'Atlas · ' + t('projects'));
    left = node('nav', null, 'atlas-project-left'); left.setAttribute('aria-label', t('projects'));
    center = node('main', null, 'atlas-project-center'); right = node('aside', null, 'atlas-project-right'); right.setAttribute('aria-label', t('context'));
    notice = node('p', '', 'atlas-project-notice'); notice.setAttribute('role', 'status');
    function drawer(panel, key) { return button(key, event => { const opened = !panel.classList.contains('is-open'); left.classList.remove('is-open'); right.classList.remove('is-open'); panel.classList.toggle('is-open', opened); event.currentTarget.setAttribute('aria-expanded', String(opened)); if (opened) panel.querySelector('button,select')?.focus(); }, 'atlas-project-drawer'); }
    header.append(drawer(left, 'projects'), title, drawer(right, 'context'), button('close', () => { cleanup(); root.remove(); scope = null; sessionStorage.removeItem(storageKey()); saveRoute(); }));
    root.append(header, left, center, right, notice); document.body.append(root);
    root.addEventListener('keydown', event => { if (event.key === 'Escape') { left.classList.remove('is-open'); right.classList.remove('is-open'); input?.focus(); } });
    let stored = {}; try { stored = JSON.parse(sessionStorage.getItem(storageKey()) || '{}'); } catch { sessionStorage.removeItem(storageKey()); }
    const q = selections(), value = q.has('org') || q.has('project') ? { org: q.get('org'), project: q.get('project'), conversation: q.get('conversation') } : stored;
    if (!value.org && memberships.length === 1) value.org = memberships[0].org_id;
    try { await choose(value); } catch (error) { cleanup(); sessionStorage.removeItem(storageKey()); showFailure(error); }
  }
  let route = location.pathname;
  const watch = new MutationObserver(() => {
    if (location.pathname !== route) {
      const prior = route; route = location.pathname; cleanup(); root?.remove();
      if (/^\/auth/.test(prior) && !/^\/(auth|admin)/.test(route)) boot();
      if (/^\/(auth|admin)/.test(route)) { if (identity) sessionStorage.removeItem(storageKey()); document.getElementById('atlas-project-entry')?.remove(); }
    }
  });
  window.addEventListener('popstate', () => { cleanup(); root?.remove(); if (!/^\/(auth|admin)/.test(location.pathname)) guarded(mount)(); });
  window.addEventListener('pagehide', cleanup);
  const start = () => { watch.observe(document.body, { childList: true, subtree: true }); boot(); };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start, { once: true }) : start();
})();
