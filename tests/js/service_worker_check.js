// Runs the app's real service worker source against a simulated outage.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const listeners = {};
const self = { addEventListener: (t, f) => { listeners[t] = f; }, skipWaiting() {}, clients: { claim() {} } };
const caches = {
  match: async (k) => 'CACHED ' + (typeof k === 'string' ? k : new URL(k.url).pathname),
  open: async () => ({ addAll: async () => {} }), keys: async () => [], delete: async () => {},
};
let serverUp = false;
const fetch = async (req) => { if (!serverUp) throw new TypeError('network down'); return 'NETWORK ' + new URL(req.url).pathname; };
new Function('self', 'caches', 'fetch', 'URL', src)(self, caches, fetch, URL);

async function ask(method, path, mode) {
  let answer;
  listeners.fetch({ request: { method, url: 'http://app' + path, mode }, respondWith: (p) => { answer = p; } });
  return answer === undefined ? 'left to the browser' : await answer;
}
(async () => {
  const cases = [
    // [label, server up?, method, path, mode, wanted]
    ['readout poll while the server is down', false, 'GET', '/rooms/1/status', 'cors', 'left to the browser'],
    ['sync-status poll while the server is down', false, 'GET', '/zones/1/sync-status', 'cors', 'left to the browser'],
    ['opening the app while the server is down', false, 'GET', '/', 'navigate', 'CACHED /'],
    ['opening another page while the server is down', false, 'GET', '/history', 'navigate', 'CACHED /'],
    ['opening the app with the server up', true, 'GET', '/', 'navigate', 'NETWORK /'],
    ['static file is cache-first', false, 'GET', '/static/styles.css', 'no-cors', 'CACHED /static/styles.css'],
    ['a POST is never intercepted', false, 'POST', '/rooms/1/temperature', 'cors', 'left to the browser'],
  ];
  let failed = 0;
  for (const [label, up, method, path, mode, wanted] of cases) {
    serverUp = up;
    const got = await ask(method, path, mode);
    const ok = got === wanted;
    if (!ok) failed++;
    console.log((ok ? 'PASS ' : 'FAIL ') + label + ' -> ' + got + (ok ? '' : '   (wanted: ' + wanted + ')'));
  }
  process.exit(failed ? 1 : 0);
})();
