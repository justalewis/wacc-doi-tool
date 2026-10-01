// Remembers the depositor's name and email in this browser so staff don't retype them.
// Nothing leaves the browser; the forms work identically without this script.
(function () {
  var fields = document.querySelectorAll('[data-remember]');
  fields.forEach(function (el) {
    var key = 'wacdoi.' + el.getAttribute('data-remember');
    try {
      if (!el.value) { el.value = localStorage.getItem(key) || ''; }
      el.form.addEventListener('submit', function () {
        try { localStorage.setItem(key, el.value); } catch (e) { /* storage blocked */ }
      });
    } catch (e) { /* storage blocked */ }
  });
})();
