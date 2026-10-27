/* Personal memory controls: stored text is always rendered as text, never HTML. */
(() => {
  const root = '/api/atlas/user-memory';
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const api = async (path = '', method = 'GET', body) => {
    const response = await fetch(root + path, {
      method, credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      ...(body ? {body: JSON.stringify(body)} : {})
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.message || value.error || 'Memory unavailable');
    return value;
  };
  function install() {
    if (document.getElementById('atlas-memory-button')) return;
    const open = element('button', 'Memory');
    open.id = 'atlas-memory-button';
    open.style.cssText = 'position:fixed;right:16px;top:60px;z-index:60;border:1px solid #888;border-radius:8px;padding:6px 12px;background:Canvas;color:CanvasText';
    const dialog = element('dialog');
    dialog.style.cssText = 'max-width:640px;width:90%;max-height:80vh;overflow:auto;padding:24px;background:Canvas;color:CanvasText;border:1px solid #888;border-radius:12px';
    const message = element('p');
    const content = element('div');
    const close = element('button', 'Close');
    close.onclick = () => { dialog.close(); content.replaceChildren(); };
    dialog.append(element('h2', 'Memory'), element('p', 'Personal memory is used only outside projects. Save something by saying “Remember that…”.'), message, content, close);
    const run = async action => { try { await action(); await load(); } catch (error) { message.textContent = error.message; } };
    const load = async () => {
      const value = await api();
      content.replaceChildren();
      message.textContent = value.policy_enabled ? '' : 'Your organization has disabled personal memory.';
      const label = element('label', ' Use personal memory ');
      const toggle = element('input'); toggle.type = 'checkbox'; toggle.checked = value.enabled; toggle.disabled = !value.policy_enabled;
      toggle.onchange = () => run(() => api('/settings', 'PATCH', {enabled: toggle.checked}));
      label.prepend(toggle); content.append(label);
      if (!value.entries.length) content.append(element('p', 'No saved memories.'));
      for (const entry of value.entries) {
        const row = element('section');
        const text = element('textarea'); text.value = entry.text; text.maxLength = 500; text.setAttribute('aria-label', 'Memory text');
        const kind = element('select'); kind.setAttribute('aria-label', 'Memory kind');
        for (const name of ['preference', 'context', 'recurring_topic', 'fact']) { const option = element('option', name); option.value = name; kind.append(option); }
        kind.value = entry.kind;
        const save = element('button', 'Save'); save.disabled = !value.effective_enabled;
        save.onclick = () => run(() => api('/' + entry.id, 'PATCH', {text: text.value, kind: kind.value}));
        const remove = element('button', 'Delete'); remove.onclick = () => run(() => api('/' + entry.id, 'DELETE'));
        row.append(element('p', entry.source + ' · ' + entry.observed_at), text, kind, save, remove); content.append(row);
      }
      const all = element('button', 'Delete all memories');
      all.onclick = () => { if (confirm('Delete all your saved memories?')) run(() => api('', 'DELETE')); };
      content.append(all);
    };
    open.onclick = () => { dialog.showModal(); run(async () => {}); };
    document.body.append(open, dialog);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
})();
