/* Read-aloud for project turns (the chat uses the native dictation and read-aloud).
   Each capability is offered only when the installation enables it. */
(() => {
  'use strict';
  const words = {
    en: { dictate: 'Dictate', stopDictation: 'Stop dictation', listening: 'Listening…', readAloud: 'Read aloud', stopReading: 'Stop reading', voiceUnavailable: 'Voice is not available on this installation.' },
    fr: { dictate: 'Dicter', stopDictation: 'Arrêter la dictée', listening: 'Écoute…', readAloud: 'Lire à voix haute', stopReading: 'Arrêter la lecture', voiceUnavailable: 'La voix n’est pas disponible sur cette installation.' },
    de: { dictate: 'Diktieren', stopDictation: 'Diktat stoppen', listening: 'Hört zu…', readAloud: 'Vorlesen', stopReading: 'Vorlesen stoppen', voiceUnavailable: 'Sprache ist auf dieser Installation nicht verfügbar.' },
    es: { dictate: 'Dictar', stopDictation: 'Detener dictado', listening: 'Escuchando…', readAloud: 'Leer en voz alta', stopReading: 'Detener lectura', voiceUnavailable: 'La voz no está disponible en esta instalación.' },
    it: { dictate: 'Dettare', stopDictation: 'Ferma dettatura', listening: 'In ascolto…', readAloud: 'Leggi ad alta voce', stopReading: 'Ferma lettura', voiceUnavailable: 'La voce non è disponibile su questa installazione.' }
  };
  const language = () => (localStorage.getItem('locale') || navigator.language || 'en').split('-')[0];
  const t = () => words[language()] || words.en;
  const config = { stt: false, tts: false };
  let recorder = null, chunks = [], stream = null, audio = null;
  const load = async () => {
    try {
      const response = await fetch('/api/atlas/voice/config', { credentials: 'same-origin', cache: 'no-store' });
      if (response.ok) Object.assign(config, await response.json());
    } catch { /* voice stays off */ }
    document.querySelectorAll('[data-atlas-voice]').forEach(button => { button.hidden = !config.stt; });
    document.querySelectorAll('[data-atlas-read]').forEach(button => { button.hidden = !config.tts; });
  };
  const micIcon = () => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '18'); svg.setAttribute('height', '18'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3zm6-3a6 6 0 0 1-12 0H4a8 8 0 0 0 7 7.94V22h2v-2.06A8 8 0 0 0 20 12z');
    svg.append(path); return svg;
  };
  const speakerIcon = () => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '18'); svg.setAttribute('height', '18'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M4 9v6h4l5 4V5L8 9H4zm12.5 3a3.5 3.5 0 0 0-2-3.15v6.3a3.5 3.5 0 0 0 2-3.15zM14.5 4.5v2.1a5.5 5.5 0 0 1 0 10.8v2.1a7.5 7.5 0 0 0 0-15z');
    svg.append(path); return svg;
  };
  const makeButton = (kind, icon) => {
    const button = document.createElement('button');
    button.type = 'button'; button.dataset[`atlas${kind}`] = ''; button.hidden = true;
    button.append(icon());
    return button;
  };
  const dictate = async button => {
    const label = t();
    if (recorder) { recorder.stop(); return; }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { button.title = label.voiceUnavailable; return; }
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { button.title = label.voiceUnavailable; return; }
    chunks = []; recorder = new MediaRecorder(stream);
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      stream?.getTracks().forEach(track => track.stop()); stream = null; recorder = null;
      button.dataset.atlasRecording = ''; delete button.dataset.atlasRecording;
      button.setAttribute('aria-label', label.dictate); button.title = label.dictate;
      const form = new FormData();
      form.append('file', new Blob(chunks, { type: recorder?.mimeType || 'audio/webm' }), 'dictation.webm');
      form.append('model', 'atlas-stt');
      try {
        const response = await fetch('/api/atlas/voice/transcriptions', { method: 'POST', credentials: 'same-origin', body: form });
        const data = await response.json().catch(() => ({}));
        const text = (data.text || '').trim();
        if (response.ok && text) {
          const input = document.querySelector('#chat-input, .atlas-project-composer textarea');
          if (input) { input.value = input.value ? input.value.replace(/\s+$/, '') + ' ' + text : text; input.dispatchEvent(new Event('input', { bubbles: true })); input.focus(); }
        }
      } catch { /* dictation failed silently; the typed text remains */ }
    };
    recorder.start();
    button.dataset.atlasRecording = '';
    button.setAttribute('aria-label', label.stopDictation); button.title = label.stopDictation;
  };
  const readAloud = async button => {
    if (audio) { audio.pause(); audio = null; button.setAttribute('aria-pressed', 'false'); button.setAttribute('aria-label', t().readAloud); button.title = t().readAloud; return; }
    const bubble = button.closest('.atlas-project-turn');
    const content = bubble?.querySelector('.markdown-prose, .answer, pre.answer');
    const text = content?.innerText?.trim();
    if (!text) return;
    button.setAttribute('aria-pressed', 'true'); button.setAttribute('aria-label', t().stopReading); button.title = t().stopReading;
    try {
      const response = await fetch('/api/atlas/voice/speech', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: 'atlas-tts', input: text, response_format: 'mp3' }) });
      if (!response.ok) throw new Error('tts');
      audio = new Audio(URL.createObjectURL(await response.blob()));
      audio.onended = () => { audio = null; button.setAttribute('aria-pressed', 'false'); button.setAttribute('aria-label', t().readAloud); button.title = t().readAloud; };
      await audio.play();
    } catch { audio = null; button.setAttribute('aria-pressed', 'false'); button.title = t().voiceUnavailable; }
  };
  const mount = () => {
    // Chat dictation is the native Open WebUI microphone (Atlas speech-to-text behind it):
    // nothing is injected into the Svelte composer, whose re-renders removed foreign nodes.
    // Chat answers keep the native read-aloud action (Atlas speech behind it when ATLAS_TTS is on):
    // appending into Svelte message rows froze the page like the composer did. Only the
    // project view, which Atlas renders itself, gets this button.
    document.querySelectorAll('.atlas-project-turn .atlas-project-turn-actions').forEach(row => {
      if (!config.tts || row.querySelector('[data-atlas-read]')) return;
      const button = makeButton('read', speakerIcon);
      button.hidden = false;
      button.setAttribute('aria-pressed', 'false');
      button.setAttribute('aria-label', t().readAloud); button.title = t().readAloud;
      button.addEventListener('click', () => readAloud(button));
      row.append(button);
    });
  };
  const start = () => {
    load().then(() => {
      new MutationObserver(() => mount()).observe(document.body, { childList: true, subtree: true });
      mount();
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
