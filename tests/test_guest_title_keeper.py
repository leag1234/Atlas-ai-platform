"""The guest chat script saves a generated title again when Open WebUI lost it
(staging 2026-11-12: SQLite lost-update race, about one chat in twelve)."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path("services/orchestrator/guest-chat.js").read_text()

HARNESS = r"""
const calls = [];
let storedTitle = process.argv[process.argv.length - 1];
const listeners = {};
global.localStorage = { getItem: key => (key === 'token' ? 'fixture' : null) };
global.navigator = { language: 'en' };
global.location = { href: 'https://atlas.test/c/chat-1', origin: 'https://atlas.test', pathname: '/c/chat-1' };
global.MutationObserver = class { observe() {} };
const element = { dataset: {}, setAttribute() {}, getAttribute() { return null; }, querySelector() { return null; },
  querySelectorAll() { return []; }, addEventListener() {}, appendChild() {}, classList: { toggle() {}, add() {} } };
global.document = {
  documentElement: element, body: element, readyState: 'loading',
  querySelector: selector => (selector.includes('stable-auth') ? {} : null),
  querySelectorAll: () => [], getElementById: () => null, addEventListener() {},
  createElement: () => ({ ...element, style: {} }),
};
const realTimeout = global.setTimeout;
global.setTimeout = fn => { fn(); return 0; };
global.setInterval = () => 0;
global.window = global;
global.addEventListener = () => {};
global.requestAnimationFrame = () => 0;
global.fetch = async (input, options = {}) => {
  calls.push({ url: String(input), method: options.method || 'GET', body: options.body || null });
  if (String(input).startsWith('/api/v1/chats/') && (options.method || 'GET') === 'GET') {
    return { ok: true, json: async () => ({ title: storedTitle }) };
  }
  return { ok: true, json: async () => ({}) };
};
class FakeSocket {
  constructor() {}
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); }
}
global.WebSocket = FakeSocket;
eval(require('fs').readFileSync(0, 'utf8'));
const socket = new window.WebSocket('wss://atlas.test/ws/socket.io/?EIO=4');
const send = payload => listeners.message.forEach(fn => fn({ data: '42' + JSON.stringify(['events', payload]) }));
send({ chat_id: 'chat-1', message_id: 'm1', data: { type: 'chat:title', data: 'Electric cars in Europe' } });
send({ chat_id: 'chat-1', message_id: 'm1', data: { type: 'chat:completion', data: { done: true } } });
realTimeout(() => console.log(JSON.stringify(calls)), 100);
"""


@unittest.skipUnless(shutil.which("node"), "node is required")
class TitleKeeperTests(unittest.TestCase):
    def run_script(self, stored: str) -> list[dict[str, object]]:
        result = subprocess.run(
            ["node", "-e", HARNESS, "--", stored],
            input=SCRIPT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        calls: list[dict[str, object]] = json.loads(
            result.stdout.strip().splitlines()[-1]
        )
        return calls

    def test_a_lost_title_is_saved_again(self) -> None:
        calls = self.run_script("New Chat")
        saves = [c for c in calls if c["method"] == "POST"]
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["url"], "/api/v1/chats/chat-1")
        self.assertEqual(
            json.loads(str(saves[0]["body"])),
            {"chat": {"title": "Electric cars in Europe"}},
        )

    def test_a_kept_title_is_not_touched(self) -> None:
        calls = self.run_script("Electric cars in Europe")
        self.assertFalse([c for c in calls if c["method"] == "POST"])


if __name__ == "__main__":
    unittest.main()
