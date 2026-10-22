(() => {
  "use strict";
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
    const stored = localStorage.getItem("atlas-ui-language");
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
    const isLong = content.scrollHeight > lineHeight(content) * 6.1;
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
  const mount = () => {
    if (document.getElementById("atlas-ui-selector")) return;
    const words = translations[locale()];
    const option = (group, value) => `<button type="button" data-atlas-option="${group}" data-value="${value}" aria-label="${group === "size" ? `${value} px` : words[value]}" title="${group === "size" ? `${value} px` : words[value]}" aria-pressed="${saved[group] === value}" class="atlas-option">${group === "size" ? `${value} px` : words[value]}</button>`;
    const root = document.createElement("div"); root.id = "atlas-ui-selector";
    root.innerHTML = `<button id="atlas-ui-toggle" type="button" aria-label="${words.title}" aria-expanded="false">Aa</button><section id="atlas-ui-popover" aria-label="${words.title}" hidden><h2>${words.title}</h2>${Object.keys(choices).map(group => `<fieldset><legend>${words[group]}</legend><div class="atlas-options" data-atlas-group="${group}">${choices[group].map(value => option(group, value)).join("")}</div></fieldset>`).join("")}</section>`;
    document.body.append(root);
    const toggle = root.querySelector("#atlas-ui-toggle"), popover = root.querySelector("#atlas-ui-popover");
    const close = () => { popover.hidden = true; toggle.setAttribute("aria-expanded", "false"); toggle.focus(); };
    toggle.addEventListener("click", () => { popover.hidden = !popover.hidden; toggle.setAttribute("aria-expanded", String(!popover.hidden)); if (!popover.hidden) popover.querySelector("button").focus(); });
    root.addEventListener("click", event => { const button = event.target.closest("[data-atlas-option]"); if (!button) return; const group = button.dataset.atlasOption; persist({ [group]: button.dataset.value }); root.querySelectorAll(`[data-atlas-option="${group}"]`).forEach(item => item.setAttribute("aria-pressed", String(item === button))); if (group === "mode") { localStorage.setItem("theme", button.dataset.value); location.reload(); } });
    document.addEventListener("keydown", event => { if (event.key === "Escape" && !popover.hidden) { event.preventDefault(); event.stopImmediatePropagation(); close(); } }, true);
  };
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
    const text = index => (words[(localStorage.getItem("atlas-ui-language") || navigator.language || "en").split("-")[0]] || words.en)[index];
    const element = (tag, cls, value) => { const node = document.createElement(tag); if (cls) node.className = cls; if (value) node.textContent = value; return node; };
    const button = (label, action) => { const node = element("button", "atlas-side-button", label); node.type = "button"; node.addEventListener("click", action); return node; };
    const safeURL = (value, sameOrigin = false) => {
      const url = new URL(value, location.href);
      if (!/^https?:$/.test(url.protocol) || url.username || url.password || (sameOrigin && url.origin !== location.origin)) throw new Error("Unsupported source URL");
      return url.href;
    };
    const download = (value, filename, type) => {
      const href = URL.createObjectURL(new Blob([value], { type })), link = element("a");
      link.href = href; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(href), 1000);
    };
    const app = [...document.body.children].find(node => node.tagName === "DIV" && node.style.display === "contents");
    if (!app) return;
    app.classList.add("atlas-app-root");
    const pane = element("aside", "atlas-side-pane"); pane.id = "atlas-side-view"; pane.hidden = true; pane.setAttribute("aria-label", "Side view");
    const grip = element("div", "atlas-side-grip"), header = element("header", "atlas-side-header"), title = element("strong", "atlas-side-title", "Side view"), slotsHost = element("div", "atlas-side-slots");
    const slots = new Map(); let active = "top", priorFocus = null;
    const stored = Number(localStorage.getItem("atlas-side-width"));
    let desktopWidth = stored || innerWidth * .45;
    // The mobile overlay's measured width must never replace the user's desktop preference.
    const setWidth = width => { desktopWidth = Math.max(320, width); pane.style.width = `${Math.min(innerWidth, desktopWidth)}px`; };
    setWidth(stored || innerWidth * .45);
    const browser = button(text(1), () => { const src = slots.get(active)?.descriptor?.src; if (src) window.open(safeURL(src), "_blank", "noopener,noreferrer"); });
    const saving = button("Download", () => slots.get(active)?.download?.());
    const refresh = () => { const slot = slots.get(active); title.textContent = slot?.descriptor?.title || "Side view"; browser.hidden = !slot?.descriptor?.src; saving.hidden = !slot?.download; header.querySelectorAll("[data-slot-select]").forEach(node => node.setAttribute("aria-pressed", String(node.dataset.slotSelect === active))); };
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
      node.tabIndex = 0; node.setAttribute("role", "separator"); node.setAttribute("aria-orientation", vertical ? "vertical" : "horizontal"); node.setAttribute("aria-label", vertical ? "Resize side view" : "Resize split");
      node.addEventListener("pointerdown", event => { node.setPointerCapture(event.pointerId); document.body.classList.add("atlas-side-dragging"); });
      node.addEventListener("pointermove", event => { if (node.hasPointerCapture(event.pointerId)) move(vertical ? event.clientX : event.clientY); });
      const finish = () => { document.body.classList.remove("atlas-side-dragging"); localStorage.setItem("atlas-side-width", String(pane.getBoundingClientRect().width)); };
      node.addEventListener("pointerup", finish); node.addEventListener("lostpointercapture", finish);
      node.addEventListener("keydown", event => { if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return; event.preventDefault(); const rect = node.getBoundingClientRect(); move((vertical ? rect.x : rect.y) + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -24 : 24)); finish(); });
    };
    const ensure = name => {
      if (slots.has(name)) return slots.get(name);
      const node = element("section", "atlas-side-slot"), toolbar = element("div", "atlas-side-toolbar"), caption = element("span", "atlas-side-caption", name === "top" ? "Top view" : "Bottom view"), content = element("div", "atlas-side-content");
      node.dataset.slot = name; toolbar.append(caption, button("Close slot", () => remove(name))); node.append(toolbar, content);
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
    const frame = slot => { const node = element("iframe", "atlas-side-frame"); node.title = slot.descriptor.title || "Side view"; node.setAttribute("sandbox", "allow-scripts"); node.referrerPolicy = "no-referrer"; slot.content.append(node); return node; };
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
        view.append(element("p", "atlas-side-diff-key", "− Before / + After · unchanged lines provide context"));
        for (const section of source.split(/(?=^diff --git )/m)) {
          if (!section) continue;
          const file = element("section", "atlas-diff-file"), pre = element("pre");
          const heading = section.match(/^\+\+\+ (?:b\/)?(.+)$/m)?.[1] || descriptor.title || "Changes";
          file.append(element("h3", "", heading));
          for (const line of section.split("\n")) pre.append(element("span", line.startsWith("+") ? "atlas-diff-add" : line.startsWith("-") ? "atlas-diff-remove" : "", line + "\n"));
          file.append(pre); view.append(file);
        }
        if (!source.trim()) view.append(element("p", "", "No changes in this diff."));
        slot.content.append(view); slot.download = () => download(source, "changes.patch", "text/plain");
      } },
      html: { async open(slot, descriptor) {
        const source = descriptor.content ?? (descriptor.src ? await fetchText(descriptor.src, slot) : "");
        if (slot.disposed) return; frame(slot).srcdoc = source;
        slot.download = () => download(source, "preview.html", "text/html");
      } },
      url: { async open(slot, descriptor) {
        const url = safeURL(descriptor.src), node = frame(slot), notice = element("p", "atlas-side-fallback", text(2)); notice.hidden = true;
        // A cross-origin load event also fires for CSP/XFO error pages. Never call it proof.
        slot.content.prepend(notice);
        const timer = setTimeout(() => { notice.hidden = false; }, 4000);
        node.addEventListener("error", () => { notice.hidden = false; });
        node.src = url; slot.cleanup.push(() => clearTimeout(timer));
      } },
      markdown: { async open(slot, descriptor) {
        const { marked } = await import("/static/atlas/vendor/marked/marked.esm.js");
        const source = descriptor.content ?? (descriptor.src ? await fetchText(descriptor.src, slot) : "");
        if (slot.disposed) return;
        const editor = element("textarea", "atlas-side-editor"), preview = element("div", "atlas-side-preview"), layout = element("div", "atlas-side-markdown"), status = element("span", "atlas-side-save-status");
        editor.setAttribute("aria-label", "Markdown source"); editor.spellcheck = false; editor.value = source; status.setAttribute("role", "status");
        const render = () => { const renderer = new marked.Renderer(); renderer.html = () => ""; renderer.image = () => ""; preview.replaceChildren(sanitize(marked.parse(editor.value, { renderer, async: false }))); };
        editor.addEventListener("input", render); render(); layout.append(editor, preview); slot.content.append(layout);
        for (const mode of ["Edit", "Preview", "Both"]) slot.toolbar.append(button(mode, () => { editor.hidden = mode === "Preview"; preview.hidden = mode === "Edit"; }));
        slot.download = () => download(editor.value, "document.md", "text/markdown;charset=utf-8");
        slot.toolbar.append(button("Download .md", slot.download));
        if (descriptor.saveTarget) {
          const target = safeURL(descriptor.saveTarget, true);
          if (!/^\/api\/v1\/files\/[a-f0-9-]+\/data\/content\/update$/i.test(new URL(target).pathname)) throw new Error("Unsupported save target");
          slot.toolbar.append(button("Save", async () => {
            status.textContent = "Saving…";
            try { const response = await fetch(target, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: editor.value }) }); const result = await response.json(); if (!response.ok || result.content !== editor.value) throw new Error("Content was not saved"); status.textContent = "Saved extracted text; original download unchanged."; }
            catch { status.textContent = "Save failed. Download .md to keep your edits."; }
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
        input.type = "number"; input.min = "1"; input.max = String(pdf.numPages); input.value = "1"; input.setAttribute("aria-label", "PDF page"); count.textContent = `/ ${pdf.numPages}`;
        let scale = 1, epoch = 0;
        const canvases = Array.from({ length: pdf.numPages }, (_, index) => { const canvas = element("canvas"); canvas.setAttribute("aria-label", `Page ${index + 1}`); canvas.dataset.page = String(index + 1); pages.append(canvas); return canvas; });
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
        const rerender = () => render().catch(() => { if (!slot.disposed) count.textContent = "PDF rendering failed"; });
        slot.toolbar.append(button("Previous page", () => navigate(-1)), input, count, button("Next page", () => navigate(1)), button("−", () => { scale = Math.max(.25, scale / 1.25); rerender(); }), button("+", () => { scale = Math.min(4, scale * 1.25); rerender(); }), button("Fit width", () => { scale = 1; rerender(); }));
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
      catch (error) { if (!slot.disposed) slot.content.append(element("p", "atlas-side-error", `Unable to open view: ${error.message}`)); }
      if (!slot.disposed) refresh();
    };
    header.append(title);
    for (const name of ["top", "bottom"]) { const control = button(name === "top" ? "Top" : "Bottom", () => { ensure(name); active = name; refresh(); }); control.dataset.slotSelect = name; header.append(control); }
    header.append(button("Split", () => { ensure("top"); ensure("bottom"); }), browser, saving, button("Fullscreen", async () => { if (document.fullscreenElement === pane) await document.exitFullscreen(); else await pane.requestFullscreen(); }), button("Close side view", close));
    pane.append(grip, header, slotsHost); document.body.append(pane);
    drag(grip, true, x => setWidth(innerWidth - x)); window.addEventListener("resize", () => setWidth(desktopWidth));
    document.addEventListener("keydown", event => { if (event.key === "Escape" && !pane.hidden) { close(); event.preventDefault(); } });
    window.atlasSideView = { open, close };
    const seen = new WeakSet(), opened = new Set();
    const chip = (node, descriptor) => { const control = button(text(0), () => open(typeof descriptor === "function" ? descriptor() : descriptor)); control.classList.add("atlas-side-chip"); node.after(control); };
    const scan = () => {
      // This pinned renderer escapes raw spans as text; hide only our exact wire marker.
      document.querySelectorAll('.chat-assistant .markdown-prose p').forEach(node => { if (/^<span class="atlas-view" data-atlas-view="(?:pdf|markdown|html|url)" [^<>]*hidden><\/span>$/.test(node.textContent.trim())) node.hidden = true; });
      document.querySelectorAll('.chat-assistant .atlas-view[data-atlas-view]').forEach(node => {
        if (seen.has(node)) return; seen.add(node);
        const descriptor = { kind: node.dataset.atlasView, src: node.dataset.src, title: node.dataset.title, slot: node.dataset.slot };
        const message = node.closest('[id^="message-"]') || node.closest(".chat-assistant");
        const identity = `${location.pathname}:${message?.id || ""}:${JSON.stringify(descriptor)}`;
        chip(node, descriptor); if (!opened.has(identity)) { opened.add(identity); open(descriptor).catch(() => {}); }
      });
      document.querySelectorAll('.chat-assistant .markdown-prose a[href], .user-message .markdown-prose a[href]').forEach(node => {
        if (seen.has(node)) return;
        const url = new URL(node.href, location.href), transport = new URLSearchParams(url.hash.slice(1));
        if (Object.hasOwn(views, transport.get("atlas-view"))) {
          seen.add(node);
          const descriptor = { kind: transport.get("atlas-view"), src: url.origin + url.pathname + url.search, title: transport.get("title") || node.textContent, slot: transport.get("slot") || "top" };
          // Normalize same-origin URLs to match the optional span descriptor.
          if (url.origin === location.origin) descriptor.src = url.pathname + url.search;
          const message = node.closest('[id^="message-"]') || node.closest(".chat-assistant");
          const identity = `${location.pathname}:${message?.id || ""}:${JSON.stringify(descriptor)}`;
          const placeholder = element("span", "atlas-view"); placeholder.hidden = true;
          placeholder.dataset.atlasView = descriptor.kind; placeholder.dataset.src = descriptor.src; placeholder.dataset.title = descriptor.title; placeholder.dataset.slot = descriptor.slot;
          seen.add(placeholder); node.before(placeholder);
          node.classList.add("atlas-side-chip"); node.textContent = text(0);
          node.addEventListener("click", event => { event.preventDefault(); open(descriptor).catch(() => {}); });
          if (!opened.has(identity)) { opened.add(identity); open(descriptor).catch(() => {}); }
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
