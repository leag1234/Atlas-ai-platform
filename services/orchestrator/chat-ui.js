(() => {
  "use strict";
  // Sovereignty: Open WebUI lets any user pick the browser "Web API" speech engine; in
  // Chrome it streams the microphone to Google. Atlas dictation never uses it, so the
  // browser engine is removed before Open WebUI looks for it (the microphone then stays
  // silent for that setting instead of sending audio away).
  try { delete window.SpeechRecognition; delete window.webkitSpeechRecognition; } catch (error) { /* read-only in some browsers */ }
  // Open WebUI stores its selected interface language in "locale" (v0.11.3).
  // Keep this in the shared overlay so the trial and host launcher agree.
  // Set the first-visit language before Open WebUI initializes its i18n store.
  // An explicit user selection always wins over the browser preference.
  if (!localStorage.getItem("locale")) {
    const supported = { fr: "fr-FR", en: "en-US", de: "de-DE", es: "es-ES", it: "it-IT" };
    const preferred = (navigator.languages || [navigator.language]).find(value => supported[value.split("-")[0]]);
    if (preferred) localStorage.setItem("locale", supported[preferred.split("-")[0]]);
  }
  const nativeFetch = window.fetch.bind(window);
  const nearbyIntent = text => {
    const value = text.replace(/<attached_files>[\s\S]*?<\/attached_files>/g, '').replace(/^\[Attachment: [^\n]*\]$/gm, '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    if (/\b(recette|recipe|translate|tradui\w*|definition|explain|explique|histoire)\b/.test(value)) return false;
    const near = /\b(near(?:by|est)?|around me|close(?:st)?|pres|proche\w*|autour|quartier|a pied|dans le coin|cerca|vicino|nahe)\b/.test(value);
    return (/\b(baker\w*|boulanger\w*|pain|bread|backerei|panaderia|panificio|convenience|superette\w*|epicerie\w*|lait|milk|pharmac\w*|medicament\w*|medicine|apotheke\w*|farmacia\w*|supermarket\w*|supermarche\w*|restaurant\w*|cafe\w*|coffee|fuel|essence)\b/.test(value) && (near || /\b(open|ouvert\w*|abiert\w*|offen|aperto|maintenant|now|heure|tout de suite|ou aller)\b/.test(value)));
  };
  const locationIdentity = async () => {
    const headers = new Headers();
    const token = localStorage.getItem('token');
    if (token) headers.set('Authorization', 'Bearer ' + token);
    try {
      const response = await nativeFetch('/api/v1/auths/', { credentials: 'same-origin', headers, cache: 'no-store' });
      if (!response.ok) return null;
      const user = await response.json();
      return typeof user.id === 'string' ? user.id : null;
    } catch { return null; }
  };
  const permissionKey = id => 'atlas-location-permission:' + id;
  async function requestLocation(signal) {
    if (!document.getElementById('atlas-nearby-style')) {
      const style = document.createElement('link');
      style.id = 'atlas-nearby-style'; style.rel = 'stylesheet'; style.href = '/static/atlas/nearby.css';
      document.head.append(style);
    }
    const id = await locationIdentity();
    const french = (localStorage.getItem('locale') || navigator.language).startsWith('fr');
    const words = french ? ['Partager votre position approximative ?', 'Cette fois seulement', 'Toujours partager', 'Refuser', 'Indiquez un lieu (par exemple près de la place Bellecour)', 'Utiliser ce lieu', 'Annuler', 'Seule la permission est mémorisée. Révocable dans les paramètres. La position approximative est utilisée pour cette demande uniquement.'] : ['Share your approximate location?', 'Share this time only', 'Always share', 'Refuse', 'Enter a place (for example near Bellecour square)', 'Use this place', 'Cancel', 'Only permission is remembered. Revoke it in settings. Approximate location is used for this request only.'];
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const locate = mode => new Promise(resolve => {
      if (!navigator.geolocation) { resolve(null); return; }
      navigator.geolocation.getCurrentPosition(position => {
        const latitude = Math.round(position.coords.latitude * 1000) / 1000;
        const step = .001 / Math.max(.01, Math.cos(latitude * Math.PI / 180));
        resolve({ consent: mode, timezone, position: { latitude, longitude: Math.max(-180, Math.min(180, Math.round(position.coords.longitude / step) * step)) } });
      }, () => resolve(null), { enableHighAccuracy: false, maximumAge: 0, timeout: 10000 });
    });
    let denied = false;
    if (id && localStorage.getItem(permissionKey(id)) === 'always') {
      const result = await locate('always');
      if (result) return result;
      denied = true;
    }
    return new Promise((resolve, reject) => {
      const dialog = document.createElement('dialog'); dialog.id = 'atlas-location-consent'; dialog.setAttribute('aria-label', words[0]);
      const title = document.createElement('h2'), note = document.createElement('p'), controls = document.createElement('div');
      title.textContent = words[0]; note.textContent = words[7]; dialog.append(title, note, controls);
      const finish = value => { signal?.removeEventListener('abort', abort); dialog.remove(); resolve(value); };
      const abort = () => { dialog.remove(); reject(new DOMException('Aborted', 'AbortError')); };
      const button = (label, action) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.addEventListener('click', action); controls.append(b); return b; };
      const typed = consent => {
        controls.replaceChildren(); title.textContent = words[4];
        const form = document.createElement('form'), input = document.createElement('input');
        input.required = true; input.minLength = 2; input.maxLength = 200; input.autocomplete = 'off'; input.setAttribute('aria-label', words[4]);
        const submit = document.createElement('button'); submit.type = 'submit'; submit.textContent = words[5];
        form.append(input, submit); controls.append(form); button(words[6], () => finish({ consent, timezone }));
        form.addEventListener('submit', event => { event.preventDefault(); finish({ consent, timezone, place: input.value.trim() }); }); input.focus();
      };
      for (const [mode, label] of [['once', words[1]], ['always', words[2]]]) {
        const b = button(label, async () => {
          controls.querySelectorAll('button').forEach(control => { control.disabled = true; });
          const result = await locate(mode);
          if (!dialog.isConnected) return;
          if (result) { if (mode === 'always' && id) localStorage.setItem(permissionKey(id), 'always'); finish(result); }
          else typed('denied');
        });
        if (mode === 'always' && !id) b.disabled = true;
      }
      button(words[3], () => typed('refused'));
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish({ consent: 'refused', timezone }); });
      document.body.append(dialog); dialog.showModal();
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort(); else if (denied) typed('denied');
    });
  }
  window.atlasLocation = { needs: nearbyIntent, request: requestLocation, async revoke() { const id = await locationIdentity(); if (id) localStorage.removeItem(permissionKey(id)); } };

  let activeCancellation = null;
  const stopMark = () => {
    const messages = document.querySelectorAll(".chat-assistant");
    const message = messages[messages.length - 1];
    if (!message || message.querySelector(".atlas-stop-mark")) return;
    const mark = document.createElement("p");
    mark.className = "atlas-stop-mark";
    mark.setAttribute("role", "status");
    mark.style.margin = "8px 0 0";
    mark.style.color = "var(--atlas-muted)";
    mark.style.fontSize = ".875em";
    mark.style.fontStyle = "italic";
    mark.textContent = "Stopped.";
    message.append(mark);
  };
  const cancelActive = () => {
    if (!activeCancellation) return;
    const token = activeCancellation;
    activeCancellation = null;
    stopMark();
    nativeFetch(`/atlas/cancel/${token}`, { method: "POST", headers: { Origin: location.origin } }).catch(() => {});
  };
  window.fetch = async (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin === location.origin && /^\/api\/(?:v1\/)?chat\/completions$/.test(url.pathname) && typeof options?.body === "string") {
      const locale = (localStorage.getItem("locale") || document.documentElement.lang || "").toLowerCase().split("-")[0];
      if (["en", "fr", "de", "es", "it", "nl"].includes(locale)) {
        const body = JSON.parse(options.body);
        const cancel_id = crypto.randomUUID();
        activeCancellation = cancel_id;
        options = { ...options, body: JSON.stringify({ ...body, ui_locale: locale, cancel_id }) };
      }
    }
    if (url.origin === location.origin && /^\/(?:api\/(?:v1\/)?)?chat\/completions$|^\/v1\/chat\/completions$/.test(url.pathname) && typeof options?.body === "string") {
      const body = JSON.parse(options.body);
      body.atlas_ui = true;
      options = { ...options, body: JSON.stringify(body) };
      const message = body.user_message || [...(body.messages || [])].reverse().find(m => m.role === 'user');
      const text = typeof message?.content === 'string' ? message.content : (message?.content || []).filter(p => p.type === 'text').map(p => p.text).join(' ');
      if (!body.location && !body.code_mode && nearbyIntent(text)) {
        const location = await requestLocation(options.signal);
        if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        if (url.pathname.startsWith('/api/')) {
          // The host expects a background-task response and persists its body.
          // It receives only an opaque capability, never location metadata.
          const saved = await nativeFetch('/api/atlas/nearby-location', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(location), signal: options.signal});
          if (!saved.ok) throw new Error('Location service unavailable');
          body.location_ref = (await saved.json()).location_ref;
          options.signal?.addEventListener('abort', () => { nativeFetch('/api/atlas/nearby-location/' + body.location_ref, {method: 'DELETE', credentials: 'same-origin'}).catch(() => {}); }, {once: true});
        } else {
          body.location = location;
        }
        options = { ...options, body: JSON.stringify(body) };
      }
    }
    return nativeFetch(input, options);
  };
  const prepareStop = () => {
    const button = document.querySelector('#stop-response-button,[aria-label="Stop"]');
    if (!button || button.dataset.atlasStopReady) return;
    button.dataset.atlasStopReady = "true";
    button.id = "stop-response-button";
    button.setAttribute("aria-label", "Stop generating");
    button.title = "Stop generating";
    button.addEventListener("click", cancelActive, { capture: true });
  };
  new MutationObserver(prepareStop).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && activeCancellation && document.querySelector('#stop-response-button,[aria-label="Stop"]')) {
      event.preventDefault();
      document.querySelector('#stop-response-button,[aria-label="Stop"]').click();
    }
  }, true);
  const key = "atlas-ui";
  const defaults = { style: "arrondi", mode: "system", accent: "green", size: "16" };
  const choices = { style: ["dense", "arrondi", "document"], mode: ["system", "light", "dark"], accent: ["green", "blue", "orange", "violet", "graphite"], size: ["14", "16", "18", "20"] };
  const translations = {
    en: { title: "Display settings", style: "Style", mode: "Mode", accent: "Accent", size: "Text size", dense: "Dense", arrondi: "Rounded", document: "Document", system: "System", light: "Light", dark: "Dark", green: "Green", blue: "Blue", orange: "Orange", violet: "Violet", graphite: "Graphite", showMore: "Show more", showLess: "Show less", copyMessage: "Copy message", copiedMessage: "Message copied", expandDiagram: "Expand diagram", closeDiagram: "Close diagram viewer", zoomIn: "Zoom in", zoomOut: "Zoom out", fitDiagram: "Fit diagram", downloadDiagram: "Download diagram", regenerateDiagram: "Regenerate diagram", copyDiagram: "Copy diagram" },
    fr: { title: "Paramètres d’affichage", style: "Style", mode: "Mode", accent: "Couleur", size: "Taille du texte", dense: "Dense", arrondi: "Arrondi", document: "Document", system: "Système", light: "Clair", dark: "Sombre", green: "Vert", blue: "Bleu", orange: "Orange", violet: "Violet", graphite: "Graphite", showMore: "Afficher plus", showLess: "Afficher moins", copyMessage: "Copier le message", copiedMessage: "Message copié", expandDiagram: "Agrandir le diagramme", closeDiagram: "Fermer la visionneuse de diagramme", zoomIn: "Agrandir", zoomOut: "Réduire", fitDiagram: "Ajuster le diagramme", downloadDiagram: "Télécharger le diagramme", regenerateDiagram: "Régénérer le diagramme", copyDiagram: "Copier le diagramme" },
    de: { title: "Anzeigeeinstellungen", style: "Stil", mode: "Modus", accent: "Akzentfarbe", size: "Textgröße", dense: "Dicht", arrondi: "Abgerundet", document: "Dokument", system: "System", light: "Hell", dark: "Dunkel", green: "Grün", blue: "Blau", orange: "Orange", violet: "Violett", graphite: "Graphit", showMore: "Mehr anzeigen", showLess: "Weniger anzeigen", copyMessage: "Nachricht kopieren", copiedMessage: "Nachricht kopiert", expandDiagram: "Diagramm vergrößern", closeDiagram: "Diagrammansicht schließen", zoomIn: "Vergrößern", zoomOut: "Verkleinern", fitDiagram: "Diagramm einpassen", downloadDiagram: "Diagramm herunterladen", regenerateDiagram: "Diagramm neu erstellen", copyDiagram: "Diagramm kopieren" },
    es: { title: "Ajustes de pantalla", style: "Estilo", mode: "Modo", accent: "Color de acento", size: "Tamaño del texto", dense: "Compacto", arrondi: "Redondeado", document: "Documento", system: "Sistema", light: "Claro", dark: "Oscuro", green: "Verde", blue: "Azul", orange: "Naranja", violet: "Violeta", graphite: "Grafito", showMore: "Mostrar más", showLess: "Mostrar menos", copyMessage: "Copiar mensaje", copiedMessage: "Mensaje copiado", expandDiagram: "Ampliar diagrama", closeDiagram: "Cerrar visor de diagramas", zoomIn: "Acercar", zoomOut: "Alejar", fitDiagram: "Ajustar diagrama", downloadDiagram: "Descargar diagrama", regenerateDiagram: "Regenerar diagrama", copyDiagram: "Copiar diagrama" },
    it: { title: "Impostazioni di visualizzazione", style: "Stile", mode: "Modalità", accent: "Colore principale", size: "Dimensione del testo", dense: "Compatto", arrondi: "Arrotondato", document: "Documento", system: "Sistema", light: "Chiaro", dark: "Scuro", green: "Verde", blue: "Blu", orange: "Arancione", violet: "Viola", graphite: "Grafite", showMore: "Mostra di più", showLess: "Mostra di meno", copyMessage: "Copia messaggio", copiedMessage: "Messaggio copiato", expandDiagram: "Ingrandisci diagramma", closeDiagram: "Chiudi visualizzatore diagrammi", zoomIn: "Ingrandisci", zoomOut: "Riduci", fitDiagram: "Adatta diagramma", downloadDiagram: "Scarica diagramma", regenerateDiagram: "Rigenera diagramma", copyDiagram: "Copia diagramma" }
  };
  const saved = (() => { try { return { ...defaults, ...JSON.parse(localStorage.getItem(key) || "{}") }; } catch { return defaults; } })();
  const apply = value => { ["style", "accent", "size"].forEach(name => document.documentElement.dataset[`atlas${name[0].toUpperCase()}${name.slice(1)}`] = value[name]); };
  apply(saved);
  const persist = value => { Object.assign(saved, value); localStorage.setItem(key, JSON.stringify(saved)); apply(saved); };
  const locale = () => {
    // The interface locale (including document.documentElement.lang) is independent.
    const stored = localStorage.getItem("locale") || localStorage.getItem("atlas-ui-language");
    const candidate = stored || navigator.language || "en";
    const language = candidate.toLowerCase().split("-")[0];
    return translations[language] ? language : "en";
  };
  const messageContent = bubble => bubble.querySelector(".markdown-prose") || bubble.querySelector("[class*='message-content']") || bubble.querySelector(".chat-user > .w-full > .flex");
  const copyTerms = /copy|copier|kopier|copiar|copia/i;
  const copyIcon = () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16"); svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M9 8h10v12H9zM5 4h10v2H7v10H5z"); svg.append(path);
    return svg;
  };
  const checkIcon = () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16"); svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m5 12 4 4L19 6"); path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor"); path.setAttribute("stroke-width", "2.5"); path.setAttribute("stroke-linecap", "round"); path.setAttribute("stroke-linejoin", "round"); svg.append(path);
    return svg;
  };
  const copyStatus = bubble => {
    let status = bubble.querySelector(".atlas-user-copy-status");
    if (!status) { status = document.createElement("span"); status.className = "atlas-user-copy-status"; status.setAttribute("aria-live", "polite"); status.setAttribute("aria-atomic", "true"); bubble.append(status); }
    return status;
  };
  const showCopied = (button, bubble) => {
    const words = translations[locale()], status = copyStatus(bubble);
    status.textContent = words.copiedMessage; button.dataset.atlasCopied = "true";
    button.setAttribute("aria-label", words.copiedMessage); button.title = words.copiedMessage;
    if (!button.classList.contains("atlas-user-native-copy")) { button.replaceChildren(checkIcon()); }
    clearTimeout(button.atlasCopyTimer);
    button.atlasCopyTimer = setTimeout(() => {
      status.textContent = ""; delete button.dataset.atlasCopied;
      button.setAttribute("aria-label", translations[locale()].copyMessage); button.title = translations[locale()].copyMessage;
      if (!button.classList.contains("atlas-user-native-copy")) { button.replaceChildren(copyIcon()); }
    }, 1500);
  };
  const nativeCopyButton = bubble => [...bubble.querySelectorAll("button")].find(button => !button.classList.contains("atlas-user-copy-button") && copyTerms.test(`${button.getAttribute("aria-label") || ""} ${button.title || ""} ${button.textContent || ""}`));
  const prepareUserCopy = bubble => {
    const content = messageContent(bubble); if (!content) return;
    const native = nativeCopyButton(bubble);
    const button = native || bubble.querySelector(".atlas-user-copy-button") || document.createElement("button");
    if (!native && !button.isConnected) { button.type = "button"; button.className = "atlas-user-copy-button"; button.append(copyIcon()); bubble.append(button); }
    if (button.dataset.atlasCopyReady) return;
    button.dataset.atlasCopyReady = "true"; button.classList.toggle("atlas-user-native-copy", Boolean(native));
    const words = translations[locale()]; button.setAttribute("aria-label", words.copyMessage); button.title = words.copyMessage;
    button.addEventListener("click", async event => {
      if (!native) { event.preventDefault(); await navigator.clipboard.writeText(content.innerText); }
      showCopied(button, bubble);
    });
  };
  // A sent message collapses only when it is taller than one screen of reading: 25 rendered lines are
  // 600 px at the default 16 px size (two thirds of a 900 px laptop window, the composer stays visible),
  // so a message that fits on one screen is always shown whole (owner report 2026-11-13: a 10-line
  // message was folded). The collapsed view keeps 12 lines (chat-ui.css), so folding always hides
  // at least as much as it shows. Rendered lines, not characters: the same rule holds on a phone.
  const LONG_MESSAGE_LINES = 25;
  const lineHeight = content => {
    const value = parseFloat(getComputedStyle(content).lineHeight);
    return Number.isFinite(value) ? value : parseFloat(getComputedStyle(content).fontSize) * 1.5;
  };
  const setAttachmentTitles = bubble => bubble.querySelectorAll(".chat-user > .mb-1 > .self-end > button .truncate").forEach(name => { if (name.textContent.trim()) name.title = name.textContent.trim(); });
  const diagramHost = svg => svg.closest(".mermaid,[data-mermaid],.diagram") || svg.parentElement;
  const diagramActions = (host, action) => {
    const terms = { download: /download|télécharg|herunterlad|descarg|scarica/i, regenerate: /regener|retry|réess|erneut|reintentar|rigenera/i, copy: /copy|copier|kopier|copiar/i }[action];
    let node = host;
    for (let depth = 0; node && depth < 3; depth += 1, node = node.parentElement) {
      const found = [...node.querySelectorAll("button,[role='button']")].find(button => button !== host.querySelector("[data-atlas-diagram-expand]") && terms.test(`${button.getAttribute("aria-label") || ""} ${button.title || ""} ${button.textContent || ""}`));
      if (found) return found;
    }
    return null;
  };
  const labelButton = (name, label, text) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = `atlas-diagram-${name}`;
    button.setAttribute("aria-label", label); button.title = label; button.textContent = text;
    return button;
  };
  const openDiagram = svg => {
    const host = diagramHost(svg); if (!host || document.querySelector("#atlas-diagram-viewer")) return;
    const words = translations[locale()], priorFocus = document.activeElement;
    const overlay = document.createElement("div"); overlay.id = "atlas-diagram-viewer"; overlay.className = "atlas-diagram-viewer";
    overlay.innerHTML = `<section class="atlas-diagram-dialog" role="dialog" aria-modal="true" aria-label="${words.expandDiagram}" tabindex="-1"><header class="atlas-diagram-toolbar"></header><div class="atlas-diagram-canvas" tabindex="0"></div></section>`;
    const toolbar = overlay.querySelector(".atlas-diagram-toolbar"), canvas = overlay.querySelector(".atlas-diagram-canvas"), clone = svg.cloneNode(true);
    // Mermaid scopes its styles (edge strokes, label backgrounds) to the SVG id: give the copy its own id and rewrite those selectors.
    if (svg.id) { const copyId = `${svg.id}-atlas-viewer`; clone.id = copyId; clone.querySelectorAll("style").forEach(style => { style.textContent = style.textContent.split(`#${svg.id}`).join(`#${copyId}`); }); }
    clone.classList.add("atlas-diagram-full-svg"); canvas.append(clone);
    const close = labelButton("close", words.closeDiagram, "×"), out = labelButton("zoom-out", words.zoomOut, "−"), fit = labelButton("fit", words.fitDiagram, "<>"), inside = labelButton("zoom-in", words.zoomIn, "+");
    const actionButtons = [["download", "↓"], ["regenerate", "↻"], ["copy", "⧉"]].map(([action, text]) => {
      const button = labelButton(action, words[`${action}Diagram`], text);
      button.addEventListener("click", async () => {
        if (action === "download" && host.dataset.atlasMechanical) {
          const link = document.createElement("a"), url = new URL(svg.src); url.searchParams.set("download", "1");
          link.href = url.href; link.download = "diagram.svg"; link.click(); return;
        }
        const native = diagramActions(host, action);
        if (native) { native.click(); return; }
        if (action === "copy" && navigator.clipboard) await navigator.clipboard.writeText(svg.outerHTML);
      });
      return button;
    });
    toolbar.append(close, out, fit, inside, ...actionButtons);
    let zoom = 2;
    const baseWidth = () => Math.max(svg.viewBox?.baseVal.width || 0, svg.naturalWidth || svg.width?.baseVal?.value || 0, svg.getBoundingClientRect().width, 320);
    const setZoom = value => { zoom = Math.max(.5, Math.min(5, value)); clone.style.width = `${baseWidth() * zoom}px`; clone.style.height = "auto"; };
    const closeViewer = () => { overlay.remove(); if (priorFocus && priorFocus.focus) priorFocus.focus(); };
    close.addEventListener("click", closeViewer); out.addEventListener("click", () => setZoom(zoom / 1.25)); inside.addEventListener("click", () => setZoom(zoom * 1.25)); fit.addEventListener("click", () => { clone.style.width = `${Math.max(1, canvas.clientWidth - 32)}px`; clone.style.height = "auto"; zoom = (canvas.clientWidth - 32) / baseWidth(); });
    canvas.addEventListener("wheel", event => { if (!event.ctrlKey) return; event.preventDefault(); setZoom(zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15)); }, { passive: false });
    overlay.addEventListener("click", event => { if (event.target === overlay) closeViewer(); });
    overlay.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); closeViewer(); return; } if (event.key !== "Tab") return; const buttons = [...overlay.querySelectorAll("button")]; const first = buttons[0], last = buttons.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } });
    document.body.append(overlay); setZoom(zoom); close.focus();
  };
  // Only real diagrams are enhanced: Open WebUI also renders small icon SVGs (chevrons of the activity details, buttons, links).
  const isDiagram = svg => {
    if (svg.closest("summary,button,a,[role='button'],[data-atlas-diagram-expand],.atlas-diagram-toolbar")) return false;
    if (svg.closest(".mermaid,[data-mermaid],.diagram")) return true;
    const box = svg.viewBox && svg.viewBox.baseVal, width = Math.max(box && box.width || 0, svg.width && svg.width.baseVal.value || 0), height = Math.max(box && box.height || 0, svg.height && svg.height.baseVal.value || 0);
    return width >= 160 && height >= 80;
  };
  const prepareDiagrams = bubble => bubble.querySelectorAll(".markdown-prose svg").forEach(svg => {
    if (!isDiagram(svg)) return;
    const host = diagramHost(svg); if (!host || host.dataset.atlasDiagramReady) return;
    host.dataset.atlasDiagramReady = "true"; host.classList.add("atlas-diagram-inline");
    svg.classList.add("atlas-diagram-svg"); svg.setAttribute("preserveAspectRatio", svg.getAttribute("preserveAspectRatio") || "xMidYMid meet");
    const words = translations[locale()], expand = labelButton("expand", words.expandDiagram, "<>"); expand.dataset.atlasDiagramExpand = "true";
    expand.addEventListener("click", event => { event.stopPropagation(); openDiagram(svg); }); svg.addEventListener("click", () => openDiagram(svg)); host.append(expand);
  });
  const prepareMechanicalImages = bubble => bubble.querySelectorAll('.markdown-prose img[src*="/images/svg/"]').forEach(image => {
    if (image.dataset.atlasMechanicalSvg || !/\/images\/svg\/[a-f0-9]{32}(?:$|[?#])/.test(image.src)) return;
    // Keep SVG in the browser's passive image context; never inject fetched markup.
    image.dataset.atlasMechanicalSvg = "ready";
    const host = document.createElement("span"); host.className = "diagram atlas-diagram-inline"; host.dataset.atlasMechanical = "true";
    image.replaceWith(host); host.append(image); image.classList.add("atlas-diagram-svg");
    const expand = labelButton("expand", translations[locale()].expandDiagram, "<>"); expand.dataset.atlasDiagramExpand = "true";
    expand.addEventListener("click", event => { event.stopPropagation(); openDiagram(image); }); image.addEventListener("click", () => openDiagram(image)); host.append(expand);
  });
  const prepareBubble = bubble => {
    const content = messageContent(bubble);
    if (!content) return;
    setAttachmentTitles(bubble);
    prepareUserCopy(bubble);
    prepareDiagrams(bubble);
    prepareMechanicalImages(bubble);
    const toggle = bubble.querySelector(".atlas-message-toggle");
    const wasExpanded = toggle && toggle.getAttribute("aria-expanded") === "true";
    bubble.classList.remove("atlas-message-collapsed");
    const isLong = content.scrollHeight > lineHeight(content) * (LONG_MESSAGE_LINES + 0.1);
    bubble.classList.toggle("atlas-long-message", isLong);
    if (!isLong) { if (toggle) toggle.remove(); return; }
    const words = translations[locale()];
    const button = toggle || document.createElement("button");
    button.type = "button";
    button.className = "atlas-message-toggle";
    button.setAttribute("aria-expanded", String(Boolean(wasExpanded)));
    button.textContent = wasExpanded ? words.showLess : words.showMore;
    if (!toggle) {
      button.addEventListener("click", () => {
        const expanded = button.getAttribute("aria-expanded") !== "true";
        button.setAttribute("aria-expanded", String(expanded));
        button.textContent = expanded ? translations[locale()].showLess : translations[locale()].showMore;
        bubble.classList.toggle("atlas-message-collapsed", !expanded);
      });
      bubble.append(button);
    }
    bubble.classList.toggle("atlas-message-collapsed", !wasExpanded);
  };
  const observeMessages = () => {
    let queued = false;
    const scan = () => { queued = false; document.querySelectorAll(".user-message").forEach(prepareBubble); document.querySelectorAll(".chat-assistant").forEach(bubble => { prepareDiagrams(bubble); prepareMechanicalImages(bubble); }); };
    const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(scan); } };
    new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["data-atlas-size"] });
    window.addEventListener("resize", schedule);
    schedule();
  };
  // Keep the recorded base chat stylesheet intact; these rules own only the
  // guest settings dialogs and theme-aware model images verified by our browser tests.
  const settingsStyle = document.createElement("style");
  settingsStyle.id = "atlas-guest-settings-style";
  settingsStyle.textContent = `
/* Guest settings share the chat palette; native dialogs provide focus trapping. */
.atlas-settings-dialog { margin:auto; width:min(560px,calc(100vw - 32px)); max-height:calc(100dvh - 32px); overflow:auto; padding:24px; border:1px solid var(--atlas-border); border-radius:20px; background:var(--atlas-bg); color:var(--atlas-text); font:14px/1.6 var(--atlas-font); box-shadow:0 24px 80px #0004; }
.atlas-settings-dialog::backdrop { background:#0006; backdrop-filter:blur(3px); }
.atlas-settings-dialog header,.atlas-settings-dialog footer,.atlas-memory-actions { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.atlas-settings-dialog h2 { font-size:22px; font-weight:600; margin:0; }
.atlas-settings-dialog button { min-height:40px; padding:8px 12px; border:1px solid var(--atlas-border); border-radius:10px; background:var(--atlas-bg); color:var(--atlas-text); font:inherit; cursor:pointer; }
.atlas-settings-dialog header button { border:0; font-size:24px; }
.atlas-settings-dialog button:hover { background:var(--atlas-surface); }
.atlas-settings-dialog button:disabled { opacity:.45; cursor:default; }
.atlas-settings-dialog :is(button,input,textarea):focus-visible { outline:2px solid var(--atlas-accent); outline-offset:3px; }
.atlas-settings-description { margin:12px 0 24px; color:var(--atlas-muted); }
.atlas-settings-dialog fieldset { margin:20px 0; }
.atlas-settings-dialog .atlas-options { flex-wrap:wrap; }
.atlas-settings-dialog .atlas-option { flex:1 0 auto; }
.atlas-settings-dialog legend { margin-bottom:8px; color:var(--atlas-muted); }
.atlas-settings-dialog .atlas-option[aria-pressed="true"],.atlas-settings-dialog .atlas-primary { background:var(--atlas-accent); color:var(--atlas-on-accent); }
.atlas-memory-toggle { display:flex; justify-content:space-between; align-items:center; gap:16px; font-weight:600; padding-bottom:20px; }
.atlas-memory-toggle input { appearance:none; flex-shrink:0; width:42px; height:24px; padding:3px; background:var(--atlas-muted); border-radius:20px; cursor:pointer; }
.atlas-memory-toggle input::before { content:""; display:block; width:18px; height:18px; background:white; border-radius:50%; transition:transform .15s; }
.atlas-memory-toggle input:checked { background:var(--atlas-accent); }
.atlas-memory-toggle input:checked::before { transform:translateX(18px); }
.atlas-memory-toggle input:disabled { opacity:.45; cursor:default; }
.atlas-memory-list { margin:0; padding:0; list-style:none; }
.atlas-memory-list li { padding:16px 0; border-top:1px solid var(--atlas-border); }
.atlas-memory-list p { white-space:pre-wrap; overflow-wrap:anywhere; margin:0 0 10px; }
.atlas-memory-actions { justify-content:flex-end; }
.atlas-memory-list textarea { width:100%; min-height:90px; resize:vertical; padding:12px; background:var(--atlas-surface); color:var(--atlas-text); border:1px solid var(--atlas-border); border-radius:10px; font:inherit; }
.atlas-memory-empty { padding:32px 12px; text-align:center; border-top:1px solid var(--atlas-border); }
.atlas-memory-empty p + p { color:var(--atlas-muted); margin-top:8px; }
.atlas-memory-status:empty { display:none; }
.atlas-settings-dialog footer { margin-top:20px; padding-top:16px; border-top:1px solid var(--atlas-border); }
.atlas-settings-dialog .atlas-danger { color:#b53629; }
html.dark .atlas-settings-dialog .atlas-danger { color:#ff9c91; }
.atlas-memory-confirm { width:min(440px,calc(100vw - 32px)); }
@media(max-width:640px) { .atlas-settings-dialog { padding:20px; } }

img[data-atlas-brand="model"] { content:url('/static/atlas/brand/svg/atlas-symbol.svg'); background:#f2f2f0; padding:5px; object-fit:contain; }
html.dark img[data-atlas-brand="model"] { content:url('/static/atlas/brand/svg/atlas-symbol-dark.svg'); background:#1a1a18; }
/* Global composer form rules must not reshape the memory editor. */
.atlas-settings-dialog form:has(textarea) { border-radius:10px!important; background:transparent; }
.atlas-settings-dialog header { background:transparent!important; }
.atlas-memory-toggle input[type="checkbox"] { background-image:none!important; border:0; box-shadow:none; }
.atlas-memory-toggle input[type="checkbox"]::after { content:none!important; }
`;
  document.head.append(settingsStyle);
  const settingsEntries = new Map();
  const settingsWords = {
    en: { appearance: "Appearance", close: "Close", revoke: "Revoke location sharing", revoked: "Location sharing revoked" },
    fr: { appearance: "Apparence", close: "Fermer", revoke: "Révoquer le partage de position", revoked: "Partage de position révoqué" },
    de: { appearance: "Darstellung", close: "Schließen", revoke: "Standortfreigabe widerrufen", revoked: "Standortfreigabe widerrufen" },
    es: { appearance: "Apariencia", close: "Cerrar", revoke: "Revocar ubicación compartida", revoked: "Ubicación compartida revocada" },
    it: { appearance: "Aspetto", close: "Chiudi", revoke: "Revoca condivisione posizione", revoked: "Condivisione posizione revocata" }
  };
  // Source: pinned Open WebUI 0.11.3 SettingsModal.svelte, tabButtonClass.
  // Borrow the live native classes, including their active/hover variants.
  const tabBase = "flex items-center gap-1.5 h-7 px-2 md:w-full shrink-0 rounded-lg text-xs text-left transition-colors duration-75 ";
  const tabIdle = "text-gray-500 hover:text-gray-700 dark:hover:text-gray-300";
  const tabActive = "font-medium text-gray-900 dark:text-white bg-gray-50 dark:bg-white/[0.04]";
  const mountSettings = () => {
    const nav = document.getElementById("search-input-settings-modal")?.closest("nav");
    if (!nav) return;
    const nativeIdle = nav.querySelector('[role="tab"][aria-selected="false"]');
    const nativeActive = nav.querySelector('[role="tab"][aria-selected="true"]');
    let anchor = nav.querySelector('[aria-controls="tab-personalization"]') || nav.querySelector('[aria-controls="tab-audio"]')?.previousElementSibling;
    for (const [id, entry] of [...settingsEntries].sort(([a], [b]) => a.localeCompare(b))) {
      let button = nav.querySelector(`[data-atlas-settings="${id}"]`);
      if (!button) {
        button = document.createElement("button"); button.type = "button";
        button.dataset.atlasSettings = id;
        button.setAttribute("aria-haspopup", "dialog");
        button.setAttribute("aria-controls", `atlas-${id}-dialog`);
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("class", "size-3.5");
        icon.setAttribute("fill", "none"); icon.setAttribute("stroke", "currentColor");
        icon.setAttribute("stroke-width", "2"); icon.setAttribute("aria-hidden", "true");
        const path = document.createElementNS(icon.namespaceURI, "path");
        path.setAttribute("d", id === "appearance" ? "M4 5h16M4 12h16M4 19h16M8 2v6m8 1v6M8 16v6" : "M8 3h8l4 4v14H4V3h4Zm0 0v6h8V3M8 14h8m-8 3h5");
        icon.append(path); button.append(icon, document.createElement("span"));
        button.addEventListener("click", () => { entry.open(button); mountSettings(); });
        if (anchor) anchor.after(button); else (nav.querySelector(".tabs") || nav).append(button);
      }
      anchor = button;
      const active = Boolean(document.getElementById(`atlas-${id}-dialog`)?.open);
      const classes = (active ? nativeActive?.className : nativeIdle?.className) || tabBase + (active ? tabActive : tabIdle);
      if (button.className !== classes) button.className = classes;
      button.setAttribute("aria-expanded", String(active));
      const label = entry.label(), span = button.querySelector("span");
      if (span.textContent !== label) span.textContent = label;
    }
  };
  window.atlasSettings = { locale, register(id, entry) { settingsEntries.set(id, entry); mountSettings(); } };
  new MutationObserver(mountSettings).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  // The host handles Escape globally and prevents the native dialog default.
  // Close only the top Atlas dialog, preserving its underlying Settings screen.
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const dialog = [...document.querySelectorAll("dialog.atlas-settings-dialog[open]")].at(-1);
    if (!dialog) return;
    event.preventDefault(); event.stopImmediatePropagation(); dialog.close();
  }, true);
  const mount = () => {
    window.atlasSettings.register("appearance", {
      label: () => settingsWords[locale()].appearance,
      open: trigger => {
        const words = translations[locale()], labels = settingsWords[locale()];
        saved.mode = localStorage.getItem("theme") || "system";
        const option = (group, value) => `<button type="button" data-atlas-option="${group}" data-value="${value}" aria-pressed="${saved[group] === value}" class="atlas-option">${group === "size" ? `${value} px` : words[value]}</button>`;
        const dialog = document.createElement("dialog"); dialog.id = "atlas-appearance-dialog"; dialog.className = "atlas-settings-dialog";
        dialog.setAttribute("aria-labelledby", "atlas-appearance-title");
        dialog.innerHTML = `<header><h2 id="atlas-appearance-title">${labels.appearance}</h2><button type="button" data-close aria-label="${labels.close}">×</button></header>${Object.keys(choices).filter(group => group !== "mode").map(group => `<fieldset><legend>${words[group]}</legend><div class="atlas-options">${choices[group].map(value => option(group, value)).join("")}</div></fieldset>`).join("")}`;
        dialog.querySelector("[data-close]").onclick = () => dialog.close();
        const revoke = document.createElement("button"); revoke.type = "button"; revoke.id = "atlas-location-revoke"; revoke.textContent = labels.revoke;
        revoke.onclick = async () => { await window.atlasLocation.revoke(); revoke.textContent = labels.revoked; }; dialog.append(revoke);
        dialog.addEventListener("click", event => {
          const button = event.target.closest("[data-atlas-option]"); if (!button) return;
          const group = button.dataset.atlasOption; persist({ [group]: button.dataset.value });
          dialog.querySelectorAll(`[data-atlas-option="${group}"]`).forEach(item => item.setAttribute("aria-pressed", String(item === button)));
          if (group === "mode") {
            const theme = button.dataset.value; localStorage.setItem("theme", theme);
            document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches));
            document.documentElement.classList.toggle("light", !document.documentElement.classList.contains("dark"));
            window.dispatchEvent(new Event("atlas-theme-change"));
          }
        });
        dialog.addEventListener("close", () => { dialog.remove(); if (trigger.isConnected) trigger.focus(); });
        document.body.append(dialog); dialog.showModal();
      }
    });
  };
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", event => {
    if (localStorage.getItem("theme") !== "system") return;
    document.documentElement.classList.toggle("dark", event.matches);
    document.documentElement.classList.toggle("light", !event.matches);
    window.dispatchEvent(new Event("atlas-theme-change"));
  });
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", observeMessages, { once: true }) : observeMessages();
})();

// Owned sibling pane: no Svelte stores or upstream artifact lifecycle hooks.
(() => {
  "use strict";
  const start = () => {
    if (window.atlasSideView) return;
    const words = {
      en: ["Open in side view", "Open in your browser", "This page could not be confirmed as frameable. If it is blank or blocked, open it in your browser."],
      fr: ["Ouvrir dans la vue latérale", "Ouvrir dans votre navigateur", "L’affichage de cette page ne peut pas être confirmé. Si elle est vide ou bloquée, ouvrez-la dans votre navigateur."],
      de: ["In Seitenansicht öffnen", "Im Browser öffnen", "Die Einbettung dieser Seite konnte nicht bestätigt werden. Ist sie leer oder blockiert, öffnen Sie sie im Browser."],
      es: ["Abrir en vista lateral", "Abrir en el navegador", "No se pudo confirmar que esta página se pueda mostrar aquí. Si está vacía o bloqueada, ábrala en el navegador."],
      it: ["Apri nella vista laterale", "Apri nel browser", "Non è possibile confermare la visualizzazione di questa pagina. Se è vuota o bloccata, aprila nel browser."]
    };
    const text = index => (words[(localStorage.getItem("locale") || localStorage.getItem("atlas-ui-language") || navigator.language || "fr").split("-")[0]] || words.fr)[index];
    // The server writes an answer's links in the conversation language (FOOTER_LABELS in artifact_tools.py, OPEN_LABELS in side_view.py).
    // The chip that replaces its open link keeps that language: a French answer read "Open in side view · Télécharger …" in an English interface (staging 2026-11-12).
    const answerOpeners = { en: /^Open (?:.+ \(version \d+\)|in side view)$/, fr: /^Ouvrir (?:.+ \(version \d+\)|dans le panneau latéral)$/, de: /^(?:.+ öffnen \(Version \d+\)|In der Seitenansicht öffnen)$/, es: /^Abrir (?:.+ \(versión \d+\)|en la vista lateral)$/, it: /^Apri (?:.+ \(versione \d+\)|nella vista laterale)$/ };
    const openText = written => { const language = Object.keys(answerOpeners).find(key => answerOpeners[key].test(written.trim())); return language ? words[language][0] : text(0); };
    const sideLabels = {
  "Side view": [
    "Side view",
    "Vue latérale",
    "Seitenansicht",
    "Vista lateral",
    "Vista laterale"
  ],
  "Download": [
    "Download",
    "Télécharger",
    "Herunterladen",
    "Descargar",
    "Scarica"
  ],
  "Resize side view": [
    "Resize side view",
    "Redimensionner la vue latérale",
    "Seitenansicht skalieren",
    "Redimensionar vista lateral",
    "Ridimensiona vista laterale"
  ],
  "Resize split": [
    "Resize split",
    "Redimensionner la séparation",
    "Teilung skalieren",
    "Redimensionar división",
    "Ridimensiona divisione"
  ],
  "Top view": [
    "Top view",
    "Vue du haut",
    "Obere Ansicht",
    "Vista superior",
    "Vista superiore"
  ],
  "Bottom view": [
    "Bottom view",
    "Vue du bas",
    "Untere Ansicht",
    "Vista inferior",
    "Vista inferiore"
  ],
  "Close slot": [
    "Close slot",
    "Fermer cette vue",
    "Diese Ansicht schließen",
    "Cerrar esta vista",
    "Chiudi questa vista"
  ],
  "Top": [
    "Top",
    "Haut",
    "Oben",
    "Arriba",
    "Sopra"
  ],
  "Bottom": [
    "Bottom",
    "Bas",
    "Unten",
    "Abajo",
    "Sotto"
  ],
  "Split": [
    "Split",
    "Diviser",
    "Teilen",
    "Dividir",
    "Dividi"
  ],
  "Fullscreen": [
    "Fullscreen",
    "Plein écran",
    "Vollbild",
    "Pantalla completa",
    "Schermo intero"
  ],
  "Close side view": [
    "Close side view",
    "Fermer la vue latérale",
    "Seitenansicht schließen",
    "Cerrar vista lateral",
    "Chiudi vista laterale"
  ],
  "Markdown source": [
    "Markdown source",
    "Source Markdown",
    "Markdown-Quelle",
    "Fuente Markdown",
    "Sorgente Markdown"
  ],
  "Edit": [
    "Edit",
    "Modifier",
    "Bearbeiten",
    "Editar",
    "Modifica"
  ],
  "Preview": [
    "Preview",
    "Aperçu",
    "Vorschau",
    "Vista previa",
    "Anteprima"
  ],
  "Both": [
    "Both",
    "Les deux",
    "Beide",
    "Ambas",
    "Entrambe"
  ],
  "Download .md": [
    "Download .md",
    "Télécharger .md",
    "Herunterladen .md",
    "Descargar .md",
    "Scarica .md"
  ],
  "Save": [
    "Save",
    "Enregistrer",
    "Speichern",
    "Guardar",
    "Salva"
  ],
  "Saving…": [
    "Saving…",
    "Enregistrement…",
    "Wird gespeichert…",
    "Guardando…",
    "Salvataggio…"
  ],
  "Saved extracted text; original download unchanged.": [
    "Saved extracted text; original download unchanged.",
    "Texte extrait enregistré ; le fichier original est inchangé.",
    "Extrahierter Text gespeichert; Originaldatei unverändert.",
    "Texto extraído guardado; archivo original sin cambios.",
    "Testo estratto salvato; file originale invariato."
  ],
  "Save failed. Download .md to keep your edits.": [
    "Save failed. Download .md to keep your edits.",
    "Échec de l’enregistrement. Téléchargez le fichier .md pour conserver vos modifications.",
    "Speichern fehlgeschlagen. Laden Sie .md herunter, um Änderungen zu behalten.",
    "No se pudo guardar. Descarga .md para conservar tus cambios.",
    "Salvataggio non riuscito. Scarica .md per conservare le modifiche."
  ],
  "PDF page": [
    "PDF page",
    "Page PDF",
    "PDF-Seite",
    "Página PDF",
    "Pagina PDF"
  ],
  "Page": [
    "Page",
    "Page",
    "Seite",
    "Página",
    "Pagina"
  ],
  "PDF rendering failed": [
    "PDF rendering failed",
    "Échec de l’affichage du PDF",
    "PDF-Darstellung fehlgeschlagen",
    "No se pudo mostrar el PDF",
    "Visualizzazione PDF non riuscita"
  ],
  "Previous page": [
    "Previous page",
    "Page précédente",
    "Vorherige Seite",
    "Página anterior",
    "Pagina precedente"
  ],
  "Next page": [
    "Next page",
    "Page suivante",
    "Nächste Seite",
    "Página siguiente",
    "Pagina successiva"
  ],
  "Fit width": [
    "Fit width",
    "Ajuster à la largeur",
    "An Breite anpassen",
    "Ajustar al ancho",
    "Adatta alla larghezza"
  ],
  "Changes": [
    "Changes",
    "Modifications",
    "Änderungen",
    "Cambios",
    "Modifiche"
  ],
  "No changes in this diff.": [
    "No changes in this diff.",
    "Aucune modification dans cette comparaison.",
    "Keine Änderungen in diesem Vergleich.",
    "No hay cambios en esta comparación.",
    "Nessuna modifica in questo confronto."
  ],
  "− Before / + After · unchanged lines provide context": [
    "− Before / + After · unchanged lines provide context",
    "− Avant / + Après · les lignes inchangées donnent le contexte",
    "− Vorher / + Nachher · unveränderte Zeilen zeigen den Kontext",
    "− Antes / + Después · las líneas sin cambios aportan contexto",
    "− Prima / + Dopo · le righe invariate danno contesto"
  ],
  "Unable to open view": [
    "Unable to open view",
    "Impossible d’ouvrir la vue",
    "Ansicht kann nicht geöffnet werden",
    "No se puede abrir la vista",
    "Impossibile aprire la vista"
  ],
  "App preview": [
    "App preview",
    "Aperçu de l'application",
    "App-Vorschau",
    "Vista previa de la aplicación",
    "Anteprima dell'app"
  ],
  "Version {n}": [
    "Version {n}",
    "Version {n}",
    "Version {n}",
    "Versión {n}",
    "Versione {n}"
  ],
  "New version available": [
    "New version available",
    "Nouvelle version disponible",
    "Neue Version verfügbar",
    "Nueva versión disponible",
    "Nuova versione disponibile"
  ],
  "Reload": [
    "Reload",
    "Recharger",
    "Neu laden",
    "Recargar",
    "Ricarica"
  ],
  "Temporary preview: data entered in the app is not kept.": [
    "Temporary preview: data entered in the app is not kept.",
    "Aperçu temporaire : les données saisies ne sont pas conservées.",
    "Temporäre Vorschau: Eingegebene Daten werden nicht gespeichert.",
    "Vista previa temporal: los datos introducidos no se conservan.",
    "Anteprima temporanea: i dati inseriti non vengono conservati."
  ],
  "The app tried to open another page and was stopped.": [
    "The app tried to open another page and was stopped.",
    "L'application a tenté d'ouvrir une autre page ; elle a été arrêtée.",
    "Die App wollte eine andere Seite öffnen und wurde angehalten.",
    "La aplicación intentó abrir otra página y se detuvo.",
    "L'app ha tentato di aprire un'altra pagina ed è stata fermata."
  ],
  "Restart app": [
    "Restart app",
    "Relancer l'application",
    "App neu starten",
    "Reiniciar la aplicación",
    "Riavvia l'app"
  ]
};
    const label = value => sideLabels[value]?.[Math.max(0,["en","fr","de","es","it"].indexOf((localStorage.getItem("locale") || localStorage.getItem("atlas-ui-language") || navigator.language || "fr").split("-")[0]))] || value;
    const element = (tag, cls, value) => { const node = document.createElement(tag); if (cls) node.className = cls; if (value) node.textContent = value; return node; };
    const button = (value, action) => { const node = element("button", "atlas-side-button", label(value)); node.type = "button"; node.addEventListener("click", action); return node; };
    const safeURL = (value, sameOrigin = false) => {
      const url = new URL(value, location.href);
      if (!/^https?:$/.test(url.protocol) || url.username || url.password || (sameOrigin && url.origin !== location.origin)) throw new Error("Unsupported source URL");
      return url.href;
    };
    const download = (value, filename, type) => {
      const href = URL.createObjectURL(new Blob([value], { type })), link = element("a");
      link.href = href; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(href), 1000);
    };
    const app = [...document.body.children].find(node => node.tagName === "DIV" && node.style.display === "contents") || document.querySelector(".atlas-project-host");
    if (!app) return;
    app.classList.add("atlas-app-root");
    const pane = element("aside", "atlas-side-pane"); pane.id = "atlas-side-view"; pane.hidden = true; pane.setAttribute("aria-label", label("Side view"));
    const grip = element("div", "atlas-side-grip"), header = element("header", "atlas-side-header"), title = element("strong", "atlas-side-title", label("Side view")), slotsHost = element("div", "atlas-side-slots");
    const slots = new Map(); let active = "top", priorFocus = null;
    const stored = Number(localStorage.getItem("atlas-side-width"));
    let desktopWidth = stored || innerWidth * .45;
    // The mobile overlay's measured width must never replace the user's desktop preference.
    const setWidth = width => { desktopWidth = Math.max(320, width); pane.style.width = `${Math.min(innerWidth, desktopWidth)}px`; };
    setWidth(stored || innerWidth * .45);
    const browser = button(text(1), () => { const src = slots.get(active)?.descriptor?.src; if (src) window.open(safeURL(src), "_blank", "noopener,noreferrer"); });
    const saving = button(label("Download"), () => slots.get(active)?.download?.());
    const openInTabEnabled = document.querySelector('meta[name="atlas-artifact-open-in-tab"]')?.content === "1";
    const refresh = () => { const slot = slots.get(active); title.textContent = slot?.descriptor?.title || label("Side view"); browser.hidden = !openInTabEnabled || slot?.descriptor?.kind !== "app" || !slot?.descriptor?.src; saving.hidden = !slot?.download; header.querySelectorAll("[data-slot-select]").forEach(node => node.setAttribute("aria-pressed", String(node.dataset.slotSelect === active))); };
    const remove = name => {
      const slot = slots.get(name); if (!slot) return;
      slot.disposed = true; slot.cleanup.forEach(fn => fn()); slot.node.remove(); slots.delete(name);
      const divider = slotsHost.querySelector(".atlas-side-divider"); if (divider) divider.remove();
      slots.forEach(item => { item.node.style.flex = "1 1 0"; });
      if (!slots.size) { pane.hidden = true; document.body.classList.remove("atlas-side-open"); if (document.fullscreenElement === pane) document.exitFullscreen(); priorFocus?.focus?.(); }
      else { active = slots.keys().next().value; refresh(); }
    };
    const close = () => [...slots.keys()].forEach(remove);
    const drag = (node, vertical, move) => {
      node.tabIndex = 0; node.setAttribute("role", "separator"); node.setAttribute("aria-orientation", vertical ? "vertical" : "horizontal"); node.setAttribute("aria-label", vertical ? label("Resize side view") : label("Resize split"));
      node.addEventListener("pointerdown", event => { node.setPointerCapture(event.pointerId); document.body.classList.add("atlas-side-dragging"); });
      node.addEventListener("pointermove", event => { if (node.hasPointerCapture(event.pointerId)) move(vertical ? event.clientX : event.clientY); });
      const finish = () => { document.body.classList.remove("atlas-side-dragging"); localStorage.setItem("atlas-side-width", String(pane.getBoundingClientRect().width)); };
      node.addEventListener("pointerup", finish); node.addEventListener("lostpointercapture", finish);
      node.addEventListener("keydown", event => { if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return; event.preventDefault(); const rect = node.getBoundingClientRect(); move((vertical ? rect.x : rect.y) + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -24 : 24)); finish(); });
    };
    const ensure = name => {
      if (slots.has(name)) return slots.get(name);
      const node = element("section", "atlas-side-slot"), toolbar = element("div", "atlas-side-toolbar"), caption = element("span", "atlas-side-caption", name === "top" ? label("Top view") : label("Bottom view")), content = element("div", "atlas-side-content");
      node.dataset.slot = name; toolbar.append(caption, button(label("Close slot"), () => remove(name))); node.append(toolbar, content);
      if (name === "top") slotsHost.prepend(node); else slotsHost.append(node);
      const slot = { node, toolbar, caption, content, cleanup: [], disposed: false, descriptor: null, download: null };
      node.addEventListener("pointerdown", () => { active = name; refresh(); }); node.addEventListener("focusin", () => { active = name; refresh(); });
      slots.set(name, slot);
      if (slots.size === 2) {
        const divider = element("div", "atlas-side-divider"); slots.get("top").node.after(divider);
        drag(divider, false, y => { const rect = slotsHost.getBoundingClientRect(), ratio = Math.max(.15, Math.min(.85, (y - rect.top) / rect.height)); slots.get("top").node.style.flex = `${ratio} 1 0`; slots.get("bottom").node.style.flex = `${1 - ratio} 1 0`; });
      }
      return slot;
    };
    const frame = slot => { const node = element("iframe", "atlas-side-frame"); node.title = slot.descriptor.title || label("Side view"); node.setAttribute("sandbox", "allow-scripts"); node.referrerPolicy = "no-referrer"; return node; };
    // Marked is only a parser. Rebuild its output from an allowlist in an inert document.
    const sanitize = html => {
      const parsed = new DOMParser().parseFromString(html, "text/html"), result = document.createDocumentFragment();
      const allowed = new Set("P BR HR H1 H2 H3 H4 H5 H6 BLOCKQUOTE UL OL LI PRE CODE STRONG EM DEL TABLE THEAD TBODY TR TH TD A".split(" "));
      const copy = (source, target) => { for (const child of source.childNodes) { if (child.nodeType === Node.TEXT_NODE) { target.append(document.createTextNode(child.textContent)); continue; } if (child.nodeType !== Node.ELEMENT_NODE || !allowed.has(child.tagName)) continue; const node = element(child.tagName); if (child.tagName === "A") { try { node.href = safeURL(child.getAttribute("href")); node.target = "_blank"; node.rel = "noopener noreferrer"; } catch { /* Unsafe destinations become plain text. */ } } copy(child, node); target.append(node); } };
      copy(parsed.body, result); return result;
    };
    const views = {
      diff: { async open(slot, descriptor) {
        const source = descriptor.content ?? (descriptor.src ? await fetchText(descriptor.src, slot) : "");
        if (slot.disposed) return;
        const view = element("div", "atlas-side-diff");
        view.append(element("p", "atlas-side-diff-key", label("− Before / + After · unchanged lines provide context")));
        for (const section of source.split(/(?=^diff --git )/m)) {
          if (!section) continue;
          const file = element("section", "atlas-diff-file"), pre = element("pre");
          const heading = section.match(/^\+\+\+ (?:b\/)?(.+)$/m)?.[1] || descriptor.title || label("Changes");
          file.append(element("h3", "", heading));
          for (const line of section.split("\n")) pre.append(element("span", line.startsWith("+") ? "atlas-diff-add" : line.startsWith("-") ? "atlas-diff-remove" : "", line + "\n"));
          file.append(pre); view.append(file);
        }
        if (!source.trim()) view.append(element("p", "", label("No changes in this diff.")));
        slot.content.append(view); slot.download = () => download(source, "changes.patch", "text/plain");
      } },
      html: { async open(slot, descriptor) {
        const source = descriptor.content ?? (descriptor.src ? await fetchText(descriptor.src, slot) : "");
        if (slot.disposed) return; const node = frame(slot); node.srcdoc = source; slot.content.append(node);
        slot.download = () => download(source, "preview.html", "text/html");
      } },
      app: { async open(slot, descriptor) {
        const node = frame(slot); node.allow = ""; node.title = `${label("App preview")} ${descriptor.title || ""}`.trim(); node.src = descriptor.src;
        const version = element("span", "atlas-side-version", label("Version {n}").replace("{n}", String(descriptor.version)));
        const notice = element("p", "atlas-side-notice", label("Temporary preview: data entered in the app is not kept."));
        let loads = 0;
        node.addEventListener("load", () => {
          if (slot.disposed) return;
          loads += 1;
          if (loads === 1) { node.focus(); return; }
          node.remove();
          const stopped = element("div", "atlas-side-stopped");
          stopped.append(element("p", "", label("The app tried to open another page and was stopped.")), button("Restart app", () => open(descriptor).catch(() => {})));
          slot.content.append(stopped);
        });
        slot.toolbar.append(version);
        slot.content.append(node, notice);
        slot.download = () => {
          const link = element("a"); link.href = descriptor.download; link.download = ""; link.click();
        };
      } },
      url: { async open(slot, descriptor) {
        const url = safeURL(descriptor.src), node = frame(slot), notice = element("p", "atlas-side-fallback", text(2)); notice.hidden = true;
        // A cross-origin load event also fires for CSP/XFO error pages. Never call it proof.
        slot.content.prepend(notice);
        const timer = setTimeout(() => { notice.hidden = false; }, 4000);
        node.addEventListener("error", () => { notice.hidden = false; });
        node.src = url; slot.content.append(node); slot.cleanup.push(() => clearTimeout(timer));
      } },
      markdown: { async open(slot, descriptor) {
        const { marked } = await import("/static/atlas/vendor/marked/marked.esm.js");
        const source = descriptor.content ?? (descriptor.src ? await fetchText(descriptor.src, slot) : "");
        if (slot.disposed) return;
        const editor = element("textarea", "atlas-side-editor"), preview = element("div", "atlas-side-preview"), layout = element("div", "atlas-side-markdown"), status = element("span", "atlas-side-save-status");
        editor.setAttribute("aria-label", label("Markdown source")); editor.spellcheck = false; editor.value = source; status.setAttribute("role", "status");
        const render = () => { const renderer = new marked.Renderer(); renderer.html = () => ""; renderer.image = () => ""; preview.replaceChildren(sanitize(marked.parse(editor.value, { renderer, async: false }))); };
        editor.addEventListener("input", render); render(); layout.append(editor, preview); slot.content.append(layout);
        if (descriptor.readOnly) { editor.hidden = true; layout.style.gridTemplateColumns = "minmax(0,1fr)"; return; }
        for (const mode of ["Edit", "Preview", "Both"]) slot.toolbar.append(button(mode, () => { editor.hidden = mode === "Preview"; preview.hidden = mode === "Edit"; }));
        slot.download = () => download(editor.value, "document.md", "text/markdown;charset=utf-8");
        slot.toolbar.append(button(label("Download .md"), slot.download));
        if (descriptor.saveTarget) {
          const target = safeURL(descriptor.saveTarget, true);
          if (!/^\/api\/v1\/files\/[a-f0-9-]+\/data\/content\/update$/i.test(new URL(target).pathname)) throw new Error("Unsupported save target");
          slot.toolbar.append(button(label("Save"), async () => {
            status.textContent = label("Saving…");
            try { const response = await fetch(target, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: editor.value }) }); const result = await response.json(); if (!response.ok || result.content !== editor.value) throw new Error("Content was not saved"); status.textContent = label("Saved extracted text; original download unchanged."); }
            catch { status.textContent = label("Save failed. Download .md to keep your edits."); }
          }), status);
        }
      } },
      pdf: { async open(slot, descriptor) {
        const pdfjs = await import("/static/atlas/vendor/pdfjs/pdf.min.mjs"); if (slot.disposed) return;
        pdfjs.GlobalWorkerOptions.workerSrc = "/static/atlas/vendor/pdfjs/pdf.worker.min.mjs";
        const source = safeURL(descriptor.src, true), loading = pdfjs.getDocument({ url: source, withCredentials: true, isEvalSupported: false });
        slot.cleanup.push(() => { loading.destroy(); });
        const pdf = await loading.promise; if (slot.disposed) return;
        const pages = element("div", "atlas-side-pdf-pages"), count = element("span", "atlas-side-page-count"), input = element("input", "atlas-side-page-input");
        input.type = "number"; input.min = "1"; input.max = String(pdf.numPages); input.value = "1"; input.setAttribute("aria-label", label("PDF page")); count.textContent = `/ ${pdf.numPages}`;
        let scale = 1, epoch = 0;
        const canvases = Array.from({ length: pdf.numPages }, (_, index) => { const canvas = element("canvas"); canvas.setAttribute("aria-label", `${label("Page")} ${index + 1}`); canvas.dataset.page = String(index + 1); pages.append(canvas); return canvas; });
        const tasks = new Set();
        const render = async () => {
          const current = ++epoch; tasks.forEach(task => task.cancel());
          for (let index = 0; index < canvases.length; index++) {
            const page = await pdf.getPage(index + 1); if (slot.disposed || epoch !== current) return;
            const viewport = page.getViewport({ scale: Math.max(.1, (slot.content.clientWidth - 24) / page.getViewport({ scale: 1 }).width) * scale });
            const canvas = canvases[index], ratio = Math.min(devicePixelRatio || 1, 2); canvas.width = Math.floor(viewport.width * ratio); canvas.height = Math.floor(viewport.height * ratio); canvas.style.width = `${viewport.width}px`; canvas.style.height = `${viewport.height}px`;
            const task = page.render({ canvasContext: canvas.getContext("2d"), viewport, transform: [ratio, 0, 0, ratio, 0, 0] }); tasks.add(task);
            try { await task.promise; canvas.dataset.rendered = "true"; } catch (error) { if (error.name !== "RenderingCancelledException") throw error; } finally { tasks.delete(task); }
          }
        };
        const navigate = step => { input.value = String(Math.max(1, Math.min(pdf.numPages, Number(input.value) + step))); canvases[Number(input.value) - 1].scrollIntoView({ block: "start" }); };
        input.addEventListener("change", () => navigate(0));
        const rerender = () => render().catch(() => { if (!slot.disposed) count.textContent = label("PDF rendering failed"); });
        slot.toolbar.append(button(label("Previous page"), () => navigate(-1)), input, count, button(label("Next page"), () => navigate(1)), button("−", () => { scale = Math.max(.25, scale / 1.25); rerender(); }), button("+", () => { scale = Math.min(4, scale * 1.25); rerender(); }), button(label("Fit width"), () => { scale = 1; rerender(); }));
        slot.content.append(pages);
        slot.download = () => { const link = element("a"); link.href = source; link.download = descriptor.title || "document.pdf"; link.click(); };
        let timer; const observer = new ResizeObserver(() => { clearTimeout(timer); timer = setTimeout(rerender, 150); }); observer.observe(slot.content);
        slot.cleanup.push(() => { epoch++; tasks.forEach(task => task.cancel()); observer.disconnect(); clearTimeout(timer); });
        await render();
      } }
    };
    async function fetchText(src, slot) {
      const controller = new AbortController(); slot.cleanup.push(() => controller.abort());
      const response = await fetch(safeURL(src, true), { credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`Source unavailable (${response.status})`); return response.text();
    }
    const open = async descriptor => {
      if (!descriptor || !Object.hasOwn(views, descriptor.kind)) throw new Error("Unsupported side view kind");
      if (descriptor.src) safeURL(descriptor.src, descriptor.kind !== "url");
      const name = descriptor.slot === "bottom" ? "bottom" : "top";
      if (pane.hidden) priorFocus = document.activeElement;
      // Replace contents without removing the slot or its split proportions.
      const old = ensure(name); old.disposed = true; old.cleanup.forEach(fn => fn());
      const slot = { ...old, cleanup: [], disposed: false, descriptor: { ...descriptor }, download: null }; slots.set(name, slot);
      slot.content.replaceChildren(); while (slot.toolbar.children.length > 2) slot.toolbar.lastChild.remove();
      slot.caption.textContent = descriptor.title || descriptor.kind; pane.hidden = false; document.body.classList.add("atlas-side-open"); active = name; refresh();
      try { await views[descriptor.kind].open(slot, descriptor); }
      catch (error) { if (!slot.disposed) slot.content.append(element("p", "atlas-side-error", `${label("Unable to open view")}: ${error.message}`)); }
      if (!slot.disposed) refresh();
    };
    header.append(title);
    for (const name of ["top", "bottom"]) { const control = button(name === "top" ? label("Top") : label("Bottom"), () => { ensure(name); active = name; refresh(); }); control.dataset.slotSelect = name; header.append(control); }
    header.append(button(label("Split"), () => { ensure("top"); ensure("bottom"); }), browser, saving, button(label("Fullscreen"), async () => { if (document.fullscreenElement === pane) await document.exitFullscreen(); else await pane.requestFullscreen(); }), button(label("Close side view"), close));
    pane.append(grip, header, slotsHost); document.body.append(pane);
    drag(grip, true, x => setWidth(innerWidth - x)); window.addEventListener("resize", () => setWidth(desktopWidth));
    document.addEventListener("keydown", event => { if (event.key === "Escape" && !pane.hidden) { close(); event.preventDefault(); } });
    window.atlasSideView = { open, close };
    const seen = new WeakSet(), opened = new Set(), versions = new Map();
    // On a phone the side view covers the whole conversation: never open it by itself there, the chip stays one tap away.
    const wide = () => matchMedia("(min-width: 768px)").matches;
    const chip = (node, descriptor) => { const control = button(text(0), () => open(typeof descriptor === "function" ? descriptor() : descriptor)); control.classList.add("atlas-side-chip"); node.after(control); };
    const scan = () => {
      // This pinned renderer escapes raw spans as text; hide only our exact wire marker.
      document.querySelectorAll('.chat-assistant .markdown-prose p').forEach(node => { if (/^<span class="atlas-view" data-atlas-view="(?:pdf|markdown|html|url)" [^<>]*hidden><\/span>$/.test(node.textContent.trim())) node.hidden = true; });
      document.querySelectorAll('.chat-assistant .atlas-view[data-atlas-view]').forEach(node => {
        if (seen.has(node)) return; seen.add(node);
        const descriptor = { kind: node.dataset.atlasView, src: node.dataset.src, title: node.dataset.title, slot: node.dataset.slot };
        const message = node.closest('[id^="message-"]') || node.closest(".chat-assistant");
        const identity = `${location.pathname}:${message?.id || ""}:${JSON.stringify(descriptor)}`;
        chip(node, descriptor); if (wide() && !opened.has(identity)) { opened.add(identity); open(descriptor).catch(() => {}); }
      });
      document.querySelectorAll('.chat-assistant .markdown-prose a[href], .user-message .markdown-prose a[href]').forEach(node => {
        if (seen.has(node)) return;
        const url = new URL(node.href, location.href), transport = new URLSearchParams(url.hash.slice(1));
        const kind = transport.get("atlas-view");
        if (Object.hasOwn(views, kind)) {
          let descriptor = null;
          if (kind === "app") {
            const artifact = transport.get("artifact"), version = Number(transport.get("version"));
            const match = url.pathname.match(/^\/api\/atlas\/artifacts\/([0-9a-f]{32})\/versions\/([1-9][0-9]{0,3})\/open$/);
            if (url.origin !== location.origin || !match || match[1] !== artifact || Number(match[2]) !== version) return;
            descriptor = { kind, artifact, version, src: url.pathname, download: url.pathname.replace(/\/open$/, "/download"), title: transport.get("title") || node.textContent, slot: transport.get("slot") || "top" };
          } else {
            descriptor = { kind, src: url.origin + url.pathname + url.search, title: transport.get("title") || node.textContent, slot: transport.get("slot") || "top" };
            if (url.origin === location.origin) descriptor.src = url.pathname + url.search;
          }
          seen.add(node);
          const message = node.closest('[id^="message-"]') || node.closest(".chat-assistant");
          const identity = `${location.pathname}:${message?.id || ""}:${JSON.stringify(descriptor)}`;
          const placeholder = element("span", "atlas-view"); placeholder.hidden = true;
          placeholder.dataset.atlasView = descriptor.kind; placeholder.dataset.src = descriptor.src || ""; placeholder.dataset.title = descriptor.title; placeholder.dataset.slot = descriptor.slot;
          if (descriptor.artifact) { placeholder.dataset.artifact = descriptor.artifact; placeholder.dataset.version = String(descriptor.version); }
          seen.add(placeholder); node.before(placeholder);
          node.classList.add("atlas-side-chip"); node.textContent = openText(node.textContent);
          node.addEventListener("click", event => { event.preventDefault(); open(descriptor).catch(() => {}); });
          if (descriptor.kind !== "app" && wide() && !opened.has(identity)) { opened.add(identity); open(descriptor).catch(() => {}); }
          if (descriptor.kind === "app") {
            const messages = [...document.querySelectorAll(".chat-assistant")], last = messages[messages.length - 1], latest = message === last || message?.contains(last);
            const current = slots.get(descriptor.slot === "bottom" ? "bottom" : "top");
            // A version this page has not met yet, in the last answer, is the one the conversation just produced.
            const known = versions.get(descriptor.artifact) || 0, produced = latest && descriptor.version > known;
            versions.set(descriptor.artifact, Math.max(known, descriptor.version));
            if (current?.descriptor?.kind === "app" && current.descriptor.artifact === descriptor.artifact && descriptor.version > current.descriptor.version) {
              // The change just asked for replaces the open version by itself: the panel stayed on version 1 behind a Reload bar (staging 2026-11-12).
              // Only a newer version already met, rendered again while an older one is open by choice, is offered and not imposed.
              if (produced) open(descriptor).catch(() => {});
              else if (!current.node.querySelector(".atlas-side-banner")) {
                const banner = element("div", "atlas-side-banner");
                banner.append(element("span", "", label("New version available")), button("Reload", () => open(descriptor).catch(() => {})));
                current.content.prepend(banner);
              }
            } else if (wide() && latest) open(descriptor).catch(() => {});
          }
          return;
        }
        const path = url.pathname, match = path.match(/\.(pdf|md)$/i); if (!match) return; seen.add(node);
        chip(node, { kind: match[1].toLowerCase() === "pdf" ? "pdf" : "markdown", src: node.href, title: node.textContent });
      });
      document.querySelectorAll('.chat-assistant .markdown-prose pre, .chat-assistant .markdown-prose .cm-editor, .user-message .markdown-prose pre, .user-message .markdown-prose .cm-editor').forEach(node => {
        if (seen.has(node)) return;
        const code = node.querySelector("code"), wrapper = node.closest(".code-block") || node.parentElement?.parentElement;
        const language = (node.querySelector(".cm-content")?.dataset.language || code?.className.match(/language-(html|markdown|md)\b/i)?.[1] || wrapper?.querySelector(".lang")?.textContent || wrapper?.querySelector(".text-xs")?.textContent || "").trim().toLowerCase();
        if (!["html", "markdown", "md"].includes(language)) return; seen.add(node);
        chip(node, () => ({ kind: language === "html" ? "html" : "markdown", content: code?.textContent ?? [...node.querySelectorAll(".cm-line")].map(line => line.textContent).join("\n"), title: language }));
      });
    };
    let queued = false;
    new MutationObserver(records => { if (records.every(record => pane.contains(record.target))) return; if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; scan(); }); } }).observe(app, { childList: true, subtree: true, characterData: true });
    scan();
    const hash = () => { if (location.hash.startsWith("#atlas-view=url:")) { try { open({ kind: "url", src: decodeURIComponent(location.hash.slice(16)), title: "Preview" }).catch(() => {}); } catch { /* Malformed hash does not disrupt chat. */ } } };
    window.addEventListener("hashchange", hash); hash();
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();

// Sidebar search (Ctrl+K, owner report 2026-11-10): the first entry of the list is
// Open WebUI's "Start a new conversation" action, so Enter after typing a word
// started a chat with that word. When the search found chats, Enter opens the
// first one instead; with no result, Enter keeps its native behaviour.
(() => {
  "use strict";
  document.addEventListener("keydown", event => {
    if (event.key !== "Enter" || event.isComposing || event.shiftKey) return;
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    const box = input.closest("[role=dialog], dialog");
    if (!box || !input.value.trim()) return;
    const first = box.querySelector("a[href^='/c/']");
    if (!first) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    first.click();
  }, true);
})();

// Owner Code workspace: the server flag is authoritative; no UI is mounted otherwise.
(() => {
  "use strict";
  const start = async () => {
    if (document.querySelector('meta[name="atlas-code-mode"]')?.content !== "1") return;
    const base = "/atlas-code", node = (tag, cls, text) => { const el = document.createElement(tag); el.className = cls || ""; if (text !== undefined) el.textContent = text; return el; };
    const button = (label, action) => { const el = node("button", "atlas-code-button", label); el.type = "button"; el.addEventListener("click", action); return el; };
    const request = (path, options = {}) => fetch(base + path, { credentials: "same-origin", cache: "no-store", ...options });
    let config;
    try { const response = await request("/code/ui"); if (!response.ok) return; config = await response.json(); if (!config.enabled) return; } catch { return; }
    let mounted, currentPath;
    const mount = () => {
      const host = document.querySelector("#chat-container"), composer = document.querySelector("#message-input-container");
      if (!host || !composer || (mounted?.isConnected && currentPath === location.pathname)) return;
      if (mounted) { mounted.stop?.(); mounted.remove(); document.querySelector("#atlas-code-entry")?.remove(); }
      currentPath = location.pathname;
      const storageKey = `atlas-code:${currentPath}`;
      let state;
      try { state = JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* An unavailable saved session starts empty. */ }
      if (!state?.id) state = { id: crypto.randomUUID(), imported: false, messages: [], queue: [] };
      let busy = false, controller, upload, shown = false, stopping = false;
      const panel = node("section", "atlas-code-panel"); panel.id = "atlas-code-panel"; panel.hidden = true; panel.setAttribute("aria-label", "Code workspace"); mounted = panel;
      const header = node("header", "atlas-code-header"), title = node("strong", "", "Code workspace"), session = node("span", "atlas-code-session"), status = node("span", "atlas-code-status", "Import a project to begin"), plan = node("ol", "atlas-code-plan"), transcript = node("div", "atlas-code-transcript"), footer = node("form", "atlas-code-composer"), input = node("textarea"), queue = node("div", "atlas-code-queue"), notice = node("p", "atlas-code-notice");
      plan.setAttribute("aria-label", "Current task plan"); plan.hidden = true; status.setAttribute("role", "status"); notice.setAttribute("role", "status");
      input.setAttribute("aria-label", "Code message"); input.placeholder = "Describe the change…"; input.rows = 2;
      const file = node("input"); file.type = "file"; file.accept = ".zip,.tar,.tar.gz,.tgz"; file.setAttribute("aria-label", "Import project archive"); file.disabled = state.imported;
      const save = () => { try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch { notice.textContent = "Browser session storage is full. Keep this tab open to retain the conversation."; } };
      const setStatus = text => { status.textContent = text; session.textContent = state.id; };
      const showQueue = () => { queue.replaceChildren(); state.queue.forEach((text, index) => { const row = node("div", "atlas-code-queued", `Queued: ${text}`); row.append(button("Remove", () => { state.queue.splice(index, 1); save(); showQueue(); })); queue.append(row); }); };
      const copy = text => button("Copy", async event => { try { await navigator.clipboard.writeText(text); event.target.textContent = "Copied"; } catch { notice.textContent = "Clipboard unavailable. Select and copy the command text."; } });
      const prose = (target, text) => {
        target.replaceChildren();
        String(text).split(/```[^\n]*\n([\s\S]*?)```/g).forEach((part, index) => { if (!part) return; if (index % 2) { const block = node("div", "atlas-code-command"), pre = node("pre", "", part); block.append(pre, copy(part)); target.append(block); } else target.append(node("p", "atlas-code-prose", part)); });
      };
      const drawPlan = (target, items) => { target.replaceChildren(); for (const item of items || []) { const row = node("li", "", `${item.status === "completed" ? "✓" : item.status === "in_progress" ? "◉" : "○"} ${item.text}`); row.dataset.status = item.status; target.append(row); } target.hidden = !target.children.length; };
      const safeDownload = value => { const url = new URL(value, location.origin); if (url.origin !== location.origin || !url.pathname.startsWith("/api/v1/terminals/atlas-files/files/serve/")) return null; return url.href; };
      const renderTurn = record => {
        transcript.append(node("div", "atlas-code-user", record.user));
        const card = node("article", "atlas-code-turn"), activity = node("div", "atlas-code-activity"), answer = node("div", "atlas-code-answer"), finalPlan = node("ol", "atlas-code-final-plan"), actions = node("div", "atlas-code-actions");
        finalPlan.setAttribute("aria-label", "Task plan"); card.append(activity, finalPlan, answer, actions); transcript.append(card);
        const running = new Map(), approvals = new Map(); let latestPlan = [], lastSeq = 0; finalPlan.hidden = true;
        const finish = () => { drawPlan(finalPlan, latestPlan); for (const step of running.values()) { step.summary.textContent = `${step.tool} — interrupted — ${((performance.now() - step.started) / 1000).toFixed(1)}s`; step.row.dataset.status = "failed"; } running.clear(); for (const approval of approvals.values()) { approval.querySelectorAll("button").forEach(el => el.disabled = true); } };
        const consume = (event, replay = false) => {
          const follow = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
          if (event.version !== 1 || event.session_id !== state.id || event.seq <= lastSeq) return;
          lastSeq = event.seq; const out = event.output || {};
          if (event.type === "tool") {
            let step = running.get(event.tool);
            if (!step) {
              const row = node("details", "atlas-code-step"), summary = node("summary"), output = node("pre", "atlas-code-output");
              row.append(summary, output); activity.append(row);
              step = { row, summary, output, started: performance.now(), tool: event.tool }; running.set(event.tool, step);
              let args = {}; try { args = JSON.parse(event.arguments || "{}"); } catch { /* Tool errors still render the real result. */ }
              if (args.command) { const command = node("div", "atlas-code-command"); command.append(node("pre", "", args.command), copy(args.command)); row.insertBefore(command, output); }
              step.label = args.path || args.command || event.label;
            }
            step.row.dataset.status = event.status;
            const duration = Number(event.ui_duration_seconds ?? out.elapsed_seconds ?? (performance.now() - step.started) / 1000);
            step.summary.textContent = `${event.tool} · ${String(step.label || event.label).slice(0, 90)} — ${event.status === "completed" ? "done" : event.status} — ${duration.toFixed(1)}s`;
            step.output.textContent = event.status === "running" ? "Waiting for output…" : (out.output !== undefined ? String(out.output) : JSON.stringify(out, null, 2)) + (out.exit_code !== undefined ? `\nExit code: ${out.exit_code}` : "");
            if (event.status !== "running") { event.ui_duration_seconds = duration; running.delete(event.tool); }
          } else if (event.type === "plan") { latestPlan = out.items || []; if (!replay) drawPlan(plan, latestPlan); }
          else if (event.type === "import") { state.imported = true; file.disabled = true; upload = null; setStatus("Project imported"); }
          else if (event.type === "diff") {
            activity.append(button(`Review diff · ${event.label}`, () => window.atlasSideView.open({ kind: "diff", title: event.label, content: out.diff || "", slot: "top" })));
          } else if (event.type === "approval") {
            let approval = approvals.get(out.approval_id);
            if (!approval) { approval = node("section", "atlas-code-approval"); approval.setAttribute("aria-label", "Approval required"); approvals.set(out.approval_id, approval); activity.append(approval); }
            approval.replaceChildren(node("strong", "", event.status === "running" ? "Approval required" : `Approval ${out.decision}`));
            if (event.status === "running") {
              approval.append(node("p", "", out.action), node("p", "", `Expires after ${out.timeout_seconds} seconds.`));
              for (const [label, allow] of [["Allow", true], ["Deny", false]]) {
                const control = button(label, async () => {
                  approval.querySelectorAll("button").forEach(el => el.disabled = true);
                  try { const response = await request(`/code/approvals/${out.approval_id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ allow }) }); if (!response.ok) throw new Error(response.status === 410 ? "Approval expired or already answered" : `Approval failed (${response.status})`); }
                  catch (error) { approval.append(node("p", "", error.message)); }
                }); control.disabled = replay; approval.append(control);
              }
            }
          } else if (["honesty", "limit", "error"].includes(event.type)) {
            if (event.type === "error") record.failed = true;
            activity.append(node("p", "atlas-code-notice", event.type === "limit" ? `${event.label}: ${out.kind} measured ${out.measured} ${out.unit || ""}; limit ${out.limit} ${out.unit || ""}.` : `${event.label}: ${out.text || out.message || out.code}`));
          } else if (event.type === "export") {
            for (const value of out.files || []) { const href = safeDownload(value); if (!href) continue; const link = node("a", "atlas-code-button", value.endsWith(".patch") ? "Download patch" : "Download workspace"); link.href = href; link.download = value.split("/").at(-1); actions.append(link); if (value.endsWith(".patch")) actions.append(button("Review diff · all files", () => window.atlasSideView.open({ kind: "diff", src: href, title: "Workspace changes", slot: "top" }))); }
          } else if (event.type === "recap") {
            record.answer = out.text;
            // Export controls already preserve the exact, verified download destinations.
            const checked = out.text.replace(/\n\[[^\]]+\]\(\/api\/v1\/terminals\/atlas-files\/files\/serve\/[^)]+\)/g, "");
            const sentences = [...new Intl.Segmenter("en", { granularity: "sentence" }).segment(checked.split("```")[0])].map(item => item.segment.trim()).filter(Boolean);
            if (sentences.length > 3 || sentences.some(sentence => sentence.endsWith("?"))) {
              const recap = sentences.filter(sentence => !sentence.endsWith("?")).slice(0, 2);
              while (recap.length < 2) recap.push(recap.length ? `This turn used ${out.steps} tool steps.` : "The turn has finished.");
              prose(answer, recap.join(" "));
              const full = node("details", "atlas-code-full-response"), body = node("div");
              full.append(node("summary", "", "Full checked response")); prose(body, checked); full.append(body); answer.append(full);
            } else prose(answer, checked + (sentences.length < 2 ? `\nThis turn used ${out.steps} tool steps.` : ""));
            answer.append(node("p", "atlas-code-usage", `${out.steps} steps · ${out.model_calls} model calls · €${Number(out.cost_eur || 0).toFixed(4)}`));
            answer.append(node("p", "atlas-code-question", out.question || "What would you like to do next?"));
            for (const label of (out.actions || ["Review the changes", "Run the tests"]).slice(0, 2)) actions.append(button(label, () => enqueue(label)));
          }
          if (!replay) { record.events.push(event); save(); if (follow) requestAnimationFrame(() => { transcript.scrollTop = transcript.scrollHeight; }); }
        };
        (record.events || []).forEach(event => consume(event, true));
        if (record.answer && !answer.childNodes.length) prose(answer, record.answer);
        if (record.note) activity.append(node("p", "atlas-code-notice", record.note));
        return { consume, finish, card, activity, running, fallback(text) { if (!record.answer && text.trim()) { record.answer = text; prose(answer, text); } } };
      };
      state.messages.forEach(record => renderTurn(record).finish());
      const stop = button("Stop", () => { stopping = true; setStatus("Stopping — waiting for workspace release…"); controller?.abort(); }); stop.disabled = true;
      panel.stop = () => { state.queue = []; stopping = true; controller?.abort(); save(); };
      const send = node("button", "atlas-code-button atlas-code-send", "Send"); send.type = "submit";
      const release = async () => {
        for (let attempt = 0; attempt < 100; attempt++) {
          const response = await request(`/code/sessions/${state.id}/status`); if (!response.ok) throw new Error("Cannot confirm that the workspace stopped. Queued messages are retained.");
          if (!(await response.json()).running) return;
          await new Promise(resolve => setTimeout(resolve, 200));
        }
        throw new Error("Workspace still running after 20 seconds. Queued messages are retained.");
      };
      const run = async text => {
        busy = true; stopping = false; file.disabled = true; stop.disabled = false; send.textContent = "Queue message"; setStatus("Running"); notice.textContent = ""; plan.hidden = true;
        transcript.querySelector(".atlas-code-empty")?.remove();
        const record = { user: text, events: [], answer: "" }; state.messages.push(record); const view = renderTurn(record); requestAnimationFrame(() => { transcript.scrollTop = transcript.scrollHeight; }); controller = new AbortController(); save();
        const tick = setInterval(() => { for (const step of view.running.values()) step.summary.textContent = `${step.tool} · ${String(step.label).slice(0, 90)} — running — ${((performance.now() - step.started) / 1000).toFixed(1)}s`; }, 500);
        let released = false;
        try {
          let content = text;
          if (!state.imported && upload) content = [{ type: "text", text }, { type: "file", file: upload }];
          const messages = state.messages.slice(0, -1).flatMap(item => [{ role: "user", content: item.user }, ...(item.answer ? [{ role: "assistant", content: item.answer }] : [])]);
          messages.push({ role: "user", content });
          const response = await request("/v1/chat/completions", { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal, body: JSON.stringify({ model: config.profile, stream: true, code_mode: true, code_session_id: state.id, messages, ui_locale: "en" }) });
          if (!response.ok) throw new Error(`Code request rejected (${response.status}): ${await response.text()}`);
          const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "", done = false, fallback = "";
          while (true) {
            const chunk = await reader.read(); buffer += decoder.decode(chunk.value, { stream: !chunk.done });
            const frames = buffer.split(/\r?\n\r?\n/); buffer = frames.pop();
            for (const frame of frames) {
              const data = frame.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
              if (!data) continue; if (data === "[DONE]") { done = true; continue; }
              const event = JSON.parse(data); if (event.error) throw new Error(event.error.message || "Code stream failed"); if (event.atlas?.code) view.consume(event.atlas.code); else if (event.atlas?.phase !== "tool_details") fallback += (event.choices || []).map(choice => choice.delta?.content || "").join("");
            }
            if (chunk.done) break;
          }
          if (!done) throw new Error("Connection ended before the turn completed. Partial activity is preserved.");
          view.fallback(fallback); setStatus(record.failed ? "Turn failed" : "Ready");
        } catch (error) {
          record.note = stopping ? "Stopped by you. Completed edits remain in the workspace." : error.message;
          view.activity.append(node("p", "atlas-code-notice", record.note)); setStatus(stopping ? "Stopped" : "Turn interrupted");
        } finally {
          clearInterval(tick); view.finish(); plan.hidden = true; stop.disabled = true;
          try { await release(); released = true; } catch (error) { notice.textContent = error.message; }
          busy = false; file.disabled = state.imported; send.textContent = "Send"; save();
          if (released && state.queue.length && panel.isConnected) { const next = state.queue.shift(); showQueue(); save(); run(next); }
        }
      };
      function enqueue(text) {
        if (!text.trim()) return;
        if (!state.imported && !upload) { notice.textContent = "Import one zip or tar archive before sending a Code message."; return; }
        if (busy) { state.queue.push(text); showQueue(); save(); } else run(text);
      }
      file.addEventListener("change", async () => {
        const selected = file.files[0]; if (!selected) return;
        if (!/\.(zip|tar|tar\.gz|tgz)$/i.test(selected.name)) { notice.textContent = "Choose a zip or tar archive."; return; }
        if (selected.size > 16 * 1024 * 1024) { notice.textContent = `Archive is ${selected.size} bytes; UI upload limit is ${16 * 1024 * 1024} bytes.`; return; }
        try { const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(",")[1]); reader.onerror = reject; reader.readAsDataURL(selected); }); upload = { filename: selected.name, file_data: data }; setStatus(`${selected.name} ready to import`); notice.textContent = ""; } catch { notice.textContent = "The archive could not be read. Choose it again."; }
      });
      footer.addEventListener("submit", event => { event.preventDefault(); const text = input.value.trim(); enqueue(text); if (state.imported || upload || busy) input.value = ""; });
      input.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); footer.requestSubmit(); } });
      const entry = button("Code", () => { shown = !shown; panel.hidden = !shown; entry.setAttribute("aria-pressed", String(shown)); if (shown) input.focus(); }); entry.id = "atlas-code-entry"; entry.setAttribute("aria-pressed", "false");
      header.append(title, button("Back to chat", () => { shown = false; panel.hidden = true; entry.setAttribute("aria-pressed", "false"); entry.focus(); }), session, status);
      const uploadLabel = node("label", "atlas-code-upload", "Project archive "); uploadLabel.append(file);
      footer.append(uploadLabel, input, send, stop, notice, queue); panel.append(header, plan, transcript, footer); host.append(panel); composer.prepend(entry);
      setStatus(state.imported ? "Ready" : "Import a project to begin"); showQueue();
      if (!state.messages.length) transcript.append(node("p", "atlas-code-empty", "Import your project, then describe a change. Review each command, test result and diff here. The workspace has no network access."));
    };
    let scheduled = false;
    new MutationObserver(() => { if (!scheduled) { scheduled = true; requestAnimationFrame(() => { scheduled = false; mount(); }); } }).observe(document.body, { childList: true, subtree: true });
    mount();
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();

// The native UI renders SSE status events separately from the assistant answer.
(() => {
  "use strict";
  const steps = new Map([
    ["Starting the GPU", "starting"], ["Generating", "generating"],
    ["Démarrage du GPU", "starting"], ["Génération en cours", "generating"],
    ["GPU wird gestartet", "starting"], ["Bild wird generiert", "generating"],
    ["Iniciando la GPU", "starting"], ["Generando", "generating"],
    ["Avvio della GPU", "starting"], ["Generazione in corso", "generating"]
  ]);
  const active = new WeakMap();
  const startupNotice = /^(?:Le service de dessin démarre\.|The drawing service is starting\.|Der Zeichendienst startet\.|El servicio de dibujo está arrancando\.|Il servizio di disegno si sta avviando\.)/;
  const refresh = () => {
    document.querySelectorAll('.chat-assistant').forEach(bubble => {
      const prose = bubble.querySelector('.markdown-prose');
      const status = [...bubble.querySelectorAll('*')].find(node => !node.children.length && !node.closest('.markdown-prose,.atlas-image-waiting') && steps.has(node.textContent.trim().replace(/ \([\d.]+s \/ [\d.]+s\)$/, '')));
      let pending = active.get(bubble), card = bubble.querySelector('.atlas-image-waiting');
      if (status) {
        const label = status.textContent.trim().replace(/ \([\d.]+s \/ [\d.]+s\)$/, '');
        pending = { state: steps.get(label), label }; active.set(bubble, pending);
      }
      if (!pending) return;
      const delivered = prose && [...prose.querySelectorAll('img')].some(img => /\/images\//.test(img.src));
      // Mark only our service notice while an image status is active. The original
      // content remains available to API clients and returns after delivery.
      if (prose) prose.querySelectorAll('p').forEach(paragraph => {
        if (startupNotice.test(paragraph.textContent.trim())) {
          paragraph.dataset.atlasImageNotice = 'true'; paragraph.hidden = !delivered;
        }
      });
      if (delivered) { card?.remove(); active.delete(bubble); return; }
      const answer = prose && [...prose.children].filter(node => !node.classList.contains('atlas-image-waiting') && !node.dataset.atlasImageNotice).map(node => node.textContent.trim()).join(' ').trim();
      if (answer) pending = { state: 'failed', label: answer };
      if (!card) {
        card = document.createElement('div'); card.className = 'atlas-image-waiting';
        card.setAttribute('role', 'status'); card.setAttribute('aria-live', 'polite');
        const spinner = document.createElement('span'); spinner.className = 'atlas-image-spinner'; spinner.setAttribute('aria-hidden', 'true');
        const label = document.createElement('span'); label.className = 'atlas-image-step'; card.append(spinner, label);
        // The native status is a horizontal button; reserve space in the answer column.
        const host = prose || bubble.querySelector('[id="response-content-container"]') || bubble;
        host.append(card);
      }
      if (card.dataset.state !== pending.state) { card.dataset.state = pending.state; card.setAttribute('aria-busy', String(pending.state !== 'failed')); }
      const label = card.querySelector('.atlas-image-step');
      if (label.textContent !== pending.label) label.textContent = pending.label;
      if (pending.state === 'failed' && prose) prose.querySelectorAll('p').forEach(paragraph => { if (paragraph.textContent.trim() === pending.label) paragraph.hidden = true; });
    });
  };
  let scheduled = false;
  const schedule = () => { if (!scheduled) { scheduled = true; requestAnimationFrame(() => { scheduled = false; refresh(); }); } };
  const start = () => { new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true, characterData: true }); schedule(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();

// Real-time dictation (ATLAS_REALTIME_DICTATION): the Atlas voice button replaces the native
// microphone. Everything Atlas adds lives in document.body: the button is laid over the
// native microphone's place (hidden by chat-ui.css, kept in the layout), the live text is
// shown in a floating panel, and the Svelte composer is only read (positions) until the
// final text is written into it once, through its own editing path. No observer inserts
// nodes into Open WebUI's trees and nothing in the composer changes while the user speaks
// (#334 and #335 froze the page twice).
(() => {
  "use strict";
  const WORDS = {
    en: { dictate: "Dictate", stop: "Stop dictation", panel: "Dictation", starting: "Starting the microphone…", listening: "Listening", finishing: "Finishing…", local: "Transcribed on Atlas servers", cloud: "Transcribed by an external service", fallback: "Atlas engine busy: transcribed by an external service", strict: "Strict privacy: audio stays in Atlas", stopButton: "Stop", insert: "Insert the text", close: "Close", autoInsert: "Insert when I pause", limit: "Maximum dictation length reached.", permission: "Microphone access was refused. Allow it in the browser's site settings, then try again.", noMic: "No microphone was found on this device.", micBusy: "The microphone is used by another application.", insecure: "This browser cannot record here: dictation needs a secure connection and a recent browser.", busy: "The dictation engine is busy. Try again in a moment.", strictBusy: "The dictation engine is busy, and strict privacy keeps audio in Atlas. Try again in a moment.", sessionOpen: "A dictation is already running in another tab or window.", budget: "Your usage limit is reached, so dictation is not available for now.", provider: "The transcription service has reached its usage limit. Try again later.", network: "The connection to Atlas was lost. Check the network and try again.", idle: "No sound came from the microphone.", noComposer: "The message box is not available here: open a conversation, then insert the text.", unavailable: "Dictation is not available right now." },
    fr: { dictate: "Dicter", stop: "Arrêter la dictée", panel: "Dictée", starting: "Démarrage du micro…", listening: "À l’écoute", finishing: "Finalisation…", local: "Transcrit sur les serveurs Atlas", cloud: "Transcrit par un service externe", fallback: "Moteur Atlas occupé : transcrit par un service externe", strict: "Confidentialité stricte : l’audio reste dans Atlas", stopButton: "Arrêter", insert: "Insérer le texte", close: "Fermer", autoInsert: "Insérer à chaque pause", limit: "Durée maximale de dictée atteinte.", permission: "L’accès au micro a été refusé. Autorisez-le dans les réglages du site du navigateur, puis réessayez.", noMic: "Aucun micro n’a été trouvé sur cet appareil.", micBusy: "Le micro est utilisé par une autre application.", insecure: "Ce navigateur ne peut pas enregistrer ici : la dictée demande une connexion sécurisée et un navigateur récent.", busy: "Le moteur de dictée est occupé. Réessayez dans un instant.", strictBusy: "Le moteur de dictée est occupé, et la confidentialité stricte garde l’audio dans Atlas. Réessayez dans un instant.", sessionOpen: "Une dictée est déjà en cours dans un autre onglet ou une autre fenêtre.", budget: "Votre limite d’usage est atteinte : la dictée n’est pas disponible pour le moment.", provider: "Le service de transcription a atteint sa limite d’usage. Réessayez plus tard.", network: "La connexion à Atlas a été perdue. Vérifiez le réseau et réessayez.", idle: "Aucun son n’est venu du micro.", noComposer: "La zone de message n’est pas disponible ici : ouvrez une conversation, puis insérez le texte.", unavailable: "La dictée n’est pas disponible pour le moment." },
    de: { dictate: "Diktieren", stop: "Diktat stoppen", panel: "Diktat", starting: "Mikrofon wird gestartet…", listening: "Hört zu", finishing: "Wird abgeschlossen…", local: "Auf Atlas-Servern transkribiert", cloud: "Von einem externen Dienst transkribiert", fallback: "Atlas-Engine ausgelastet: von einem externen Dienst transkribiert", strict: "Strikter Datenschutz: Audio bleibt in Atlas", stopButton: "Stoppen", insert: "Text einfügen", close: "Schließen", autoInsert: "Bei jeder Pause einfügen", limit: "Maximale Diktatdauer erreicht.", permission: "Der Mikrofonzugriff wurde verweigert. Erlauben Sie ihn in den Website-Einstellungen des Browsers und versuchen Sie es erneut.", noMic: "Auf diesem Gerät wurde kein Mikrofon gefunden.", micBusy: "Das Mikrofon wird von einer anderen Anwendung verwendet.", insecure: "Dieser Browser kann hier nicht aufnehmen: Das Diktat braucht eine sichere Verbindung und einen aktuellen Browser.", busy: "Die Diktat-Engine ist ausgelastet. Versuchen Sie es gleich noch einmal.", strictBusy: "Die Diktat-Engine ist ausgelastet, und strikter Datenschutz hält Audio in Atlas. Versuchen Sie es gleich noch einmal.", sessionOpen: "In einem anderen Tab oder Fenster läuft bereits ein Diktat.", budget: "Ihr Nutzungslimit ist erreicht, das Diktat ist vorerst nicht verfügbar.", provider: "Der Transkriptionsdienst hat sein Nutzungslimit erreicht. Versuchen Sie es später erneut.", network: "Die Verbindung zu Atlas wurde unterbrochen. Prüfen Sie das Netzwerk und versuchen Sie es erneut.", idle: "Vom Mikrofon kam kein Ton.", noComposer: "Das Nachrichtenfeld ist hier nicht verfügbar: Öffnen Sie eine Unterhaltung und fügen Sie den Text dann ein.", unavailable: "Das Diktat ist gerade nicht verfügbar." },
    es: { dictate: "Dictar", stop: "Detener el dictado", panel: "Dictado", starting: "Iniciando el micrófono…", listening: "Escuchando", finishing: "Finalizando…", local: "Transcrito en los servidores de Atlas", cloud: "Transcrito por un servicio externo", fallback: "Motor de Atlas ocupado: transcrito por un servicio externo", strict: "Privacidad estricta: el audio se queda en Atlas", stopButton: "Detener", insert: "Insertar el texto", close: "Cerrar", autoInsert: "Insertar en cada pausa", limit: "Se alcanzó la duración máxima del dictado.", permission: "Se denegó el acceso al micrófono. Permítalo en los ajustes del sitio del navegador y vuelva a intentarlo.", noMic: "No se encontró ningún micrófono en este dispositivo.", micBusy: "Otra aplicación está usando el micrófono.", insecure: "Este navegador no puede grabar aquí: el dictado necesita una conexión segura y un navegador reciente.", busy: "El motor de dictado está ocupado. Vuelva a intentarlo en un momento.", strictBusy: "El motor de dictado está ocupado y la privacidad estricta mantiene el audio en Atlas. Vuelva a intentarlo en un momento.", sessionOpen: "Ya hay un dictado en curso en otra pestaña o ventana.", budget: "Se alcanzó su límite de uso: el dictado no está disponible por ahora.", provider: "El servicio de transcripción alcanzó su límite de uso. Vuelva a intentarlo más tarde.", network: "Se perdió la conexión con Atlas. Compruebe la red y vuelva a intentarlo.", idle: "No llegó ningún sonido del micrófono.", noComposer: "El cuadro de mensaje no está disponible aquí: abra una conversación y luego inserte el texto.", unavailable: "El dictado no está disponible en este momento." },
    it: { dictate: "Detta", stop: "Ferma la dettatura", panel: "Dettatura", starting: "Avvio del microfono…", listening: "In ascolto", finishing: "Completamento…", local: "Trascritto sui server di Atlas", cloud: "Trascritto da un servizio esterno", fallback: "Motore di Atlas occupato: trascritto da un servizio esterno", strict: "Privacy rigorosa: l’audio resta in Atlas", stopButton: "Ferma", insert: "Inserisci il testo", close: "Chiudi", autoInsert: "Inserisci a ogni pausa", limit: "Durata massima della dettatura raggiunta.", permission: "L’accesso al microfono è stato negato. Consentilo nelle impostazioni del sito del browser, poi riprova.", noMic: "Nessun microfono trovato su questo dispositivo.", micBusy: "Il microfono è usato da un’altra applicazione.", insecure: "Questo browser non può registrare qui: la dettatura richiede una connessione sicura e un browser recente.", busy: "Il motore di dettatura è occupato. Riprova tra un momento.", strictBusy: "Il motore di dettatura è occupato e la privacy rigorosa mantiene l’audio in Atlas. Riprova tra un momento.", sessionOpen: "Una dettatura è già in corso in un’altra scheda o finestra.", budget: "Il tuo limite di utilizzo è stato raggiunto: la dettatura non è disponibile per ora.", provider: "Il servizio di trascrizione ha raggiunto il suo limite di utilizzo. Riprova più tardi.", network: "La connessione ad Atlas si è interrotta. Controlla la rete e riprova.", idle: "Dal microfono non è arrivato alcun suono.", noComposer: "La casella del messaggio non è disponibile qui: apri una conversazione, poi inserisci il testo.", unavailable: "La dettatura non è disponibile in questo momento." },
    nl: { dictate: "Dicteren", stop: "Dicteren stoppen", panel: "Dicteren", starting: "Microfoon wordt gestart…", listening: "Luistert", finishing: "Wordt afgerond…", local: "Getranscribeerd op Atlas-servers", cloud: "Getranscribeerd door een externe dienst", fallback: "Atlas-engine bezet: getranscribeerd door een externe dienst", strict: "Strikte privacy: audio blijft in Atlas", stopButton: "Stoppen", insert: "Tekst invoegen", close: "Sluiten", autoInsert: "Invoegen bij elke pauze", limit: "Maximale dicteerduur bereikt.", permission: "Toegang tot de microfoon is geweigerd. Sta die toe in de site-instellingen van de browser en probeer het opnieuw.", noMic: "Er is geen microfoon gevonden op dit apparaat.", micBusy: "De microfoon wordt door een andere toepassing gebruikt.", insecure: "Deze browser kan hier niet opnemen: dicteren vereist een beveiligde verbinding en een recente browser.", busy: "De dicteerengine is bezet. Probeer het zo meteen opnieuw.", strictBusy: "De dicteerengine is bezet en strikte privacy houdt audio in Atlas. Probeer het zo meteen opnieuw.", sessionOpen: "Er loopt al een dictaat in een ander tabblad of venster.", budget: "Uw gebruikslimiet is bereikt: dicteren is voorlopig niet beschikbaar.", provider: "De transcriptiedienst heeft zijn gebruikslimiet bereikt. Probeer het later opnieuw.", network: "De verbinding met Atlas is verbroken. Controleer het netwerk en probeer het opnieuw.", idle: "Er kwam geen geluid van de microfoon.", noComposer: "Het berichtvak is hier niet beschikbaar: open een gesprek en voeg de tekst daarna in.", unavailable: "Dicteren is op dit moment niet beschikbaar." }
  };
  const locale = () => {
    let stored = "";
    try { stored = localStorage.getItem("locale") || ""; } catch { stored = ""; }
    return (stored || document.documentElement.lang || navigator.language || "en").toLowerCase().split("-")[0];
  };
  const language = () => (WORDS[locale()] ? locale() : "en");
  const t = key => WORDS[language()][key] || WORDS.en[key];
  const AUTO = "atlas-dictation-auto-insert";
  const autoInsert = () => { try { return localStorage.getItem(AUTO) === "1"; } catch { return false; } };
  const ERRORS = { session_open: "sessionOpen", busy: "busy", local_busy: "busy", strict_privacy_unavailable: "strictBusy", budget_limit: "budget", provider_limit: "provider", idle_timeout: "idle" };
  const svg = (paths, filled) => {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("fill", filled ? "currentColor" : "none"); icon.setAttribute("stroke", filled ? "none" : "currentColor"); icon.setAttribute("stroke-width", "1.5");
    for (const [tag, attributes] of paths) {
      const part = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [name, value] of Object.entries(attributes)) part.setAttribute(name, value);
      icon.append(part);
    }
    return icon;
  };
  const micIcon = () => svg([["rect", { x: "9", y: "2", width: "6", height: "12", rx: "3" }], ["path", { "stroke-linecap": "round", "stroke-linejoin": "round", d: "M5 10v1a7 7 0 0 0 14 0v-1M12 18v4m0 0H9m3 0h3" }]], false);
  const stopIcon = () => svg([["rect", { x: "7", y: "7", width: "10", height: "10", rx: "2" }]], true);
  const make = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text) node.textContent = text; return node; };

  let button = null, panel = null, ui = null, observed = null, resize = null, queued = false, lastPlace = "", config = null, nextConfig = 0;
  const fresh = () => ({ state: "idle", socket: null, audio: null, stream: null, finals: [], partial: "", closed: [], live: null, started: 0, ticker: 0, finishing: 0, engine: "", fallback: false, privacy: "standard", error: "", note: "", cursor: null, ready: false });
  let s = fresh();

  // --- text: stable (cleaned or final) in normal colour, the open utterance in grey ---
  const pieces = () => {
    const stable = []; let open = ""; let i = 0;
    while (i < s.finals.length) {
      const closed = s.closed.find(range => range.from === i && range.to > i);
      if (closed) { stable.push(closed.text); i = closed.to; continue; }
      const live = s.live;
      if (live && live.from === i && live.to === s.finals.length) {
        if (!live.partial) { stable.push(live.text); i = live.to; continue; }
        if (s.partial) { open = live.text; i = live.to; break; }
      }
      stable.push(s.finals[i] || ""); i += 1;
    }
    if (!open && s.partial) open = s.live && s.live.partial && s.live.from === s.finals.length ? s.live.text : s.partial;
    return { stable: stable.filter(Boolean).join(" "), open };
  };
  const finalText = () => { const { stable, open } = pieces(); return [stable, open].filter(Boolean).join(" ").replace(/\s+/g, " ").trim(); };

  // --- placement: over the native microphone, which keeps its layout box ---
  const composer = mic => mic.closest("#message-input-container") || mic.parentElement;
  const uncovered = (rect, mic) => {
    const stack = document.elementsFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const top = stack.find(node => node !== button && !button.contains(node) && !(panel && panel.contains(node)));
    return !top || top === mic || composer(mic).contains(top);
  };
  const place = () => {
    queued = false;
    const mic = document.getElementById("voice-input-button");
    let visible = false;
    if (mic) {
      if (mic !== observed && resize) { resize.disconnect(); resize.observe(mic); resize.observe(composer(mic)); observed = mic; }
      const rect = mic.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth) {
        visible = uncovered(rect, mic);
        const key = [rect.left, rect.top, rect.width, rect.height].map(Math.round).join(",");
        if (key !== lastPlace) {
          lastPlace = key;
          const [left, top, width, height] = key.split(",");
          button.style.left = left + "px"; button.style.top = top + "px"; button.style.width = width + "px"; button.style.height = height + "px";
        }
        if (panel && !panel.hidden) placePanel(composer(mic).getBoundingClientRect());
      }
    }
    // An open session keeps its button (the user must be able to stop) even if the composer moved away.
    const show = visible || s.state !== "idle";
    if (button.hidden === show) button.hidden = !show;
  };
  const placePanel = box => {
    const width = Math.max(240, Math.min(box.width, innerWidth - 16));
    const left = Math.max(8, Math.min(box.left, innerWidth - width - 8));
    const bottom = Math.max(8, innerHeight - box.top + 8);
    const style = `${Math.round(left)},${Math.round(width)},${Math.round(bottom)}`;
    if (panel.dataset.place === style) return;
    panel.dataset.place = style;
    panel.style.left = Math.round(left) + "px"; panel.style.width = Math.round(width) + "px"; panel.style.bottom = Math.round(bottom) + "px";
  };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(place); } };

  // --- panel ---
  const buildPanel = () => {
    panel = make("div", "atlas-dictation-panel"); panel.hidden = true;
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "false");
    const text = make("div", "atlas-dictation-text"); text.setAttribute("aria-live", "polite");
    const stable = make("span", "atlas-dictation-stable"), open = make("span", "atlas-dictation-open");
    text.append(stable, open);
    const message = make("p", "atlas-dictation-message"); message.setAttribute("role", "alert"); message.hidden = true;
    const bar = make("div", "atlas-dictation-bar");
    const dot = make("span", "atlas-dictation-dot"); dot.setAttribute("aria-hidden", "true");
    const time = make("span", "atlas-dictation-time", "0:00");
    const status = make("span", "atlas-dictation-status");
    const autoLabel = make("label", "atlas-dictation-auto"); const auto = document.createElement("input"); auto.type = "checkbox";
    const autoText = make("span"); autoLabel.append(auto, autoText);
    auto.addEventListener("change", () => { try { localStorage.setItem(AUTO, auto.checked ? "1" : "0"); } catch { /* this browser only */ } });
    const stop = make("button", "atlas-dictation-stop"); stop.type = "button";
    const insert = make("button", "atlas-dictation-insert"); insert.type = "button"; insert.hidden = true;
    const close = make("button", "atlas-dictation-close"); close.type = "button"; close.hidden = true;
    for (const control of [stop, insert, close]) control.addEventListener("mousedown", event => event.preventDefault());
    stop.addEventListener("click", () => finish());
    insert.addEventListener("click", () => deliver());
    close.addEventListener("click", () => reset());
    bar.append(dot, time, status, autoLabel, stop, insert, close);
    panel.append(text, message, bar);
    document.body.append(panel);
    ui = { text, stable, open, message, dot, time, status, auto, autoText, stop, insert, close };
  };
  const clock = () => {
    if (!ui) return;
    const seconds = s.started ? Math.floor((performance.now() - s.started) / 1000) : 0;
    const value = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    if (ui.time.textContent !== value) ui.time.textContent = value;
  };
  const render = () => {
    const active = s.state !== "idle";
    button.setAttribute("aria-pressed", String(s.state === "starting" || s.state === "live"));
    button.dataset.state = s.state;
    const label = s.state === "starting" || s.state === "live" ? t("stop") : t("dictate");
    if (button.getAttribute("aria-label") !== label) { button.setAttribute("aria-label", label); button.title = label; button.replaceChildren(s.state === "starting" || s.state === "live" ? stopIcon() : micIcon()); }
    if (!panel) { if (!active) return; buildPanel(); }
    panel.hidden = !active;
    if (!active) return;
    panel.setAttribute("aria-label", t("panel")); panel.dataset.state = s.state;
    const { stable, open } = pieces();
    if (ui.stable.textContent !== stable) ui.stable.textContent = stable;
    const shown = open ? (stable ? " " : "") + open : "";
    if (ui.open.textContent !== shown) ui.open.textContent = shown;
    ui.text.hidden = !stable && !open;
    const message = s.error || s.note;
    ui.message.textContent = message; ui.message.hidden = !message;
    ui.message.dataset.kind = s.error ? "error" : "note";
    ui.status.textContent = s.state === "starting" ? t("starting") : s.state === "finishing" ? t("finishing") : s.state === "live" ? [t("listening"), s.privacy === "strict" ? t("strict") : s.fallback ? t("fallback") : t(s.engine === "cloud" ? "cloud" : "local")].join(" · ") : "";
    ui.autoText.textContent = t("autoInsert"); ui.auto.checked = autoInsert();
    // A failed session shows its sentence and actions only.
    for (const node of [ui.dot, ui.time, ui.auto.parentElement]) node.hidden = s.state === "error";
    ui.stop.textContent = t("stopButton"); ui.stop.hidden = !(s.state === "starting" || s.state === "live");
    ui.insert.textContent = t("insert"); ui.insert.hidden = !(s.state === "error" && finalText());
    ui.close.textContent = t("close"); ui.close.hidden = s.state !== "error";
    clock(); schedule();
  };

  // --- the single write into the composer, through its own editing path ---
  const write = text => {
    const editor = document.getElementById("chat-input");
    if (!editor) return false;
    window.dispatchEvent(new CustomEvent("atlas:dictation-insert"));
    const textarea = editor instanceof HTMLTextAreaElement;
    // The cursor recorded when dictation started, if the composer still holds it; else the end.
    const cursor = s.cursor;
    editor.focus({ preventScroll: true });
    let before = "";
    if (textarea) {
      const end = editor.value.length;
      const at = cursor && typeof cursor.start === "number" && cursor.start <= end ? cursor : { start: end, end };
      editor.setSelectionRange(at.start, Math.min(at.end, end));
      before = editor.value.slice(0, editor.selectionStart);
    } else {
      const selection = getSelection();
      const kept = cursor && cursor.range && editor.contains(cursor.range.startContainer) && editor.contains(cursor.range.endContainer);
      const target = kept ? cursor.range : document.createRange();
      if (!kept) { target.selectNodeContents(editor); target.collapse(false); }
      selection.removeAllRanges(); selection.addRange(target);
      const range = selection.getRangeAt(0).cloneRange(); range.setStart(editor, 0);
      before = range.toString();
    }
    const value = (before && !/\s$/.test(before) ? " " : "") + text;
    const length = textarea ? editor.value.length : editor.textContent.length;
    // Open WebUI's state follows the editor's own input handling (Tiptap or the textarea binding).
    let done = document.execCommand("insertText", false, value);
    const grew = () => (textarea ? editor.value.length : editor.textContent.length) > length;
    if (!done || !grew()) {
      if (textarea) {
        editor.setRangeText(value, editor.selectionStart, editor.selectionEnd, "end");
        editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      } else {
        const data = new DataTransfer(); data.setData("text/plain", value);
        editor.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
      }
      done = grew();
    }
    return done;
  };

  // --- session ---
  const release = () => {
    if (s.stream) s.stream.getTracks().forEach(track => track.stop());
    if (s.audio) s.audio.close().catch(() => {});
    s.stream = null; s.audio = null;
  };
  const closeSocket = () => {
    const socket = s.socket; s.socket = null;
    if (socket) { socket.onmessage = socket.onclose = socket.onerror = null; try { socket.close(); } catch { /* already closed */ } }
  };
  const reset = () => {
    release(); closeSocket(); clearInterval(s.ticker); clearTimeout(s.finishing);
    s = fresh(); render();
  };
  const fail = key => {
    if (s.state === "idle") return;
    release(); closeSocket(); clearInterval(s.ticker); clearTimeout(s.finishing);
    s.state = "error"; s.error = t(key); render();
  };
  // The final text is written once; without a composer it stays in the panel to insert later.
  const deliver = () => {
    const value = finalText();
    release(); closeSocket(); clearInterval(s.ticker); clearTimeout(s.finishing);
    if (!value || write(value)) { s = fresh(); render(); return; }
    s.state = "error"; s.error = t("noComposer"); render();
  };
  const complete = () => {
    if (s.state === "idle" || s.state === "error") return;
    deliver();
  };
  const finish = () => {
    if (s.state === "starting" && (!s.socket || s.socket.readyState !== WebSocket.OPEN)) { reset(); return; }
    if (s.state !== "starting" && s.state !== "live") return;
    s.state = "finishing"; render();
    try { s.socket.send(JSON.stringify({ type: "stop" })); } catch { /* the close handler completes */ }
    release();
    // The server sends the last final and clean-up, then done; never wait forever for it.
    s.finishing = setTimeout(complete, 12000);
  };
  const onMessage = event => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === "ready") {
      s.ready = true; s.engine = data.engine; s.fallback = Boolean(data.fallback); s.privacy = data.privacy === "strict" ? "strict" : "standard";
      if (s.state === "starting") s.state = "live";
    } else if (data.type === "partial") s.partial = data.text || "";
    else if (data.type === "final") {
      s.finals[data.index] = data.text || ""; s.partial = "";
      if (s.live && !s.live.final) s.live = null;
    } else if (data.type === "cleaned") {
      if (data.final) { s.closed.push(data); s.live = null; } else s.live = data;
    } else if (data.type === "turn" && data.event === "end") {
      if (s.state === "live" && autoInsert() && finalText()) { setTimeout(finish, 0); }
    } else if (data.type === "limit") s.note = t("limit");
    else if (data.type === "error") { fail((data.code === "local_busy" && data.privacy === "strict") ? "strictBusy" : ERRORS[data.code] || "unavailable"); return; }
    else if (data.type === "done") { complete(); return; }
    render();
  };
  const microphoneError = error => {
    const name = error && error.name;
    if (name === "NotAllowedError" || name === "SecurityError") return "permission";
    if (name === "NotFoundError" || name === "OverconstrainedError") return "noMic";
    if (name === "NotReadableError" || name === "AbortError") return "micBusy";
    if (name === "NotSupportedError") return "insecure";
    return "unavailable";
  };
  const scope = () => {
    const query = new URLSearchParams(location.search);
    return query.get("org") && query.get("project") ? { org: query.get("org"), project: query.get("project") } : null;
  };
  const begin = async () => {
    const editor = document.getElementById("chat-input");
    s = fresh();
    // Where the user was typing: the text goes there at the end (the button keeps the focus).
    s.cursor = null;
    if (editor && editor.contains(document.activeElement)) {
      if (editor instanceof HTMLTextAreaElement) s.cursor = { start: editor.selectionStart, end: editor.selectionEnd };
      else { const selection = getSelection(); if (selection.rangeCount && editor.contains(selection.anchorNode)) s.cursor = { range: selection.getRangeAt(0).cloneRange() }; }
    }
    s.state = "starting"; s.started = performance.now(); render();
    s.ticker = setInterval(clock, 500);
    const current = s;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioWorkletNode || !window.WebSocket) { fail("insecure"); return; }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (error) { if (s === current) fail(microphoneError(error)); return; }
    if (s !== current || s.state !== "starting") { stream.getTracks().forEach(track => track.stop()); return; }
    s.stream = stream;
    let capture;
    try {
      try { s.audio = new AudioContext({ sampleRate: 16000 }); } catch { s.audio = new AudioContext(); }
      await s.audio.audioWorklet.addModule("/api/atlas/dictation/dictation-worklet.js");
      if (s !== current) return;
      capture = new AudioWorkletNode(s.audio, "dictation-capture");
      s.audio.createMediaStreamSource(stream).connect(capture);
    } catch { if (s === current) fail("unavailable"); return; }
    const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/atlas/dictation/stream`);
    socket.binaryType = "arraybuffer"; s.socket = socket;
    socket.onmessage = onMessage;
    socket.onclose = () => { if (s !== current) return; if (s.state === "finishing") complete(); else fail(s.ready ? "network" : "unavailable"); };
    socket.onopen = () => {
      if (s !== current) return;
      // The interface language, whatever it is: the server maps it to each engine (pinned,
      // detected, or another engine for a language the local one lacks).
      const code = /^[a-z]{2}$/.test(locale()) ? locale() : null;
      socket.send(JSON.stringify({ type: "start", engine: "auto", language: code, scope: scope(), cleanup: true }));
      // Audio is sent from the start: the server reads it once its engine is ready.
      capture.port.onmessage = ({ data }) => { if (s === current && (s.state === "starting" || s.state === "live") && socket.readyState === WebSocket.OPEN) socket.send(data.pcm); };
    };
  };
  const toggle = () => {
    if (s.state === "starting" || s.state === "live") finish();
    else if (s.state === "error") { reset(); begin().catch(() => fail("unavailable")); }
    else if (s.state === "idle") begin().catch(() => fail("unavailable"));
  };

  // --- mounting: only once the installation enables it and the user is signed in ---
  const mount = () => {
    button = make("button", "atlas-dictation-button"); button.type = "button"; button.hidden = true;
    // The editor keeps its focus and cursor: the text goes where the user was typing.
    button.addEventListener("mousedown", event => event.preventDefault());
    button.addEventListener("click", toggle);
    document.body.append(button);
    document.documentElement.dataset.atlasDictation = "on";
    resize = new ResizeObserver(schedule);
    addEventListener("resize", schedule, { passive: true });
    addEventListener("scroll", schedule, { passive: true, capture: true });
    document.addEventListener("transitionend", schedule, { passive: true, capture: true });
    addEventListener("keydown", event => {
      if (event.key !== "Escape" || s.state === "idle") return;
      event.preventDefault(); event.stopPropagation();
      if (s.state === "error") reset(); else finish();
    }, true);
    render();
  };
  const probe = () => {
    // A cheap check: one element lookup, then one rectangle once mounted.
    if (button) { schedule(); return; }
    if (config === false || performance.now() < nextConfig || !document.getElementById("voice-input-button")) return;
    nextConfig = performance.now() + 15000;
    fetch("/api/atlas/dictation/config", { credentials: "same-origin", cache: "no-store" })
      .then(response => (response.ok ? response.json() : null))
      .then(value => {
        if (!value) return; // signed out or unavailable: tried again later
        config = Boolean(value.enabled);
        if (config && !button) mount();
      })
      .catch(() => {});
  };
  const start = () => { setInterval(probe, 400); probe(); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true }); else start();
})();
