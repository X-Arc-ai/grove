/* This bootstrap belongs only to the explicit synthetic demo artifact. */
(function () {
  'use strict';
  var query = new URLSearchParams(window.location.search);
  var requested = query.get('provider');
  var provider = requested === 'claude' || requested === 'chatgpt' ? requested : null;
  var start = query.get('start') === '1' && !!provider;
  var values = Object.create(null);
  values['grove-minimal-codex-v3'] = JSON.stringify({
    version: 3, schema: 4, auth: {provider: 'demo', email: ''}, name: 'Maya',
    onboardingStep: start ? 1 : 0, onboardingComplete: start, tourComplete: start
  });
  window.GroveDemo = Object.freeze({
    storage: Object.freeze({
      getItem: function (key) { return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null; },
      setItem: function (key, value) { values[key] = String(value); },
      removeItem: function (key) { delete values[key]; }
    })
  });
  // Keep only fixed public controls in this URL. No file or account data belongs here.
  window.history.replaceState(null, '', '/demo/onboarding' + (start ? '?provider=' + provider + '&start=1' : ''));
  document.title = 'Simulation · Grove';
  document.addEventListener('DOMContentLoaded', function () {
    var marker = document.createElement('aside');
    marker.id = 'grove-demo-marker';
    marker.setAttribute('aria-label', 'Simulation controls');
    marker.innerHTML = '<div><strong>Simulation</strong><span>Invented data. Your file was not uploaded or saved.</span></div>' +
      '<a href="/demo/onboarding">Restart demo</a><a href="/">Return to live Grove</a>';
    document.body.appendChild(marker);
    if (start) window.GroveSimulation.importSource('conversations', {
      provider: provider, name: 'Synthetic example', label: provider === 'claude' ? 'Claude' : 'ChatGPT'
    });
  });
})();
