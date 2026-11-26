// Keep each target box and Off button showing the hub's state. The readout
// above them is re-fetched every few seconds and carries the hub's target
// in data-target (empty = off); copy that into the box unless the user is
// in the middle of changing it.
(function () {
  function boxFor(el) {
    var zone = el.closest('.zone');
    return zone && zone.querySelector('.setpoint-input');
  }
  function syncOffButton(zone, hub) {
    var off = zone.querySelector('.setpoint-off button');
    if (!off) return;
    off.disabled = hub === '';
    if (hub === '') off.setAttribute('aria-disabled', 'true');
    else off.removeAttribute('aria-disabled');
  }
  function syncTargets() {
    document.querySelectorAll('.readout[data-target]').forEach(function (readout) {
      var zone = readout.closest('.zone');
      var input = zone && zone.querySelector('.setpoint-input');
      if (!input) return;
      var hub = readout.getAttribute('data-target');
      syncOffButton(zone, hub);
      var moved = input.dataset.hub !== undefined && input.dataset.hub !== hub;
      input.dataset.hub = hub;
      input.placeholder = hub === '' ? 'Off' : '';
      // An unsent change stands while it is being typed, and until the hub's
      // target moves. Focus alone must not protect the box: after Enter the
      // cursor stays in it, and it would go stale again.
      if (input.dataset.edited && (document.activeElement === input || !moved)) return;
      delete input.dataset.edited;
      input.value = hub;
    });
  }
  function markEdited(e) {
    if (e.target.matches && e.target.matches('.setpoint-input')) e.target.dataset.edited = '1';
  }
  document.addEventListener('input', markEdited);
  document.addEventListener('change', markEdited);
  // A sent request (Set target or Off) ends the edit; the reply's readout
  // then carries the value the hub was given.
  document.addEventListener('htmx:beforeRequest', function (e) {
    var form = e.target.closest && e.target.closest('form.setpoint, form.setpoint-off');
    var input = form && boxFor(form);
    if (input) delete input.dataset.edited;
  });
  document.addEventListener('htmx:afterSettle', syncTargets);
  // Leaving the page drops unsent changes, so coming back shows the truth.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) return;
    document.querySelectorAll('.setpoint-input').forEach(function (input) {
      delete input.dataset.edited;
    });
  });
  syncTargets();
})();
