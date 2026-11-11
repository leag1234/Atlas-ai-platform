// Dictation test page: microphone -> AudioWorklet (16 kHz PCM) -> WebSocket -> live text.
(() => {
  const STRINGS = {
    en: { title: 'Dictation', start: 'Start', stop: 'Stop', engine: 'Engine', cloud: 'Cloud', local: 'Local', cleanup: 'Clean-up', strict: 'Strict privacy', first: 'Speech to first text', final: 'End of speech to final text', listening: 'Listening…', ended: 'Finished', failed: 'Dictation stopped: ' },
    fr: { title: 'Dictée', start: 'Démarrer', stop: 'Arrêter', engine: 'Moteur', cloud: 'Cloud', local: 'Local', cleanup: 'Correction', strict: 'Confidentialité stricte', first: 'Parole jusqu’au premier texte', final: 'Fin de parole jusqu’au texte final', listening: 'À l’écoute…', ended: 'Terminé', failed: 'Dictée arrêtée : ' },
    de: { title: 'Diktat', start: 'Starten', stop: 'Stoppen', engine: 'Engine', cloud: 'Cloud', local: 'Lokal', cleanup: 'Korrektur', strict: 'Strikter Datenschutz', first: 'Sprechbeginn bis erster Text', final: 'Sprechende bis endgültiger Text', listening: 'Hört zu…', ended: 'Beendet', failed: 'Diktat gestoppt: ' },
    es: { title: 'Dictado', start: 'Empezar', stop: 'Parar', engine: 'Motor', cloud: 'Nube', local: 'Local', cleanup: 'Corrección', strict: 'Privacidad estricta', first: 'Voz hasta el primer texto', final: 'Fin de voz hasta el texto final', listening: 'Escuchando…', ended: 'Terminado', failed: 'Dictado detenido: ' },
    it: { title: 'Dettatura', start: 'Avvia', stop: 'Ferma', engine: 'Motore', cloud: 'Cloud', local: 'Locale', cleanup: 'Correzione', strict: 'Privacy rigorosa', first: 'Voce fino al primo testo', final: 'Fine della voce fino al testo finale', listening: 'In ascolto…', ended: 'Terminato', failed: 'Dettatura interrotta: ' },
  };
  let stored = '';
  try { stored = localStorage.getItem('locale') || ''; } catch (error) { stored = ''; }
  const lang = (stored || navigator.language || 'en').slice(0, 2).toLowerCase();
  const t = STRINGS[lang] || STRINGS.en;
  document.documentElement.lang = STRINGS[lang] ? lang : 'en';
  document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = t[node.dataset.i18n]; });
  document.title = t.title;

  const mic = document.getElementById('mic');
  const box = document.getElementById('text');
  const status = document.getElementById('status');
  const first = document.getElementById('first');
  const last = document.getElementById('last');
  const LOUD = 0.02;
  const state = { finals: [], partial: '', closed: [], live: null, log: [] };
  window.__dictation = state;
  let socket = null;
  let audio = null;
  let stream = null;
  let speechStart = null;
  let lastLoud = null;
  let lastFinalAt = 0;
  let waitingFirst = true;

  const now = () => performance.now();
  const ms = (value) => `${Math.round(value)} ms`;

  function render() {
    box.textContent = '';
    const parts = [];
    let i = 0;
    let partialCovered = false;
    while (i < state.finals.length) {
      const closed = state.closed.find((range) => range.from === i);
      const live = state.live;
      if (closed) { parts.push(closed.text); i = closed.to; continue; }
      if (live && live.from === i && live.to === state.finals.length && (!live.partial || state.partial)) {
        parts.push(live.text); partialCovered = live.partial; i = live.to; continue;
      }
      parts.push(state.finals[i]); i += 1;
    }
    if (!partialCovered && state.live && state.live.from === state.finals.length && state.live.partial && state.partial) {
      parts.push(state.live.text); partialCovered = true;
    }
    box.append(document.createTextNode(parts.join(' ')));
    if (state.partial && !partialCovered) {
      const span = document.createElement('span');
      span.className = 'partial';
      span.textContent = (parts.length ? ' ' : '') + state.partial;
      box.append(span);
    }
  }

  function onMessage(message) {
    const data = JSON.parse(message.data);
    const at = now();
    state.log.push({ at, ...data });
    if ((data.type === 'partial' || data.type === 'final') && waitingFirst && speechStart !== null) {
      first.textContent = ms(at - speechStart);
      waitingFirst = false;
    }
    if (data.type === 'partial') { state.partial = data.text; }
    else if (data.type === 'final') {
      state.finals[data.index] = data.text;
      state.partial = '';
      if (state.live && !state.live.final) state.live = null;
      if (lastLoud !== null && lastLoud > lastFinalAt) last.textContent = ms(at - lastLoud);
      lastFinalAt = at;
    } else if (data.type === 'cleaned') {
      if (data.final) { state.closed.push(data); state.live = null; } else { state.live = data; }
    } else if (data.type === 'turn' && data.event === 'start') {
      waitingFirst = true;
    } else if (data.type === 'error') {
      status.textContent = t.failed + (data.message || data.code);
    } else if (data.type === 'done') {
      status.textContent = t.ended;
    }
    render();
  }

  async function start() {
    state.finals = []; state.partial = ''; state.closed = []; state.live = null; state.log = [];
    first.textContent = '-'; last.textContent = '-'; status.textContent = '';
    speechStart = null; lastLoud = null; lastFinalAt = 0; waitingFirst = true;
    render();
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    try { audio = new AudioContext({ sampleRate: 16000 }); } catch (error) { audio = new AudioContext(); }
    await audio.audioWorklet.addModule('/api/atlas/dictation/dictation-worklet.js');
    const source = audio.createMediaStreamSource(stream);
    const capture = new AudioWorkletNode(audio, 'dictation-capture');
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${scheme}//${location.host}/api/atlas/dictation/stream`);
    socket.binaryType = 'arraybuffer';
    socket.onmessage = onMessage;
    socket.onclose = () => { stop(false); };
    socket.onopen = () => {
      socket.send(JSON.stringify({
        type: 'start',
        engine: document.getElementById('engine').value,
        language: STRINGS[lang] ? lang : null,
        privacy: document.getElementById('strict').checked ? 'strict' : 'standard',
        cleanup: document.getElementById('cleanup').checked,
      }));
      capture.port.onmessage = ({ data }) => {
        const at = now();
        if (data.level > LOUD) {
          if (speechStart === null || (waitingFirst && lastLoud !== null && at - lastLoud > 800)) speechStart = at;
          lastLoud = at;
        }
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(data.pcm);
      };
      source.connect(capture);
      status.textContent = t.listening;
    };
    mic.setAttribute('aria-pressed', 'true');
    mic.textContent = t.stop;
  }

  function stop(sendStop) {
    if (sendStop && socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'stop' }));
    if (stream) stream.getTracks().forEach((track) => track.stop());
    if (audio) audio.close();
    stream = null; audio = null;
    mic.setAttribute('aria-pressed', 'false');
    mic.textContent = t.start;
  }

  mic.addEventListener('click', () => {
    if (mic.getAttribute('aria-pressed') === 'true') stop(true);
    else start().catch((error) => { status.textContent = t.failed + error.name; stop(false); });
  });
})();
