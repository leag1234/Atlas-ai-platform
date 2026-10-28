/* Pinned Open WebUI 0.11.3 guest enhancements; native handlers own chat state. */
(() => {
  'use strict';
  if (!document.querySelector('script[src*="stable-auth.js"]')) return;
  document.documentElement.dataset.atlasGuest = 'true';
  const translations = {
    fr: ['Envoyer un message', 'La réponse a été interrompue.', 'Réessayer', 'Plus d’actions', 'Modifier', 'Copier', 'Lire à voix haute', 'Bonne réponse', 'Mauvaise réponse', 'Continuer', 'Régénérer', 'Créer une branche', 'Arrêter la réponse', 'Réponse arrêtée.'],
    en: ['Send a message', 'The response was interrupted.', 'Retry', 'More actions', 'Edit', 'Copy', 'Read aloud', 'Good response', 'Bad response', 'Continue', 'Regenerate', 'Branch conversation', 'Stop generating', 'Stopped.'],
    de: ['Nachricht senden', 'Die Antwort wurde unterbrochen.', 'Erneut versuchen', 'Weitere Aktionen', 'Bearbeiten', 'Kopieren', 'Vorlesen', 'Gute Antwort', 'Schlechte Antwort', 'Fortsetzen', 'Neu generieren', 'Unterhaltung verzweigen', 'Antwort stoppen', 'Antwort gestoppt.'],
    es: ['Enviar un mensaje', 'La respuesta se ha interrumpido.', 'Reintentar', 'Más acciones', 'Editar', 'Copiar', 'Leer en voz alta', 'Buena respuesta', 'Mala respuesta', 'Continuar', 'Regenerar', 'Crear una rama', 'Detener respuesta', 'Respuesta detenida.'],
    it: ['Invia un messaggio', 'La risposta è stata interrotta.', 'Riprova', 'Altre azioni', 'Modifica', 'Copia', 'Leggi ad alta voce', 'Buona risposta', 'Cattiva risposta', 'Continua', 'Rigenera', 'Crea una diramazione', 'Interrompi risposta', 'Risposta interrotta.']
  };
  const language = () => (localStorage.getItem('locale') || navigator.language || 'en').split('-')[0];
  const words = () => translations[language()] || translations.en;
  const setText = (node, value) => { if (node && node.textContent !== value) node.textContent = value; };
  const attr = (node, key, value) => { if (node.getAttribute(key) !== value) node.setAttribute(key, value); };
  const turns = new Map();
  const host = turn => document.getElementById('message-' + turn.id);
  const stop = () => document.getElementById('stop-response-button');
  function interrupt(turn) {
    if (turn.done || turn.interrupted) return;
    turn.interrupted = true;
    refresh();
  }
  function eventReceived(event) {
    const turn = turns.get(event.message_id);
    if (!turn || (turn.chat && turn.chat !== event.chat_id) || turn.done) return;
    turn.chat ||= event.chat_id;
    const { type, data } = event.data || {};
    if (type === 'status' || type === 'chat:completion' || type === 'response:completion' || type === 'chat:message:delta') {
      turn.last = Date.now();
      if (data?.error) interrupt(turn);
      else if (data?.done && type !== 'status' && !turn.interrupted) { turn.done = true; turn.interrupted = false; }
    }
    refresh();
  }
  // Observe Engine.IO frames without consuming or changing native socket events.
  const Socket = window.WebSocket;
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      if (!new URL(args[0], location.href).pathname.includes('/ws/socket.io')) return;
      this.addEventListener('message', event => {
        if (typeof event.data !== 'string' || !event.data.startsWith('42')) return;
        try {
          const [name, payload] = JSON.parse(event.data.slice(event.data.indexOf('[')));
          if (name === 'events') eventReceived(payload);
        } catch { /* Other Engine.IO frames are owned by the native client. */ }
      });
      this.addEventListener('close', () => turns.forEach(interrupt));
    }
  };
  const fetchNative = window.fetch.bind(window);
  window.fetch = async (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const chatRequest = url.origin === location.origin && /^\/api\/(v1\/)?chat\/completions$/.test(url.pathname);
    let turn;
    if (chatRequest && typeof options?.body === 'string') {
      const body = JSON.parse(options.body);
      if (body.id) {
        turn = { id: body.id, chat: body.chat_id, last: Date.now(), done: false, interrupted: false };
        turns.set(turn.id, turn);
        options.signal?.addEventListener('abort', () => { if (!turn.interrupted) turn.done = true; }, { once: true });
      }
    }
    try {
      const result = await fetchNative(input, options);
      if (turn && !result.ok) interrupt(turn);
      if (turn && result.ok && result.headers.get('content-type')?.includes('application/json')) {
        result.clone().json().then(data => { if (data.chat_id) turn.chat = data.chat_id; }).catch(() => {});
      }
      // Socket.IO can fall back to polling after a proxy reconnect.
      if (url.origin === location.origin && url.pathname.includes('/ws/socket.io') && result.ok) {
        result.clone().text().then(text => text.split('\x1e').forEach(frame => {
          if (!frame.startsWith('42')) return;
          try { const [name, payload] = JSON.parse(frame.slice(frame.indexOf('['))); if (name === 'events') eventReceived(payload); } catch { /* Not an event frame. */ }
        })).catch(() => {});
      }
      return result;
    } catch (error) {
      if (turn && error.name !== 'AbortError') interrupt(turn);
      throw error;
    }
  };
  document.addEventListener('click', event => {
    if (event.target.closest?.('#stop-response-button')) turns.forEach(turn => { if (!turn.interrupted) turn.done = true; });
  }, true);
  setInterval(() => { turns.forEach(turn => { if (Date.now() - turn.last >= 45000) interrupt(turn); }); }, 1000);

  async function retry(turn, button) {
    button.disabled = true;
    stop()?.click();
    // Native stop settles its task first and exposes the original regenerate handler.
    for (let attempt = 0; attempt < 50; attempt++) {
      const regenerate = host(turn)?.querySelector('.regenerate-response-button');
      if (regenerate) { turn.done = true; regenerate.click(); refresh(); return; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    button.disabled = false;
  }
  function recovery() {
    turns.forEach(turn => {
      const bubble = host(turn);
      if (!bubble) { if (turn.done) turns.delete(turn.id); return; }
      bubble.classList.toggle('atlas-interrupted', turn.interrupted && !turn.done);
      let card = bubble.querySelector('.atlas-interruption');
      if (!turn.interrupted || turn.done) { card?.remove(); return; }
      if (!card) {
        card = document.createElement('div'); card.className = 'atlas-interruption'; card.setAttribute('role', 'status');
        const label = document.createElement('span'), button = document.createElement('button');
        button.type = 'button'; button.addEventListener('click', () => retry(turn, button));
        card.append(label, button);
        (bubble.querySelector('#response-content-container') || bubble).append(card);
      }
      setText(card.firstElementChild, words()[1]); setText(card.lastElementChild, words()[2]);
      // A stopped native task has no busy composer; the interrupted notice remains.
      if (stop() && !turn.stopping) { turn.stopping = true; stop().click(); }
    });
  }
  const actionMatchers = [/^edit$|^modifier$|bearbeiten|^editar$|^modifica$/i, /^copy$|^copier$|kopieren|^copiar$|^copia$/i, /read aloud|lire.*voix|vorlesen|leer en voz|leggi/i, /good response|bonne réponse|gute antwort|buena respuesta|buona risposta/i, /bad response|mauvaise réponse|schlechte antwort|mala respuesta|cattiva risposta/i, /continue|continuer|fortsetzen|continuar|continua/i, /regenerate|r[ée]g[ée]n[ée]rer|regenerieren|neu generieren|regenerar|rigenera/i, /fork chat|créer une branche|verzweigen|crear una rama|diramazione/i];
  document.addEventListener('keydown', event => {
    const menu = document.querySelector('.atlas-action-menu[open]');
    if (menu && event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); menu.close(); }
  }, true);
  function actions() {
    document.querySelectorAll('[id^="message-"]:has(.chat-assistant) .buttons').forEach(row => {
      row.classList.add('atlas-actions');
      const overflow = [];
      row.querySelectorAll('button:not([data-atlas-more])').forEach(button => {
        let action = button.dataset.atlasAction;
        if (action === undefined) {
          const index = actionMatchers.findIndex(pattern => pattern.test(button.getAttribute('aria-label') || ''));
          if (index < 0) return;
          action = String(index); button.dataset.atlasAction = action;
        }
        attr(button, 'aria-label', words()[Number(action) + 4]);
        if (['0', '5', '7'].includes(action)) { button.classList.add('atlas-overflow-action'); overflow.push(button); }
      });
      const existingMore = row.querySelector('[data-atlas-more]');
      if (existingMore) attr(existingMore, 'aria-label', words()[3]);
      if (!overflow.length || existingMore) return;
      const more = document.createElement('button'); more.type = 'button'; more.dataset.atlasMore = 'true'; more.textContent = '…';
      attr(more, 'aria-label', words()[3]); more.setAttribute('aria-haspopup', 'dialog');
      more.addEventListener('click', () => {
        const menu = document.createElement('dialog'); menu.className = 'atlas-action-menu'; attr(menu, 'aria-label', words()[3]);
        row.querySelectorAll('.atlas-overflow-action').forEach(original => {
          const item = document.createElement('button'); item.type = 'button'; item.textContent = original.getAttribute('aria-label');
          item.addEventListener('click', () => { menu.close(); original.click(); }); menu.append(item);
        });
        menu.addEventListener('click', event => { if (event.target === menu) menu.close(); });
        menu.addEventListener('close', () => { menu.remove(); more.focus(); });
        const bounds = more.getBoundingClientRect();
        menu.style.left = Math.max(8, Math.min(innerWidth - 216, bounds.left)) + 'px';
        menu.style.top = Math.max(8, bounds.top - menu.children.length * 44 - 16) + 'px';
        document.body.append(menu); menu.showModal();
      }); row.append(more);
    });
  }
  function composer() {
    const input = document.getElementById('chat-input');
    if (input) {
      attr(input, 'aria-label', words()[0]);
      document.documentElement.style.setProperty('--atlas-composer-placeholder', JSON.stringify(words()[0]));
      if (input.hasAttribute('data-placeholder')) attr(input, 'data-placeholder', words()[0]);
    }
    const voice = document.querySelector('#message-input-container [aria-label="Voice Input"],#message-input-container [data-atlas-voice]');
    if (voice) { voice.dataset.atlasVoice = 'true'; const label = ({fr:'Saisie vocale', de:'Spracheingabe', es:'Entrada de voz', it:'Input vocale'})[language()] || 'Voice Input'; attr(voice, 'aria-label', label); attr(voice, 'title', label); }
    if (stop()) { attr(stop(), 'aria-label', words()[12]); attr(stop(), 'title', words()[12]); }
    document.querySelectorAll('.atlas-stop-mark').forEach(node => setText(node, words()[13]));
    document.querySelectorAll('#message-input-container button').forEach(chip => {
      const name = chip.querySelector('.truncate.flex-1');
      const size = name?.nextElementSibling;
      if (!name || !size || !/\d/.test(size.textContent)) return;
      chip.classList.add('atlas-file-chip'); name.classList.add('atlas-file-name'); size.classList.add('atlas-file-size');
      attr(name, 'title', name.textContent.trim());
      if (!size.dataset.atlasOriginal) size.dataset.atlasOriginal = size.textContent.trim();
      const match = size.dataset.atlasOriginal.match(/([\d.,]+)\s*([KMGT]?)(?:i?B|o|bytes?)/i);
      if (match) {
        const number = Number(match[1].replace(',', '.'));
        const unit = match[2].toUpperCase() + (language() === 'fr' ? 'o' : 'B');
        setText(size, new Intl.NumberFormat(language(), { maximumFractionDigits: 0 }).format(number) + ' ' + unit);
      }
      const icon = chip.querySelector('svg');
      if (icon && !icon.classList.contains('animate-spin') && !icon.dataset.atlasFileType) {
        const extension = name.textContent.split('.').pop().toLowerCase();
        const paths = { document: 'M6 3h12v18H6zM9 7h6M9 11h6M9 15h6', pdf: 'M6 3h8l4 4v14H6z M9 11h6M9 15h6M9 18h4', table: 'M3 4h18v16H3zM3 10h18M3 15h18M10 4v16', image: 'M3 3h18v18H3zM3 17l6-6 4 4 3-3 5 5M15 7h1', archive: 'M5 3h14v18H5zM11 3v3h2v3h-2v3h2v5h-2' };
        const type = extension === 'pdf' ? 'pdf' : /csv|xlsx?|ods/.test(extension) ? 'table' : /png|jpe?g|svg|webp/.test(extension) ? 'image' : /zip|tar|gz/.test(extension) ? 'archive' : 'document';
        icon.dataset.atlasFileType = type; icon.replaceChildren();
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', paths[type]);
        icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('fill', 'none'); icon.setAttribute('stroke', 'currentColor'); icon.setAttribute('stroke-width', '1.5'); icon.append(path);
      }
    });
  }
  let release = '';
  fetchNative('/static/atlas/atlas-version.json', { cache: 'no-cache' }).then(r => r.ok ? r.json() : null).then(data => { if (/^\d+\.\d+\.\d+(?:[-.][\w.-]+)?$/.test(data?.version)) { release = data.version; refresh(); } }).catch(() => {});
  const titles = new WeakMap();
  function shell() {
    const controls = document.querySelector('button[aria-label="Controls"],button[data-atlas-controls]');
    if (controls) { controls.dataset.atlasControls = 'true'; attr(controls, 'aria-label', ({fr:'Paramètres de conversation',de:'Unterhaltungseinstellungen',es:'Ajustes de conversación',it:'Impostazioni conversazione'})[language()] || 'Conversation settings'); }
    const skip = document.querySelector('a[href="#main-content"]');
    if (skip) setText(skip, ({fr:'Aller au contenu principal', de:'Zum Hauptinhalt', es:'Ir al contenido principal', it:'Vai al contenuto principale'})[language()] || 'Skip to main content');
    const title = document.getElementById('chat-context-menu-button')?.parentElement?.previousElementSibling;
    // The menu trigger is nested in a tooltip; resolve its title sibling by shape.
    const candidate = title?.matches('.truncate') ? title : document.querySelector('nav .truncate.text-left');
    if (candidate && candidate.clientWidth) {
      let state = titles.get(candidate);
      if (!state || candidate.textContent !== state.shown) state = { full: candidate.textContent.trim(), shown: '' };
      const full = state.full.charAt(0).toLocaleUpperCase(language()) + state.full.slice(1);
      const canvas = document.createElement('canvas'), context = canvas.getContext('2d'); context.font = getComputedStyle(candidate).font;
      let text = full;
      const maximum = candidate.clientWidth;
      if (context.measureText(text).width > maximum) {
        const parts = full.split(/\s+/);
        while (parts.length && context.measureText(parts.join(' ') + '…').width > maximum) parts.pop();
        text = parts.join(' ') + '…';
      }
      state.shown = text; titles.set(candidate, state); attr(candidate, 'title', full); setText(candidate, text);
    }
    if (!release) return;
    document.querySelectorAll('div.text-center').forEach(container => {
      if (container.children.length || container.closest('[role="dialog"],dialog')) return;
      if (/^Atlas(?: \(Open WebUI\))? [‧·] v[\d.]+$/.test(container.textContent.trim()) || container.dataset.atlasFooter) {
        container.dataset.atlasFooter = 'true'; setText(container, 'Atlas ' + release);
      }
    });
    let footer = document.querySelector('.atlas-release');
    const nativeFooter = document.querySelector('[data-atlas-footer]:not(.atlas-release)');
    const composer = document.getElementById('message-input-container');
    if (nativeFooter) footer?.remove();
    else if (composer) {
      if (!footer) { footer = document.createElement('div'); footer.className = 'atlas-release'; footer.dataset.atlasFooter = 'true'; composer.after(footer); }
      setText(footer, 'Atlas ' + release);
    }
  }
  let scheduled = false;
  function refresh() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; composer(); actions(); shell(); recovery(); });
  }
  const start = () => { new MutationObserver(refresh).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['data-placeholder'] }); refresh(); };
  window.addEventListener('resize', refresh);
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
})();
