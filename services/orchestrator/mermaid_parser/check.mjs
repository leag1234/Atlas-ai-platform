// Same parser family as Open WebUI v0.11.3; no scripts or network in the DOM.
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
const dom = new JSDOM('');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import('mermaid');
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
// Parser errors may contain user text. Only booleans cross this boundary.
console.error = () => {};
console.warn = () => {};
console.log = () => {};
const sources = JSON.parse(readFileSync(0, 'utf8'));
const results = [];
for (const source of sources) {
  try {
    results.push(Boolean(await mermaid.parse(source, { suppressErrors: true })));
  } catch {
    results.push(false);
  }
}
process.stdout.write(JSON.stringify(results));
