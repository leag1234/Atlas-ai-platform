/* Translation overrides for the pinned Open WebUI 0.11.3 i18n store.
 * Source: shipped DKxluP3e.js and SettingsModal.svelte source map. Keep the
 * native store so search labels, tooltips, dialogs and language changes agree.
 * The stable browser contract checks this import against the shipped image.
 */
import { i as nativeLocale } from '/_app/immutable/chunks/DKxluP3e.js';

const response = await fetch('/static/atlas/settings-locales.json');
if (!response.ok) throw new Error('Atlas settings translations unavailable');
const overrides = await response.json();
let i18n;
const apply = () => {
  if (!i18n?.isInitialized) return;
  for (const [language, strings] of Object.entries(overrides)) {
    // Do not create a namespace before the upstream bundle has been loaded.
    if (i18n.hasResourceBundle(language, 'translation')) {
      i18n.addResourceBundle(language, 'translation', strings, true, true);
    }
  }
  nativeLocale.set(i18n);
};
nativeLocale.subscribe(value => {
  if (value === i18n) return;
  i18n = value;
  i18n.on('initialized', apply);
  i18n.on('loaded', apply);
  i18n.on('languageChanged', apply);
  apply();
});

// Native language names are English literals rather than translation keys.
const localizeLanguages = () => {
  const select = document.querySelector('#tab-general select option[value="fr-FR"]')?.parentElement;
  if (!select) return;
  const language = localStorage.getItem('locale') || navigator.language;
  const names = new Intl.DisplayNames([language], { type: 'language' });
  for (const option of select.options) {
    if (!/^[a-z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$/.test(option.value) || option.value === 'dg-DG') continue;
    const special = { 'bo-TB': 'Tibetan', 'ie-GA': 'Irish', 'kab-DZ': 'Kabyle' };
    const label = special[option.value] ? i18n.t(special[option.value]) : names.of(option.value);
    if (label && option.textContent !== label) option.textContent = label;
  }
};
new MutationObserver(localizeLanguages).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
localizeLanguages();

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
