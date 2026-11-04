/* Account authorization through the server's owner-scoped Pipedream contract.
 * A connection never implies a content import or a completed provider consent. */
(function () {
  'use strict';
  var labels={connected:'Connected',not_connected:'Not connected',attention:'Reconnect required',unavailable:'Status needs a check',checking:'Check in progress'};
  var instances=new Set(), returns=new Set();
  function account(){var s=window.GroveSimulation.getState();return s.auth?s.account:null;}
  function esc(v){return String(v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function slug(v){return typeof v==='string'&&/^[a-z0-9][a-z0-9_-]{0,119}$/.test(v);}
  function error(res,fallback){var e=res.body&&res.body.error;if(res.status===403&&e&&e.code==='scope_denied')return 'Connections are disabled for this prepared demo account. Use your own account to connect apps.';return e&&typeof e.message==='string'?e.message:fallback;}
  function mount(scope){scope.querySelectorAll('[data-pipedream-apps]').forEach(function(root){
    if(root.dataset.pipedreamMounted)return;
    root.dataset.pipedreamMounted='true';
    var owner=account(),generation=0,catalogRequest=0,statusRequest=0,apps=[],states={},selected=null;
    var query='',next=null,total=null,configured=false,busy=false,loading=false,note='',catalogError='',confirm=false,popup=null,timer=null;
    function current(ticket){return !!owner&&root.isConnected&&account()===owner&&ticket===generation;}
    function icon(app){var key={google_calendar:'calendar',google_drive:'drive'}[app.provider]||app.provider;return (window.GroveIcons||{})[key]||'<span class="gp-app-initial">'+esc(app.name.slice(0,2))+'</span>';}
    function cards(){return apps.map(function(app){var status=states[app.provider];return '<button type="button" class="gp-integration '+(status==='connected'?'is-connected':'')+'" data-pipedream-select="'+app.provider+'" aria-label="'+esc(app.name)+' connection" '+(!app.connectable||busy?'disabled':'')+'><span class="gp-integration-mark">'+icon(app)+'</span><span><strong>'+esc(app.name)+'</strong><small>'+esc(app.connectable?(labels[status]||'Check connection'):'No account required')+'</small></span><span class="gp-integration-status">'+(status==='connected'?'✓':'+')+'</span></button>';}).join('');}
    function details(){
      if(!selected)return '';
      var status=states[selected.provider]||'checking';
      // A failed status read must not prevent a new consent attempt. The Connect
      // endpoint validates configuration and account ownership independently.
      var action=status==='connected'?'<button type="button" class="gp-small" data-pipedream-disconnect="'+selected.provider+'" '+(busy?'disabled':'')+'>Disconnect</button>':'<button type="button" class="gp-small" data-pipedream-connect="'+selected.provider+'" '+(!configured||busy||status==='checking'?'disabled':'')+'>'+esc(status==='attention'?'Reconnect ':'Connect ')+esc(selected.name)+'</button>';
      var dialog=confirm?'<section class="gp-pipedream-confirm" role="alertdialog" aria-modal="false" aria-label="Disconnect '+esc(selected.name)+'?"><h3>Disconnect '+esc(selected.name)+'?</h3><p>This removes this account connection. It does not remove existing Grove memories.</p><button type="button" class="gp-small" data-pipedream-cancel '+(busy?'disabled':'')+'>Cancel</button><button type="button" class="gp-small gp-danger" data-pipedream-confirm '+(busy?'disabled':'')+'>Disconnect '+esc(selected.name)+'</button></section>':'';
      return '<section class="gp-pipedream-detail"><button type="button" class="gp-text" data-pipedream-back '+(busy?'disabled':'')+'>All apps</button><div class="gp-setting-row"><span><strong>'+esc(selected.name)+'</strong><small data-pipedream-status="'+selected.provider+'">'+esc(labels[status])+'</small></span>'+action+'</div>'+dialog+'</section>';
    }
    function paint(){
      var focused=root.contains(document.activeElement)&&document.activeElement.hasAttribute('data-pipedream-search');
      var pos=focused?document.activeElement.selectionStart:null;
      root.innerHTML='<label class="gp-integration-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input type="search" maxlength="128" placeholder="Search integrations" aria-label="Search integrations" data-pipedream-search value="'+esc(query)+'" '+(busy?'disabled':'')+'></label>'+details()+'<div class="gp-integrations" data-pipedream-results>'+cards()+'</div>'+(loading?'<p role="status">Loading apps…</p>':'')+(catalogError?'<p role="status">'+esc(catalogError)+'</p><button class="gp-small" data-pipedream-retry>Try again</button>':'')+(!loading&&!catalogError&&!apps.length?'<p>No matching apps. Try another name.</p>':'')+(next?'<button class="gp-small" data-pipedream-more '+(loading||busy?'disabled':'')+'>More apps</button>':'')+(total!==null?'<p class="gp-footnote">'+total.toLocaleString()+' apps in the catalog</p>':'')+'<p class="gp-section-copy">Connect your accounts with Pipedream.</p><p class="gp-section-copy" role="status" aria-live="polite">'+esc(note)+'</p><button type="button" class="gp-small" data-pipedream-refresh '+(busy?'disabled':'')+'>Check connections</button>';
      var input=root.querySelector('[data-pipedream-search]');
      input.oninput=function(e){query=e.target.value;selected=null;confirm=false;catalogRequest++;clearTimeout(timer);timer=setTimeout(function(){loadCatalog(false);},200);};
      if(focused){input.focus();try{input.setSelectionRange(pos,pos);}catch(_) {}}
      root.querySelectorAll('[data-pipedream-select]').forEach(function(button){button.onclick=function(){selected=apps.find(function(app){return app.provider===button.dataset.pipedreamSelect;});confirm=false;refresh();};});
      var button=root.querySelector('[data-pipedream-back]');if(button)button.onclick=function(){selected=null;confirm=false;statusRequest++;paint();};
      button=root.querySelector('[data-pipedream-connect]');if(button)button.onclick=connect;
      button=root.querySelector('[data-pipedream-disconnect]');if(button)button.onclick=function(){confirm=true;paint();root.querySelector('[data-pipedream-cancel]').focus();};
      button=root.querySelector('[data-pipedream-cancel]');if(button)button.onclick=function(){confirm=false;paint();root.querySelector('[data-pipedream-disconnect]').focus();};
      button=root.querySelector('[data-pipedream-confirm]');if(button)button.onclick=disconnect;
      button=root.querySelector('[data-pipedream-more]');if(button)button.onclick=function(){loadCatalog(true);};
      button=root.querySelector('[data-pipedream-retry]');if(button)button.onclick=function(){loadCatalog(false);};
      root.querySelector('[data-pipedream-refresh]').onclick=refresh;
    }
    async function loadCatalog(more){
      var ticket=generation,request=++catalogRequest,cursor=more?next:null;
      if(!current(ticket)||busy)return;
      loading=true;catalogError='';if(!more){apps=[];next=null;}paint();
      try{
        var path='/v1/views/pipedream/apps?q='+encodeURIComponent(query.trim());if(cursor)path+='&cursor='+encodeURIComponent(cursor);
        var res=await window.GroveLive.getJSON(path);
        if(!current(ticket)||request!==catalogRequest)return;
        if(!res.ok)throw new Error(error(res,'The app catalog could not load. Try again.'));
        var body=res.body;
        if(!body||body.account!==owner||!Array.isArray(body.apps)||body.apps.length>30||!(body.next_cursor===null||typeof body.next_cursor==='string'&&body.next_cursor.length<=1024)||!(body.total===null||Number.isSafeInteger(body.total)&&body.total>=0))throw new Error('The app catalog could not load. Try again.');
        var seen={};
        if(!body.apps.every(function(app){if(!app||!slug(app.provider)||seen[app.provider]||typeof app.name!=='string'||!app.name||app.name.length>200||!['oauth','keys','none',null].includes(app.auth_type)||app.connectable!==['oauth','keys'].includes(app.auth_type))return false;seen[app.provider]=true;return true;}))throw new Error('The app catalog could not load. Try again.');
        apps=(more?apps:[]).concat(body.apps.filter(function(app){return !more||!apps.some(function(old){return old.provider===app.provider;});}));
        next=body.next_cursor;total=body.total;configured=body.configured===true;
      }catch(err){if(current(ticket)&&request===catalogRequest)catalogError=err.message||'The app catalog could not load. Try again.';}
      finally{if(current(ticket)&&request===catalogRequest){loading=false;paint();}}
    }
    async function refresh(){
      var ticket=generation,request=++statusRequest,app=selected;
      if(!current(ticket)||busy)return;
      if(app)states[app.provider]='checking';note='Check in progress.';paint();
      try{
        var res=await window.GroveLive.getJSON('/v1/views/pipedream/accounts'+(app?'?provider='+encodeURIComponent(app.provider):''));
        if(!current(ticket)||request!==statusRequest)return;
        if(!res.ok)throw new Error(error(res,'Connection status could not be checked. Try again.'));
        if(!res.body||res.body.account!==owner||!Array.isArray(res.body.providers))throw new Error('Connection status could not be checked. Try again.');
        if(app&&!res.body.providers.some(function(row){return row.provider===app.provider;}))throw new Error('Connection status could not be checked. Try again.');
        res.body.providers.forEach(function(row){if(slug(row.provider)&&['connected','not_connected','attention','unavailable'].includes(row.status))states[row.provider]=row.status;});
        configured=res.body.configured===true;note=configured?'Status checked with Pipedream.':'';
      }catch(err){if(current(ticket)&&request===statusRequest){if(app)states[app.provider]='unavailable';note=err.message||'Connection status could not be checked. Try again.';}}
      finally{if(current(ticket)&&request===statusRequest)paint();}
    }
    async function connect(){
      var app=selected,ticket=generation;if(!app||!configured||busy||!current(ticket))return;
      popup=window.open('about:blank','_blank');if(!popup){note='Allow popups for Grove, then select Connect again.';paint();return;}popup.opener=null;
      var opened=popup;busy=true;confirm=false;statusRequest++;note='Open account authorization with Pipedream.';paint();
      try{
        var res=await window.GroveLive.postJSON('/v1/views/pipedream/'+encodeURIComponent(app.provider)+'/connect',{});
        if(!current(ticket)){opened.close();return;}
        if(!res.ok)throw new Error(error(res,'Could not start the connection. Try again.'));
        if(!res.body||res.body.account!==owner||res.body.provider!==app.provider)throw new Error('Could not start the connection. Try again.');
        var url=new URL(res.body.connect_url);
        if(url.protocol!=='https:'||url.username||url.password||!(url.hostname==='pipedream.com'||url.hostname.endsWith('.pipedream.com')))throw new Error('Could not start the connection. Try again.');
        opened.location.href=url.href;note='Complete authorization in the new tab, then select Check connections.';
      }catch(err){opened.close();if(current(ticket))note=err.message||'Could not start the connection. Try again.';}
      finally{if(current(ticket)){busy=false;paint();}}
    }
    async function disconnect(){
      var app=selected,ticket=generation;if(!app||!confirm||busy||!current(ticket))return;
      busy=true;statusRequest++;note='Disconnect in progress.';paint();
      try{
        var res=await window.GroveLive.postJSON('/v1/views/pipedream/'+encodeURIComponent(app.provider)+'/disconnect',{confirm:true});
        if(!current(ticket))return;
        if(!res.ok)throw new Error(error(res,'Could not confirm disconnection. Check connections again.'));
        if(!res.body||res.body.account!==owner||res.body.provider!==app.provider||res.body.status!=='not_connected')throw new Error('Could not confirm disconnection. Check connections again.');
        states[app.provider]='not_connected';note='Account disconnected. Existing Grove memories stay.';
      }catch(err){if(current(ticket)){states[app.provider]='unavailable';note=err.message||'Could not confirm disconnection. Check connections again.';}}
      finally{if(current(ticket)){busy=false;confirm=false;paint();}}
    }
    function reset(force){
      if(!force&&root.isConnected&&account()===owner)return;
      generation++;catalogRequest++;statusRequest++;clearTimeout(timer);
      if(popup&&!popup.closed)popup.close();
      apps=[];states={};selected=null;configured=false;busy=false;confirm=false;next=null;total=null;query='';
      returns.delete(onReturn);
      if(!root.isConnected){instances.delete(reset);return;}
      owner=null;note='Your account changed. Reload Grove.';paint();
    }
    root.addEventListener('keydown',function(event){if(event.key==='Escape'&&confirm){event.stopPropagation();if(!busy){confirm=false;paint();}}});
    function onReturn(){if(popup&&!busy&&current(generation))refresh();}
    instances.add(reset);returns.add(onReturn);paint();loadCatalog(false);refresh();
  });}
  ['grove:change','grove:unauthenticated','grove:session-changed'].forEach(function(event){window.addEventListener(event,function(){instances.forEach(function(reset){reset(event!=='grove:change');});});});
  window.addEventListener('focus',function(){returns.forEach(function(refresh){refresh();});});
  window.GrovePipedream={mount:mount};
}());
