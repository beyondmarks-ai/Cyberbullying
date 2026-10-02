// DOM-level rendering checks; does not connect to a browser or a live dashboard.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.textContent = ''; this.className = ''; this.listeners = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  set innerHTML(value) { throw new Error('Untrusted analysis must not be rendered as HTML'); }
}
const elements = new Map();
const context = vm.createContext({
  document: {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement: tag => new Element(tag),
    createElementNS: (_, tag) => new Element(tag),
  },
  window: { location: { search: '' }, addEventListener() {} },
  URLSearchParams, URL, Date, Math,
  fetch: () => new Promise(() => {}),
  setInterval() {}, setTimeout() {}, clearInterval() {},
});
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'index.html'), 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInContext(code, context);
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const base = { kind: 'dm', source: '@test', text: 'example', time: '2026-10-02T00:00:00Z' };
const cases = [
  { ...base, analysis: { bullying: false, severity: 'none', confidence: .9, reason: 'Neutral' } },
  { ...base, analysis: { bullying: true, severity: 'low', confidence: .9, reason: 'Put-down' } },
  { ...base, analysis: { bullying: false, needs_review: true, severity: 'none', reason: 'Ambiguous',
    evidence: ['<img src=x onerror=alert(1)>'], uncertainties: ['Unclear gesture'], languages: ['uncertain'],
    segments: [{ start: 58, end: 90, status: 'review', summary: 'Ambiguous gesture' }] } },
  { ...base, analysis: { bullying: false, severity: 'unknown', reason: 'Failed' } },
];
context.fixture = cases;
vm.runInContext('events = fixture; render()', context);
assert.equal(elements.get('#alerts').textContent, 3);
const rows = elements.get('#events').children;
assert.equal(rows.length, 4);
assert.match(rows[1].className, /alert/);
assert.match(rows[2].className, /review/);
assert.match(text(rows[2]), /Needs human review/);
assert.match(text(rows[2]), /<img src=x onerror=alert\(1\)>/);
assert.match(text(rows[2]), /58-90s: review/);
assert.match(text(rows[3]), /Analysis unavailable/);
vm.runInContext("filter = 'review'; render()", context);
assert.equal(elements.get('#events').children.length, 2);
vm.runInContext("filter = 'alert'; render()", context);
assert.equal(elements.get('#events').children.length, 3);
console.log('PASS: review/alert filters, legacy results, unavailable state, segment evidence, safe text rendering');

(async () => {
  const requests = [];
  context.window.confirm = () => true;
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => options.method === 'DELETE' ? { state: 'deleted' } :
      { state: 'available', url: 'https://test.blob.core.windows.net/private/media?test-read-only', expires_at: 1900000000 } };
  };
  context.fixture = ['image', 'video', 'audio'].map((kind, index) => ({
    ...base, kind, id: `event-${index}`, text: 'message plus transcript', original_text: 'Original message',
    previews: [{ id: String(index).repeat(64), kind, state: 'available' }],
  }));
  vm.runInContext("events = fixture; filter = 'all'; render()", context);
  await new Promise(resolve => setImmediate(resolve));
  const nodes = node => [node, ...node.children.flatMap(nodes)];
  const current = elements.get('#events').children;
  assert.equal(current.length, 3);
  for (const [index, tag] of ['img', 'video', 'audio'].entries()) {
    assert.ok(nodes(current[index]).some(node => node.tag === tag));
    assert.match(text(current[index]), /Original message/);
    assert.doesNotMatch(text(current[index]), /message plus transcript/);
  }
  assert.equal(requests.length, 3);
  assert.ok(requests.every(r => r.options.headers['X-Preview-Request'] === '1'));
  vm.runInContext('render()', context);
  assert.equal(elements.get('#events').children[1], current[1], 'Unchanged polling must not reset playback');
  const remove = nodes(current[0]).find(node => node.textContent === 'Delete preview');
  await remove.listeners.click();
  assert.match(text(current[0]), /Preview deleted/);
  assert.equal(nodes(current[0]).filter(node => node.tag === 'img').length, 0);
  assert.equal(requests.at(-1).options.method, 'DELETE');
  console.log('PASS: image/video/audio preview grid, original text, stable polling, deletion and private API headers');
})().catch(error => { console.error(error); process.exitCode = 1; });
