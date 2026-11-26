var addBtn = document.getElementById('add-block');
var blocks = document.getElementById('blocks');

// Reveal one hidden time-frame row at a time (up to 6) and move focus into
// it. Rows left empty are ignored on save, so hiding only affects the view.
addBtn.addEventListener('click', function () {
  var hidden = blocks.querySelector('.block-row.is-hidden');
  if (hidden) {
    hidden.classList.remove('is-hidden');
    hidden.querySelector('input').focus();
  }
  if (!blocks.querySelector('.block-row.is-hidden')) addBtn.setAttribute('hidden', '');
});

// Remove a time frame: clear its inputs (so it is not saved), hide it, and
// put focus on "Add time frame" so keyboard users are not dropped to <body>.
blocks.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-remove-block]');
  if (!btn) return;
  var row = btn.closest('[data-block-row]');
  row.querySelectorAll('input').forEach(function (i) {
    i.value = '';
    i.removeAttribute('aria-invalid');
  });
  var msg = row.querySelector('.field-error');
  if (msg) msg.remove();
  row.classList.remove('has-error');
  row.classList.add('is-hidden');
  addBtn.removeAttribute('hidden');
  addBtn.focus();
});

// After loading from the radiator, take the user to the result.
var radiator = document.querySelector('#radiator [role="status"], #radiator [role="alert"]');
if (radiator) {
  radiator.setAttribute('tabindex', '-1');
  radiator.scrollIntoView({ block: 'center' });
  radiator.focus({ preventScroll: true });
}

// After a failed save, take the user straight to what needs fixing.
var errors = document.getElementById('editor-errors');
if (errors) {
  errors.scrollIntoView({ block: 'center' });
  errors.focus({ preventScroll: true });
}
