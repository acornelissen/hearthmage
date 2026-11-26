// Register the service worker so the app can be installed to the home screen.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  });
}

// Progressive enhancement: turn the +/- buttons into a target stepper.
// The number input works on its own if this never runs.
document.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-step]');
  if (!btn) return;
  var wrap = btn.closest('[data-stepper]');
  var input = wrap && wrap.querySelector('input[name="temperature"]');
  if (!input) return;
  var step = parseFloat(btn.getAttribute('data-step')) || 0;
  var min = input.min !== '' ? parseFloat(input.min) : -Infinity;
  var max = input.max !== '' ? parseFloat(input.max) : Infinity;
  var cur = parseFloat(input.value);
  if (isNaN(cur)) cur = isFinite(min) ? min : 0;
  input.value = String(Math.min(max, Math.max(min, cur + step)));
  input.dispatchEvent(new Event('change', { bubbles: true }));
});
