// Runs the page's real "target box follows the hub" script against a stub DOM.
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).find((s) => s.includes('syncTargets'));

function makePage(target) {
  const input = { value: target, placeholder: target === '' ? 'Off' : '', dataset: {}, matches: (s) => s === '.setpoint-input' };
  const off = { disabled: target === '', attrs: target === '' ? { 'aria-disabled': 'true' } : {},
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
  const zone = { querySelector: (s) => (s === '.setpoint-input' ? input : s === '.setpoint-off button' ? off : null) };
  const readout = { attrs: { 'data-target': target }, getAttribute(k) { return this.attrs[k]; },
    hasAttribute(k) { return k in this.attrs; }, closest: (s) => (s === '.zone' ? zone : null) };
  const form = { closest: (s) => (s.includes('form.setpoint') ? form : s === '.zone' ? zone : null) };
  const listeners = {};
  const document = {
    activeElement: null, hidden: false,
    addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
    querySelectorAll(s) { return s === '.readout[data-target]' ? ('data-target' in readout.attrs ? [readout] : []) : s === '.setpoint-input' ? [input] : []; },
  };
  const fire = (t, e) => (listeners[t] || []).forEach((f) => f(e || {}));
  new Function('document', script)(document);
  return {
    input, off, document,
    poll(hub) { if (hub === null) delete readout.attrs['data-target']; else readout.attrs['data-target'] = hub; fire('htmx:afterSettle'); },
    type(v) { document.activeElement = input; input.value = v; fire('input', { target: input }); },
    step(v) { input.value = v; fire('change', { target: input }); },
    blur() { document.activeElement = null; },
    send() { fire('htmx:beforeRequest', { target: form }); },
    hide() { document.hidden = true; fire('visibilitychange'); document.hidden = false; },
  };
}

const results = [];
const check = (label, got, want) => results.push([JSON.stringify(got) === JSON.stringify(want), label, got, want]);

let p = makePage('10.0');
p.poll('16.0'); check('change from elsewhere reaches the box', p.input.value, '16.0');
p.poll(''); check('off elsewhere: box empty, reads Off', [p.input.value, p.input.placeholder], ['', 'Off']);
check('off elsewhere: Off button disabled', [p.off.disabled, p.off.attrs['aria-disabled']], [true, 'true']);
p.poll('18.0'); check('back on: Off button usable again', [p.off.disabled, 'aria-disabled' in p.off.attrs], [false, false]);

p = makePage('');  // page loaded while the zone was off (tonight's Lounge)
check('loaded off: Off button starts disabled', p.off.disabled, true);
p.type('16'); p.send(); p.poll('16.0');
check('loaded off, then set to 16: Off button enabled', [p.off.disabled, 'aria-disabled' in p.off.attrs], [false, false]);
check('loaded off, then set to 16: box shows 16.0', p.input.value, '16.0');

p = makePage('10.0');
p.step('10.5'); p.poll('10.0'); check('unsent stepper change stands', p.input.value, '10.5');
p.poll('14.0'); check('hub moving replaces an unsent change', p.input.value, '14.0');
p.type('17'); p.poll('15.0'); check('value being typed is left alone', p.input.value, '17');
p.blur(); p.poll('15.0'); check('...and still stands after leaving the box', p.input.value, '17');
p.hide(); p.poll('15.0'); check('leaving the page drops the unsent change', p.input.value, '15.0');
p.type('10'); p.send(); p.poll('10.0'); p.poll('');
check('typed 10 + Enter (cursor still in box), switched off elsewhere', [p.input.value, p.input.placeholder], ['', 'Off']);
p.poll(null); check('unreachable zone leaves the box alone', [p.input.value, p.input.placeholder], ['', 'Off']);

let failed = 0;
for (const [ok, label, got, want] of results) {
  if (!ok) failed++;
  console.log((ok ? 'PASS ' : 'FAIL ') + label + (ok ? '' : '  got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want)));
}
process.exit(failed ? 1 : 0);
