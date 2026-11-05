/* Account authorization through the server's owner-scoped Pipedream contract.
 * A connection never implies a content import or a completed provider consent. */
(function () {
  'use strict';
  // The six rows of setup, in the sim's words. The catalog answers only a search.
  var FEATURED=[
    {provider:'gmail',name:'Gmail',detail:'Mail',connectable:true},
    {provider:'google_calendar',name:'Google Calendar',detail:'Plans & events',connectable:true},
    {provider:'notion',name:'Notion',detail:'Notes & knowledge',connectable:true},
    {provider:'slack_v2',name:'Slack',detail:'Team conversations',connectable:true},
    {provider:'linear',name:'Linear',detail:'Projects & tasks',connectable:true},
    {provider:'dropbox',name:'Dropbox',detail:'Files & folders',connectable:true}
  ];
  var MARKS={google_calendar:'calendar',google_drive:'drive',google_sheets:'sheets',slack_v2:'slack',airtable_oauth:'airtable',microsoft_outlook:'microsoft'};
  var RESULTS=8;
  var instances=new Set(), returns=new Set();
  function account(){var s=window.GroveSimulation.getState();return s.auth?s.account:null;}
  function esc(v){return String(v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function slug(v){return typeof v==='string'&&/^[a-z0-9_][a-z0-9_-]{0,119}$/.test(v);}
  function mark(app){return (window.GroveIcons||{})[MARKS[app.provider]||app.provider]||'';}
  function logo(app){return typeof app.logo==='string'&&/^https:\/\/assets\.pipedream\.net\//.test(app.logo)?app.logo:'';}
  // Apps with a logo come first: ours, then Pipedream's. The server's popularity order stays.
  function ranked(rows){
    function weight(app){return mark(app)?0:logo(app)?1:2;}
    return rows.map(function(app,index){return {app:app,index:index};}).sort(function(a,b){return weight(a.app)-weight(b.app)||a.index-b.index;}).map(function(row){return row.app;}).slice(0,RESULTS);
  }
  function error(res,fallback){var e=res.body&&res.body.error;if(res.status===403&&e&&e.code==='scope_denied')return 'Connections are disabled for this prepared demo account. Use your own account to connect apps.';return e&&typeof e.message==='string'?e.message:fallback;}
  function mount(scope){scope.querySelectorAll('[data-pipedream-apps]').forEach(function(root){
    if(root.dataset.pipedreamMounted)return;
    root.dataset.pipedreamMounted='true';
    var owner=account(),generation=0,catalogRequest=0,statusRequest=0,found=[],states={},pendingApp=null,confirmApp=null;
    var query='',configured=false,busy=false,loading=false,note='',popup=null,timer=null;
    function current(ticket){return !!owner&&root.isConnected&&account()===owner&&ticket===generation;}
    function shown(){return query.trim()?found:FEATURED;}
    function icon(app){var own=mark(app);if(own)return own;var url=logo(app);if(url)return '<img src="'+esc(url)+'" alt="" loading="lazy" referrerpolicy="no-referrer">';return '<span class="gp-app-initial">'+esc(app.name.slice(0,2))+'</span>';}
    function cards(){return shown().map(function(app){var status=states[app.provider],done=status==='connected',wait=pendingApp===app.provider;return '<button type="button" class="gp-integration '+(done?'is-connected':'')+'" data-pipedream-select="'+app.provider+'" aria-label="'+(done?'Connected to ':'Connect ')+esc(app.name)+'" '+(busy?'disabled':'')+'><span class="gp-integration-mark">'+icon(app)+'</span><span><strong>'+esc(app.name)+'</strong>'+(app.detail?'<small>'+esc(app.detail)+'</small>':'')+'</span><span class="gp-integration-status">'+(wait?'…':done?'✓':'+')+'</span></button>';}).join('');}
    function dialog(){
      if(!confirmApp)return '';
      return '<section class="gp-pipedream-confirm" role="alertdialog" aria-modal="false" aria-label="Disconnect '+esc(confirmApp.name)+'?"><h3>Disconnect '+esc(confirmApp.name)+'?</h3><p>Your Grove memories stay.</p><button type="button" class="gp-small" data-pipedream-cancel '+(busy?'disabled':'')+'>Cancel</button><button type="button" class="gp-small gp-danger" data-pipedream-confirm '+(busy?'disabled':'')+'>Disconnect</button></section>';
    }
    function paint(){
      var focused=root.contains(document.activeElement)&&document.activeElement.hasAttribute('data-pipedream-search');
      var pos=focused?document.activeElement.selectionStart:null;
      var empty=query.trim()&&!loading&&!found.length&&!note;
      root.innerHTML='<label class="gp-integration-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input type="search" maxlength="128" placeholder="Search 3,000+ integrations" aria-label="Search 3,000+ integrations" data-pipedream-search value="'+esc(query)+'" '+(busy?'disabled':'')+'></label><div class="gp-integrations" data-pipedream-results>'+cards()+'</div>'+(empty?'<p class="gp-empty-sources">No matches — try another name</p>':'')+dialog()+'<p class="gp-section-copy" role="status" aria-live="polite"'+(note?'':' hidden')+'>'+esc(note)+'</p>';
      var input=root.querySelector('[data-pipedream-search]');
      input.oninput=function(e){query=e.target.value;confirmApp=null;note='';catalogRequest++;clearTimeout(timer);if(!query.trim()){found=[];loading=false;paint();return;}timer=setTimeout(loadCatalog,200);};
      if(focused){input.focus();try{input.setSelectionRange(pos,pos);}catch(_) {}}
      root.querySelectorAll('[data-pipedream-select]').forEach(function(button){button.onclick=function(){
        var app=shown().find(function(row){return row.provider===button.dataset.pipedreamSelect;});if(!app)return;
        if(states[app.provider]==='connected'){confirmApp=app;paint();root.querySelector('[data-pipedream-cancel]').focus();return;}
        connect(app);
      };});
      var button=root.querySelector('[data-pipedream-cancel]');if(button)button.onclick=function(){confirmApp=null;paint();};
      button=root.querySelector('[data-pipedream-confirm]');if(button)button.onclick=disconnect;
    }
    async function loadCatalog(){
      var ticket=generation,request=++catalogRequest;
      if(!current(ticket)||busy)return;
      loading=true;note='';paint();
      try{
        var res=await window.GroveLive.getJSON('/v1/views/pipedream/apps?q='+encodeURIComponent(query.trim()));
        if(!current(ticket)||request!==catalogRequest)return;
        if(!res.ok)throw new Error(error(res,'The app catalog could not load. Try again.'));
        var body=res.body;
        if(!body||body.account!==owner||!Array.isArray(body.apps)||body.apps.length>30)throw new Error('The app catalog could not load. Try again.');
        var seen={};
        if(!body.apps.every(function(app){if(!app||!slug(app.provider)||seen[app.provider]||typeof app.name!=='string'||!app.name||app.name.length>200||!['oauth','keys','none',null].includes(app.auth_type)||app.connectable!==['oauth','keys'].includes(app.auth_type))return false;seen[app.provider]=true;return true;}))throw new Error('The app catalog could not load. Try again.');
        found=ranked(body.apps.filter(function(app){return app.connectable;}).map(function(app){return Object.assign({},app,{detail:typeof app.category==='string'?app.category:''});}));
        configured=body.configured===true;
      }catch(err){if(current(ticket)&&request===catalogRequest){found=[];note=err.message||'The app catalog could not load. Try again.';}}
      finally{if(current(ticket)&&request===catalogRequest){loading=false;paint();}}
    }
    // A failed status read shows nothing: the rows stay usable and the Connect call decides.
    async function refresh(provider){
      var ticket=generation,request=++statusRequest;
      if(!current(ticket)||busy)return;
      try{
        var res=await window.GroveLive.getJSON('/v1/views/pipedream/accounts'+(provider?'?provider='+encodeURIComponent(provider):''));
        if(!current(ticket)||request!==statusRequest)return;
        if(!res.ok){if(res.status===403)note=error(res,'');return;}
        if(!res.body||res.body.account!==owner||!Array.isArray(res.body.providers))return;
        res.body.providers.forEach(function(row){if(slug(row.provider)&&['connected','not_connected','attention','unavailable'].includes(row.status))states[row.provider]=row.status;});
        configured=res.body.configured===true;
      }catch(_){return;}
      finally{if(current(ticket)&&request===statusRequest)paint();}
    }
    async function connect(app){
      var ticket=generation;if(busy||!current(ticket))return;
      popup=window.open('about:blank','_blank');if(!popup){note='Allow popups for Grove, then try again.';paint();return;}popup.opener=null;
      var opened=popup;busy=true;pendingApp=app.provider;confirmApp=null;note='';statusRequest++;paint();
      try{
        var res=await window.GroveLive.postJSON('/v1/views/pipedream/'+encodeURIComponent(app.provider)+'/connect',{});
        if(!current(ticket)){opened.close();return;}
        if(!res.ok)throw new Error(error(res,'Could not start the connection. Try again.'));
        if(!res.body||res.body.account!==owner||res.body.provider!==app.provider)throw new Error('Could not start the connection. Try again.');
        var url=new URL(res.body.connect_url);
        if(url.protocol!=='https:'||url.username||url.password||!(url.hostname==='pipedream.com'||url.hostname.endsWith('.pipedream.com')))throw new Error('Could not start the connection. Try again.');
        opened.location.href=url.href;
      }catch(err){opened.close();pendingApp=null;if(current(ticket))note=err.message||'Could not start the connection. Try again.';}
      finally{if(current(ticket)){busy=false;paint();}}
    }
    async function disconnect(){
      var app=confirmApp,ticket=generation;if(!app||busy||!current(ticket))return;
      busy=true;statusRequest++;note='';paint();
      try{
        var res=await window.GroveLive.postJSON('/v1/views/pipedream/'+encodeURIComponent(app.provider)+'/disconnect',{confirm:true});
        if(!current(ticket))return;
        if(!res.ok)throw new Error(error(res,'Could not disconnect. Try again.'));
        if(!res.body||res.body.account!==owner||res.body.provider!==app.provider||res.body.status!=='not_connected')throw new Error('Could not disconnect. Try again.');
        states[app.provider]='not_connected';
      }catch(err){if(current(ticket))note=err.message||'Could not disconnect. Try again.';}
      finally{if(current(ticket)){busy=false;confirmApp=null;paint();}}
    }
    function reset(force){
      if(!force&&root.isConnected&&account()===owner)return;
      generation++;catalogRequest++;statusRequest++;clearTimeout(timer);
      if(popup&&!popup.closed)popup.close();
      found=[];states={};configured=false;busy=false;confirmApp=null;pendingApp=null;query='';
      returns.delete(onReturn);
      if(!root.isConnected){instances.delete(reset);return;}
      owner=null;note='Your account changed. Reload Grove.';paint();
    }
    root.addEventListener('keydown',function(event){if(event.key==='Escape'&&confirmApp){event.stopPropagation();if(!busy){confirmApp=null;paint();}}});
    // Back from the provider's tab: read the app that was just authorized, then the six rows.
    function onReturn(){if(!popup||busy||!current(generation))return;var app=pendingApp;pendingApp=null;paint();refresh(app&&!FEATURED.some(function(row){return row.provider===app;})?app:null);}
    instances.add(reset);returns.add(onReturn);paint();refresh(null);
  });}
  ['grove:change','grove:unauthenticated','grove:session-changed'].forEach(function(event){window.addEventListener(event,function(){instances.forEach(function(reset){reset(event!=='grove:change');});});});
  window.addEventListener('focus',function(){returns.forEach(function(refresh){refresh();});});
  // The Sources rows of Settings use the same calls and the same checks as the setup rows.
  async function connectOne(provider){
    var popup=window.open('about:blank','_blank');
    if(!popup)throw new Error('Allow popups for Grove, then try again.');
    popup.opener=null;
    try{
      var owner=account(),res=await window.GroveLive.postJSON('/v1/views/pipedream/'+encodeURIComponent(provider)+'/connect',{});
      if(!res.ok)throw new Error(error(res,'Could not start the connection. Try again.'));
      if(!res.body||res.body.account!==owner||res.body.provider!==provider)throw new Error('Could not start the connection. Try again.');
      var url=new URL(res.body.connect_url);
      if(url.protocol!=='https:'||url.username||url.password||!(url.hostname==='pipedream.com'||url.hostname.endsWith('.pipedream.com')))throw new Error('Could not start the connection. Try again.');
      popup.location.href=url.href;
    }catch(err){popup.close();throw err;}
  }
  async function disconnectOne(provider){
    var owner=account(),res=await window.GroveLive.postJSON('/v1/views/pipedream/'+encodeURIComponent(provider)+'/disconnect',{confirm:true});
    if(!res.ok)throw new Error(error(res,'Could not disconnect. Try again.'));
    if(!res.body||res.body.account!==owner||res.body.provider!==provider||res.body.status!=='not_connected')throw new Error('Could not disconnect. Try again.');
  }
  window.GrovePipedream={mount:mount,connect:connectOne,disconnect:disconnectOne};
}());
