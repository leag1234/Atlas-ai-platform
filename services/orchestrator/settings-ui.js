/* Translation overrides for the Open WebUI i18n store (Open WebUI 0.11.3 and 0.13.3).
 * Keep the native store so search labels, tooltips, dialogs and language changes agree.
 * The store is found by shape in the modules the app loads, never by a build hash: each
 * Open WebUI release renames its chunks (0.11.3 shipped it in DKxluP3e.js).
 */

// Filter known unused stable features, without replacing the menu or its
// children: independent entries (including API keys) retain native behavior.
if (document.querySelector('script[src="/static/atlas/stable-auth.js"]')) {
  const style = document.createElement('style');
  style.id = 'atlas-stable-menu-style';
  style.textContent = `
    .app-dropdown-menu .user-menu-status,
    .app-dropdown-menu > div:has(> a[href="/calendar"]),
    .app-dropdown-menu > div:has(> a[href="/automations"]) { display:none!important; }
  `;
  document.head.append(style);
}

let i18n;

// Native language names are English literals rather than translation keys.
const localizeLanguages = () => {
  const select = document.querySelector('#tab-general select option[value="fr-FR"]')?.parentElement;
  if (!select) return;
  const language = localStorage.getItem('locale') || navigator.language;
  const names = new Intl.DisplayNames([language], { type: 'language' });
  for (const option of select.options) {
    if (!/^[a-z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$/.test(option.value) || option.value === 'dg-DG') continue;
    const special = { 'bo-TB': 'Tibetan', 'ie-GA': 'Irish', 'kab-DZ': 'Kabyle' };
    const label = special[option.value] ? i18n?.t(special[option.value]) : names.of(option.value);
    if (label && option.textContent !== label) option.textContent = label;
  }
};
new MutationObserver(localizeLanguages).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

// The native i18n store: a Svelte writable store whose value is the i18next instance.
const isLocaleStore = value => {
  if (!value || typeof value !== 'object' || typeof value.subscribe !== 'function' || typeof value.set !== 'function') return false;
  let current;
  const stop = value.subscribe(item => { current = item; });
  if (typeof stop === 'function') stop();
  return Boolean(current && typeof current.addResourceBundle === 'function' && typeof current.hasResourceBundle === 'function' && typeof current.t === 'function');
};
// Walk the static imports of the app entry and of the root layout (node 0), which owns the
// store: only modules the page loads anyway are imported, and no build hash is assumed.
const findLocaleStore = async () => {
  const entry = [...document.scripts].map(script => script.textContent.match(/\/_app\/immutable\/entry\/app\.[\w-]+\.js/)?.[0]).find(Boolean);
  if (!entry) throw new Error('Open WebUI app entry not found');
  const queue = [new URL(entry, location.href).href], visited = new Set(queue);
  const enqueue = (path, base) => { const url = new URL(path, base).href; if (!visited.has(url)) { visited.add(url); queue.push(url); } };
  while (queue.length && visited.size < 2000) {
    const url = queue.shift();
    let module;
    try { module = await import(url); } catch { continue; }
    let store;
    try { store = Object.values(module).find(isLocaleStore); } catch { store = null; }
    if (store) return store;
    const source = await (await fetch(url)).text();
    if (/\/entry\/app\./.test(url)) {
      const layout = source.match(/import\(["']([^"']*\/nodes\/0\.[\w-]+\.js)["']\)/)?.[1];
      if (layout) enqueue(layout, url);
    }
    for (const match of source.matchAll(/(?:\bfrom|\bimport)\s*["'](\.{1,2}\/[^"']+\.js)["']/g)) enqueue(match[1], url);
  }
  throw new Error('Open WebUI i18n store not found');
};

const response = await fetch('/static/atlas/settings-locales.json');
if (!response.ok) throw new Error('Atlas settings translations unavailable');
const overrides = await response.json();
const nativeLocale = await findLocaleStore();
let applying = false;
const apply = () => {
  if (applying || !i18n?.isInitialized) return;
  applying = true;
  try {
    for (const [language, strings] of Object.entries(overrides)) {
      // Do not create a namespace before the upstream bundle has been loaded.
      if (i18n.hasResourceBundle(language, 'translation')) {
        i18n.addResourceBundle(language, 'translation', strings, true, true);
      }
    }
    nativeLocale.set(i18n);
  } finally {
    applying = false;
  }
};
nativeLocale.subscribe(value => {
  if (value === i18n) return;
  i18n = value;
  i18n.on('initialized', apply);
  i18n.on('loaded', apply);
  i18n.on('languageChanged', apply);
  // Open WebUI 0.13 replaces whole bundles (admin translation overrides): apply again after any
  // bundle added by someone else, once that change has finished.
  i18n.on('added', () => { if (!applying) queueMicrotask(apply); });
  apply();
});
localizeLanguages();
