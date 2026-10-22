'use strict';
const el = id => document.getElementById(id);
const project = () => el('projects').value;
const base = () => '/projects/' + encodeURIComponent(project());
async function api(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) throw new Error('Operation failed (' + response.status + ').');
  return response.json();
}
function options(id, values) {
  el(id).replaceChildren(...values.map(value => {
    const option = document.createElement('option');
    option.value = value.id;
    option.textContent = value.name;
    return option;
  }));
}
async function history() {
  const conversation = el('conversations').value;
  const turns = conversation ? await api(base() + '/conversations/' + encodeURIComponent(conversation)) : [];
  el('history').replaceChildren(...turns.map(turn => {
    const block = document.createElement('pre');
    block.append(document.createTextNode('You: ' + turn.question + '\nATLAS : '));
    const pattern = /\[Source\]\((\/projects\/[a-f0-9-]{36}\/sources\/[a-f0-9]{64})\)/g;
    let position = 0;
    for (const match of turn.answer.matchAll(pattern)) {
      block.append(document.createTextNode(turn.answer.slice(position, match.index)));
      const link = document.createElement('a'); link.href = match[1]; link.textContent = 'Source';
      block.append(link); position = match.index + match[0].length;
    }
    block.append(document.createTextNode(turn.answer.slice(position)));
    return block;
  }));
}
async function facts() {
  const values = await api(base() + '/facts');
  el('facts').replaceChildren(...values.map(value => {
    const li = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = value.text + ' ';
    const edit = document.createElement('button'); edit.textContent = 'Edit';
    edit.onclick = action(async () => {
      const replacement = prompt('Edit fact', value.text);
      if (replacement) { await api(base() + '/facts/' + value.id, 'PATCH', {text: replacement}); await facts(); }
    });
    const erase = document.createElement('button'); erase.textContent = 'Erase';
    erase.onclick = action(async () => { await api(base() + '/facts/' + value.id, 'DELETE'); await facts(); });
    li.append(text, edit, erase); return li;
  }));
}
async function selectProject() {
  if (!project()) return;
  el('instructions').value = (await api(base())).instructions;
  options('conversations', await api(base() + '/conversations'));
  const documents = await api(base() + '/documents');
  el('documents').replaceChildren(...documents.map(doc => { const li = document.createElement('li'); li.textContent = doc.name; return li; }));
  await facts(); await history();
}
function action(fn) {
  return async () => { el('status').textContent = 'Working…'; try { await fn(); el('status').textContent = ''; } catch (error) { el('status').textContent = error.message; } };
}
el('projects').onchange = action(selectProject);
el('conversations').onchange = action(history);
el('create-project').onclick = action(async () => {
  const name = prompt('Project name'); if (!name) return;
  const value = await api('/projects', 'POST', {name});
  options('projects', await api('/projects')); el('projects').value = value.id; await selectProject();
});
el('save-project').onclick = action(async () => { await api(base(), 'PATCH', {name: el('projects').selectedOptions[0].textContent, instructions: el('instructions').value}); });
el('create-conversation').onclick = action(async () => {
  const name = prompt('Conversation name'); if (!name) return;
  const value = await api(base() + '/conversations', 'POST', {name});
  options('conversations', await api(base() + '/conversations')); el('conversations').value = value.id; await history();
});
el('add-document').onclick = action(async () => { await api(base() + '/documents', 'POST', {name: el('document-name').value, text: el('document-text').value, lang: 'fr'}); await selectProject(); });
el('erase').onclick = action(async () => { await api(base() + '/facts', 'DELETE'); await facts(); });
el('send').onclick = action(async () => {
  if (!project() || !el('conversations').value) throw new Error('Select a project and a conversation.');
  const request = {model: 'atlas', project_id: project(), conversation_id: el('conversations').value, messages: [{role: 'user', content: el('question').value}]};
  el('send').disabled = true;
  try { await api('/v1/chat/completions', 'POST', request); el('question').value = ''; await facts(); await history(); }
  finally { el('send').disabled = false; }
});
action(async () => { options('projects', await api('/projects')); await selectProject(); })();
