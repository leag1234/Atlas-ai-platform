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
  let replayAfter = 0, configuring = false, uploading = false;
  const liveRequests = new Set();
  const selections = () => new URLSearchParams(location.search);
  const base = () => {
    if (!scope || !uuid(scope.org) || !uuid(scope.project)) throw new Error(t('selectionError'));
    return `/api/atlas/orgs/${scope.org}/projects/${scope.project}`;
  };
  const storageKey = () => `atlas-project:${identity}`;
  const say = message => { if (notice) notice.textContent = message; };
  function failure(error) { if (error.name !== 'AbortError') say(error.message || t('failure')); }
  const guarded = fn => async event => { const current = generation; try { await fn(event); } catch (error) { if (current === generation) failure(error); } };
  function checkScope(current) { if (current !== generation) throw new DOMException('Scope changed', 'AbortError'); }
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
      const error = await response.json().catch(() => ({})); checkScope(current);
      const detail = `${t('failure')} (${response.status}): ${error.message || error.error || response.statusText}`;
      if (response.status === 401) { sessionStorage.removeItem(storageKey()); cleanup(); root?.remove(); }
      throw Object.assign(new Error(detail), { status: response.status });
    }
    return response;
  }
  async function jsonResponse(response, current) { const value = await response.json(); checkScope(current); return value; }
  const api = async (path, method = 'GET', body) => { const current = generation; return jsonResponse(await request(path, { method, body }), current); };
  async function list(path) {
    const current = generation, items = []; let cursor = null;
    do { const result = await api(path + (cursor ? (path.includes('?') ? '&' : '?') + 'cursor=' + encodeURIComponent(cursor) : '')); checkScope(current); items.push(...result.items); cursor = result.next_cursor; } while (cursor);
    return items;
  }
  function saveRoute() {
    const url = new URL(location.href);
    for (const key of ['org', 'project', 'conversation', 'session']) scope?.[key] ? url.searchParams.set(key, scope[key]) : url.searchParams.delete(key);
    history.replaceState(null, '', url);
    if (identity && scope) sessionStorage.setItem(storageKey(), JSON.stringify(scope));
  }
  function cleanup() {
    generation++; sideResize?.disconnect(); lifecycle?.abort(); stream?.abort(); clearTimeout(polling);
    window.atlasSideView?.close(); document.body.classList.remove('atlas-project-open');
    document.querySelector('.atlas-app-root')?.removeAttribute('inert');
    events.clear(); liveRequests.clear(); replayAfter = 0; configuring = uploading = false; turns = []; selected.clear(); session = null; sending = false; panelData = undefined; ledgerSession = '';
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
      const b = button('newSession', () => choose({ org: scope.org, project: scope.project, conversation: conversation.id })); b.textContent = conversation.title;
      b.dataset.conversation = conversation.id; b.setAttribute('aria-current', String(conversation.id === scope?.conversation)); left.append(b);
    }
    const fresh = button('newSession', () => nameDialog('newSession', async title => {
      const conversation = await api(base() + '/conversations', 'POST', { title }); await choose({ org: scope.org, project: scope.project, conversation: conversation.id });
    })); fresh.disabled = !scope?.project; left.append(fresh, button('ordinary', () => { cleanup(); scope = null; sessionStorage.removeItem(storageKey()); saveRoute(); root.remove(); if (owned) location.href = '/'; }));
  }
  async function choose(value) {
    cleanup(); lifecycle = new AbortController(); const current = generation;
    scope = value; projects = []; conversations = []; files = []; folders = []; center.replaceChildren(); right.replaceChildren(); say(t('loading'));
    for (const key of ['org', 'project', 'conversation', 'session']) if (value[key] && !uuid(value[key])) throw new Error(t('selectionError'));
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
    saveRoute(); navigation();
    const side = document.getElementById('atlas-side-view'); if (side && sideResize) sideResize.observe(side);
    document.body.classList.add('atlas-project-open');
    document.querySelector('.atlas-app-root')?.setAttribute('inert', '');
    say('');
    if (value.project) await mountConversation(current);
  }
  async function mountConversation(current) {
    [files, folders] = await Promise.all([list(base() + '/files'), list(base() + '/folders')]);
    if (current !== generation) return;
    center.replaceChildren();
    transcript = node('div', null, 'atlas-project-transcript'); transcript.setAttribute('role', 'log'); transcript.setAttribute('aria-label', t('conversations'));
    activity = node('div'); activity.className = 'atlas-project-activity';
    composer = node('form', null, 'atlas-project-composer');
    input = node('textarea'); input.maxLength = 32000; input.required = true; input.setAttribute('aria-label', t('message'));
    toggle = node('div', null, 'atlas-project-toolbar');
    for (const mode of ['chat', 'work']) { const b = button(mode, async () => { if (sending || configuring || uploading) return; action = mode; session = null; delete scope.session; saveRoute(); await setAction(); }); b.dataset.action = mode; toggle.append(b); }
    sessionLabel = node('p', '', 'atlas-project-detail');
    uploads = node('input'); uploads.type = 'file'; uploads.multiple = true;
    uploads.addEventListener('change', guarded(upload));
    send = node('button', t('send')); send.type = 'submit'; send.dataset.send = '';
    stop = button('stop', cancel); stop.disabled = true; stop.dataset.stop = '';
    composer.append(toggle, sessionLabel, field('message', input), field('upload', uploads), send, stop);
    composer.addEventListener('submit', guarded(submit));
    input.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!send.disabled) composer.requestSubmit(); } });
    const history = node('div', null, 'atlas-project-history'); history.append(transcript, activity); center.append(history, composer);
    if (!scope.conversation) { send.disabled = true; input.disabled = true; say(t('selectionError')); await panels(); return; }
    await refreshTurns();
    if (scope.session) {
      session = await api(base() + '/work-sessions/' + scope.session);
      if (session.conversation_id !== scope.conversation) throw new Error(t('selectionError'));
      action = 'work';
      for (const version of session.base_file_versions) if (files.some(f => f.version_id === version.version_id)) selected.add(version.version_id);
    }
    await setAction();
    await replay(); await panels();
    if (current === generation) { input.focus(); schedule(current); }
  }
  function renderTurns() {
    transcript.replaceChildren();
    for (const turn of turns) {
      const article = node('article', null, 'atlas-project-turn'); article.dataset.turn = turn.id;
      article.append(node('small', `${t(turn.action)} · ${t(turn.state)}`));
      for (const ref of turn.content_refs) if (ref.text) article.append(prose(ref.text, ref.kind === 'question' ? 'question' : 'answer'));
      if (turn.usage?.cost_eur != null) article.append(detail('cost', turn.usage.cost_eur));
      if (turn.usage?.unknown_eur) article.append(detail('unknownCost', turn.usage.unknown_eur));
      if (turn.usage?.reserved_eur) article.append(detail('reservedCost', turn.usage.reserved_eur));
      transcript.append(article);
    }
  }
  function prose(text, cls) {
    const content = node('pre', null, cls);
    // Plain text plus authorized scoped links; source text never becomes HTML.
    const pattern = /\[([^\]]+)\]\(([^)]+)\)/g;
    let offset = 0;
    for (const match of text.matchAll(pattern)) {
      content.append(document.createTextNode(text.slice(offset, match.index)));
      try { const link = node('a', match[1]); link.href = projectURL(match[2]);
        if (new URL(link.href).pathname.includes('/sources/')) link.addEventListener('click', guarded(async event => { event.preventDefault(); const source = await api(link.href); await window.atlasSideView.open({ kind:'markdown', content:source.text, title:t('coverage') }); }));
        content.append(link); }
      catch { content.append(document.createTextNode(match[0])); }
      offset = match.index + match[0].length;
    }
    content.append(document.createTextNode(text.slice(offset))); return content;
  }
  async function refreshTurns() {
    const current = generation;
    const result = await list(base() + '/conversations/' + scope.conversation + '/turns'); checkScope(current); turns = result; if (!sending) renderTurns(); renderEvents();
  }
  function controls() {
    const activeWork = action === 'work' && ['running','awaiting_approval','cancelling'].includes(session?.state);
    send.disabled = sending || configuring || uploading || activeWork || !scope?.conversation; input.disabled = sending;
    uploads.disabled = sending || configuring || uploading; stop.disabled = !sending && !['running','awaiting_approval','cancelling'].includes(session?.state);
    toggle.querySelectorAll('button').forEach(b => { b.disabled = sending || configuring || uploading; b.setAttribute('aria-pressed', String(b.dataset.action === action)); });
    refreshApprovals();
    right.querySelectorAll('[data-session-state]').forEach(value => { value.textContent = t('status') + ': ' + t(session?.state || 'unknown'); });
    right.querySelectorAll('.atlas-project-file input').forEach(control => { const file = files.find(f => f.version_id === control.dataset.version); control.disabled = sending || configuring || uploading || file?.locality === 'local_only' || file?.availability === 'not_retained'; });
  }
  async function setAction() {
    const current = generation;
    if (action === 'work' && !session) {
      configuring = true; controls();
      try {
      const chosen = files.filter(f => selected.has(f.version_id));
      session = await api(base() + '/work-sessions', 'POST', { conversation_id: scope.conversation, folder_ids: [...new Set(chosen.map(f => f.folder_id))], base_versions: chosen.map(f => ({ file_id: f.id, version_id: f.version_id })) });
      scope.session = session.id; saveRoute();
      } finally { if (current === generation) { configuring = false; controls(); } }
    }
    const limits = session?.effective_limits;
    sessionLabel.textContent = limits && action === 'work' ? `${t('limits')}: ${limits.tool_calls} · ${limits.wall_seconds} ${t('seconds')} · ${t('cost')} ${limits.cost_eur}` : `${t('chat')} · 120–300 ${t('seconds')}`;
    controls();
  }
  async function upload() {
    const current = generation, incoming = [...uploads.files]; uploading = true; controls();
    try {
      let folder = folders.find(f => f.locality === 'server' && f.model_use === 'allow' && f.id !== projects.find(p => p.id === scope.project)?.deliverables_folder_id);
      if (!folder) folder = await api(base() + '/folders', 'POST', { name: t('files'), locality: 'server', model_use: 'allow' });
      for (const file of incoming) {
        folder = (await list(base() + '/folders')).find(value => value.id === folder.id);
        const data = new FormData(); data.append('file', file); data.append('path', file.name); data.append('expected_parent_revision', String(folder.revision));
        const result = await jsonResponse(await request(base() + '/files?folder_id=' + folder.id, { method: 'POST', body: data }), current); selected.add(result.file.version_id);
      }
      [files, folders] = await Promise.all([list(base() + '/files'), list(base() + '/folders')]);
      session = null; delete scope.session; saveRoute(); await setAction(); await panels();
      say(t('saved'));
    } finally { if (current === generation) { uploads.value = ''; uploading = false; controls(); } }
  }
  async function consume(response, callback) {
    const current = generation, reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    try {
      while (true) {
        const part = await reader.read(); checkScope(current); buffer += decoder.decode(part.value || new Uint8Array(), { stream: !part.done });
        buffer = buffer.replace(/\r\n/g, '\n');
        if (buffer.length > 1048576) throw new Error(t('failure'));
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (data && data !== '[DONE]') await callback(JSON.parse(data));
        }
        if (part.done) break;
      }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
  function eventRecord(event) {
    if (!scope || event.org_id !== scope.org || event.project_id !== scope.project || !Number.isSafeInteger(event.seq)) return;
    if (events.has(event.seq)) return;
    events.set(event.seq, event);
    renderEvents();
  }
  function renderEvents() {
    for (const event of [...events.values()].sort((a,b) => a.seq - b.seq)) renderEvent(event);
  }
  function renderEvent(event) {
    if (!activity || activity.querySelector('[data-event-seq="' + event.seq + '"]')) return;
    if (!turns.some(turn => turn.request_id === event.request_id) && !liveRequests.has(event.request_id)) return;
    if (event.conversation_id && event.conversation_id !== scope.conversation) return;
    if (!event.version) return;
    const item = node('section', null, 'atlas-project-event'); item.dataset.eventSeq = String(event.seq);
    item.append(node('small', `${event.label || event.type} · ${t(event.status || event.state || '')}`));
    const out = event.output || {};
    for (const text of [event.text, event.error?.message, out.text, out.detail]) if (text) item.append(prose(text));
    if (out.tool_result) { const details = node('details'); details.append(node('summary', t('results')), node('pre', out.tool_result)); item.append(details); }
    if (out.items) for (const step of out.items) item.append(node('p', `${step.status}: ${step.text || step.title || step.step || ''}`));
    if (out.measurement) item.append(node('p', `${out.measurement.measured} / ${out.measurement.limit} ${out.measurement.unit}`));
    if (out.cost_eur != null) item.append(node('p', `${t('cost')}: ${out.cost_eur}`));
    if (out.diff) item.append(button('preview', () => window.atlasSideView.open({ kind: 'diff', title: t('preview'), content: out.diff })));
    if (out.approval) item.append(approval(out.approval));
    activity.insertBefore(item, [...activity.children].find(n => Number(n.dataset.eventSeq) > event.seq) || null);
  }
  function refreshApprovals() {
    if (!root) return;
    const pending = new Set((session?.pending_approvals || []).filter(value => value.decision === 'pending' && Date.parse(value.expires_at) > Date.now()).map(value => value.id));
    root.querySelectorAll('[data-approval]').forEach(card => card.querySelectorAll('button').forEach(control => { control.disabled = card.dataset.decided === 'true' || !pending.has(card.dataset.approval); }));
  }
  function approval(value) {
    const card = node('section', null, 'atlas-project-approval'); card.dataset.approval = value.id;
    card.append(node('strong', t('approvals')), node('p', `${t(value.action_kind)} · ${value.paths.join(', ')}`), node('p', `${t(value.destination.kind)} · ${value.maximum_bytes} ${t('bytes')}`), node('p', `${t('expires')}: ${value.expires_at}`));
    for (const allow of [true, false]) {
      const b = button(allow ? 'accept' : 'deny', async () => {
        root.querySelectorAll('[data-approval]').forEach(other => { if (other.dataset.approval === value.id) { other.dataset.decided = 'true'; other.querySelectorAll('button').forEach(control => { control.disabled = true; }); } });
        try { await api(base() + '/approvals/' + value.id + '/decision', 'POST', { allow, action_digest: value.action_digest }); card.append(node('p', t('approveRecorded'))); }
        catch (error) { card.append(node('p', error.message)); throw error; }
      });
      b.disabled = !(session?.pending_approvals || []).some(a => a.id === value.id && a.decision === 'pending' && Date.parse(a.expires_at) > Date.now()); card.append(b);
    }
    return card;
  }
  async function replay() {
    let count;
    do {
      const current = generation, after = replayAfter; count = 0;
      await consume(await request(base() + '/events?after=' + after), event => { checkScope(current); count++; replayAfter = Math.max(replayAfter, event.seq); eventRecord(event); });
    } while (count === 100);
  }
  function schedule(current) {
    polling = setTimeout(async () => {
      try {
        await replay();
        if (session) { const id = session.id, value = await api(base() + '/work-sessions/' + id); if (session?.id === id) { session = value; controls(); } }
        await refreshTurns();
      } catch (error) { if (current === generation) failure(error); }
      if (current === generation) schedule(current);
    }, 1500);
  }
  async function cancel() {
    const current = generation;
    if (action === 'work' && session) await api(base() + '/work-sessions/' + session.id + '/cancel', 'POST', { request_id: crypto.randomUUID() });
    checkScope(current); stream?.abort(); say(t('stopped'));
  }
  async function submit(event) {
    event.preventDefault(); if (sending || configuring || uploading || !scope?.conversation || !input.value.trim()) return;
    const current = generation, question = input.value.trim();
    sending = true; controls(); stream = new AbortController();
    const pending = node('article', null, 'atlas-project-turn'); const answer = node('pre', '', 'answer'); pending.append(node('pre', question, 'question'), answer); transcript.append(pending);
    try {
      if (action === 'work' && (!session || ['completed','failed','cancelled'].includes(session.state))) { session = null; await setAction(); }
      const body = { model: window.atlasProjectProfile, org_id: scope.org, project_id: scope.project, conversation_id: scope.conversation, project_action: action, file_version_ids: [...selected], messages: [{ role: 'user', content: question }], stream: true };
      if (action === 'work') body.work_session_id = session.id;
      const response = await request('/v1/chat/completions', { method: 'POST', body, signal: stream.signal });
      await consume(response, chunk => {
        if (current !== generation) return;
        if (chunk.project_id) { liveRequests.add(chunk.request_id); eventRecord(chunk); }
        const delta = chunk.choices?.[0]?.delta?.content;
        if (delta) answer.textContent += delta;
        if (chunk.error) throw new Error(typeof chunk.error === 'string' ? chunk.error : chunk.error.message);
      });
      checkScope(current); input.value = '';
    } catch (error) { if (current === generation) failure(error); }
    finally {
      if (current === generation) { sending = false; controls(); await refreshTurns(); await replay(); if (session) session = await api(base() + '/work-sessions/' + session.id); [files, folders] = await Promise.all([list(base() + '/files'), list(base() + '/folders')]); await panels(); controls(); input.focus(); }
    }
  }
  let ledgerTab = 'server', ledgerSession = '', panelData, sideResize;
  const detail = (key, value) => node('p', `${t(key)}: ${value == null ? t('unknown') : value}`, 'atlas-project-detail');
  async function panels() {
    const current = generation, projectBase = base();
    const [instructions, memory, deliverables, jobs, summary, ledger, context, policy] = await Promise.all([
      api(projectBase + '/instructions'), list(projectBase + '/memory'), list(projectBase + '/deliverables'), list(projectBase + '/jobs'),
      api(projectBase + '/ledger/summary' + (ledgerSession ? '?session_id=' + ledgerSession : '')),
      list(projectBase + '/ledger' + (ledgerSession ? '?session_id=' + ledgerSession : '')),
      scope.conversation ? api(projectBase + '/context', 'POST', { conversation_id: scope.conversation, query: input?.value.trim() || t('context'), action }) : null,
      memberships.find(m => m.org_id === scope.org)?.role === 'admin' ? api(`/api/atlas/orgs/${scope.org}/policy`) : null,
    ]);
    checkScope(current); panelData = { instructions, memory, deliverables, jobs, summary, ledger, context, policy };
    renderPanels();
  }
  function renderPanels() {
    if (!panelData) return;
    const { instructions, memory, deliverables, jobs, summary, ledger, context, policy } = panelData;
    right.replaceChildren(node('h2', t('environment')), detail('workspace', t('server')), detail('device', t('unavailable')));
    if (session) { const status = detail('status', t(session.state)); status.dataset.sessionState = ''; right.append(detail('workspace', session.environment.workspace_id), status); }
    for (const folder of folders) right.append(detail('files', `${folder.name} · ${folder.locality === 'local_only' ? t('local') + ' · ' + t('device') : t('server')} · ${t(folder.model_use)}`));
    right.append(detail('repository', session?.repository_id || t('unavailable')), node('h2', t('context')), detail('instructions', instructions.version));
    if (instructions.text) right.append(node('p', instructions.text));
    right.append(node('h3', t('memory')));
    for (const entry of memory) right.append(node('p', entry.text), detail('status', t(entry.status)));
    if (!memory.length) right.append(node('p', t('empty')));
    right.append(node('h3', t('selectedFiles')));
    for (const file of files) {
      const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.dataset.version = file.version_id; checkbox.checked = selected.has(file.version_id); checkbox.disabled = sending || configuring || uploading || file.locality === 'local_only' || file.availability === 'not_retained';
      checkbox.addEventListener('change', guarded(async () => { if (sending || configuring || uploading) return; checkbox.checked ? selected.add(file.version_id) : selected.delete(file.version_id); session = null; delete scope.session; saveRoute(); await setAction(); if (panelData) renderPanels(); }));
      const label = node('label', null, 'atlas-project-file'); label.append(checkbox, node('span', file.relative_path));
      right.append(label, detail('version', file.version_id), detail('coverage', t(file.index_status)), button('preview', () => previewFile(file)));
    }
    const coverage = context?.context_manifest?.coverage;
    for (const passage of context?.passages || []) right.append(detail('version', passage.version_id), detail('coverage', JSON.stringify(passage.locator)));
    if (coverage) {
      right.append(detail('coverage', `${coverage.selected_units}/${coverage.total_units ?? t('unknown')} ${coverage.unit} · ${coverage.complete ? t('completed') : t('unknown')}`));
      for (const warning of coverage.warnings) right.append(node('p', warning));
    }
    right.append(detail('connectors', policy ? (policy.effective_policy.connector_allowlist.join(', ') || t('empty')) : t('unavailable')), detail('imagePolicy', policy?.effective_policy.image_safety_floor || t('unavailable')));
    for (const value of session?.pending_approvals || []) if (value.decision === 'pending') right.append(approval(value));
    right.append(node('h2', t('results')));
    for (const item of deliverables) {
      const block = node('section'); block.dataset.deliverable = item.id;
      block.append(detail('format', item.mime), detail('bytes', item.size_bytes), detail('validation', t(item.validation.status)), detail('status', t(item.strategy)));
      const link = node('a', t('download')); link.href = projectURL(item.download_route); link.download = ''; block.append(link, button('preview', () => previewDeliverable(item))); right.append(block);
    }
    if (!deliverables.length) right.append(node('p', t('empty')));
    right.append(node('h3', t('jobs')));
    if (session) { right.append(detail('session', `${session.id} · ${t(session.state)}`)); if (['running','awaiting_approval','cancelling'].includes(session.state)) right.append(button('cancel', cancel)); }
    for (const job of jobs) right.append(detail('jobs', `${job.kind} · ${t(job.status)} · ${job.progress ?? t('unknown')}`));
    right.append(node('p', t('scheduled')), node('h2', t('ledger')));
    const tabs = node('div', null, 'atlas-project-ledger-tabs'); tabs.setAttribute('role', 'tablist');
    for (const [tab, key] of [['local','local'], ['server','onServer'], ['model','model'], ['produced','produced']]) {
      const control = button(key, () => { ledgerTab = tab; renderPanels(); }); control.setAttribute('role', 'tab'); control.setAttribute('aria-selected', String(tab === ledgerTab)); control.dataset.ledgerTab = tab; tabs.append(control);
    }
    right.append(tabs);
    const sessions = [...new Set(ledger.map(e => e.session_id).filter(Boolean))]; if (session && !sessions.includes(session.id)) sessions.push(session.id);
    const filter = node('select'); filter.append(new Option(t('allSessions'), '')); sessions.forEach(id => filter.append(new Option(id, id))); filter.value = ledgerSession;
    filter.addEventListener('change', guarded(async () => { ledgerSession = filter.value; await panels(); })); right.append(field('session', filter));
    const panel = node('div'); panel.setAttribute('role','tabpanel');
    const total = summary[ledgerTab];
    if (ledgerTab === 'model') {
      panel.append(detail('payloadBytes', total.payload_bytes), detail('uniqueBytes', total.unique_source_bytes), detail('calls', total.calls), detail('possibly_sent', total.possibly_sent_calls));
      for (const event of ledger.filter(e => e.model_dispatch)) {
        const call = event.model_dispatch;
        panel.append(detail('status', `${event.kind} · ${event.outcome} · ${event.timestamp}`), detail('model', call.provider_ref), detail('sourceBytes', call.byte_counts?.source_bytes ?? event.byte_counts.source_bytes), detail('payloadBytes', event.byte_counts.serialized_payload_bytes), detail('extractBytes', event.byte_counts.selected_utf8_bytes), detail('imageBytes', event.byte_counts.image_bytes));
        for (const source of call.sources) { const file = files.find(f => f.id === source.file_id); if (file) { panel.append(button('preview', () => previewFile({ ...file, version_id: source.version_id })), detail('coverage', JSON.stringify(source.locator))); } }
      }
    } else {
      panel.append(detail('files', total.known === false ? t('unknown') : total.files), detail('bytes', total.known === false ? t('unknown') : total.bytes));
    }
    panel.append(detail('asOf', summary.as_of_seq));
    for (const event of ledger.filter(e => ['failed','unknown','denied'].includes(e.outcome))) panel.append(detail('status', `${event.kind} · ${event.outcome} · ${event.timestamp}`));
    right.append(panel, button('refresh', panels));
  }
  function projectURL(value) {
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(base() + '/') || url.username || url.password) throw new Error(t('selectionError'));
    return url.href;
  }
  async function previewFile(file) {
    const src = projectURL(base() + '/files/' + file.id + '/content?version=' + file.version_id);
    const suffix = file.relative_path.split('.').pop().toLowerCase();
    const kind = ({ pdf:'pdf', md:'markdown', markdown:'markdown', html:'html', htm:'html', diff:'diff', patch:'diff', txt:'markdown' })[suffix];
    if (kind) { await window.atlasSideView.open({ kind, src, title:file.relative_path }); return; }
    const item = panelData?.deliverables.find(item => item.file_version_id === file.version_id);
    if (item) { await previewDeliverable(item); return; }
    say(t('previewUnavailable'));
    const link = node('a', t('download')); link.href = src; link.download = file.relative_path; notice.append(' ', link);
  }
  async function previewDeliverable(item) {
    const src = projectURL(base() + '/deliverables/' + item.id + '/preview');
    const current = generation, response = await request(src);
    if ((response.headers.get('content-type') || '').startsWith('application/pdf')) await window.atlasSideView.open({ kind:'pdf', src, title:t('preview') });
    else { const content = await response.text(); checkScope(current); await window.atlasSideView.open({ kind:'markdown', content, title:t('preview') }); }
  }
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
    const side = document.getElementById('atlas-side-view');
    if (side) { sideResize = new ResizeObserver(() => root.style.setProperty('--atlas-side-width', side.getBoundingClientRect().width + 'px')); sideResize.observe(side); }
    root.addEventListener('keydown', event => { if (event.key === 'Escape') { left.classList.remove('is-open'); right.classList.remove('is-open'); input?.focus(); } });
    let stored = {}; try { stored = JSON.parse(sessionStorage.getItem(storageKey()) || '{}'); } catch { sessionStorage.removeItem(storageKey()); }
    const q = selections(), value = q.has('org') || q.has('project') ? { org: q.get('org'), project: q.get('project'), conversation: q.get('conversation'), session: q.get('session') } : stored;
    if (!value.org && memberships.length === 1) value.org = memberships[0].org_id;
    try { await choose(value); } catch (error) { if (error.name === 'AbortError') return; cleanup(); sessionStorage.removeItem(storageKey()); showFailure(error); }
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
