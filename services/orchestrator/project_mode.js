/* REQ-PROJ-08: canonical scoped state; no host transcript or Svelte stores. */
(() => {
  'use strict';
  if (window.atlasProjectMounted) return;
  window.atlasProjectMounted = true;
  const t = key => window.atlasProjectLocale.text(key);
  const countLabel = (count, singular, plural) => {
    const language = window.atlasProjectLocale.language();
    const key = new Intl.PluralRules(language).select(count) === 'one' ? singular : plural;
    return `${count} ${t(key).toLocaleLowerCase(language)}`;
  };
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const uuid = value => /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value || '');
  const owned = location.pathname === '/project-mode';
  let root, left, center, right, notice, identity = '', scope, lifecycle, stream, polling, generation = 0;
  let memberships = [], projects = [], conversations = [], files = [], folders = [], selected = new Set(), turns = [], events = new Map(), session = null, action = 'chat', sending = false;
  let transcript, activity, composer, input, send, stop, uploads, sessionLabel, projectInfo;
  let projectTab = 'mine', projectSearch = '', projectSort = 'recent';
  let booting = false;
  let replayAfter = 0, configuring = false, uploading = false;
  const liveRequests = new Set();
  const selections = () => new URLSearchParams(location.search);
  const base = () => {
    if (!scope || !uuid(scope.org) || !uuid(scope.project)) throw new Error(t('selectionError'));
    return `/api/atlas/orgs/${scope.org}/projects/${scope.project}`;
  };
  const storageKey = () => `atlas-project:${identity}`;
  const say = message => { if (notice) notice.textContent = message; };
  function errorNotice(target, error, key = 'failure') {
    const message = [401,403,404].includes(error.status) ? 'accessFailure' : [413,429].includes(error.status) ? 'limitFailure' : error.status >= 500 ? 'serviceFailure' : key;
    target.replaceChildren(node('span', t(message)));

  }
  function failure(error) { if (error.name !== 'AbortError' && notice) errorNotice(notice, error); }
  function locator(value) {
    if (!value) return t('unknown');
    if (value.kind === 'text') return `${t('text')}, ${t('characters')} ${value.start}–${value.end}`;
    if (value.kind === 'page') return `${t('page')} ${value.page}`;
    if (value.kind === 'paragraph') return `${t('paragraph')} ${value.paragraph}`;
    if (value.kind === 'sheet_range') return `${t('sheet')} ${value.sheet}, ${value.range}`;
    if (value.kind === 'table_cell') return `${t('table')} ${value.table}, ${t('row')} ${value.row}, ${t('column')} ${value.column}`;
    return t('unknown');
  }
  const guarded = fn => async event => { try { await fn(event); } catch (error) { if (error.name !== 'AbortError' && root?.isConnected) failure(error); } };
  function checkScope(current) { if (current !== generation) throw new DOMException('Scope changed', 'AbortError'); }
  function button(key, fn, cls) { const b = node('button', t(key), cls); b.type = 'button'; b.addEventListener('click', guarded(fn)); return b; }
  function field(key, control) { const label = node('label', t(key)); label.append(control); control.setAttribute('aria-label', t(key)); return label; }
  async function request(path, options = {}) {
    const headers = new Headers(options.headers);
    if (options.method && options.method !== 'GET') headers.set('Idempotency-Key', crypto.randomUUID());
    if (options.body && !(options.body instanceof FormData)) { headers.set('Content-Type', 'application/json'); options.body = JSON.stringify(options.body); }
    const current = generation;
    const response = await fetch(path, { credentials: 'same-origin', signal: lifecycle ? AbortSignal.any([lifecycle.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000), ...options, headers });
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
    generation++; sideResize?.disconnect(); hostResize?.disconnect(); observedHost = null; lifecycle?.abort(); stream?.abort(); clearTimeout(polling);
    window.atlasSideView?.close(); document.body.classList.remove('atlas-project-open');
    document.querySelector('[data-atlas-project-host-main]')?.removeAttribute('inert');
    projectSummaries.clear(); events.clear(); liveRequests.clear(); replayAfter = 0; configuring = uploading = false; turns = []; selected.clear(); session = null; sending = false; panelData = undefined;
  }
  function picker(key, values, selectedID, change) {
    const select = node('select'); select.append(new Option('—', ''));
    values.forEach(value => { const option = new Option(value.name || value.title || value.org_id, value.id || value.org_id); option.title = value.id || value.org_id; select.append(option); });
    select.value = selectedID || ''; select.addEventListener('change', guarded(() => change(select.value)));
    return field(key, select);
  }
  async function nameDialog(key, submit, initial = '') {
    const dialog = node('dialog', null, 'atlas-project-dialog'), form = node('form'), name = node('input');
    name.required = true; name.maxLength = 120; name.value = initial;
    const create = node('button', t(key === 'renameProject' ? 'save' : 'create')); create.type = 'submit';
    form.append(node('h2', t(key)), field('name', name), create, button('cancel', () => dialog.close())); dialog.append(form); root.append(dialog);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.addEventListener('submit', guarded(async event => { event.preventDefault(); create.disabled = true; try { await submit(name.value.trim()); dialog.close(); } finally { create.disabled = false; } }));
    dialog.showModal(); name.focus();
  }
  function ordinary() {
    const url = new URL(location.href); url.searchParams.delete('atlas_projects'); history.replaceState(null,'',url);
    cleanup(); scope = null; sessionStorage.removeItem(storageKey()); saveRoute(); root?.remove();
    if (owned) location.href = '/';
  }
  async function createProject() {
    if (!root?.isConnected) await mount();
    return nameDialog('newProject', async name => {
      const project = await api(`/api/atlas/orgs/${scope.org}/projects`, 'POST', { name, instructions: '', default_action: 'chat' });
      await choose({ org: scope.org, project: project.id });
    });
  }
  function navigation() {
    left.replaceChildren();
    if (owned) {
      const logo = node('img'); logo.src = '/static/atlas/brand/svg/atlas-logotype.svg'; logo.alt = 'Atlas'; logo.className = 'atlas-project-logo';
      left.append(logo, button('ordinary', ordinary));
      const row = node('div', null, 'atlas-project-nav-row');
      row.append(button('projects', () => choose({org:scope.org})), iconButton('newProject', '+', createProject)); left.append(row);
    }
    if (owned && memberships.length > 1) left.append(picker('organization', memberships, scope?.org, org => choose({org})));
  }
  function iconButton(key, symbol, fn) {
    const value = button(key, fn); value.textContent = symbol; value.setAttribute('aria-label', t(key)); value.title = t(key); if (key === 'remove') value.dataset.remove = ''; if (key === 'upload') value.dataset.uploadControl = ''; return value;
  }
  function relativeTime(value) {
    const seconds = Math.round((Date.parse(value) - Date.now()) / 1000);
    if (!Number.isFinite(seconds)) return '';
    const unit = Math.abs(seconds) < 3600 ? 'minute' : Math.abs(seconds) < 86400 ? 'hour' : 'day';
    return new Intl.RelativeTimeFormat(window.atlasProjectLocale.language(), {numeric:'auto'}).format(Math.round(seconds / ({minute:60,hour:3600,day:86400}[unit])), unit);
  }
  function projectsPage() {
    root.dataset.view = 'projects'; right.replaceChildren(); center.replaceChildren();
    const header = node('div', null, 'atlas-project-page-heading');
    header.append(node('h1', t('projects')), button('newProject', createProject, 'atlas-project-primary')); center.append(header);
    const toolbar = node('div', null, 'atlas-project-list-toolbar'), tabs = node('div', null, 'atlas-project-tabs'); tabs.setAttribute('role','tablist');
    const available = [['mine','yourProjects'],['shared','sharedProjects']];
    if (projects.some(p => p.visibility === 'organization')) available.push(['organization','organization']);
    if (!available.some(([key]) => key === projectTab)) projectTab = 'mine';
    for (const [key,label] of available) {
      const tab = button(label, () => { projectTab = key; projectsPage(); }); tab.setAttribute('role','tab'); tab.setAttribute('aria-selected', String(key === projectTab)); tabs.append(tab);
    }
    const search = node('input'); search.type = 'search'; search.placeholder = t('searchProjects'); search.setAttribute('aria-label',t('searchProjects')); search.value = projectSearch;

    const searchButton = iconButton('searchProjects', '⌕', () => search.focus()); searchButton.className = 'atlas-project-search-button';
    search.addEventListener('input', () => { projectSearch = search.value; renderCards(); });
    const sort = node('select'); sort.setAttribute('aria-label', t('sortProjects')); sort.append(new Option(t('recentActivity'),'recent'), new Option(t('name'),'name')); sort.value = projectSort;
    sort.addEventListener('change', () => { projectSort = sort.value; renderCards(); });
    toolbar.append(tabs, searchButton, search, sort);
    if (!owned && memberships.length > 1) toolbar.append(picker('organization',memberships,scope.org,org => choose({org})));
    center.append(toolbar);
    const grid = node('div', null, 'atlas-project-grid'); grid.setAttribute('role','tabpanel'); center.append(grid);
    function renderCards() {
      grid.replaceChildren();
      const values = projects.filter(p => (projectTab === 'organization' ? p.visibility === 'organization' : projectTab === 'shared' ? p.shared_with_me === true : p.shared_with_me !== true && p.visibility !== 'organization') && (p.name + ' ' + (p.description || '')).toLocaleLowerCase().includes(projectSearch.toLocaleLowerCase()));
      values.sort((a,b) => projectSort === 'name' ? a.name.localeCompare(b.name) : (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0));
      for (const project of values) {
        const card = node('button', null, 'atlas-project-card'); card.type = 'button'; card.dataset.projectCard = project.id;
        const top = node('div', null, 'atlas-project-card-top'), lock = node('span', '', 'atlas-project-lock'); lock.setAttribute('aria-label',t('privateProject')); lock.title = t('privateProject');
        top.append(node('h2', project.name), lock); card.append(top);
        const summary = node('p', (project.description || '').split('\n')[0], 'atlas-project-card-description'); card.append(summary);
        const counts = node('p', '', 'atlas-project-card-counts'); card.append(counts);
        loadProjectSummary(project, summary, counts);
        card.append(node('small', relativeTime(project.updated_at || project.created_at)));
        card.addEventListener('click', guarded(() => choose({org:scope.org,project:project.id}))); grid.append(card);
      }
      if (!values.length) { const empty = node('div', null, 'atlas-project-empty'); empty.append(node('h2', t(projectTab === 'shared' ? 'noSharedProjects' : 'noProjects')), node('p', t(projectSearch ? 'noSearchResults' : 'projectIntro'))); grid.append(empty); }
    }
    renderCards();
  }
  const projectSummaries = new Map();
  async function loadProjectSummary(project, description, counts) {
    const current = generation, path = `/api/atlas/orgs/${scope.org}/projects/${project.id}`;
    try {
      if (!projectSummaries.has(path)) projectSummaries.set(path, Promise.all([api(path + '/instructions'), list(path + '/files'), list(path + '/conversations')]));
      const [instructions, projectFiles, projectConversations] = await projectSummaries.get(path); checkScope(current);
      description.textContent = (project.description || instructions.text || '').trim().split('\n')[0];
      counts.textContent = `${countLabel(projectFiles.length, 'file', 'files')} · ${countLabel(projectConversations.length, 'conversation', 'conversations')}`;
    } catch(error) { projectSummaries.delete(path); if (error.name !== 'AbortError') errorNotice(counts,error); }
  }
  async function renameProject() {
    await nameDialog('renameProject', async name => {
      projectInfo = await jsonResponse(await request(base(), {method:'PATCH', headers:{'If-Match':`"${projectInfo.revision}"`},body:{name}}), generation);
      projects = projects.map(p => p.id === scope.project ? projectInfo : p); projectHeading();
    }, projectInfo.name);
  }
  function projectHeading() {
    center.querySelector('.atlas-project-heading')?.remove();
    const heading = node('div', null, 'atlas-project-heading');
    const breadcrumb = button('projects', () => choose({org:scope.org}), 'atlas-project-breadcrumb'); heading.append(breadcrumb);
    if (scope.conversation) {
      heading.append(node('span', ' / '), button('project', () => choose({org:scope.org,project:scope.project}), 'atlas-project-breadcrumb'));
      heading.lastChild.textContent = projectInfo.name;
    } else {
      const title = node('div', null, 'atlas-project-page-heading'); title.append(node('h1',projectInfo.name), iconButton('renameProject','✎',renameProject)); heading.append(title);
      if (projectInfo.description) heading.append(node('p', projectInfo.description, 'atlas-project-description'));
    }
    center.prepend(heading);
  }
  function shortTitle(value) {
    const text = (value || '').replace(/\s+/g, ' ').trim();
    if (text.length <= 60) return text;
    const prefix = text.slice(0, 60), end = prefix.lastIndexOf(' ');
    return (end > 0 ? prefix.slice(0, end) : '') + '…';
  }
  function conversationList() {
    const list = node('section', null, 'atlas-project-conversations'); list.append(node('h2', t('conversations')));
    for (const conversation of conversations) {
      const row = node('button', null, 'atlas-project-conversation'); row.type = 'button'; row.dataset.conversation = conversation.id;
      row.title = conversation.title; row.append(node('span', shortTitle(conversation.title)), node('small', relativeTime(conversation.updated_at || conversation.created_at)));
      row.addEventListener('click', guarded(() => choose({org:scope.org,project:scope.project,conversation:conversation.id}))); list.append(row);
    }
    if (!conversations.length) list.append(node('p',t('noConversations')));
    center.append(list);
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
      projectInfo = {...projects.find(p => p.id === value.project), ...await api(base())}; action = 'chat';
      conversations = await list(base() + '/conversations');
      if (value.conversation && !conversations.some(c => c.id === value.conversation)) throw new Error(t('selectionError'));
    }
    if (current !== generation) return;
    saveRoute(); navigation();
    const side = document.getElementById('atlas-side-view'); if (side && sideResize) sideResize.observe(side);
    document.body.classList.add('atlas-project-open');
    hostMain()?.setAttribute('inert', '');
    say('');
    if (value.project) await mountConversation(current); else projectsPage();
  }
  const eligibleFiles = () => files.filter(f => f.locality !== 'local_only' && f.availability !== 'not_retained' && f.id !== projectInfo.instructions_file_id && folders.some(folder => folder.id === f.folder_id && folder.model_use === 'allow'));
  async function mountConversation(current) {
    [files, folders] = await Promise.all([list(base() + '/files'), list(base() + '/folders')]);
    if (current !== generation) return;
    center.replaceChildren(); root.dataset.view = scope.conversation ? 'conversation' : 'project'; projectHeading();
    eligibleFiles().forEach(f => selected.add(f.version_id));
    transcript = node('div', null, 'atlas-project-transcript'); transcript.setAttribute('role', 'log'); transcript.setAttribute('aria-label', t('conversations'));
    activity = node('div'); activity.className = 'atlas-project-activity';
    composer = node('form', null, 'atlas-project-composer');
    input = node('textarea'); input.maxLength = 32000; input.required = true; input.setAttribute('aria-label', t('message'));
    sessionLabel = node('p', '', 'atlas-project-detail');
    uploads = node('input'); uploads.type = 'file'; uploads.multiple = true;
    uploads.addEventListener('change', guarded(() => upload()));
    send = node('button', t('send')); send.type = 'submit'; send.dataset.send = '';
    stop = button('stop', cancel); stop.disabled = true; stop.dataset.stop = '';
    input.placeholder = t('composerPlaceholder');
    const tools = node('div', null, 'atlas-project-composer-tools');
    uploads.setAttribute('aria-label',t('upload')); uploads.hidden = true;
    tools.append(iconButton('upload','+', () => uploads.click()), uploads, sessionLabel, stop, send);
    composer.append(input, tools);
    composer.addEventListener('submit', guarded(submit));
    input.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!send.disabled) composer.requestSubmit(); } });
    const history = node('div', null, 'atlas-project-history'); history.append(transcript, activity); center.append(history, composer);
    if (!scope.conversation) { history.hidden = true; conversationList(); controls(); await panels(); return; }
    await refreshTurns();
    if (scope.session) {
      session = await api(base() + '/work-sessions/' + scope.session);
      if (session.conversation_id !== scope.conversation) throw new Error(t('selectionError'));
      action = 'work';
      selected.clear();
      for (const version of session.base_file_versions) if (files.some(f => f.version_id === version.version_id)) selected.add(version.version_id);
    }
    await setAction();
    await replay(); await restoreSession(); await panels();
    if (current === generation) { input.focus(); schedule(current); }
  }
  function renderTurns() {
    transcript.replaceChildren();
    for (const turn of turns) {
      const questions = turn.content_refs.filter(ref => ref.kind === 'question' && ref.text);
      if (questions.length) {
        const userArticle = node('article', null, 'atlas-project-turn'); userArticle.dataset.turn = turn.id;
        questions.forEach(ref => userArticle.append(prose(ref.text, 'question')));
        transcript.append(userArticle);
      }
      const article = node('article', null, 'atlas-project-turn'); article.dataset.turn = turn.id;
      const answers = turn.content_refs.filter(ref => ref.kind !== 'question' && ref.text);
      if (answers.length) article.append(node('small', `Atlas · ${t(turn.state)}`));
      answers.forEach(ref => article.append(prose(withoutFileLinks(ref.text), 'answer')));
      const steps = [...events.values()].filter(e => e.version && e.request_id === turn.request_id && (!e.conversation_id || e.conversation_id === scope.conversation));
      if (steps.length) {
        const group = node('details', null, 'atlas-project-disclosure'); group.dataset.steps = turn.request_id;
        group.append(node('summary', `${t('steps')} (${steps.length})`));
        const terminal = ['completed','failed','cancelled'].includes(turn.state);
        for (const step of steps.sort((a,b) => a.seq - b.seq)) {
          const state = step.status || step.state || 'running';
          group.append(node('p', t(terminal && ['running','ready','awaiting_approval'].includes(state) ? turn.state : state)));
        }
        article.append(group);
      }
      for (const item of panelData?.deliverables || []) if (item.turn_id === turn.request_id || item.request_id === turn.request_id || turn.content_refs.some(ref => ref.text?.includes(item.download_route))) article.append(fileCard(item));
      const lastContext = turn.usage?.context;
      if (lastContext?.layers) {
        const used = node('details', null, 'atlas-project-disclosure'); used.dataset.contextUsed = ''; used.append(node('summary',t('contextUsed')));
        for (const [layer,count] of Object.entries(lastContext.layers)) used.append(detail(layer,count)); article.append(used);
      }
      transcript.append(article);
    }
    for (const task of panelData?.scheduled?.items || []) if (task.proposal && task.conversation_id === scope.conversation) {
      const card=node('section',null,'atlas-project-approval');card.dataset.scheduleProposal=task.id;
      card.append(node('h3',task.name),node('p',task.instruction),node('p',scheduleWords(task)),detail('taskCap',task.daily_cap_eur));
      if(task.target_file) card.append(detail('taskTarget',task.target_file));
      if(panelData.scheduled.can_manage && task.can_confirm !== false) card.append(button('taskConfirm',async () => {await api(base()+'/scheduled-tasks/'+task.id+'/confirm','POST',{});await panels();}));
      transcript.append(card);
    }
  }
  function withoutFileLinks(text) {
    const cards = panelData?.deliverables || [];
    const routes = new Set(cards.flatMap(item => [item.download_route, item.download_route?.replace(/\/download$/, '/preview')]));
    const ids = new Set(cards.map(item => item.id));
    const names = cards.flatMap(item => [files.find(file => file.version_id === item.file_version_id)?.relative_path, item.filename]).filter(Boolean).map(name => name.split('/').pop().replace(/[*_`]/g,''));
    return text.replace(/\bchunk\s+`?[a-f0-9]{32,64}`?\s*:\s*/gi, '').replace(/^.*(?:mémoire|memory|record|identifiant).*\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b.*$/gim, '').replace(/\[([^\[\]]+)\]\(([^)]+)\)/g, (match, label, href) => {
      try {
        const path = new URL(href,location.origin).pathname, artifact = path.match(/\/deliverables\/([^/]+)\/(?:download|preview)$/);
        return routes.has(path) || (artifact && ids.has(artifact[1])) ? '' : match;
      } catch { return match; }
    }).split('\n').filter(line => {
      const plain = line.replace(/[*_`]/g,'').trim().replace(/^#{1,6}\s+/, '').replace(/^[-•]\s*/, '');
      if (/^\s*(?:[#*_`]+\s*)?(?:La\s+)?(?:validation|vérification|verification)\b.*\b(?:package_readable|requested_content|source_coverage)\b/i.test(line)) return false;
      if (cards.length && names.some(name => plain.startsWith(name) && /^[\s·|:—–-]*(?:\([\d\s]+ (?:octets|bytes)\))?[\s·|:—–-]*$/i.test(plain.slice(name.length)))) return false;
      return !(/^(?:(?:Lien de )?(?:téléchargement|prévisualisation)|(?:Download|Preview)(?: link)?)\s*:\s*[·|—–-]*$/i.test(plain) || (cards.length && /^(?:Fichier produit|File produced)(?:\s*:|\s*$)/i.test(plain)) || (cards.length && /^(?:Fichier|File)(?:\s+(?:DOCX|XLSX|PDF|PPTX))?\s*:\s*[·|—–-]*$/i.test(plain)) || plain === '·');
    }).join('\n').trim();
  }
  let markdownModule;
  function scopedLink(label, href) {
    const link = node('a',label); link.href = projectURL(href);
    if (new URL(link.href).pathname.includes('/sources/')) link.addEventListener('click', guarded(async event => {
      event.preventDefault(); const source = await api(link.href);
      await window.atlasSideView.open({kind:'markdown',content:source.text,title:`${files.find(file => file.id === source.file_id)?.relative_path || t('files')} · ${locator(source.locator)}`,readOnly:true});
    }));
    return link;
  }
  function prose(text, cls) {
    const content = node('div', null, cls), plain = node('pre'); content.append(plain);
    // Safe text remains usable if the optional local Markdown asset cannot load.
    const pattern = /\[([^\[\]]+)\]\(([^)]+)\)/g;
    let offset = 0;
    for (const match of text.matchAll(pattern)) {
      plain.append(document.createTextNode(text.slice(offset,match.index)));
      try {plain.append(scopedLink(match[1],match[2]));} catch {plain.append(document.createTextNode(match[0]));}
      offset = match.index + match[0].length;
    }
    plain.append(document.createTextNode(text.slice(offset)));
    if (cls === 'question') return content;
    const current = generation;
    markdownModule ||= import('/static/atlas/vendor/marked/marked.esm.js');
    markdownModule.then(({marked}) => {
      if (current !== generation || !content.isConnected) return;
      const renderer = new marked.Renderer(); renderer.html = () => ''; renderer.image = () => '';
      const parsed = new DOMParser().parseFromString(marked.parse(text,{renderer,async:false}),'text/html');
      const allowed = new Set('P BR HR H1 H2 H3 H4 H5 H6 BLOCKQUOTE UL OL LI PRE CODE STRONG EM DEL TABLE THEAD TBODY TR TH TD A'.split(' '));
      const fragment = document.createDocumentFragment();
      const copy = (source,target) => {
        for (const child of source.childNodes) {
          if (child.nodeType === Node.TEXT_NODE) {target.append(document.createTextNode(child.textContent)); continue;}
          if (child.nodeType !== Node.ELEMENT_NODE || !allowed.has(child.tagName)) continue;
          if (child.tagName === 'A') {
            try {target.append(scopedLink(child.textContent,child.getAttribute('href')));} catch {target.append(document.createTextNode(child.textContent));}
          } else {const element = node(child.tagName); copy(child,element); target.append(element);}
        }
      };
      copy(parsed.body,fragment); content.replaceChildren(fragment); content.dataset.format = 'markdown';
    }).catch(() => {content.dataset.format = 'text';});
    return content;
  }
  async function refreshTurns() {
    const current = generation;
    const result = await list(base() + '/conversations/' + scope.conversation + '/turns'); checkScope(current); turns = result; if (!sending) renderTurns(); renderEvents();
  }
  function controls() {
    const activeWork = action === 'work' && ['running','awaiting_approval','cancelling'].includes(session?.state);
    send.disabled = sending || configuring || uploading || activeWork; input.disabled = sending;
    uploads.disabled = sending || configuring || uploading || activeWork; stop.disabled = !sending && !['running','awaiting_approval','cancelling'].includes(session?.state);
    stop.hidden = stop.disabled;
    refreshApprovals();
    root.querySelectorAll('[data-upload-control],.atlas-project-drop').forEach(control => {control.disabled = uploads.disabled;});
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
    sessionLabel.textContent = sending ? t('running') : 'Atlas';
    controls();
  }
  async function upload(incoming = [...uploads.files]) {
    const current = generation; uploading = true; controls();
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
    const opened = new Set([...transcript.querySelectorAll('details[data-steps][open]')].map(n => n.dataset.steps));
    renderTurns();
    transcript.querySelectorAll('details[data-steps]').forEach(n => {n.open = opened.has(n.dataset.steps);});
    activity.replaceChildren();
    const approvals = new Map();
    for (const event of [...events.values()].sort((a,b) => a.seq - b.seq)) {
      if (!turns.some(turn => turn.request_id === event.request_id) && !liveRequests.has(event.request_id)) continue;
      if (event.conversation_id && event.conversation_id !== scope.conversation) continue;
      if (event.version && event.output?.approval) approvals.set(event.output.approval.id, event.output.approval);
    }
    for (const value of approvals.values()) activity.append(approval(value));
  }
  function refreshApprovals() {
    if (!root) return;
    const pending = new Set((session?.pending_approvals || []).filter(value => value.decision === 'pending' && Date.parse(value.expires_at) > Date.now()).map(value => value.id));
    root.querySelectorAll('[data-approval]').forEach(card => card.querySelectorAll('button').forEach(control => { control.disabled = card.dataset.decided === 'true' || !pending.has(card.dataset.approval); }));
  }
  function approval(value) {
    const card = node('section', null, 'atlas-project-approval'); card.dataset.approval = value.id;
    card.append(node('strong', t('approvals')), node('p', `${t(value.action_kind)} · ${value.paths.join(', ')}`), node('p', `${t(value.destination.kind)} · ${value.maximum_bytes} ${t('bytes')}`), node('p', `${t('expires')}: ${new Intl.DateTimeFormat(window.atlasProjectLocale.language(),{dateStyle:'short',timeStyle:'short'}).format(new Date(value.expires_at))}`));
    let alwaysAllow;
    if (value.remote) {
      const remote = value.remote;
      card.append(node('p', `${t('target')}: ${remote.target_name}`), node('pre', remote.command), node('p', `${t('directory')}: ${remote.cwd} · ${t('timeout')}: ${remote.timeout_s}s`), node('p', `${t('codeDigest')}: ${remote.code_digest}`));
      if (remote.code_egress) card.append(node('p', t('codeTransfer')));
      alwaysAllow = node('input'); alwaysAllow.type = 'checkbox'; alwaysAllow.dataset.runnerAlways = '';
      const label = node('label', t('alwaysAllow')); label.prepend(alwaysAllow); card.append(label);
    }
    if (value.publication) {
      const publication = value.publication;
      for (const [label, text] of [['Branch', publication.branch], ['Target', publication.target], ['Commit message', publication.message], ['PR title', publication.title], ['PR body', publication.body], ['Base commit', publication.base_commit]]) card.append(node('p', `${t(label)}: ${text}`));
      const changes = node('ul');
      for (const change of publication.diff) changes.append(node('li', `${t(change.status)}: ${change.path}`));
      card.append(changes);
    }
    if (value.decision !== 'pending') { card.dataset.decided = 'true'; card.append(node('p', t(value.decision))); }
    for (const allow of [true, false]) {
      const b = button(allow ? 'accept' : 'deny', async () => {
        root.querySelectorAll('[data-approval]').forEach(other => { if (other.dataset.approval === value.id) { other.dataset.decided = 'true'; other.querySelectorAll('button').forEach(control => { control.disabled = true; }); } });
        try { await api(base() + '/approvals/' + value.id + '/decision', 'POST', { allow, action_digest: value.action_digest, ...(value.remote ? { always_allow: allow && alwaysAllow.checked } : {}) }); card.append(node('p', t('approveRecorded'))); }
        catch (error) { card.append(node('p', t('failure'))); throw error; }
      });
      b.disabled = value.decision !== 'pending' || !(session?.pending_approvals || []).some(a => a.id === value.id && a.decision === 'pending' && Date.parse(a.expires_at) > Date.now()); card.append(b);
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
  async function restoreSession() {
    if (session || !scope?.conversation) return;
    const latest = [...events.values()].filter(e => e.conversation_id === scope.conversation && (e.session_id || e.output?.approval?.session_id)).sort((a,b) => b.seq - a.seq)[0];
    const id = latest?.session_id || latest?.output?.approval?.session_id;
    if (!uuid(id)) return;
    const value = await api(base() + '/work-sessions/' + id);
    if (value.conversation_id !== scope.conversation) throw new Error(t('selectionError'));
    session = value; scope.session = id; action = 'work'; selected.clear();
    for (const version of session.base_file_versions) if (files.some(f => f.version_id === version.version_id)) selected.add(version.version_id);
    saveRoute(); controls();
  }
  function schedule(current) {
    polling = setTimeout(async () => {
      try {
        await replay(); await restoreSession();
        if (session) { const id = session.id, value = await api(base() + '/work-sessions/' + id); if (session?.id === id) { session = value; controls(); } }
        await refreshTurns();
        if(panelData && !root.querySelector('dialog[open]') && (!panelData.scheduledAt || Date.now()-panelData.scheduledAt>15000)) {
          panelData.scheduled=await api(base()+'/scheduled-tasks?limit=100');panelData.scheduledAt=Date.now();renderPanels();renderTurns();
        }
      } catch (error) { if (current === generation) failure(error); }
      if (current === generation) schedule(current);
    }, 1500);
  }
  async function cancel() {
    const current = generation;
    if (action === 'work' && session) {
      const active = await api(base() + '/work-sessions/' + session.id);
      if (active.lease?.request_id) await api(base() + '/work-sessions/' + active.id + '/cancel', 'POST', { request_id: active.lease.request_id });
    }
    checkScope(current); stream?.abort(); say(t('stopped'));
  }
  async function submit(event) {
    event.preventDefault(); if (sending || configuring || uploading || !input.value.trim()) return;
    const question = input.value.trim();
    if (!scope.conversation) {
      configuring = true; controls();
      try { const conversation = await api(base() + '/conversations', 'POST', {title:shortTitle(question)}); await choose({org:scope.org,project:scope.project,conversation:conversation.id}); input.value = question; }
      finally { configuring = false; controls(); }
    }
    const current = generation;
    sending = true; sessionLabel.textContent = t('running'); controls(); stream = new AbortController();
    const pending = node('article', null, 'atlas-project-turn'); const answer = node('pre', '', 'answer'); pending.append(node('pre', question, 'question'), answer); transcript.append(pending);
    try {
      // The scoped agent can answer directly or call tools; no user mode selection.
      action = 'work';
      [files, folders] = await Promise.all([list(base() + '/files'), list(base() + '/folders')]);
      const versions = eligibleFiles().map(file => file.version_id);
      if (session && (session.base_file_versions.length !== versions.length || session.base_file_versions.some(file => !versions.includes(file.version_id)))) {session = null; delete scope.session;}
      selected = new Set(versions);
      if (action === 'work' && (!session || ['failed','cancelled'].includes(session.state))) { session = null; await setAction(); }
      const profile = action === 'work' ? (window.atlasProjectWorkProfile || window.atlasProjectProfile) : window.atlasProjectProfile;
      const body = { atlas_ui: true, model: profile, ui_locale: window.atlasProjectLocale.language(), ui_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, org_id: scope.org, project_id: scope.project, conversation_id: scope.conversation, project_action: action, file_version_ids: [...selected], messages: [{ role: 'user', content: question }], stream: true };
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
      if (current === generation) {
        sending = false; configuring = true; controls();
        try { await refreshTurns(); await replay(); if (session) session = await api(base() + '/work-sessions/' + session.id); [files, folders] = await Promise.all([list(base() + '/files'), list(base() + '/folders')]); await panels(); }
        finally { if (current === generation) { configuring = false; sessionLabel.textContent = 'Atlas'; controls(); input.focus(); } }
      }
    }
  }
  let panelData, sideResize, hostResize, observedHost;
  const detail = (key, value) => node('p', `${t(key)}: ${value == null ? t('unknown') : value}`, 'atlas-project-detail');
  async function panels() {
    const current = generation;
    const [instructions, memory, deliverables, scheduled] = await Promise.all([api(base() + '/instructions'),list(base() + '/memory'),list(base() + '/deliverables'),api(base()+'/scheduled-tasks?limit=100')]);
    checkScope(current); panelData = {instructions,memory,deliverables,scheduled}; renderPanels(); renderTurns();
  }
  async function requestChange(key, value) {
    const conversation = await api(base() + '/conversations', 'POST', {title:t(key)});
    await choose({org:scope.org,project:scope.project,conversation:conversation.id});
    input.value = t(key) + '\n' + (value.text ?? value.path) + '\n' + t('changeApproval');
    composer.requestSubmit();
  }
  function confirmChange(key, value) {
    const dialog = node('dialog',null,'atlas-project-dialog');
    dialog.append(node('h2',t('confirmRemove')),node('p',value.path || value.text),node('p',t('changeApproval')),
      button('remove',async () => {dialog.close(); await requestChange(key,value);}),button('cancel',() => dialog.close()));
    dialog.addEventListener('close',() => dialog.remove(),{once:true}); root.append(dialog); dialog.showModal();
  }
  function fileCard(item) {
    const file = files.find(f => f.version_id === item.file_version_id), card = node('section', null, 'atlas-project-file-card'); card.dataset.deliverable = item.id;
    const name = (file?.relative_path || item.filename || t('files')).split('/').pop();
    card.append(node('span', name.split('.').pop().toUpperCase(), 'atlas-project-file-type'), node('strong',name)); card.querySelector('strong').title = name;
    const actions = node('div'); const download = node('a',t('download')); download.href = projectURL(item.download_route); download.download = name;
    actions.append(button('preview', () => previewDeliverable(item)),download); card.append(actions); return card;
  }
  function scheduleWords(value) {
    const s = value.schedule, weekday = new Intl.DateTimeFormat(window.atlasProjectLocale.language(), {weekday:'long',timeZone:'UTC'}).format(new Date(Date.UTC(2026,0,5+s.weekday)));
    const cadence = s.frequency === 'daily' ? t('taskDaily') : s.frequency === 'weekly' ? t('taskWeekly').replace('{day}',weekday) : t('taskEvery').replace('{n}',s.days);
    return `${cadence} · ${s.time} (${s.timezone})`;
  }
  function taskInput(value) {
    return Object.fromEntries(['name','instruction','schedule','target_file','enabled','daily_cap_eur','locale'].map(key => [key,value[key]]));
  }
  async function changeTask(value, enabled) {
    await apiTask(value, 'PUT', {...taskInput(value),enabled}); await panels();
  }
  async function apiTask(value, method, body) {
    const current = generation;
    return jsonResponse(await request(base()+'/scheduled-tasks/'+value.id,{method,body,headers:{'If-Match':`"${value.revision}"`}}),current);
  }
  function scheduledPanel(data) {
    const panel = node('section',null,'atlas-project-panel'); panel.dataset.panel = 'scheduled';
    const heading = node('div',null,'atlas-project-panel-heading'); heading.append(node('h2',t('taskHeading')));
    if (data.can_manage) heading.append(iconButton('taskCreate','+',() => taskDialog()));
    panel.append(heading);
    if (!data.items.length) panel.append(node('p',t('taskEmpty'),'atlas-project-detail'));
    for (const value of data.items) {
      const row = node('article',null,'atlas-project-scheduled-task'); row.dataset.taskId=value.id;
      row.append(node('strong',value.name),node('p',scheduleWords(value),'atlas-project-detail'));
      const next = value.enabled ? new Intl.DateTimeFormat(window.atlasProjectLocale.language(),{dateStyle:'medium',timeStyle:'short',timeZone:value.schedule.timezone}).format(new Date(value.next_run)) : t(value.proposal ? 'proposed' : 'taskPaused');
      row.append(node('p',`${t('taskNext')}: ${next}`,'atlas-project-detail'));
      if (value.pause_reason) row.append(node('p',t('taskReason_'+value.pause_reason),'atlas-project-detail'));
      if (value.last_result) {
        const result = value.last_result, label = t('taskLast')+': '+t(result.status === 'budget_cap' ? 'taskCapReached' : result.status);
        if (result.conversation_id) {
          const link = node('a',label); link.href = `?atlas_projects=1&org=${encodeURIComponent(scope.org)}&project=${encodeURIComponent(scope.project)}&conversation=${encodeURIComponent(result.conversation_id)}`;
          link.addEventListener('click',guarded(async event => {event.preventDefault();await choose({org:scope.org,project:scope.project,conversation:result.conversation_id});})); row.append(link);
        } else row.append(node('p',label,'atlas-project-detail'));
      }
      if (data.can_manage) {
        const actions = node('div',null,'atlas-project-task-actions');
        if (value.proposal && value.can_confirm !== false) actions.append(button('taskConfirm',async () => {await api(base()+'/scheduled-tasks/'+value.id+'/confirm','POST',{});await panels();}));
        else if (!value.proposal) actions.append(button(value.enabled ? 'taskPause' : 'taskResume',() => changeTask(value,!value.enabled)));
        actions.append(button('taskEdit',() => taskDialog(value)));
        if (value.enabled && value.last_result?.status !== 'running') actions.append(button('taskRun',async () => {await api(base()+'/scheduled-tasks/'+value.id+'/run','POST',{});await panels();}));
        actions.append(button('remove',() => deleteTask(value))); row.append(actions);
      }
      panel.append(row);
    }
    return panel;
  }
  function deleteTask(value) {
    const dialog=node('dialog',null,'atlas-project-dialog'); dialog.dataset.taskDelete='';
    dialog.append(node('h2',t('taskDelete')),node('p',value.name),button('remove',async () => {await apiTask(value,'DELETE');dialog.close();await panels();}),button('cancel',() => dialog.close()));
    dialog.addEventListener('close',() => dialog.remove(),{once:true});root.append(dialog);dialog.showModal();
  }
  function taskDialog(value) {
    const dialog=node('dialog',null,'atlas-project-dialog atlas-project-task-dialog'), form=node('form'); dialog.dataset.taskDialog='';
    const name=node('input'), instruction=node('textarea'), frequency=node('select'), days=node('input'), time=node('input'), zone=node('input'), weekday=node('select'), target=node('input'), cap=node('input');
    name.required=true; name.maxLength=120; name.value=value?.name || '';
    instruction.required=true;instruction.maxLength=8000;instruction.rows=4;instruction.value=value?.instruction || '';
    for (const key of ['daily','every','weekly']) frequency.append(new Option(t('taskFrequency_'+key),key));frequency.value=value?.schedule.frequency || 'daily';
    days.type='number';days.min='1';days.max='365';days.value=value?.schedule.days || 3;
    time.type='time';time.required=true;time.value=value?.schedule.time || '09:00';
    zone.required=true;zone.value=value?.schedule.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    for (let i=0;i<7;i++) weekday.append(new Option(new Intl.DateTimeFormat(window.atlasProjectLocale.language(),{weekday:'long',timeZone:'UTC'}).format(new Date(Date.UTC(2026,0,5+i))),i));weekday.value=value?.schedule.weekday ?? 0;
    target.maxLength=240;target.value=value?.target_file || '';target.placeholder='Summary.docx';
    cap.type='number';cap.min='0.01';cap.max='2';cap.step='0.01';cap.required=true;cap.value=value?.daily_cap_eur ?? 0.5;
    const daysField=field('taskDays',days), weekdayField=field('taskWeekday',weekday);
    function cadence() {daysField.hidden=frequency.value!=='every';weekdayField.hidden=frequency.value!=='weekly';}frequency.addEventListener('change',cadence);cadence();
    const submit=node('button',t(value ? 'save' : 'create'));submit.type='submit';
    const actions=node('div',null,'atlas-project-task-actions');actions.append(submit,button('cancel',() => dialog.close()));
    form.append(node('h2',t(value ? 'taskEdit' : 'taskCreate')),field('name',name),field('taskInstruction',instruction),field('taskFrequency',frequency),daysField,weekdayField,field('taskTime',time),field('taskZone',zone),field('taskTarget',target),field('taskCap',cap),actions);
    form.addEventListener('submit',guarded(async event => {event.preventDefault();submit.disabled=true;try {
      const body={name:name.value.trim(),instruction:instruction.value.trim(),schedule:{frequency:frequency.value,days:Number(days.value),weekday:Number(weekday.value),time:time.value,timezone:zone.value,anchor:value?.schedule.anchor || null},target_file:target.value.trim() || null,enabled:value?.enabled ?? true,daily_cap_eur:Number(cap.value),locale:window.atlasProjectLocale.language()};
      if(value) await apiTask(value,'PUT',body);else await api(base()+'/scheduled-tasks','POST',body);
      dialog.close();await panels();
    } finally {submit.disabled=false;}}));
    dialog.append(form);root.append(dialog);dialog.addEventListener('close',() => dialog.remove(),{once:true});dialog.showModal();name.focus();
  }

  function renderPanels() {
    if (!panelData) return;
    const {instructions,memory,deliverables} = panelData; right.replaceChildren();
    const panels = node('div', null, 'atlas-project-panels'); right.append(panels);
    const instructionsPanel = node('section', null, 'atlas-project-panel'); instructionsPanel.dataset.panel = 'instructions';
    const text = node('textarea'); text.value = instructions.text || ''; text.setAttribute('aria-label',t('instructions')); text.placeholder = t('instructionsPlaceholder'); text.maxLength = 4000;
    const change = button('editInConversation', () => requestChange('changeInstructions', {text:text.value})); change.hidden = true;
    text.addEventListener('input', () => {change.hidden = text.value === (instructions.text || '');});
    instructionsPanel.append(node('h2',t('instructions')),text,change); panels.append(instructionsPanel);
    const filePanel = node('section',null,'atlas-project-panel'); filePanel.dataset.panel = 'files';
    const title = node('div',null,'atlas-project-panel-heading'); title.append(node('h2',t('files')),iconButton('upload','+',() => uploads.click())); filePanel.append(title);
    const drop = node('button',t('dropFiles'),'atlas-project-drop'); drop.type = 'button'; drop.addEventListener('click',() => uploads.click());
    drop.addEventListener('dragover', event => {event.preventDefault(); drop.classList.add('dragging');});
    drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
    drop.addEventListener('drop',guarded(async event => {event.preventDefault();drop.classList.remove('dragging'); if (!uploads.disabled) await upload([...event.dataTransfer.files]);})); filePanel.append(drop);
    for (const file of files.filter(f => f.id !== projectInfo.instructions_file_id)) {
      const item = deliverables.find(d => d.file_version_id === file.version_id);
      if (item) {const card = fileCard(item); card.append(iconButton('remove','×',() => confirmChange('removeFile',{file_id:file.id,path:file.relative_path,base_version:file.version_id,revision:file.revision}))); filePanel.append(card);continue;}
      const row = node('div',null,'atlas-project-file'); row.dataset.fileId = file.id;
      const open = button('preview',() => previewFile(file)); open.textContent = file.relative_path; open.title = file.relative_path; row.append(open,iconButton('remove','×',() => confirmChange('removeFile', {file_id:file.id,path:file.relative_path,base_version:file.version_id,revision:file.revision}))); filePanel.append(row);
    }
    for (const item of deliverables.filter(d => !files.some(f => f.version_id === d.file_version_id))) filePanel.append(fileCard(item));
    panels.append(filePanel, scheduledPanel(panelData.scheduled));
    if (memory.length) {
      const memoryPanel = node('section',null,'atlas-project-panel'); memoryPanel.dataset.panel = 'memory'; memoryPanel.append(node('h2',t('memory')));
      for (const entry of memory) { const row = node('div',null,'atlas-project-memory'); row.append(node('p',entry.text),iconButton('remove','×',() => confirmChange('removeMemory',{memory_id:entry.id,text:entry.text,revision:entry.revision}))); memoryPanel.append(row); } panels.append(memoryPanel);
    }
    controls();
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
    else { const content = await response.text(); checkScope(current); await window.atlasSideView.open({ kind:'markdown', content, title:t('preview'), readOnly:true }); }
  }
  function hostMain() {
    const input = document.querySelector('#chat-input');
    const main = document.querySelector('#chat-container') || input?.closest('main') || input?.closest('#chat');
    if (main) main.dataset.atlasProjectHostMain = '';
    return main;
  }
  function installNavigation() {
    if (owned || !identity || /^\/(auth|admin)/.test(location.pathname)) return;
    const sidebar = document.querySelector('#pinned-menu-items-list') || document.querySelector('#sidebar');
    if (!sidebar) return;
    if (!document.getElementById('atlas-project-entry')) {
      const row = node('div',null,'atlas-project-nav-row'); row.id = 'atlas-project-navigation';
      const entry = button('projects',async () => {await mount(); await choose({org:scope?.org || memberships[0]?.org_id});}); entry.id = 'atlas-project-entry';
      row.append(entry,iconButton('newProject','+',createProject));
      const anchor = sidebar.id === 'pinned-menu-items-list' ? null : sidebar.querySelector('a[href="/notes"],a[href="/workspace"],a[href="/"]');
      if (anchor) (anchor.parentElement === sidebar ? anchor : anchor.parentElement).insertAdjacentElement('afterend',row); else sidebar.prepend(row);
    }
    document.body.classList.add('atlas-project-enabled');
    const rail = document.querySelector('#sidebar');
    if (sidebar.id === 'pinned-menu-items-list' && rail && !rail.querySelector('[data-project-rail]')) {
      const entry = iconButton('projects','▦', async () => {await mount();await choose({org:scope?.org || memberships[0]?.org_id});}); entry.dataset.projectRail = ''; rail.append(entry);
      new ResizeObserver(entries => {entry.hidden = entries[0].contentRect.width > 100;}).observe(rail);
    }
    document.querySelectorAll('[aria-controls="sidebar-folders-content"]').forEach(control => {control.parentElement.hidden = true;});
    const title = document.title.replace(/Atlas \(Open WebUI\)|Open WebUI/g,'Atlas');
    if (document.title !== title) document.title = title;
    if (root?.isConnected && !owned) {
      const main = hostMain();
      if (main) {
        const align = () => {if (root?.isConnected) root.style.left = Math.max(0,main.getBoundingClientRect().left) + 'px';};
        if (observedHost !== main) {
          hostResize?.disconnect(); observedHost = main;
          hostResize = new ResizeObserver(align); hostResize.observe(main);
        }
        align();
      }
    }
  }
  async function adminEntry() {
    if (!location.pathname.startsWith('/admin/settings') || document.getElementById('atlas-project-admin')) return;
    lifecycle = new AbortController();
    const me = await api('/api/atlas/me');
    memberships = me.memberships.filter(m => m.role === 'admin');
    if (!memberships.length) return;
    scope = {org:memberships[0].org_id};
    projects = await list(`/api/atlas/orgs/${scope.org}/projects`);
    root = node('div',null,'atlas-project-admin'); root.id = 'atlas-project-admin';
    root.append(button('Remote targets',runnerAdmin));
    (document.querySelector('#main-content') || document.body).append(root);
  }
  async function runnerAdmin() {
    const current = generation, orgBase = `/api/atlas/orgs/${scope.org}/runner-targets`;
    const dialog = node('dialog', null, 'atlas-project-dialog'), panel = node('section'), form = node('form');
    dialog.dataset.runnerAdmin = ''; panel.append(node('h2', t("Remote execution targets")));
    panel.append(node('p', t("Register a dedicated Linux or macOS account. Host key fingerprints must be verified independently. Private keys stay in the runner broker.")));
    const entries = node('div'), status = node('p'); status.setAttribute('role', 'status');
    async function reload() {
      const targets = await api(orgBase); checkScope(current); entries.replaceChildren();
      for (const target of targets.items) {
        const row = node('section'); row.append(node('strong', target.name), node('p', `${target.os} · ${target.permission_mode} · ${target.status}`));
        const probe = node('button', t("Probe")); probe.type = 'button'; probe.addEventListener('click', guarded(async () => { const result = await api(`${orgBase}/${target.id}/probe`, 'POST', {}); status.textContent = JSON.stringify(result.last_probe); await reload(); })); row.append(probe);
        for (const enabled of [true, false]) { const grant = node('button', t(enabled ? 'Grant to this project' : 'Revoke from this project')); grant.type = 'button'; grant.disabled = !scope.project; grant.addEventListener('click', guarded(async () => { await api(base() + `/runner-targets/${target.id}/grant`, 'PUT', { enabled }); status.textContent = t(enabled ? 'Target granted to this project.' : 'Grant and egress permission revoked.'); })); row.append(grant); }
        const audit = node('button', t("Command audit")); audit.type = 'button'; audit.disabled = !scope.project; audit.addEventListener('click', guarded(async () => { const result = await api(base() + `/runner-targets/${target.id}/commands`); const detail = node('pre', JSON.stringify(result.items, null, 2)); row.append(detail); })); row.append(audit);
        const mode = node('select'); for (const value of ['ask','rules','auto']) mode.append(new Option(t(value), value)); mode.value = target.permission_mode; mode.setAttribute('aria-label', `${t('Permission mode')}: ${target.name}`);
        mode.addEventListener('change', guarded(async () => {
          const fields = ['name','os','arch','shell','host','port','user','work_root','credential_ref','credential_fingerprint','host_key_fingerprints','jump_target_id','capabilities','permission_mode','allow_rules','deny_rules','sensitive_rules','max_parallel_sessions','disk_quota_bytes','monthly_minutes','same_locality'];
          const body = Object.fromEntries(fields.map(key => [key, key === 'permission_mode' ? mode.value : target[key]]));
          await request(`${orgBase}/${target.id}`, { method:'PUT', headers:{'If-Match':`"${target.revision}"`}, body }); await api(`${orgBase}/${target.id}/probe`, 'POST', {}); await reload();
        })); row.append(mode); entries.append(row);
      }
    }
    const controls = {};
    for (const [key, label, value] of [['name','Name',''],['host','Host',''],['port','SSH port','22'],['user','SSH user','runner'],['work_root','Working root','/home/runner/work'],['host_key_fingerprints','Pinned host key SHA256 fingerprints (one per line)',''],['credential_ref','Credential reference',''],['credential_fingerprint','Credential public key fingerprint',''],['allow_rules','Allow command rules (one per line)','make *\npython3 -m unittest *'],['deny_rules','Deny command rules (one per line)','sudo *']]) {
      const input = node(key.endsWith('rules') || key === 'host_key_fingerprints' ? 'textarea' : 'input'); input.name = key; input.value = value; input.setAttribute('aria-label', t(label)); if (!key.endsWith('rules')) input.required = true;
      const labelNode = node('label', t(label)); labelNode.append(input); form.append(labelNode); controls[key] = input;
    }
    const targetOS = node('select'); targetOS.name = 'os'; targetOS.setAttribute('aria-label',t('Target operating system')); for (const os of ['linux','macos']) targetOS.append(new Option(os,os)); form.append(targetOS);
    const permission = node('select'); permission.name = 'permission_mode'; permission.setAttribute('aria-label',t('Permission mode')); for (const mode of ['rules','ask','auto']) permission.append(new Option(t(mode),mode)); form.append(permission);
    const publicKey = node('pre'), generate = node('button', t("Generate a key pair")); generate.type = 'button';
    generate.addEventListener('click', guarded(async () => { generate.disabled = true; try { const key = await api(orgBase + '/credentials', 'POST', {}); controls.credential_ref.value = key.credential_ref; controls.credential_fingerprint.value = key.fingerprint; publicKey.textContent = `${t('Install this public key in the target account. It is shown once:')}\n${key.public_key}\n${key.fingerprint}`; } finally { generate.disabled = false; } }));
    const submit = node('button', t("Register target")); submit.type = 'submit';
    form.addEventListener('submit', guarded(async event => {
      event.preventDefault(); submit.disabled = true;
      try { const value = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value.trim()])); value.port = Number(value.port); for (const key of ['allow_rules','deny_rules','host_key_fingerprints']) value[key] = value[key].split('\n').map(s => s.trim()).filter(Boolean); value.permission_mode = permission.value; value.os = targetOS.value;
        const target = await api(orgBase, 'POST', value); const probed = await api(`${orgBase}/${target.id}/probe`, 'POST', {}); status.textContent = JSON.stringify(probed.last_probe); await reload();
      } finally { submit.disabled = false; }
    }));
    form.append(generate, publicKey, submit); panel.append(picker('project',projects,scope.project,async project => {scope.project = project; await reload();}), entries, form, status, button('close', () => dialog.close())); dialog.append(panel); root.append(dialog); dialog.addEventListener('close', () => { publicKey.textContent = ''; dialog.remove(); }, {once:true}); dialog.showModal(); await reload();
  }
  async function boot() {
    if (location.pathname.startsWith('/admin/settings')) {try {await adminEntry();} catch(error) {showFailure(error);} return;}
    if (/^\/(auth|admin)/.test(location.pathname)) return;
    if (booting) return; booting = true;
    try {
      const me = await fetch('/api/atlas/me', {credentials:'same-origin'});
      if (!me.ok) {if (owned || selections().has('project') || selections().has('atlas_projects')) throw new Error(`${t('failure')} (${me.status})`); return;}
      memberships = (await me.json()).memberships;
      const host = await fetch('/api/v1/auths/', {credentials:'same-origin'});
      if (!host.ok) {if (!owned && !selections().has('project')) return; throw new Error(t('signIn'));}
      identity = (await host.json()).id; if (!identity) throw new Error(t('signIn'));
      document.getElementById('atlas-project-failure')?.remove();
      for (let i = sessionStorage.length - 1; i >= 0; i--) { const key = sessionStorage.key(i); if (key.startsWith('atlas-project:') && key !== storageKey()) sessionStorage.removeItem(key); }
      installNavigation();
      if (owned || selections().has('project') || selections().has('atlas_projects')) await mount();
    } catch(error) {showFailure(error);} finally {booting = false;}
  }
  function showFailure(error) {
    cleanup(); root?.remove(); document.getElementById('atlas-project-failure')?.remove();
    const warning = node('aside','','atlas-project-failure'); errorNotice(warning,error,'shellFailure'); warning.id = 'atlas-project-failure'; warning.setAttribute('role','alert');
    warning.append(button('close',() => warning.remove())); document.body.append(warning);
  }
  async function mount() {
    if (root?.isConnected) return;
    root = node('section',null,'atlas-project'); root.id = 'atlas-project'; root.lang = window.atlasProjectLocale.language(); root.setAttribute('aria-label',t('projects')); root.dataset.owned = String(owned);
    left = node('nav',null,'atlas-project-left'); left.setAttribute('aria-label',t('projects'));
    center = node('main',null,'atlas-project-center'); right = node('aside',null,'atlas-project-right'); right.setAttribute('aria-label',t('context'));
    notice = node('div','','atlas-project-notice'); notice.setAttribute('role','status');
    const mobile = node('header',null,'atlas-project-mobile-header');
    const home = button('ordinary',ordinary), logo = node('img'); logo.src = '/static/atlas/brand/svg/atlas-logotype.svg'; logo.alt = 'Atlas'; home.replaceChildren(logo);
    mobile.append(home,button('projects',() => choose({org:scope.org})));
    root.append(mobile,left,center,right,notice); document.body.append(root);
    const side = document.getElementById('atlas-side-view');
    if (side) {sideResize = new ResizeObserver(() => root.style.setProperty('--atlas-side-width',side.getBoundingClientRect().width+'px')); sideResize.observe(side);}
    const q = selections(); const value = {org:q.get('org') || memberships[0]?.org_id,project:q.get('project'),conversation:q.get('conversation'),session:q.get('session')};
    try {await choose(value); installNavigation();} catch(error) {if (error.name !== 'AbortError') showFailure(error);}
  }
  let route = location.pathname;
  const watch = new MutationObserver(() => {
    installNavigation();
    if (!identity && !booting && document.querySelector('#chat-input')) boot();
    if (location.pathname !== route) {
      const prior = route; route = location.pathname; cleanup(); root?.remove();
      if (route.startsWith('/admin/settings')) guarded(adminEntry)();
      if (/^\/(auth|admin)/.test(prior) && !/^\/(auth|admin)/.test(route)) boot();
      if (/^\/(auth|admin)/.test(route)) { if (identity) sessionStorage.removeItem(storageKey()); document.getElementById('atlas-project-navigation')?.remove(); }
    }
  });
  window.addEventListener('popstate', () => { cleanup(); root?.remove(); if (owned || selections().has('project') || selections().has('atlas_projects')) guarded(mount)(); });
  document.addEventListener('click', event => {const link = event.target.closest?.('#sidebar a, a#sidebar-new-chat-button, #pinned-menu-items-list a'); if (link && root?.isConnected) ordinary();});
  window.addEventListener('resize', installNavigation);
  window.addEventListener('pagehide', cleanup);
  const start = () => { watch.observe(document.body, { childList: true, subtree: true }); boot(); };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start, { once: true }) : start();
})();
