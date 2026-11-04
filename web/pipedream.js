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
      var note = '', popup = null, query = '', googleOpen = false;
      var catalog = window.GroveIntegrations || [];
      var defaults = [{id:'google',name:'Google',category:'Gmail, Drive, Calendar'}, {id:'notion',name:'Notion',category:'Notes & knowledge'}, {id:'slack',name:'Slack',category:'Team conversations'}, {id:'microsoft',name:'Microsoft 365',category:'Mail, files & teams'}, {id:'linear',name:'Linear',category:'Projects & tasks'}, {id:'dropbox',name:'Dropbox',category:'Files & folders'}];
      function current(ticket) {
        return root.isConnected && owner && account() === owner && ticket === version;
      }
      function results() {
        var rows = query.trim() ? catalog.filter(function (p) { return (p.name+' '+p.category).toLowerCase().includes(query.trim().toLowerCase()); }).slice(0,30) : defaults;
        if (googleOpen && !query.trim()) rows = [{id:'gmail',name:'Gmail'}, {id:'google_calendar',name:'Google Calendar'}, {id:'google_drive',name:'Google Drive'}];
        return rows.map(function (p) {
          var supported = providers.find(function (item) { return item.id === p.id; });
          var group = p.id === 'google', state = supported ? states[p.id] || 'checking' : 'unavailable';
          var mark = (window.GroveIcons || {})[supported ? supported.icon : p.id] || '<span class="gp-app-initial">'+esc(p.name.slice(0,2))+'</span>';
          return '<button type="button" class="gp-integration '+(state==='connected'?'is-connected':'')+'" '+(group?'data-pipedream-google':'data-pipedream-connect="'+esc(p.id)+'"')+' aria-label="'+(group?'Choose a Google app':state==='connected'?'Connected to '+esc(p.name):'Connect '+esc(p.name))+'" '+(!group&&(!supported||!configured||busy||state==='connected')?'disabled':'')+'><span class="gp-integration-mark">'+mark+'</span><span><strong>'+esc(p.name)+'</strong><small'+(supported?' data-pipedream-status="'+p.id+'"':'')+'>'+esc(group?p.category:labels[state])+'</small></span><span class="gp-integration-status">'+(state==='connected'?'✓':'+')+'</span></button>';
        }).join('') || '<p class="gp-empty-sources">No matches — try another name</p>';
      }
      function bindResults() {
        root.querySelectorAll('[data-pipedream-connect]').forEach(function (button) { button.onclick=function(){if(providers.some(function(p){return p.id===button.dataset.pipedreamConnect;}))connect(button.dataset.pipedreamConnect);}; });
        var group=root.querySelector('[data-pipedream-google]');if(group)group.onclick=function(){googleOpen=true;paint();};
      }
      function paint() {
        root.innerHTML = '<label class="gp-integration-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input type="search" placeholder="Search 3,000+ integrations" aria-label="Search 3,000+ integrations" data-pipedream-search value="'+esc(query)+'"></label>'+ (googleOpen&&!query?'<button class="gp-text" data-pipedream-back>All apps</button>':'')+'<div class="gp-integrations" data-pipedream-results>'+results()+'</div><p class="gp-section-copy">Connect an account with Pipedream. Emails, events and pages are not imported.</p><p class="gp-section-copy" role="status" aria-live="polite">'+esc(note)+'</p><button type="button" class="gp-small" data-pipedream-refresh '+(busy?'disabled':'')+'>Check connections</button>';
        bindResults();
        root.querySelector('[data-pipedream-search]').oninput=function(e){query=e.target.value;root.querySelector('[data-pipedream-results]').innerHTML=results();bindResults();};
        var back=root.querySelector('[data-pipedream-back]');if(back)back.onclick=function(){googleOpen=false;paint();};
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
