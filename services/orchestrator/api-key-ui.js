/* Atlas key management: credentials live only in the open one-time dialog. */
(() => {
  'use strict';
  const words = {
    en: ['API keys','Create a key','Name','Expiry','days','Create','Close','Copy','Copied','Copy this key now. It will never be shown again. Keep it private.','Revoke','Revoke this key? Applications using it will lose access immediately.','Cancel','No API keys yet.','Ask your administrator to create or revoke keys.','Created','Last used','Expires','Daily quota','Today','Never','Revoked','Expired','Scopes','Owner','My keys','Organization','Create for a user (email)','Organization policy','Disabled','Administrators only','Self-service','Requests per day','EUR per day','Maximum expiry (days)','Active keys per user','Save policy','Model','Base URL','Could not complete this action. Try again.','You have reached the active-key limit. Revoke an unused key first.','This expiry exceeds your organization’s limit.','The user must sign in once before a key can be created.','Loading…','Secret key','Policy saved','Assistant','Model access','Key revoked'],
    fr: ['Clés API','Créer une clé','Nom','Expiration','jours','Créer','Fermer','Copier','Copié','Copiez cette clé maintenant. Elle ne sera plus jamais affichée. Gardez-la secrète.','Révoquer','Révoquer cette clé ? Les applications qui l’utilisent perdront immédiatement leur accès.','Annuler','Aucune clé API pour le moment.','Demandez à votre administrateur de créer ou révoquer des clés.','Création','Dernière utilisation','Expiration','Quota quotidien','Aujourd’hui','Jamais','Révoquée','Expirée','Autorisations','Propriétaire','Mes clés','Organisation','Créer pour un utilisateur (adresse e-mail)','Politique de l’organisation','Désactivées','Administrateurs uniquement','Libre-service','Requêtes par jour','EUR par jour','Expiration maximale (jours)','Clés actives par utilisateur','Enregistrer la politique','Modèle','URL de base','Impossible de terminer cette action. Réessayez.','Vous avez atteint la limite de clés actives. Révoquez d’abord une clé inutilisée.','Cette expiration dépasse la limite de votre organisation.','L’utilisateur doit se connecter une fois avant la création d’une clé.','Chargement…','Clé secrète','Politique enregistrée','Assistant','Accès au modèle','Clé révoquée'],
    de: ['API-Schlüssel','Schlüssel erstellen','Name','Ablauf','Tage','Erstellen','Schließen','Kopieren','Kopiert','Kopieren Sie diesen Schlüssel jetzt. Er wird nie wieder angezeigt. Halten Sie ihn geheim.','Widerrufen','Diesen Schlüssel widerrufen? Anwendungen verlieren damit sofort den Zugriff.','Abbrechen','Noch keine API-Schlüssel.','Bitten Sie Ihren Administrator, Schlüssel zu erstellen oder zu widerrufen.','Erstellt','Zuletzt verwendet','Läuft ab','Tageskontingent','Heute','Nie','Widerrufen','Abgelaufen','Berechtigungen','Eigentümer','Meine Schlüssel','Organisation','Für einen Benutzer erstellen (E-Mail)','Organisationsrichtlinie','Deaktiviert','Nur Administratoren','Selbstbedienung','Anfragen pro Tag','EUR pro Tag','Maximale Gültigkeit (Tage)','Aktive Schlüssel pro Benutzer','Richtlinie speichern','Modell','Basis-URL','Diese Aktion ist fehlgeschlagen. Versuchen Sie es erneut.','Die Grenze aktiver Schlüssel ist erreicht. Widerrufen Sie zuerst einen ungenutzten Schlüssel.','Diese Gültigkeit überschreitet die Organisationsgrenze.','Der Benutzer muss sich zuerst einmal anmelden.','Laden…','Geheimer Schlüssel','Richtlinie gespeichert','Assistent','Modellzugriff','Schlüssel widerrufen'],
    es: ['Claves API','Crear una clave','Nombre','Caducidad','días','Crear','Cerrar','Copiar','Copiado','Copie esta clave ahora. No volverá a mostrarse. Manténgala en secreto.','Revocar','¿Revocar esta clave? Las aplicaciones perderán el acceso inmediatamente.','Cancelar','Todavía no hay claves API.','Pida a su administrador que cree o revoque claves.','Creación','Último uso','Caduca','Cuota diaria','Hoy','Nunca','Revocada','Caducada','Permisos','Propietario','Mis claves','Organización','Crear para un usuario (correo electrónico)','Política de la organización','Desactivadas','Solo administradores','Autoservicio','Solicitudes por día','EUR por día','Caducidad máxima (días)','Claves activas por usuario','Guardar política','Modelo','URL base','No se pudo completar la acción. Inténtelo de nuevo.','Ha alcanzado el límite de claves activas. Revoque primero una clave sin uso.','Esta caducidad supera el límite de su organización.','El usuario debe iniciar sesión una vez antes de crear una clave.','Cargando…','Clave secreta','Política guardada','Asistente','Acceso al modelo','Clave revocada'],
    it: ['Chiavi API','Crea una chiave','Nome','Scadenza','giorni','Crea','Chiudi','Copia','Copiato','Copiate questa chiave ora. Non verrà più mostrata. Conservatela in segreto.','Revoca','Revocare questa chiave? Le applicazioni perderanno immediatamente l’accesso.','Annulla','Nessuna chiave API.','Chiedete all’amministratore di creare o revocare le chiavi.','Creazione','Ultimo utilizzo','Scadenza','Quota giornaliera','Oggi','Mai','Revocata','Scaduta','Permessi','Proprietario','Le mie chiavi','Organizzazione','Crea per un utente (e-mail)','Politica dell’organizzazione','Disattivate','Solo amministratori','Autogestione','Richieste al giorno','EUR al giorno','Scadenza massima (giorni)','Chiavi attive per utente','Salva politica','Modello','URL di base','Impossibile completare l’azione. Riprovate.','È stato raggiunto il limite di chiavi attive. Revocate prima una chiave inutilizzata.','Questa scadenza supera il limite dell’organizzazione.','L’utente deve accedere una volta prima di creare una chiave.','Caricamento…','Chiave segreta','Politica salvata','Assistente','Accesso al modello','Chiave revocata']
  };
  const locale = () => (localStorage.getItem('locale') || localStorage.getItem('atlas-ui-language') || navigator.language || 'en').split('-')[0];
  const t = i => (words[locale()] || words.en)[i];
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
  const button = (text, action, cls) => { const node = el('button', text, cls); node.type = 'button'; node.onclick = action; return node; };
  const base = '/api/atlas/keys';
  let state, dialog, content, status, all = false, opener;
  const request = async (path = '', method = 'GET', data) => {
    const response = await fetch(base + path, { method, credentials: 'same-origin', headers: data ? { 'Content-Type': 'application/json' } : {}, body: data ? JSON.stringify(data) : undefined, cache: 'no-store' });
    const value = response.status === 204 ? {} : await response.json();
    if (!response.ok) { const error = new Error(value.error); error.code = value.error; throw error; }
    return value;
  };
  const failure = error => { if (status?.isConnected) status.textContent = t(({ active_key_limit: 40, expiry_exceeds_policy: 41, user_never_signed_in: 42, policy_denied: 14 })[error.code] || 39); };
  const run = async action => { try { await action(); } catch (error) { failure(error); } };
  const field = (parent, label, input) => { const node = el('label'); node.append(el('span', label), input); parent.append(node); return input; };
  const input = (type, value = '') => { const node = el('input'); node.type = type; node.value = value; return node; };
  const select = pairs => { const node = el('select'); pairs.forEach(([value, label]) => { const option = el('option', label); option.value = value; node.append(option); }); return node; };
  const date = value => value ? new Date(value * 1000).toLocaleDateString(locale()) : t(20);
  const euro = value => new Intl.NumberFormat(locale(), { style: 'currency', currency: 'EUR' }).format(value / 1000000);
  const reload = async () => { state = await request(all ? '?all=true' : ''); render(); };
  const secretBox = value => {
    const section = el('section', '', 'atlas-key-secret');
    section.append(el('p', t(9)));
    const secret = field(section, t(44), input('text', value)); secret.readOnly = true; secret.autocomplete = 'off'; secret.spellcheck = false;
    const copy = button(t(7), () => run(async () => { await navigator.clipboard.writeText(secret.value); copy.textContent = t(8); }));
    section.append(copy, button(t(6), () => { secret.value = ''; section.remove(); }));
    content.prepend(section); secret.focus(); secret.select();
  };
  const revoke = key => {
    const confirm = el('dialog', '', 'atlas-key-dialog atlas-key-confirm');
    confirm.setAttribute('aria-label', t(10)); confirm.append(el('h2', key.name), el('p', t(11)));
    const close = () => { confirm.close(); confirm.remove(); };
    confirm.append(button(t(12), close), button(t(10), () => run(async () => { await request('/' + encodeURIComponent(key.id), 'DELETE'); close(); await reload(); status.textContent = t(48); }), 'atlas-key-danger'));
    confirm.addEventListener('cancel', close); dialog.append(confirm); confirm.showModal();
  };
  const policyForm = () => {
    const details = el('details'); details.append(el('summary', t(28))); const form = el('form', '', 'atlas-key-form');
    const mode = field(form, t(28), select([['off',t(29)],['admin_only',t(30)],['self_service',t(31)]])); mode.value = state.policy.mode;
    const fields = [['daily_requests',32,1,1000000],['daily_micro_eur',33,0.000001,1000],['max_lifetime_days',34,30,365],['active_key_limit',35,1,100]];
    const controls = fields.map(([name,label,min,max]) => { const control = field(form,t(label),input('number',state.policy[name] / (name === 'daily_micro_eur' ? 1000000 : 1))); control.required = true; control.min = min; control.max = max; control.step = name === 'daily_micro_eur' ? '0.000001' : '1'; return [name,control]; });
    const save = button(t(36)); save.type = 'submit'; form.append(save);
    form.onsubmit = event => { event.preventDefault(); run(async () => { save.disabled = true; try { await request('/policy','PUT',Object.fromEntries([['mode',mode.value],...controls.map(([name,control]) => [name,Math.round(Number(control.value) * (name === 'daily_micro_eur' ? 1000000 : 1))])])); await reload(); status.textContent = t(45); } finally { save.disabled = false; } }); };
    details.append(form); return details;
  };
  const render = () => {
    if (!content?.isConnected) return;
    content.replaceChildren(); status.textContent = '';
    const allowed = state.admin || state.policy.mode === 'self_service';
    if (state.admin) {
      const tabs = el('div','','atlas-key-tabs');
      [false,true].forEach(value => { const tab = button(t(value ? 26 : 25), () => run(async () => { all = value; await reload(); })); tab.setAttribute('aria-pressed',String(all === value)); tabs.append(tab); }); content.append(tabs);
      if (all) content.append(policyForm());
    }
    if (!allowed) content.append(el('p',t(14),'atlas-key-muted'));
    if (allowed) {
      const form = el('form','','atlas-key-form'); form.append(el('h3',t(1)));
      const name = field(form,t(2),input('text')); name.required = true; name.maxLength = 100; name.autocomplete = 'off';
      const expiry = field(form,t(3),select([30,90,365].filter(day => day <= state.policy.max_lifetime_days).map(day => [String(day),`${day} ${t(4)}`]))); expiry.value = String(state.policy.max_lifetime_days >= 90 ? 90 : 30);
      const scopes = field(form,t(23),select([['assistant',t(46)],['model',t(47)]]));
      const owner = state.admin && all ? field(form,t(27),input('email')) : null;
      const create = button(t(5)); create.type = 'submit'; form.append(create);
      form.onsubmit = event => { event.preventDefault(); run(async () => { create.disabled = true; try { const value = await request('','POST',{ name:name.value.trim(),lifetime_days:Number(expiry.value),scopes:[scopes.value],...(owner?.value ? {login:owner.value} : {}) }); await reload(); if (content?.isConnected) secretBox(value.key); } finally { create.disabled = false; } }); }; content.append(form);
    }
    const list = el('div','','atlas-key-list');
    if (!state.data.length) list.append(el('p',t(13),'atlas-key-muted'));
    state.data.slice().reverse().forEach(key => {
      const card = el('article','','atlas-key-card'); const heading = el('div','','atlas-key-card-head'); heading.append(el('h3',key.name),el('code',key.prefix + '…'));
      const inactive = key.revoked_at ? t(21) : key.expires_at * 1000 <= Date.now() ? t(22) : '';
      if (inactive) heading.append(el('span',inactive,'atlas-key-muted'));
      card.append(heading); const meta = el('dl');
      const values = [[23,key.scopes.map(scope => t(scope === 'assistant' ? 46 : 47)).join(', ')],[15,date(key.created_at)],[16,date(key.last_used_at)],[17,date(key.expires_at)],[18,`${key.daily_requests} · ${euro(key.daily_micro_eur)}`],[19,`${key.usage.requests} · ${euro(key.usage.charged_micro_eur)}`]];
      if (all) values.unshift([24,key.subject]); values.forEach(([label,value]) => meta.append(el('dt',t(label)),el('dd',value))); card.append(meta);
      if (allowed && !inactive) card.append(button(t(10),() => revoke(key),'atlas-key-danger')); list.append(card);
    }); content.append(list);
    const guide = el('details'); guide.append(el('summary',`${t(38)} · ${t(37)} atlas`));
    guide.append(el('p',`${t(38)} : https://${location.host}/api/atlas/v1`),el('p',`${t(37)} : atlas`));
    guide.append(el('pre',`curl https://${location.host}/api/atlas/v1/chat/completions \\\n  -H "Authorization: Bearer $ATLAS_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"atlas","messages":[{"role":"user","content":"Bonjour"}]}'`)); content.append(guide);
  };
  const open = trigger => {
    if (dialog?.open) return; opener = trigger; all = false;
    dialog = el('dialog','','atlas-key-dialog'); dialog.id = 'atlas-api-keys-dialog'; dialog.setAttribute('aria-labelledby','atlas-key-title');
    const header = el('div','','atlas-key-header'), title = el('h2',t(0)); title.id = 'atlas-key-title';
    const close = () => { dialog.querySelectorAll('input').forEach(node => { node.value = ''; }); dialog.close(); dialog.remove(); opener?.focus(); };
    header.append(title,button(t(6),close)); content = el('div'); status = el('p',t(43),'atlas-key-status'); status.setAttribute('role','status');
    dialog.append(header,status,content); document.body.append(dialog); dialog.addEventListener('cancel',event => { event.preventDefault(); close(); }); dialog.showModal(); run(reload);
  };
  // Attach only to the native sidebar account menu, including its portal.
  const mount = () => {
    if (!state || (state.policy.mode === 'off' && !state.admin)) return;
    const triggers = [...document.querySelectorAll('#sidebar [aria-haspopup="true"], #sidebar [aria-haspopup="menu"], #sidebar [data-dropdown-menu-trigger]')].slice(-1);
    triggers.forEach(trigger => {
      if (trigger.getAttribute('aria-expanded') !== 'true' && trigger.dataset.state !== 'open') return;
      const controlled = document.getElementById(trigger.getAttribute('aria-controls'));
      const menu = controlled || trigger._tippy?.popper?.querySelector('.tippy-content') || [...document.querySelectorAll('[role="menu"], [data-tippy-root] .tippy-content')].find(node => node.getBoundingClientRect().height > 0);
      if (!menu || menu.querySelector('[data-atlas-api-keys]')) return;
      const entry = button(t(0),event => { event.preventDefault(); event.stopPropagation(); trigger.click(); open(trigger); },'atlas-key-menu-entry');
      entry.dataset.atlasApiKeys = ''; entry.setAttribute('role','menuitem'); menu.append(entry);
    });
  };
  const start = async () => {
    try { state = await request(); } catch { return; }
    let queued = false;
    new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; mount(); }); } }).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['aria-expanded','data-state']}); mount();
  };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded',start,{once:true}) : start();
})();
