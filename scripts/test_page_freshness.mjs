import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

// Execute the shipped inline script unchanged. Only platform boundaries are
// synthetic: a minimal DOM, wall clock/timers, and explicit HTTP responses.
// No browser/layout, network, feeder or component availability is simulated.
const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
assert.equal(scripts.length, 1, 'fixture requires the one real inline script');
const source = scripts[0][1];
const MINUTE = 60_000;
const START = Date.parse('2026-10-20T12:00:00Z');
const PUBLISHED = {
  updated_at: '2026-10-01T09:00:00Z',
  components: [{ id: 'synthetic-api', name: 'Synthetic API', status: 'operational' }],
  incidents: []
};

class Element {
  className = '';
  text = '';
  markup = null;
  get textContent() { return this.text; }
  set textContent(value) { this.text = String(value ?? ''); this.markup = null; }
  get innerHTML() {
    return this.markup ?? this.text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  }
  set innerHTML(value) { this.markup = String(value); this.text = this.markup.replace(/<[^>]*>/g, ''); }
}

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(structuredClone(data)) };
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

function page(overrides = {}) {
  let now = START, nextTimer = 0;
  const timers = new Map(), requests = [], elements = new Map(), unexpected = [];
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element());
  for (const [id, name] of [['checked', 'checked'], ['banner', 'banner']]) elements.get(id).className = name;
  elements.get('banner-text').textContent = 'Consultando…';
  const get = id => { assert(elements.has(id), `unknown DOM id ${id}`); return elements.get(id); };
  const routes = {
    status: () => response(PUBLISHED),
    heartbeat: () => response({ checked_at: new Date(now).toISOString(), interval_s: 30 }),
    actions: () => response({ workflow_runs: [{ updated_at: new Date(now).toISOString() }] }),
    ...overrides
  };
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  function schedule(fn, delay, interval) {
    assert.equal(typeof fn, 'function', 'only callable timers are supported');
    assert(Number.isFinite(delay) && delay > 0, 'finite positive timer required');
    const id = ++nextTimer;
    timers.set(id, { fn, at: now + delay, interval: interval ? delay : 0 });
    return id;
  }
  const context = vm.createContext({
    document: { getElementById: get, createElement: () => new Element() },
    Date: ClockDate,
    setInterval: (fn, delay) => schedule(fn, delay, true),
    setTimeout: (fn, delay) => schedule(fn, delay, false),
    clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id),
    fetch: (input, init) => {
      const url = String(input);
      const kind = url === 'status.json' ? 'status'
        : url.startsWith('https://raw.githubusercontent.com/BRSQL/status/heartbeat/heartbeat.json?') ? 'heartbeat'
        : url === 'https://api.github.com/repos/BRSQL/status/actions/workflows/probe.yml/runs?status=success&per_page=1' ? 'actions'
        : null;
      if (!kind) unexpected.push(url);
      assert(kind, `unexpected HTTP request: ${url}`);
      requests.push({ kind, url, init, at: now });
      return Promise.resolve().then(() => routes[kind]());
    }
  });
  vm.runInContext(source, context, { filename: 'index.html:inline', timeout: 1000 });
  return {
    get, requests, routes, now: () => now,
    count: kind => requests.filter(r => r.kind === kind).length,
    async ready() { await settle(); },
    async advance(milliseconds) {
      const end = now + milliseconds;
      let fired = 0;
      while (true) {
        const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        assert(++fired < 1000, 'fixture timer runaway');
        const [id, timer] = due;
        now = timer.at;
        if (timer.interval) timer.at += timer.interval; else timers.delete(id);
        timer.fn();
        await settle();
      }
      now = end;
      await settle();
    },
    close() { timers.clear(); assert.deepEqual(unexpected, [], 'all HTTP must use an explicit fixture'); }
  };
}

function stale(p) { return p.get('checked').className.split(/\s+/).includes('stale'); }
function assertFresh(p) {
  assert.equal(stale(p), false);
  assert.match(p.get('checked').textContent, /Verificação automática.*agora há pouco/);
}
function assertAged(p) {
  assert.deepEqual({ stale: stale(p), saysRecent: p.get('checked').textContent.includes('agora há pouco') },
    { stale: true, saysRecent: false }, 'last known verification must age without a new response');
}
function assertPublishedOperational(p) {
  assert.match(p.get('components').innerHTML, /Operacional/);
  assert.doesNotMatch(p.get('components').innerHTML, /Fora do ar/);
}

test('fresh heartbeat renders independently of the older last status change', async t => {
  const p = page(); t.after(() => p.close()); await p.ready();
  assertFresh(p);
  assert.match(p.get('stamp').textContent, /^Última mudança:/);
  assertPublishedOperational(p);
  assert.equal(p.count('actions'), 0);
});

test('old heartbeat warns about verification without declaring a component outage', async t => {
  const p = page({ heartbeat: () => response({ checked_at: new Date(START - 20 * MINUTE).toISOString(), interval_s: 30 }) });
  t.after(() => p.close()); await p.ready();
  assertAged(p);
  assertPublishedOperational(p);
  assert.equal(p.get('banner').className, 'banner');
  assert.match(p.get('banner-text').textContent, /Todos os serviços operando normalmente/);
});

test('exactly fifteen minutes preserves the existing strictly-greater threshold', async t => {
  const p = page({ heartbeat: () => response({ checked_at: new Date(START - 15 * MINUTE).toISOString(), interval_s: 30 }) });
  t.after(() => p.close()); await p.ready();
  assert.equal(stale(p), false);
  assert.match(p.get('checked').textContent, /há 15 min/);
});

test('last verification ages after later status requests reject', async t => {
  const p = page(); t.after(() => p.close()); await p.ready(); assertFresh(p);
  p.routes.status = () => Promise.reject(new Error('synthetic status transport failure'));
  p.routes.heartbeat = () => response({ checked_at: new Date(START).toISOString(), interval_s: 30 });
  await p.advance(20 * MINUTE);
  assert(p.count('status') > 1);
  assertAged(p);
  assertPublishedOperational(p);
});

test('last verification ages while later status HTTP remains pending', async t => {
  const held = deferred(), p = page();
  t.after(async () => { held.resolve(response(PUBLISHED)); await settle(); p.close(); });
  await p.ready(); assertFresh(p);
  p.routes.status = () => held.promise;
  p.routes.heartbeat = () => response({ checked_at: new Date(START).toISOString(), interval_s: 30 });
  await p.advance(20 * MINUTE);
  assert(p.count('status') > 1);
  assertAged(p);
  assertPublishedOperational(p);
});

test('last verification ages while later status JSON remains pending', async t => {
  const held = deferred(), p = page();
  t.after(async () => { held.resolve(PUBLISHED); await settle(); p.close(); });
  await p.ready(); assertFresh(p);
  p.routes.status = () => ({ ok: true, status: 200, json: () => held.promise });
  p.routes.heartbeat = () => response({ checked_at: new Date(START).toISOString(), interval_s: 30 });
  await p.advance(20 * MINUTE);
  assert(p.count('status') > 1);
  assertAged(p);
  assertPublishedOperational(p);
});

test('last verification ages while later heartbeat HTTP remains pending', async t => {
  const held = deferred(), p = page();
  t.after(async () => { held.resolve(response({ checked_at: new Date(START).toISOString(), interval_s: 30 })); await settle(); p.close(); });
  await p.ready(); assertFresh(p);
  p.routes.heartbeat = () => held.promise;
  await p.advance(20 * MINUTE);
  assert(p.count('heartbeat') > 1);
  assertAged(p);
  assertPublishedOperational(p);
});

test('first pending status request does not prevent a valid heartbeat from appearing', async t => {
  const held = deferred(), p = page({ status: () => held.promise });
  t.after(async () => { held.resolve(response(PUBLISHED)); await settle(); p.close(); });
  await p.ready();
  assert.equal(p.count('status'), 1);
  assert.match(p.get('checked').textContent, /Verificação automática.*agora há pouco/);
  assert.equal(stale(p), false);
  assert.equal(p.get('banner-text').textContent, 'Consultando…');
});

for (const [name, checked_at] of [['invalid', 'not-a-date'], ['missing', undefined]]) {
  test(`${name} heartbeat timestamp uses a valid secondary observation`, async t => {
    const p = page({ heartbeat: () => response({ checked_at, interval_s: 30 }) });
    t.after(() => p.close()); await p.ready();
    assert.doesNotMatch(p.get('checked').textContent, /NaN|Invalid Date/);
    assertFresh(p);
    assert.match(p.get('checked').textContent, /sonda secundária/);
    assert.equal(p.count('actions'), 1);
  });
}

test('invalid timestamps in both sources show uncertainty instead of a fresh verification', async t => {
  const p = page({
    heartbeat: () => response({ checked_at: 'not-a-date', interval_s: 30 }),
    actions: () => response({ workflow_runs: [{ updated_at: 'also-invalid' }] })
  });
  t.after(() => p.close()); await p.ready();
  assert.equal(stale(p), true);
  assert.doesNotMatch(p.get('checked').textContent, /NaN|Invalid Date|agora há pouco/);
  assert.match(p.get('checked').textContent, /sem informação/);
  assertPublishedOperational(p);
});

test('missing heartbeat endpoint still uses the current secondary source', async t => {
  const p = page({ heartbeat: () => response({}, 404) }); t.after(() => p.close()); await p.ready();
  assertFresh(p);
  assert.match(p.get('checked').textContent, /sonda secundária/);
  assert.equal(p.count('actions'), 1);
});

test('two transport failures retain the existing unavailable-information warning', async t => {
  const fail = () => Promise.reject(new Error('synthetic observation failure'));
  const p = page({ heartbeat: fail, actions: fail }); t.after(() => p.close()); await p.ready();
  assert.equal(stale(p), true);
  assert.match(p.get('checked').textContent, /sem informação/);
  assertPublishedOperational(p);
});

test('ordinary five-minute refresh accepts a newly observed timestamp', async t => {
  const p = page(); t.after(() => p.close()); await p.ready(); assertFresh(p);
  const before = p.get('checked').textContent;
  await p.advance(5 * MINUTE);
  assertFresh(p);
  assert.notEqual(p.get('checked').textContent, before);
  assert(p.count('status') >= 2 && p.count('heartbeat') >= 2);
});

test('new valid observation removes an earlier stale warning', async t => {
  const p = page({ heartbeat: () => response({ checked_at: new Date(START - 20 * MINUTE).toISOString(), interval_s: 30 }) });
  t.after(() => p.close()); await p.ready(); assertAged(p);
  p.routes.heartbeat = () => response({ checked_at: new Date(p.now()).toISOString(), interval_s: 30 });
  await p.advance(5 * MINUTE);
  assertFresh(p);
  assertPublishedOperational(p);
});

// Separate concurrency boundary: an older request must not replace the result
// of a later refresh. These cases do not impose timestamp ordering on sources.
test('older successful heartbeat response cannot replace a later refresh', async t => {
  const held = deferred(), p = page();
  t.after(async () => { held.resolve(response({ checked_at: new Date(START).toISOString(), interval_s: 30 })); await settle(); p.close(); });
  await p.ready(); assertFresh(p);
  p.routes.heartbeat = () => held.promise;
  await p.advance(5 * MINUTE);
  p.routes.heartbeat = () => response({ checked_at: new Date(p.now()).toISOString(), interval_s: 30 });
  await p.advance(5 * MINUTE); assertFresh(p);
  const current = p.get('checked').textContent;
  held.resolve(response({ checked_at: new Date(START).toISOString(), interval_s: 30 }));
  await settle();
  assert.equal(p.get('checked').textContent, current);
  assert.equal(stale(p), false);
});

test('older failed observation cannot erase a later successful refresh', async t => {
  const held = deferred(), p = page();
  t.after(async () => { held.resolve(response({}, 503)); await settle(); p.close(); });
  await p.ready(); assertFresh(p);
  p.routes.heartbeat = () => held.promise;
  p.routes.actions = () => response({}, 503);
  await p.advance(5 * MINUTE);
  p.routes.heartbeat = () => response({ checked_at: new Date(p.now()).toISOString(), interval_s: 30 });
  await p.advance(5 * MINUTE); assertFresh(p);
  const current = p.get('checked').textContent;
  held.resolve(response({}, 503));
  await settle();
  assert.equal(p.get('checked').textContent, current);
  assert.equal(stale(p), false);
});
