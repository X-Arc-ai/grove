/* Hosted account authorization only. Connection status never changes import state. */
(function () {
  'use strict';
  var providers = [
    { id: 'gmail', name: 'Gmail', icon: 'gmail' },
    { id: 'google_calendar', name: 'Google Calendar', icon: 'calendar' },
    { id: 'notion', name: 'Notion', icon: 'notion' }
  ];
  var labels = {
    connected: 'Connected', not_connected: 'Not connected', attention: 'Reconnect required',
    unavailable: 'Not available yet', checking: 'Check in progress'
  };
  var instances = new Set();
  function account() {
    var state = window.GroveSimulation.getState();
    return state.auth ? state.account : null;
  }
  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function mount(scope) {
    scope.querySelectorAll('[data-pipedream-apps]').forEach(function (root) {
      if (root.dataset.pipedreamMounted) return;
      root.dataset.pipedreamMounted = 'true';
      var owner = account(), states = {}, configured = false, busy = false, version = 0;
      var note = '', popup = null;
      function current(ticket) {
        return root.isConnected && owner && account() === owner && ticket === version;
      }
      function paint() {
        root.innerHTML = '<p class="gp-section-copy">Connect an account with Pipedream. ' +
          'This only checks the connection. Emails, events and pages are not imported.</p>' +
          '<div class="gp-integrations">' + providers.map(function (p) {
            var state = states[p.id] || 'checking';
            var mark = (window.GroveIcons || {})[p.icon] || esc(p.name.slice(0, 1));
            return '<button type="button" class="gp-integration ' +
              (state === 'connected' ? 'is-connected' : '') + '" data-pipedream-connect="' + p.id +
              '" aria-label="' + (state === 'connected' ? 'Connected to ' : 'Connect ') + p.name +
              '" ' + (!configured || busy || state === 'connected' ? 'disabled' : '') + '>' +
              '<span class="gp-integration-mark">' + mark + '</span><span><strong>' + p.name +
              '</strong><small data-pipedream-status="' + p.id + '">' + labels[state] +
              '</small></span><span class="gp-integration-status">' +
              (state === 'connected' ? '✓' : '+') + '</span></button>';
          }).join('') + '</div><p class="gp-section-copy" role="status" aria-live="polite">' +
          esc(note) + '</p><button type="button" class="gp-small" data-pipedream-refresh ' +
          (busy ? 'disabled' : '') + '>Check connections</button>';
        root.querySelectorAll('[data-pipedream-connect]').forEach(function (button) {
          button.onclick = function () { connect(button.dataset.pipedreamConnect); };
        });
        root.querySelector('[data-pipedream-refresh]').onclick = refresh;
      }
      function error(res, fallback) {
        var value = res.body && res.body.error && res.body.error.message;
        return typeof value === 'string' ? value : fallback;
      }
      async function refresh() {
        if (!owner || busy || account() !== owner || !root.isConnected) return;
        busy = true; configured = false; states = {}; note = 'Check in progress.';
        var ticket = ++version;
        paint();
        try {
          var res = await window.GroveLive.getJSON('/v1/views/pipedream/accounts');
          if (!current(ticket)) return;
          if (!res.ok) throw new Error(error(res, 'Could not check connections. Try again.'));
          if (!res.body || res.body.account !== owner) throw new Error('Your account changed. Reload Grove.');
          if (!Array.isArray(res.body.providers)) throw new Error('Could not check connections. Try again.');
          providers.forEach(function (p) {
            var row = res.body.providers.find(function (value) { return value.provider === p.id; });
            states[p.id] = row && labels[row.status] ? row.status : 'unavailable';
          });
          configured = res.body.configured === true;
          note = configured ? 'Status checked with Pipedream. No content was imported.' :
            'Connections are not available yet. Server setup is incomplete.';
        } catch (err) {
          if (!current(ticket)) return;
          states = {}; providers.forEach(function (p) { states[p.id] = 'unavailable'; });
          note = err.message || 'Could not check connections. Try again.';
        } finally {
          if (current(ticket)) { busy = false; paint(); }
        }
      }
      async function connect(id) {
        if (!configured || busy || account() !== owner) return;
        // Reserve a window during the user click so asynchronous token creation is not blocked.
        popup = window.open('about:blank', '_blank');
        if (!popup) { note = 'Allow popups for Grove, then select Connect again.'; paint(); return; }
        popup.opener = null;
        busy = true; note = 'Open account authorization with Pipedream.';
        var ticket = ++version, opened = popup;
        paint();
        try {
          var res = await window.GroveLive.postJSON('/v1/views/pipedream/' + id + '/connect', {});
          if (!current(ticket)) { opened.close(); return; }
          if (!res.ok) throw new Error(error(res, 'Could not start the connection. Try again.'));
          if (!res.body || res.body.account !== owner) throw new Error('Your account changed. Reload Grove.');
          var url = new URL(res.body.connect_url);
          if (url.protocol !== 'https:' || url.username || url.password ||
              !(url.hostname === 'pipedream.com' || url.hostname.endsWith('.pipedream.com'))) {
            throw new Error('Could not start the connection. Try again.');
          }
          opened.location.href = url.href;
          note = 'Complete authorization in the new tab, then select Check connections.';
        } catch (err) {
          opened.close();
          if (current(ticket)) note = err.message || 'Could not start the connection. Try again.';
        } finally {
          if (current(ticket)) { busy = false; paint(); }
        }
      }
      function reset() {
        if (!root.isConnected) { instances.delete(reset); return; }
        if (account() === owner) return;
        ++version; owner = account(); configured = false; busy = false; states = {};
        if (popup && !popup.closed) popup.close();
        providers.forEach(function (p) { states[p.id] = 'unavailable'; });
        note = 'Your account changed. Reload Grove.'; paint();
      }
      instances.add(reset);
      refresh();
    });
  }
  window.addEventListener('grove:change', function () { instances.forEach(function (reset) { reset(); }); });
  window.addEventListener('grove:unauthenticated', function () { instances.forEach(function (reset) { reset(); }); });
  window.GrovePipedream = { mount: mount };
}());
