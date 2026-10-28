/* Personal memory controls: stored text is always rendered as text, never HTML. */
(() => {
  const translations = {
    fr: { title: 'Mémoire', explanation: 'Atlas retient ce que vous lui demandez de retenir, par exemple « Retiens que je suis végétarien ». Cette mémoire sert dans vos discussions, jamais dans les projets.', enabled: 'Utiliser la mémoire', empty: 'Aucun souvenir enregistré.', hint: 'Demandez à Atlas de retenir quelque chose dans une discussion.', edit: 'Modifier', remove: 'Supprimer', save: 'Enregistrer', cancel: 'Annuler', clear: 'Tout effacer', confirm: 'Effacer tous les souvenirs ?', warning: 'Cette action est définitive. Atlas ne pourra plus utiliser ces souvenirs.', close: 'Fermer', text: 'Texte du souvenir', disabled: 'Votre organisation a désactivé la mémoire.', loading: 'Chargement…', error: 'La mémoire est indisponible. Réessayez.', retry: 'Réessayer' },
    en: { title: 'Memory', explanation: 'Atlas remembers what you ask it to remember, for example “Remember that I am vegetarian”. This memory is used in your chats, never in projects.', enabled: 'Use personal memory', empty: 'No saved memories.', hint: 'Ask Atlas to remember something in a chat.', edit: 'Edit', remove: 'Delete', save: 'Save', cancel: 'Cancel', clear: 'Clear all', confirm: 'Clear all memories?', warning: 'This cannot be undone. Atlas will no longer be able to use these memories.', close: 'Close', text: 'Memory text', disabled: 'Your organization has disabled personal memory.', loading: 'Loading…', error: 'Memory is unavailable. Please try again.', retry: 'Try again' },
    de: { title: 'Erinnerungen', explanation: 'Atlas merkt sich, worum Sie bitten, zum Beispiel „Merke dir, dass ich vegetarisch esse“. Diese Erinnerungen werden in Ihren Chats verwendet, niemals in Projekten.', enabled: 'Erinnerungen verwenden', empty: 'Keine gespeicherten Erinnerungen.', hint: 'Bitten Sie Atlas in einem Chat, sich etwas zu merken.', edit: 'Bearbeiten', remove: 'Löschen', save: 'Speichern', cancel: 'Abbrechen', clear: 'Alle löschen', confirm: 'Alle Erinnerungen löschen?', warning: 'Dies kann nicht rückgängig gemacht werden. Atlas kann diese Erinnerungen nicht mehr verwenden.', close: 'Schließen', text: 'Erinnerungstext', disabled: 'Ihre Organisation hat Erinnerungen deaktiviert.', loading: 'Wird geladen…', error: 'Erinnerungen sind nicht verfügbar. Bitte erneut versuchen.', retry: 'Erneut versuchen' },
    es: { title: 'Memoria', explanation: 'Atlas recuerda lo que le pides recordar, por ejemplo «Recuerda que soy vegetariano». Esta memoria se usa en tus conversaciones, nunca en los proyectos.', enabled: 'Usar memoria personal', empty: 'No hay recuerdos guardados.', hint: 'Pide a Atlas que recuerde algo en una conversación.', edit: 'Editar', remove: 'Eliminar', save: 'Guardar', cancel: 'Cancelar', clear: 'Borrar todo', confirm: '¿Borrar todos los recuerdos?', warning: 'Esta acción no se puede deshacer. Atlas ya no podrá usar estos recuerdos.', close: 'Cerrar', text: 'Texto del recuerdo', disabled: 'Tu organización ha desactivado la memoria.', loading: 'Cargando…', error: 'La memoria no está disponible. Inténtalo de nuevo.', retry: 'Reintentar' },
    it: { title: 'Memoria', explanation: 'Atlas ricorda ciò che gli chiedi di ricordare, per esempio «Ricorda che sono vegetariano». Questa memoria viene usata nelle tue conversazioni, mai nei progetti.', enabled: 'Usa memoria personale', empty: 'Nessun ricordo salvato.', hint: 'Chiedi ad Atlas di ricordare qualcosa in una conversazione.', edit: 'Modifica', remove: 'Elimina', save: 'Salva', cancel: 'Annulla', clear: 'Cancella tutto', confirm: 'Cancellare tutti i ricordi?', warning: 'Questa azione è irreversibile. Atlas non potrà più usare questi ricordi.', close: 'Chiudi', text: 'Testo del ricordo', disabled: 'La tua organizzazione ha disattivato la memoria.', loading: 'Caricamento…', error: 'La memoria non è disponibile. Riprova.', retry: 'Riprova' }
  };
  const words = () => translations[window.atlasSettings.locale()] || translations.en;
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (label, action, className) => {
    const node = element('button', label, className); node.type = 'button'; node.onclick = action; return node;
  };
  const api = async (path = '', method = 'GET', body) => {
    const response = await fetch('/api/atlas/user-memory' + path, {
      method, credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      ...(body ? {body: JSON.stringify(body)} : {})
    });
    if (!response.ok) throw new Error('memory_unavailable');
    return response.json();
  };
  function open(trigger) {
    const w = words(), dialog = element('dialog', undefined, 'atlas-settings-dialog');
    dialog.id = 'atlas-memory-dialog'; dialog.setAttribute('aria-labelledby', 'atlas-memory-title');
    const title = element('h2', w.title); title.id = 'atlas-memory-title';
    const header = element('header'), close = button('×', () => dialog.close()); close.setAttribute('aria-label', w.close);
    header.append(title, close);
    const message = element('p', '', 'atlas-memory-status'); message.setAttribute('role', 'status');
    const content = element('div');
    dialog.append(header, element('p', w.explanation, 'atlas-settings-description'), message, content);
    let busy = false;
    const run = async action => {
      if (busy) return; busy = true;
      content.querySelectorAll('button,input').forEach(control => { control.disabled = true; });
      try { await action(); await load(); }
      catch {
        message.replaceChildren(document.createTextNode(w.error + ' '), button(w.retry, () => run(async () => {})));
        // Keep unsaved edits intact; allow the user to retry the failed action.
        content.querySelectorAll('[data-memory-action]').forEach(control => { control.disabled = false; });
      } finally { busy = false; }
    };
    const load = async () => {
      const value = await api();
      if (!dialog.open) return;
      content.replaceChildren(); message.textContent = value.policy_enabled ? '' : w.disabled;
      const label = element('label', w.enabled, 'atlas-memory-toggle');
      const toggle = element('input'); toggle.type = 'checkbox'; toggle.setAttribute('role', 'switch'); toggle.checked = value.enabled; toggle.disabled = !value.policy_enabled;
      toggle.onchange = () => run(() => api('/settings', 'PATCH', {enabled: toggle.checked}));
      label.append(toggle); content.append(label);
      const list = element('ul', undefined, 'atlas-memory-list'); content.append(list);
      if (!value.entries.length) {
        const empty = element('div', undefined, 'atlas-memory-empty'); empty.append(element('p', w.empty), element('p', w.hint)); content.append(empty);
      }
      for (const entry of value.entries) {
        const row = element('li'), text = element('p', entry.text), actions = element('div', undefined, 'atlas-memory-actions');
        const edit = button(w.edit, () => {
          const form = element('form'), input = element('textarea'); input.value = entry.text; input.maxLength = 500; input.required = true; input.setAttribute('aria-label', w.text);
          const save = button(w.save, null, 'atlas-primary'); save.type = 'submit'; save.dataset.memoryAction = 'save';
          const cancel = button(w.cancel, () => { form.replaceWith(text, actions); edit.focus(); });
          form.onsubmit = event => { event.preventDefault(); if (input.value.trim()) run(() => api('/' + entry.id, 'PATCH', {text: input.value.trim(), kind: entry.kind})); };
          const controls = element('div', undefined, 'atlas-memory-actions'); controls.append(cancel, save); form.append(input, controls); row.replaceChildren(form); input.focus();
        });
        edit.disabled = !value.effective_enabled;
        const remove = button(w.remove, () => run(() => api('/' + entry.id, 'DELETE'))); remove.dataset.memoryAction = 'delete';
        actions.append(edit, remove); row.append(text, actions); list.append(row);
      }
      const footer = element('footer');
      const all = button(w.clear, () => {
        const confirm = element('dialog', undefined, 'atlas-settings-dialog atlas-memory-confirm'); confirm.setAttribute('aria-labelledby', 'atlas-memory-confirm-title');
        const heading = element('h2', w.confirm); heading.id = 'atlas-memory-confirm-title';
        const controls = element('footer');
        const cancel = button(w.cancel, () => confirm.close());
        controls.append(cancel, button(w.clear, () => { confirm.close(); run(() => api('', 'DELETE')); }, 'atlas-danger'));
        confirm.append(heading, element('p', w.warning), controls); document.body.append(confirm);
        confirm.addEventListener('close', () => { confirm.remove(); if (all.isConnected) all.focus(); }); confirm.showModal(); cancel.focus();
      }, 'atlas-danger');
      all.disabled = !value.entries.length;
      footer.append(all, button(w.close, () => dialog.close())); content.append(footer);
    };
    dialog.addEventListener('close', () => { dialog.remove(); if (trigger.isConnected) trigger.focus(); });
    document.body.append(dialog); dialog.showModal(); message.textContent = w.loading; run(async () => {});
  }
  window.atlasSettings.register('memory', {label: () => words().title, open});
})();
