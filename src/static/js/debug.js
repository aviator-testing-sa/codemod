document.addEventListener('DOMContentLoaded', function() {
  const debugTrigger = document.getElementById('debug-trigger');
  const debug = document.getElementById('debug');
  const debugNav = document.getElementById('debug-nav');

  if (debugTrigger) {
    debugTrigger.addEventListener('click', function(e) {
      e.preventDefault();
      debug.classList.toggle('open');
    });
  }

  if (debugNav) {
    debugNav.addEventListener('click', function(e) {
      if (e.target.tagName === 'A') {
        e.preventDefault();

        const action = e.target.getAttribute('rel');
        const arg = e.target.getAttribute('data-arg') || null;

        // Assuming $('.content').infinitescroll is a function that needs to be called
        // and is available in the global scope, or imported from another module.
        // Since it is not standard JS, it is being left as is.  If this requires
        // migration, additional details will be needed.
        $('.content').infinitescroll(action, arg);
      }
    });
  }
});
