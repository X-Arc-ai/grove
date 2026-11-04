/* Grove product journey. Every overlay uses the native graph's typography and tokens. */
(function () {
  'use strict';
  var api = window.GroveSimulation;
  var demo = !!window.GroveDemo;
  if (!api) return;
  // Live mode (no injected fixture): the session, sign-in and logout go to the server (web/live.js).
  var live = api.isLive && api.isLive() && window.GroveLive ? window.GroveLive : null;
  var SIGNIN_COPY = {
    email: 'We could not send a code right now. Try again.',
    code: 'That code is invalid or expired. Request a new code and try again.',
    offline: 'Grove could not be reached. Try again in a moment.',
    logout: 'Grove could not sign you out. Try again.',
    google: 'Google sign-in did not finish. Try again.',
    tooMany: 'Please wait a few minutes before you request another code.'
  };
  var icons = window.GroveIcons || {};
  var ui = { step: api.getState().onboardingStep || 0, panel: null, guide: null, guideStep:0, tour: 0, tourNode: null, error: '', host: null, hostStage: 0 };
  var liveUploads = {};
  var liveData = {owner:null};
  var liveSessionReady = false;
  function refreshLiveData(force){
    if(!live)return;
    var state=api.getState(),owner=state.auth&&state.account;
    if(liveData.owner!==owner)liveData={owner:owner};
    if(!owner||!liveSessionReady||(!force&&liveData.started))return;
    var request=liveData={owner:owner,started:true,pending:true,sources:null,connections:null,health:null,checkedAt:null};
    function current(){var now=api.getState();return request===liveData&&now.auth&&now.account===owner;}
    var reminderContext=api.pendingExportContext();
    api.refreshPendingExports().then(function(){
      if(current()&&api.isPendingExportContext(reminderContext)){ui.reminderError='';render(true);}
    }).catch(function(){
      if(current()&&api.isPendingExportContext(reminderContext)){ui.reminderError='Grove could not load your export reminders. Try again.';render(true);}
    });
    var reads=[['sources',live.ENDPOINTS.sourceCoverage,'connectors'],['connections',live.ENDPOINTS.sourceConnections,'connections'],['health',live.ENDPOINTS.health,null]];
    Promise.all(reads.map(function(row){return live.getJSON(row[1]).then(function(res){
      if(!current()||!res.ok||!res.body)return;
      var value=row[2]?res.body[row[2]]:res.body;
      if(row[2]?!Array.isArray(value):!Number.isFinite(value.nodes)||!Number.isFinite(value.edges))return;
      request[row[0]]=value;
    }).catch(function(){});})).then(function(){if(current()){request.pending=false;request.checkedAt=new Date().toLocaleTimeString();render(true);}});
  }
  function coverageMessage(){return liveData.pending?'Check in progress':liveData.sources===null?'Source information could not be read. Try again.':'';}
  function liveSourceAccounts(){
    return (liveData.sources||[]).map(function(row){
      var id={claude_ai:'claude',whatsapp:'chats',google_calendar:'calendar'}[row.provider]||row.provider;
      var connection=(liveData.connections||[]).find(function(c){return c.id===row.connector_id;});
      return {id:id,label:PROVIDERS[id]?PROVIDERS[id].label:row.provider,kind:['notes','files','images'].includes(row.type)?'file':'account',live:true,readCount:row.read_count,outcome:row.last_outcome,status:connection?connection.state:'uploaded',lastRead:row.last_successful_read};
    });
  }
  var root = document.createElement('div'); root.id = 'grove-product'; document.body.appendChild(root);
  var modalRoot = document.createElement('div'); modalRoot.id='gp-modal-root'; root.appendChild(modalRoot);
  var chrome = document.createElement('div'); chrome.id='gp-chrome'; root.appendChild(chrome);
  var tourRoot = document.createElement('div'); tourRoot.id='gp-tour-root'; root.appendChild(tourRoot);
  ui.editName=false;ui.nameDraft='';ui.guideStage=0;ui.uploadFile=null;ui.settingsTab='account';ui.authStage=api.getState().auth?null:'signin';ui.requestId=null;ui.requestTab='open';ui.requestDrafts={};ui.guideDevice='iphone';ui.email='';ui.code='';ui.query='';ui.connecting={};ui.keyApp=null;ui.sourceTab='chats';ui.filePicker=false;ui.noteEditor=false;ui.confirm=null;ui.notice='';ui.growthDraft=null;ui.noteDraft={title:'',body:''};
  var lastShape='', lastTourShape='', moving=false, tourAfter=0, arrivalTimer=null, uiEpoch=0, authEpoch=0, signinError='';
  function accountMode(){return live&&window.location.pathname===live.PAGES.signup?'signup':'login';}
  function logoutPage(){return live&&window.location.pathname===live.PAGES.logout;}
  var progress=document.createElement('div');progress.id='gp-onboarding-progress';progress.setAttribute('aria-live','polite');root.appendChild(progress);
  // Live mode, signed in here while the session check failed: the offline text, over the map and the loading cover.
  var sessionNote=document.createElement('p');sessionNote.id='gp-session-note';sessionNote.setAttribute('role','alert');sessionNote.hidden=true;document.body.appendChild(sessionNote);
  var reduceMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
  var PROVIDERS = {
    chatgpt: {label:'ChatGPT',color:'var(--text)',source:'conversations',accept:'.zip,.json',file:'chatgpt-export.zip',steps:[['Open Settings','In ChatGPT, open your profile menu, then Settings.'],['Export your data','Choose Data Controls → Export data → Confirm export.'],['Bring the export here','Download the ZIP from the email, then choose it below.']]},
    claude: {label:'Claude',color:'#d97757',source:'conversations',accept:'.zip,.json',file:'claude-export.zip',steps:[['Open Settings','In Claude, open your profile menu, then Settings.'],['Request your export','Choose Privacy → Export data.'],['Bring the export here','Download the ZIP from the email, then choose it below.']]},
    muse: {label:'Muse',color:'var(--text)',source:'conversations',accept:'.zip,.json',file:'muse-context-sample.json',steps:[]},
    grok: {label:'Grok',color:'var(--text)',source:'conversations',accept:'.zip,.json',file:'grok-export.zip',steps:[['Open your account','Open Settings in Grok.'],['Request an export','Open Data Controls and request a download of your data.'],['Bring the export here','Download your archive when it is ready, then choose it below.']]},
    chats: {label:'WhatsApp',color:'#25a85a',source:'chats',accept:'.zip,.txt',file:'whatsapp-chat.zip',steps:[['Open a conversation','Choose the conversation you want your Grove to remember.'],['Export the chat','Open the chat options and choose Export chat, without media.'],['Bring the export here','Save the text file or ZIP, then choose it below.']]},
    notes: {label:'Notes & files',color:'#d4a52a',source:'notes',accept:'.pdf,.docx,.txt,.md,.zip',file:'work-notes.zip',steps:[['Choose what matters','A project brief, meeting notes or a document you return to.'],['Keep its original format','PDF, text, Markdown and document files are welcome.'],['Bring it into your brain','Choose a file below. You can always add more later.']]}
  };
  Object.assign(PROVIDERS,{
    gmail:{label:'Gmail',source:'conversations',color:'var(--text)'},
    drive:{label:'Google Drive',source:'notes',color:'var(--text)'},
    calendar:{label:'Calendar',source:'calendar',color:'var(--text)'},
    notion:{label:'Notion',source:'notes',color:'var(--text)'},
    linear:{label:'Linear',source:'notes',color:'var(--text)'},
    slack:{label:'Slack',source:'chats',color:'var(--text)'},
    microsoft:{label:'Microsoft 365',source:'notes',color:'var(--text)'}
  });
  var AI_PROVIDERS=['muse','chats','chatgpt','claude','grok'];
  var HOST_ORDER=['muse','chatgpt','claude','grok'];
  var catalog=window.GroveIntegrations||[];
  catalog.forEach(function(a){if(!PROVIDERS[a.id])PROVIDERS[a.id]={label:a.name,source:/Communication/.test(a.category)?'chats':'notes',color:'var(--text)'};});
  function appMark(id){return icons[id]?icon(id):'<span class="gp-app-initial">'+esc((PROVIDERS[id]&&PROVIDERS[id].label||id).slice(0,2))+'</span>';}
  function accountActive(id){var a=api.getState().accountImports[id];return a&&a.status!=='disconnected';}

  function wordmark(){return '<span class="wm gp-wordmark"><span>grove</span><span class="wm-dot" aria-hidden="true"></span></span>';}
  function pendingExports(s){return Object.keys(s.pendingExports||{}).filter(function(id){return !!PROVIDERS[id];});}
  function needsHistory(s){if(live)return pendingExports(s).length>0||!liveSourceAccounts().some(function(a){return AI_PROVIDERS.includes(a.id)&&a.readCount>0;});return pendingExports(s).length>0||!s.sources.some(function(so){return (so.providers||[]).some(function(id){return AI_PROVIDERS.includes(id);});});}
  function esc(x){return String(x==null?'':x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function icon(id){if(id==='muse'&&!icons.muse)return '';return icons[id] || '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';}
  function arrow(){return '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';}
  function exploreArrow(){return '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 15 15 5M5 5h10v10" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';}
  function close(){return '<button class="gp-icon" data-action="close" aria-label="Close popup">×</button>';}
  function source(id){return api.getState().sources.find(function(s){return s.id===id;}) || {};}
  function sourceState(id){var s=source(id);return s.status==='importing'?'Learning · '+s.progress+'%':s.status==='ready'?'Added to your Grove':'Not added';}
  function sourceLabel(s){return s.provider && PROVIDERS[s.provider] ? PROVIDERS[s.provider].label : s.label;}
  function shell(content,cls,modal){return '<div class="gp-backdrop '+(modal?'is-centered':'')+'"><section class="gp-dialog '+(cls||'')+'" role="dialog" aria-modal="true" aria-labelledby="gp-title">'+content+'</section></div>';}
  function heading(kicker,title,copy){title=title.replace(/\.$/,'');copy=copy&&copy.replace(/\.$/,'');return '<div class="gp-heading"><p class="gp-kicker">'+kicker+'</p><h1 id="gp-title" tabindex="-1">'+title+'</h1>'+(copy?'<p class="gp-copy">'+copy+'</p>':'')+'</div>';}
  function welcome(){return '<div class="gp-welcome-art" aria-hidden="true">'+(window.GroveEntryVisual||'').replace('class="gp-entry-canvas"','class="gp-entry-canvas" data-scene="welcome"')+'</div>'+shell(heading('', wordmark()+'<br>starts with you','Bring your world together, one source at a time')+'<button class="gp-primary gp-begin" data-action="start">Get Started</button>','gp-welcome',true);}
  function signIn(){
    var code=ui.authStage==='code',signup=accountMode()==='signup',checking=ui.authStage==='checking';
    var initialLogin=!code&&!signup&&!checking;
    var title=signup?'Create account':'Log in';
    var body=checking?'<div class="gp-auth-loading" role="status" aria-label="Checking your sign-in"><span></span><span></span><span></span></div>':code?'<div class="gp-auth-intro"><h2>Check your inbox</h2><p>If a code arrives at '+esc(ui.email)+', enter it below.</p></div><form data-code-form><label for="gp-code">Verification code</label><input id="gp-code" class="gp-code-input" name="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" aria-label="Six digit code" placeholder="000000" value="'+esc(ui.code)+'" required><button class="gp-primary gp-wide">'+title+'</button></form><button class="gp-text" data-action="auth-back">Use another email</button>':(signup?'<div class="gp-auth-intro"><h2>'+title+'</h2><p>Verify your email or Google account to start your Grove.</p></div>':'')+'<div class="gp-signin-options"><button data-signin="google">'+icon('google')+'Continue with Google</button><button data-signin="apple" disabled aria-disabled="true">'+icon('apple')+'Continue with Apple</button></div><div class="gp-auth-divider"><span>or</span></div><form data-email-form><label for="gp-email">Email address</label><input id="gp-email" name="email" type="email" autocomplete="email" placeholder="you@example.com" value="'+esc(ui.email)+'" required><button class="gp-primary gp-wide">Continue with email</button></form>';
    var navigation=live&&!checking?'<nav class="gp-auth-navigation" aria-label="Account access">'+(signup?'<a class="gp-text" href="'+esc(live.authPagePath(live.PAGES.login))+'">Already have an account? Log in</a>':'')+'<a class="gp-text" href="'+live.PAGES.privacy+'">Privacy</a></nav>':'';
    return '<div class="gp-backdrop gp-entry-backdrop"><section class="gp-entry" role="dialog" aria-modal="true" '+(initialLogin?'id="gp-title" tabindex="-1" aria-label="Log in"':'aria-labelledby="gp-title"')+'><div class="gp-entry-art" aria-hidden="true">'+(window.GroveEntryVisual||'')+'</div><div class="gp-auth">'+(initialLogin?'<header class="gp-auth-brand">'+wordmark()+'</header>':'<header class="gp-auth-brand"><h1 id="gp-title" tabindex="-1">'+wordmark()+'</h1><p>Give your AI a world to understand</p></header>')+'<div class="gp-auth-form">'+body+'</div>'+navigation+'</div></section></div>';
  }
  function logoutConfirmation(){return shell(heading('', 'Log out?', 'Your Grove stays saved to your account.')+'<button class="gp-primary gp-wide" data-action="logout">Log out</button><a class="gp-text gp-wide" href="'+live.PAGES.home+'">Stay in Grove</a>'+(ui.logoutError?'<p class="gp-footnote" role="alert">'+esc(ui.logoutError)+'</p>':''),'gp-logout',true);}
  // The example address is fixture mode only. Live mode knows the typed address for the email
  // code and none for Google (the server holds it), so the account shows no made-up address.
  function signInEmail(provider){return live?(provider==='email'?ui.email:''):(ui.email||'anas@example.com');}
  function setAddress(url){try{if(window.history&&window.history.replaceState)window.history.replaceState(null,'',url);}catch(_){/* a sandbox or a file page keeps its address */}}
  // Live mode: what a signed-in answer of the server (verify, session) settles here. Another account
  // than the stored one resets the local state first. A world that holds items, on a browser with no
  // onboarding in progress, opens on the map. Says whether it did. Runs twice with no second effect.
  function arrive(body){
    liveSessionReady=true;
    var previousAccount=api.getState().account;
    if(api.bindAccount(body.account)){
      if(window.GroveMaterials)window.GroveMaterials.reset();
      if(previousAccount&&previousAccount!==body.account)live.notifySessionChange();
      liveUploads={};ui.reminderRequest=null;ui.reminderError='';
      ui.step=0;ui.guideStage=0;ui.editName=false;ui.nameDraft='';ui.growthDraft=null;ui.noteDraft={title:'',body:''};ui.requestDrafts={};ui.uploadFile=null;ui.connecting={};ui.query='';ui.notice='';
      if(window.GroveView&&window.GroveView.clear)window.GroveView.clear();
    }
    if(live.isAuthPage())setAddress(live.PAGES.home);
    var s=api.getState();
    if(!body.has_items||s.onboardingComplete||s.onboardingStep)return false;
    api.resumeOnboarding();return true;
  }
  function finishSignIn(provider,body){authEpoch++;ui.error='';signinError='';if(live)arrive(body||{});api.signIn(provider,signInEmail(provider));ui.authStage=null;ui.panel=null;if(api.getState().onboardingComplete){render(true);if(live&&window.reloadGrove)window.reloadGrove();}else setStep(api.getState().onboardingStep||0);}
  // Sign-in calls. Live mode: Google signs in on its own pages; email posts the address, then
  // the code; a failure shows in the sign-in error slot. Fixture mode (the checks) has no server:
  // any six-digit code is accepted. Apple has no sign-in yet (decision 24).
  function chooseSignIn(provider){
    if(provider==='apple'){ui.appleNote=true;render(true);return;}
    if(live&&provider==='google'){live.googleSignIn();return;}
    finishSignIn(provider);
  }
  function startEmail(email){
    ui.error='';ui.email=email;ui.code='';
    var epoch=++authEpoch;
    if(!live){ui.authStage='code';render(true);return Promise.resolve();}
    return live.emailStart(email).then(function(res){if(epoch!==authEpoch)return;if(res.ok){ui.authStage='code';}else{ui.error=res.status===429?SIGNIN_COPY.tooMany:SIGNIN_COPY.email;}render(true);},function(){if(epoch!==authEpoch)return;ui.error=SIGNIN_COPY.offline;render(true);});
  }
  function verifyEmail(code){
    code=String(code||'').trim();
    var epoch=++authEpoch;
    if(!live){if(/^[0-9]{6}$/.test(code))finishSignIn('email');else{ui.error=SIGNIN_COPY.code;render(true);}return Promise.resolve();}
    // A sign-in that the consent page asked for goes back to it (live.js checks the next).
    return live.emailVerify(ui.email,code).then(function(res){if(epoch!==authEpoch||res.stale)return;if(res.ok){if(!live.returnToNext())finishSignIn('email',res.body);}else{ui.error=SIGNIN_COPY.code;render(true);}},function(){if(epoch!==authEpoch)return;ui.error=SIGNIN_COPY.offline;render(true);});
  }
  // Live mode, on load, the way back from a Google sign-in that did not finish: the query leaves the
  // address, and the error waits for the card (a session check that succeeds drops it).
  function readSigninError(){
    var query=new URLSearchParams(window.location.search||'');
    if(!query.has('signin_error'))return;
    if(query.get('signin_error')==='google')signinError=SIGNIN_COPY.google;
    query.delete('signin_error');
    setAddress((window.location.pathname||live.PAGES.home)+(query.toString()?'?'+query.toString():''));
  }
  function showSigninError(){if(signinError){ui.error=signinError;signinError='';}}
  // Live mode, on load: the server session decides between the sign-in screen and the views.
  // A session with no local sign-in is the way back from Google, or a session another page began;
  // its answer names the method. Only a 401 ends the local sign-in.
  // Any other failure says nothing about the session: the local state stays and the offline text
  // shows. With no local sign-in the card shows it, and no graph of an unknown session stays behind.
  function sessionUnknown(){
    if(api.getState().auth)ui.offline=true;
    else if(ui.authStage==='checking'){endSession();ui.authStage='signin';ui.error=SIGNIN_COPY.offline;}
    else return;
    render(true);
  }
  function checkSession(){
    var epoch=authEpoch;
    return live.session().then(function(res){
      if(epoch!==authEpoch||res.stale)return;
      if(res.ok&&res.body&&res.body.signed_in){
        // Signed in already on a sign-in page the consent page sent here: straight back to it.
        if(live.returnToNext())return;
        var resumed=arrive(res.body),waiting=ui.authStage==='checking';
        if(!api.getState().auth){finishSignIn(res.body.method||'google',res.body);return;}
        signinError='';if(waiting)ui.authStage=null;if(waiting||resumed)render(true);if(resumed&&window.reloadGrove)window.reloadGrove();
        return;
      }
      if(res.status!==401){sessionUnknown();return;}
      if(api.getState().auth)live.notifySessionChange();
      endSession();
      ui.authStage='signin';showSigninError();render(true);
    },sessionUnknown);
  }
  // Live mode, the session is over (a confirmed logout, or a 401 from a live call): the local
  // sign-in ends, the graph leaves memory and the screen, panels close and the sign-in card shows.
  // A person already on the sign-in card (checking, the email code) keeps the step they are on.
  function endSession(){
    if(window.GroveMaterials)window.GroveMaterials.reset();
    authEpoch++;live.invalidateSession();
    liveSessionReady=false;liveData={owner:null};
    liveUploads={};ui.reminderRequest=null;ui.reminderError='';
    var onCard=!api.getState().auth&&!!ui.authStage;
    api.signOut();
    ui.step=0;ui.guideStage=0;ui.editName=false;ui.nameDraft='';ui.growthDraft=null;
    ui.noteDraft={title:'',body:''};ui.requestDrafts={};ui.uploadFile=null;ui.connecting={};ui.query='';ui.notice='';
    if(window.GroveView&&window.GroveView.clear)window.GroveView.clear();
    if(!live.isAuthPage())setAddress(live.PAGES.login);
    if(onCard){if(ui.authStage==='checking')ui.authStage='signin';showSigninError();render(true);return;}
    endTour();ui.panel=null;ui.guide=null;ui.confirm=null;ui.host=null;ui.authStage='signin';ui.error='';ui.email='';ui.code='';ui.logoutError='';ui.offline=false;showSigninError();
    render(true);
  }
  // Logout signs out here only when the server confirms it, or when the session is already gone.
  function logout(){
    if(window.GroveMaterials)window.GroveMaterials.reset();
    authEpoch++;
    ui.logoutError='';
    var failed=function(){ui.logoutError=SIGNIN_COPY.logout;render(true);};
    var gone=function(){return live.session().then(function(res){if(res.status===401)endSession();else failed();},failed);};
    return live.logout().then(function(res){if(res.ok)endSession();else return gone();},gone);
  }

  function stepper(){return '<div class="gp-stepper" aria-label="Step '+ui.step+' of 4"><span class="gp-step-number">0'+ui.step+' <em>/ 04</em></span><div class="gp-step-track" aria-hidden="true">'+[1,2,3,4].map(function(n){return '<i class="'+(ui.step>=n?'is-done':'')+'"></i>';}).join('')+'</div></div>';}
  function providerButton(id){var p=PROVIDERS[id],so=source(p.source),added=(so.providers||[so.provider]).includes(id)&&so.status!=='idle';return '<button class="gp-provider-row" data-guide="'+id+'"><span class="gp-provider-icon" style="color:'+p.color+'">'+icon(id)+'</span><span><strong>'+p.label+'</strong><small data-provider-state="'+id+'">'+(added?sourceState(p.source):'')+'</small></span>'+arrow()+'</button>';}
  function sourceChoice(id,label){var p=PROVIDERS[id],src=p?p.source:id,so=source(src),added=id==='calendar'?so.status!=='idle':(so.providers||[so.provider]).includes(id)&&so.status!=='idle';return '<button class="gp-source-choice '+(added?'is-added':'')+'" '+(id==='calendar'?'data-action="calendar"':'data-guide="'+id+'"')+' data-source-choice="'+id+'" data-hover-source="'+id+'"><span class="gp-source-orb"><svg class="gp-source-progress" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="30"/></svg><span class="gp-choice-mark" style="color:'+(p?p.color:'var(--text)')+'">'+icon(id)+'</span><i class="gp-source-check" aria-hidden="true">✓</i></span><strong><small>'+(id==='notes'?'Add':'Connect')+'</small>'+label+'</strong></button>';}
  function integrationResults(){
    var q=ui.query.trim().toLowerCase(),rows;
    if(q)rows=catalog.filter(function(a){return (a.name+' '+a.category).toLowerCase().includes(q);}).slice(0,30).map(function(a){return [a.id,a.name,a.category];});
    else rows=[['google','Google','Gmail, Drive, Calendar'],['notion','Notion','Notes & knowledge'],['slack','Slack','Team conversations'],['microsoft','Microsoft 365','Mail, files & teams'],['linear','Linear','Projects & tasks'],['dropbox','Dropbox','Files & folders']];
    return rows.length?rows.map(function(x){var active=x[0]==='google'?api.getState().googleConnected:accountActive(x[0]);return '<button class="gp-integration '+(active?'is-connected':'')+'" data-integration="'+x[0]+'" aria-label="'+(active?(demo?'Simulated connection to ':'Connected to '):(demo?'Simulate ':'Connect '))+esc(x[1])+'"><span class="gp-integration-mark">'+appMark(x[0])+'</span><span><strong>'+esc(x[1])+'</strong><small>'+esc(x[2])+'</small></span><span class="gp-integration-status">'+(ui.connecting[x[0]]?'…':active?'✓':'+')+'</span></button>';}).join(''):'<p class="gp-empty-sources">No matches — try another name</p>';
  }
  function loadViewAsset(name){
    var key=name==='guides'?'guide':'catalog';
    if(ui[key+'Loading']||ui[key+'Error'])return;
    ui[key+'Loading']=true;
    window.GroveDeferred.load(name).then(function(){
      if(name==='catalog'){catalog=window.GroveIntegrations||[];catalog.forEach(function(a){if(!PROVIDERS[a.id])PROVIDERS[a.id]={label:a.name,source:'notes',color:'var(--text)'};});}
    },function(){ui[key+'Error']='This view could not load. Try again.';}).finally(function(){ui[key+'Loading']=false;render(true);});
  }
  function assetNotice(name){var key=name==='guides'?'guide':'catalog';return '<p class="gp-section-copy" role="status">'+(ui[key+'Error']?esc(ui[key+'Error']):'Loading '+(name==='guides'?'your guide':'apps')+'…')+'</p>'+(ui[key+'Error']?'<button class="gp-small" data-retry-asset="'+name+'">Try again</button>':'');}
  function integrationChoices(){if(live)return '<div data-pipedream-apps></div>';if(!window.GroveIntegrations&&window.GroveDeferred){loadViewAsset('catalog');return assetNotice('catalog');}if(live)return '<div data-pipedream-apps></div>';return '<label class="gp-integration-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input type="search" placeholder="Search 3,000+ integrations" aria-label="Search 3,000+ integrations" data-integration-search value="'+esc(ui.query)+'"></label><div class="gp-integrations" id="gp-integration-results">'+integrationResults()+'</div><div id="gp-inline-auth"></div>';}
  function hostChoices(){var hosts=live||demo?[]:api.getState().connectedHosts;return '<div class="gp-host-choices">'+HOST_ORDER.map(function(id){var done=hosts.includes(id);return '<button class="gp-host-choice '+(done?'is-connected':'')+'" data-host-inline="'+id+'" aria-label="'+(live?'Set up Grove in ':demo?'View setup guide for ':done?'Disconnect Grove from ':'Connect Grove to ')+PROVIDERS[id].label+'"><span class="gp-provider-icon">'+icon(id)+'</span><span><strong>'+PROVIDERS[id].label+'</strong><small>'+(live||demo?'View setup directions':done?'Grove is connected':'Connect Grove')+'</small></span><span class="gp-host-status">'+(ui.connecting['host:'+id]?'…':done?'✓':'+')+'</span></button>';}).join('')+'</div>';}
  function stepContent(){
    if(ui.step===1)return heading('','Start with your conversations','Give Grove the context behind how you think')+'<div class="gp-source-choices">'+AI_PROVIDERS.map(function(id){return sourceChoice(id,PROVIDERS[id].label);}).join('')+'</div>';
    if(ui.step===2)return heading('','Bring your world closer','Keep your Grove in step with your everyday life')+integrationChoices();
    if(ui.step===3)return heading('','Add what matters','The knowledge and ideas you want your AI to know')+filesComposer();
    return heading('','Take Grove with you','Let your favorite AI pick up where you left off')+hostChoices()+customMcp();
  }
  function setup(){return shell(stepper()+stepContent()+'<div class="gp-step-footer">'+(ui.step>1?'<button class="gp-back" data-action="back" aria-label="Previous step">'+arrow()+'</button>':'<span aria-hidden="true"></span>')+(ui.step<4?'<button class="gp-text" data-action="skip">Skip for now</button>':'')+'<button class="gp-next" data-action="next" aria-label="'+(ui.step===4?'Enter your Grove':'Next step')+'"><i>'+arrow()+'</i></button></div>','gp-setup',false);}
  function move(action){action();var el=modalRoot.querySelector('.gp-dialog');if(el&&!reduceMotion.matches)el.animate([{opacity:0},{opacity:1}],{duration:240,easing:'ease-out'});}


  var WALKS={
    chatgpt:[['chatgpt-home','Open ChatGPT and click your profile.'],['chatgpt-menu','Open Settings.'],['chatgpt-data','Choose Data controls, then Export data.'],['chatgpt-confirm','Click Confirm export. Your download arrives by email.']],
    claude:[['claude-home','Open Claude on the web and click your profile.'],['claude-menu','Open Settings.'],['claude-privacy','Choose Privacy, then Export data. Your download arrives by email.']]
  };
  var HELP={chats:'https://faq.whatsapp.com/1180414079177245/',chatgpt:'https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data',claude:'https://support.claude.com/en/articles/9450526-export-your-claude-data',grok:'https://x.ai/legal/faq'};
  function liveUpload(id){
    var account=JSON.stringify(api.pendingExportContext());
    if(!liveUploads[id]||liveUploads[id].account!==account)liveUploads[id]={account:account,file:null,pending:false,result:null,error:''};
    return liveUploads[id];
  }
  function uploadResult(result){
    if(!result)return '';
    var labels={taken_in:'Import accepted',nothing_new:'No new messages',kept_out:'No retained messages',failed:'Import failed'};
    var counts=[['chats','Chats read'],['chats_new','New chats'],['sources_added','Sources added'],['episodes_added','Episodes added'],['messages_read','Messages read'],['messages_kept','Messages kept'],['failed_chats','Failed chats']];
    return '<div class="gp-upload-result" role="status"><p><strong>'+esc(labels[result.state]||'Upload result')+'</strong></p>'+(result.failed_chats?'<p>Some chats failed. Keep this export and retry the upload.</p>':'')+'<p>'+counts.map(function(row){return row[1]+': '+esc(result[row[0]]);}).join(' · ')+'</p>'+Object.keys(result.screened||{}).map(function(key){return '<p>Screened '+esc(key)+': '+esc(result.screened[key])+'</p>';}).join('')+'<p>Your graph has not changed from this upload.</p></div>';
  }
  function canUpload(id){return !live||id==='chatgpt'||id==='claude';}
  function exportPicker(id){
    var p=PROVIDERS[id],state=live?liveUpload(id):{file:ui.uploadFile},ready=canUpload(id);
    var input=live&&ready?'<input type="file" accept="'+p.accept+'" data-pick-export="'+id+'" aria-label="'+p.label+' export file" hidden>':'';
    return (live&&ready?'<p class="gp-section-copy">Upload export opens a simulation with invented data. Your file will not be uploaded or saved.</p>':'')+input+'<button class="gp-upload-zone" data-action="pick-export"'+(!ready||state.pending?' disabled':'')+'><span>'+icon('notes')+'</span><strong>'+(state.file?esc(state.file.name):id==='notes'?'Choose a file':demo?'Use sample export':'Choose your export')+'</strong><small>'+(state.pending?'Upload in progress':state.file?'Ready to upload':id==='notes'?'Notes and documents':'Your conversations, ready for Grove')+'</small></button>'+(live&&state.pending?'<p class="gp-section-copy" role="status" aria-live="polite">Upload in progress</p>':'')+(live&&state.error?'<p class="gp-section-copy" role="alert">'+esc(state.error)+'</p>':'')+(live?uploadResult(state.result):'');
  }
  function guide(){
    var id=ui.guide,p=PROVIDERS[id],walk=WALKS[id],upload=ui.guideStage===1||id==='notes';
    if(id==='google'||['notion','linear','slack','microsoft'].includes(id))return integrationGuide(id);
    var title=upload?'Upload your '+(id==='notes'?'files':p.label+' history'):id==='muse'?'Connect Muse':'Export your '+p.label+' history';
    var help=!upload&&HELP[id]?'<a class="gp-help" href="'+HELP[id]+'" target="_blank" rel="noreferrer" aria-label="Official '+p.label+' export instructions" title="Official instructions">↗</a>':'';
    var header='<header class="gp-guide-heading"><span class="gp-provider-icon" style="color:'+p.color+'">'+icon(id)+'</span><h1 id="gp-title" tabindex="-1">'+title+help+'</h1><button class="gp-icon" data-action="guide-back" aria-label="Close guide">×</button></header>';
    if(!window.GroveAppScreens&&window.GroveDeferred){
      loadViewAsset('guides');
      var pending=assetNotice('guides')+'<footer class="gp-guide-footer"><button class="gp-text" data-action="guide-back">Back</button></footer>';
      return api.getState().onboardingComplete?settingsFrame(pending,'sources',sourceTrail(id,upload)):shell(header+pending,'gp-guide gp-visual-guide',false);
    }
    var frames=upload?[{html:transferFilm(id),label:id==='chats'?'Save or share your WhatsApp export, then bring it to Grove':'Download your export and upload it to Grove'}]:walk?walk.map(function(step){return {html:window.GroveAppScreens[step[0]],label:step[1].replace(/\.$/,'')};}):[];
    if(!upload&&!walk&&p.steps.length&&id!=='notes')frames=p.steps.slice(0,2).map(function(step,i){return {html:'<div class="film-instruction"><div class="film-logo">'+icon(id)+'</div><span>0'+(i+1)+'</span><h2>'+step[0]+'</h2><p>'+step[1].replace(/\.$/,'')+'</p></div>',label:step[0]};});
    if(!frames.length)frames.push({html:transferFilm(id),label:'Bring your context to Grove'});
    if(id==='chats'&&!upload)frames=window.GroveWhatsAppGuide.frames(ui.guideDevice);
    var frameSeconds=id==='chats'&&!upload?5:!upload&&(id==='chatgpt'||id==='claude')?2.2:3.6,duration=frames.length*frameSeconds,css=window.GroveAppScreenCSS+(window.GroveWhatsAppGuide?window.GroveWhatsAppGuide.css:'')+filmCSS(frames.length,duration)+(upload?'html,body,.film-stage,.film-transfer{height:360px}':'');
    var doc='<!doctype html><html><head><meta charset="utf-8"><style>'+css+'</style></head><body><div class="film-stage" data-frame-seconds="'+frameSeconds+'" data-frame-count="'+frames.length+'">'+frames.map(function(f,i){return '<section class="film-frame" style="animation-delay:-'+(duration-i*frameSeconds).toFixed(2)+'s">'+f.html+'</section>';}).join('')+'</div></body></html>';
    var film='<div class="gp-app-frame" aria-label="Animated guide: '+esc(frames.map(function(f){return f.label;}).join('. '))+'"><iframe title="Automatic '+p.label+' guide" tabindex="-1" aria-hidden="true" inert sandbox srcdoc="'+esc(doc)+'"></iframe></div>';
    var picker=upload?exportPicker(id):'';
    var selected=live?liveUpload(id):{file:ui.uploadFile};
    var primary=upload?'<button class="gp-primary" data-action="upload-export"'+(!canUpload(id)||!selected.file||selected.pending?' disabled':'')+'>'+(selected.pending?'Upload in progress':'Upload '+(id==='notes'?'file':'export'))+'</button>':id==='muse'?'<button class="gp-primary" data-import="muse">Connect Muse</button>':'<button class="gp-primary" data-action="export-requested"'+(live&&ui.reminderRequest?' disabled':'')+'>'+(id==='chats'?'I have my export':'I’ve requested it')+'</button>';
    var devices=id==='chats'&&!upload?'<div class="gp-device-choice" aria-label="WhatsApp device">'+[['iphone','iPhone'],['android','Android']].map(function(d){return '<button data-guide-device="'+d[0]+'" aria-pressed="'+(ui.guideDevice===d[0])+'">'+d[1]+'</button>';}).join('')+'</div>':'';
    var controls=frames.length>1?'<div class="gp-film-tools"><button class="gp-text" data-action="film-toggle" aria-label="Pause guide">Ⅱ</button></div>':'';
    var content=header+devices+film+readableGuide(frames)+picker+controls+'<footer class="gp-guide-footer"><button class="gp-text" data-action="guide-back">'+(upload?(id==='notes'||id==='chats'?'Later':'When the email arrives'):'Back')+'</button>'+primary+'</footer>';
    if(api.getState().onboardingComplete){content=content.replace(header,'<p class="gp-guide-purpose">'+(upload?'Upload your export to bring your context into Grove':'Follow the steps to export your history')+help+'</p>');return settingsFrame('<div class="gp-nested-guide '+(upload?'gp-upload-guide':'')+'">'+content+'</div>','sources',sourceTrail(id,upload));}
    return shell(content,'gp-guide gp-visual-guide'+(upload?' gp-upload-guide':''),false);
  }
  function integrationGuide(id){
    var google=id==='google',p=google?{label:'Google'}:PROVIDERS[id],marks=google?['gmail','drive','calendar']:[id];
    var content='<div class="gp-integration-route">'+marks.map(function(k){return '<span>'+icon(k)+'<small>'+PROVIDERS[k].label+'</small></span>';}).join('')+'</div><p class="gp-copy">'+(google?'Your mail, files and plans, together':'Bring '+p.label+' into your Grove')+'</p><footer class="gp-guide-footer"><button class="gp-text" data-action="guide-back">Later</button><button class="gp-primary" data-connect-source="'+id+'">Connect '+p.label+'</button></footer>';
    if(api.getState().onboardingComplete)return settingsFrame(content,'sources',sourceTrail(id,false));
    return shell('<header class="gp-guide-heading"><span class="gp-provider-icon">'+icon(google?'google':id)+'</span><h1 id="gp-title" tabindex="-1">Connect '+p.label+'</h1><button class="gp-icon" data-action="guide-back" aria-label="Close guide">×</button></header>'+content,'gp-guide gp-integration-guide',false);
  }
  function transferFilm(id){return '<div class="film-transfer"><h2>'+(id==='muse'?'Your context, connected':id==='notes'?'Bring your files':'Bring your history')+'</h2><div class="film-route"><span class="film-end">'+icon(id)+'</span><span class="film-line" aria-hidden="true">→</span><span class="film-file">'+icon('notes')+'<small>'+(id==='notes'?'Notes':id==='muse'?'Context':id==='chats'?'.txt / .zip':'.zip')+'</small></span><span class="film-line" aria-hidden="true">→</span><span class="film-end film-grove">g<b></b></span></div><div class="film-labels"><span>'+(id==='muse'?'Muse':id==='notes'?'Your files':id==='chats'?'WhatsApp':'Email')+'</span><span>'+(id==='muse'?'Connect':id==='notes'?'Add':id==='chats'?'Save or share':'Download')+'</span><span>Grove</span></div></div>';}
  function filmThemeCSS(){return document.documentElement.getAttribute('data-theme')==='dark'?'html,body,.film-stage,.film-transfer,.film-instruction{background:#111113!important;color:#f0f0f0!important}.film-labels,.film-instruction>span,.film-line{color:#b3b3ac!important}.film-end{border-color:#50504d!important}.film-frame [class*="bg-"]{background-color:#19191c!important}.film-frame [class*="text-"]{color:#e8e8e5!important}.film-frame [class*="border-"]{border-color:#444448!important}':'';}
  function filmCSS(count,duration){var fade=(100/count).toFixed(6);return filmThemeCSS()+'.film-server{font:15px ui-monospace,monospace;padding:16px;border:1px solid currentColor;border-radius:6px;margin-top:20px}html,body{margin:0;width:760px;height:475px;overflow:hidden;background:#f7f6f0}.film-stage{position:relative;width:760px;height:475px;pointer-events:none;user-select:none}.film-frame{position:absolute;inset:0;opacity:0;animation:filmFrame '+duration+'s steps(1,end) infinite}.film-frame:only-child{opacity:1;animation:none}.film-frame:last-child{z-index:2}@keyframes filmFrame{0%{opacity:1} '+fade+'%,100%{opacity:0}}.film-transfer,.film-instruction{box-sizing:border-box;width:760px;height:475px;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#f7f6f0;color:#242521;font-family:ui-monospace,monospace}.film-transfer h2,.film-instruction h2{font-size:25px;font-weight:500;letter-spacing:-1px;margin:0 0 46px}.film-route{display:grid;grid-template-columns:100px 85px 100px 85px 100px;align-items:center;justify-items:center;gap:12px}.film-end,.film-file{width:70px;height:70px;display:flex;align-items:center;justify-content:center}.film-end{border:1px solid #d6d5cd;border-radius:50%}.film-end svg{width:30px;height:30px}.film-file{flex-direction:column;gap:8px}.film-file svg{width:31px;height:31px}.film-file small{font-size:11px;white-space:nowrap;text-align:center}.film-line{width:85px;text-align:center;color:#a6a69c;font:300 28px ui-monospace,monospace}.film-line i{position:absolute;width:5px;height:5px;border-radius:50%;background:#c4a044;top:-2px;animation:filmDot 3.6s ease-in-out infinite}.film-labels{display:grid;grid-template-columns:100px 100px 100px;column-gap:109px;width:518px;margin-top:25px;font-size:12px;color:#77786f;text-align:center}.film-grove{position:relative;font-size:36px;letter-spacing:-4px}.film-grove b{position:absolute;width:6px;height:6px;border-radius:50%;background:#c4a044;right:17px;top:23px}.film-instruction h2{margin:20px 0;font-size:27px}.film-instruction p{font-size:14px;max-width:540px;text-align:center;line-height:1.8}.film-instruction>span{color:#8c8d83;font-size:12px}.film-logo{margin-bottom:30px}.film-logo svg{width:34px;height:34px}[data-target]{animation:filmTarget 1.8s ease-in-out infinite}@keyframes filmDot{0%{left:0;opacity:0}25%,75%{opacity:1}100%{left:100%;opacity:0}}@keyframes fileFloat{50%{transform:translateY(-5px)}}@keyframes filmTarget{50%{outline-color:#eedc9e}}@media(prefers-reduced-motion:reduce){*{animation:none!important}.film-frame{display:none}.film-frame:first-child{display:block;opacity:1}}';}
  // The illustration keeps its reference proportions. Mobile instructions remain
  // real, unscaled text and follow the same elapsed-time clock as its active frame.
  var guideTextTimer=null;
  function readableGuide(frames){
    if(!frames.length||!frames.every(function(f){return f.title&&f.detail;}))return '';
    return '<div class="gp-guide-reader" data-guide-reader aria-label="Current guide instruction">'+frames.map(function(f,i){return '<section data-guide-step="'+i+'"'+(i?' hidden':'')+'><small>Step '+(i+1)+' of '+frames.length+'</small><h2>'+esc(f.title)+'</h2><p>'+esc(f.detail)+'</p></section>';}).join('')+'</div>';
  }
  function syncGuideText(){
    var frame=root.querySelector('.gp-app-frame iframe'),reader=root.querySelector('[data-guide-reader]');
    if(!frame||!reader){clearInterval(guideTextTimer);guideTextTimer=null;return;}
    var sections=reader.querySelectorAll('[data-guide-step]');
    var seconds=Number((frame.dataset.base.match(/data-frame-seconds="([\d.]+)"/)||[])[1])||3;
    var elapsed=Number(frame.dataset.elapsed)||0;
    if(frame.dataset.paused!=='true')elapsed+=(performance.now()-Number(frame.dataset.started))/1000;
    var active=reduceMotion.matches?0:Math.floor(elapsed/seconds)%sections.length;
    sections.forEach(function(section,i){section.hidden=i!==active;});
  }
  function fitGuide(){
    clearInterval(guideTextTimer);guideTextTimer=null;
    var frame=root.querySelector('.gp-app-frame');
    if(frame){
      var iframe=frame.querySelector('iframe');
      if(!iframe.dataset.started){iframe.dataset.started=String(performance.now());iframe.dataset.base=iframe.getAttribute('srcdoc');}
      iframe.style.transform='scale('+(frame.clientWidth/760)+')';
      if(root.querySelector('[data-guide-reader]')){syncGuideText();guideTextTimer=setInterval(syncGuideText,100);}
    }
  }
  window.addEventListener('resize',fitGuide);
  window.addEventListener('grove:theme',function(){if(ui.guide||ui.host)render(true);});
  function info(copy){return '<span class="gp-info" tabindex="0" role="note" aria-label="'+esc(copy)+'">i<span class="gp-tooltip" role="tooltip">'+esc(copy)+'</span></span>';}
  function filesComposer(){
    if(live)return '<div class="gp-materials" data-materials="'+(api.getState().onboardingComplete?'sources':'onboarding')+'"></div>';
    var all=live?[]:Object.values(api.getState().files||{}),notes=all.filter(function(f){return f.kind==='note';}),files=all.filter(function(f){return f.kind!=='note';});
    function rows(items){return '<div class="gp-file-added">'+items.map(function(f){var a=api.getState().accountImports[f.id];return '<div><span>'+icon('notes')+'</span><strong>'+esc(f.name)+'</strong><small class="gp-status" data-account-status="'+f.id+'">'+(a&&a.status==='importing'?a.progress+'%':'')+'</small></div>';}).join('')+'</div>';}
    var samples=['About me.md','Project direction.pptx','Research notes.pdf','Reading list.docx','Ideas.txt'];
    var available=samples.filter(function(name){return !files.some(function(f){return f.name===name;});});
    var picker=!live&&ui.filePicker?'<div class="gp-file-picker"><p class="gp-kicker">Choose files</p><form data-files-form>'+available.map(function(name){return '<label><input type="checkbox" name="file" value="'+name+'" checked><span>'+name+'</span><small>'+name.split('.').pop().toUpperCase()+'</small></label>';}).join('')+(!available.length?'<p class="gp-section-copy">All example files have been added</p>':'')+'<footer><button class="gp-text" type="button" data-action="cancel-files">Cancel</button><button class="gp-small" '+(!available.length?'disabled':'')+'>Add selected</button></footer></form></div>':'';
    var editor=!live&&ui.noteEditor?'<form class="gp-note-form" data-note-form><label>Title<input name="title" placeholder="A little about me" maxlength="100" value="'+esc(ui.noteDraft.title)+'" required></label><label>Note<textarea name="body" placeholder="What would you like Grove to know?" rows="3" required>'+esc(ui.noteDraft.body)+'</textarea></label><footer><button type="button" class="gp-text" data-action="cancel-note">Cancel</button><button class="gp-small">Add note</button></footer></form>':'';
    return '<div class="gp-files-composer"><section class="gp-material-section"><header><div><h2>Your files</h2><p>Documents, slides, PDFs, and more</p></div><button class="gp-small" data-action="choose-files"'+(live?' disabled':'')+'>Add files +</button></header>'+picker+rows(files)+(!files.length&&!ui.filePicker?'<p class="gp-material-empty">Bring the things you return to</p>':'')+'</section><section class="gp-material-section"><header><div><h2>Your notes</h2><p>Your own words, directly to Grove</p></div><button class="gp-small" data-action="write-note"'+(live?' disabled':'')+'>Add note +</button></header>'+editor+rows(notes)+(!notes.length&&!ui.noteEditor?'<p class="gp-material-empty">An idea, a preference, or something worth knowing</p>':'')+'</section></div>';
  }

  function sourceAccounts(s){if(live)return liveSourceAccounts();return Object.keys(s.accountImports).map(function(id){var a=s.accountImports[id];return Object.assign({},a,{id:id,label:a.kind==='file'?a.label:PROVIDERS[id]?PROVIDERS[id].label:(a.label||id)});});}
  function sourceAction(kind,label,attr){
    var paths={update:'M19 7v5h-5M5 17v-5h5M6.1 7a7 7 0 0 1 11.6-1L19 8M5 16l1.3 2A7 7 0 0 0 18 17',disconnect:'M12 3v9M7 5.5a8 8 0 1 0 10 0',remove:'M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14M10 10v7M14 10v7',reconnect:'M12 5v14M5 12h14'};
    return '<button class="gp-source-action '+(kind==='disconnect'||kind==='remove'?'gp-destructive':kind==='reconnect'?'gp-positive':'')+'" '+attr+' aria-label="'+esc(label)+'" title="'+esc(label)+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="'+paths[kind]+'"/></svg></button>';
  }
  function sourceRow(a){
    if(a.live){
      var words={took_in:'Import accepted',nothing_new:'No new messages',kept_out:'No retained messages',failed:'Import failed'};
      return '<div class="gp-source-row"><span class="gp-provider-icon">'+appMark(a.id)+'</span><div class="gp-source-name"><strong>'+esc(a.label)+'</strong><small>'+esc(words[a.outcome]||'No read status recorded')+'</small>'+(a.status==='disconnected'?'<small>Disconnected · memories retained</small>':'')+'</div><span class="gp-source-value">'+(Number.isFinite(a.readCount)?esc(a.readCount)+' episodes read':'—')+'</span></div>';
    }
    var off=a.status==='disconnected',c=a.contribution||{nodes:0,edges:0},file=a.kind==='file';
    var detail=demo?'Synthetic example':file?((api.getState().files[a.id]||{}).kind==='note'?'Note':'File'):a.id==='chats'?'Uploaded history':off?'Memories retained':'Syncs automatically';
    var update=a.id==='chats'&&!off?sourceAction('update','Update WhatsApp history','data-upload-provider="chats"'):'';
    return '<div class="gp-source-row '+(off?'is-disconnected':'')+'"><span class="gp-provider-icon">'+(file?icon('notes'):appMark(a.id))+'</span><div class="gp-source-name"><strong>'+esc(a.label)+'</strong><small>'+detail+'</small><small class="gp-source-status" data-account-status="'+a.id+'">'+(off?'Disconnected':a.status==='importing'?'Adding · '+a.progress+'%':'')+'</small></div><button class="gp-source-value" data-source-value="'+a.id+'" aria-label="Show contributions from '+esc(a.label)+'"><span><b data-contribution-nodes="'+a.id+'">'+c.nodes+'</b> nodes</span><span><b data-contribution-edges="'+a.id+'">'+c.edges+'</b> connections</span></button><div class="gp-source-actions">'+update+(file?sourceAction('remove','Remove '+a.label,'data-remove-file="'+a.id+'"'):sourceAction(off?'reconnect':'disconnect',(off?'Reconnect ':'Disconnect ')+a.label,'data-source-toggle="'+a.id+'"'))+'</div></div>';
  }
  function pendingRows(s){return pendingExports(s).map(function(id){var previous=s.accountImports[id]&&s.accountImports[id].loadedCount>0;return '<div class="gp-source-row gp-source-pending"><span class="gp-provider-icon">'+icon(id)+'</span><div class="gp-source-name"><strong>'+PROVIDERS[id].label+'</strong><small>'+(previous?'New chats aren’t in your Grove yet':'Not in your Grove yet')+'</small></div><span class="gp-pending-status"><i class="gp-notification-dot" aria-hidden="true"></i>Upload needed</span><button class="gp-text gp-upload-pending" data-upload-provider="'+id+'" aria-label="Upload '+PROVIDERS[id].label+' export">Upload '+arrow()+'</button></div>';}).join('');}
  function groupedSources(s){
    var accounts=sourceAccounts(s);
    return (live?'<p class="gp-section-copy" role="status">'+esc(coverageMessage())+'</p><button class="gp-text" data-action="refresh-live-data">Check sources</button>':'')+[['chats','Chats'],['apps','Synced apps'],['files','Files & notes']].map(function(group){
      if(live&&group[0]==='files')return '<section class="gp-source-group"><h3>Files &amp; notes</h3><div class="gp-materials" data-materials="settings"></div></section>';
      var pending=group[0]==='chats'?pendingRows(s):'';
      var rows=accounts.filter(function(a){return group[0]==='files'?a.kind==='file':a.kind!=='file'&&(group[0]==='chats'?AI_PROVIDERS.includes(a.id):!AI_PROVIDERS.includes(a.id));});
      return '<section class="gp-source-group"><header><h3>'+group[1]+'</h3><button class="gp-text" data-add-group="'+group[0]+'">Add +</button></header>'+pending+(rows.length?rows.map(sourceRow).join(''):pending?'':'<p class="gp-source-empty">'+{chats:'Your conversations can start here',apps:'Connect the places your work lives',files:'Add a document, a plan, or a note'}[group[0]]+'</p>')+'</section>';
    }).join('');
  }
  function openConfirmation(type,id){
    var pane=modalRoot.querySelector('.gp-settings-content');ui.confirmScroll=pane?pane.scrollTop:0;
    ui.confirm={type:type,id:id};ui.confirmReturn=type==='file'?'[data-remove-file="'+id+'"]':'[data-source-toggle="'+id+'"]';render(true);
  }
  function confirmation(){
    var c=ui.confirm;
    if(c.type==='brain')return '<div class="gp-confirm-backdrop"><section class="gp-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="gp-confirm-title" aria-describedby="gp-confirm-copy"><header><h2 id="gp-confirm-title" tabindex="-1">Delete your brain?</h2><button class="gp-icon" data-action="cancel-confirm" aria-label="Close confirmation">×</button></header><p id="gp-confirm-copy">This would remove the memories and connections in your Grove. Your login account would stay.</p><div class="gp-confirm-actions"><button class="gp-small" data-action="cancel-confirm">Cancel</button><button class="gp-small gp-danger" disabled aria-disabled="true">Delete brain</button></div></section></div>';
    var disconnect=c.type==='disconnect',label=disconnect?(PROVIDERS[c.id]||{}).label||c.id:'';
    var title=disconnect?'Disconnect '+label+'?':'Remove this file?';
    var copy=disconnect?'New imports will stop. The memories already in your Grove stay.':'Memories supported only by this file will be removed. Shared memories stay.';
    return '<div class="gp-confirm-backdrop"><section class="gp-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="gp-confirm-title" aria-describedby="gp-confirm-copy"><header><h2 id="gp-confirm-title" tabindex="-1">'+esc(title)+'</h2><button class="gp-icon" data-action="cancel-confirm" aria-label="Close confirmation">×</button></header><p id="gp-confirm-copy">'+copy+'</p><div class="gp-confirm-actions"><button class="gp-small" data-action="cancel-confirm">Cancel</button><button class="gp-small gp-danger" data-action="confirm-delete">'+(disconnect?'Disconnect':'Remove file')+'</button></div></section></div>';
  }
  function growthForm(s){var g=ui.growthDraft||s.growth;return '<h2>My Grove</h2><p class="gp-section-copy">A little guidance, entirely up to you</p>'+(live?'<p class="gp-section-copy">Your guidance notes, saved on this browser.</p>':'')+'<form data-growth-form class="gp-growth-form"><label><span class="gp-field-label">Focus on '+info('Tell Grove which work, people, or ideas deserve more attention. Leave this blank to let your sources lead.')+'</span><textarea name="focus" rows="2" placeholder="The work, people, and ideas that matter to you">'+esc(g.focus)+'</textarea></label><label><span class="gp-field-label">How to read my sources '+info('Set the context for interpretation. For example, a plan is a possibility until there is evidence it happened.')+'</span><textarea name="reading" rows="2" placeholder="Treat plans as possibilities until I confirm what happened">'+esc(g.reading)+'</textarea></label><label><span class="gp-field-label">Leave out '+info('Describe topics or details that should not shape your Grove, such as temporary logistics or routine chatter.')+'</span><textarea name="avoid" rows="2" placeholder="Routine chatter, temporary details, or topics to ignore">'+esc(g.avoid)+'</textarea></label><div class="gp-check-row"><label class="gp-check-label"><input type="checkbox" name="confirm" '+(g.confirm?'checked':'')+'>Ask before changing an established preference</label>'+info('Grove will bring a question to your input inbox before replacing a preference it has already learned.')+'</div><footer><span class="gp-save-note gp-positive" role="status">'+esc(ui.notice)+'</span><button class="gp-small">'+(live?'Save browser guidance':'Save guidance')+'</button></footer></form>';}
  // The sim's example name is fixture mode only; live mode with no name known says 'You'.
  function shownName(state){return state.name||(live?'You':'Anas');}
  function accountSettings(state){
    if(demo)return '<h2>Simulation</h2><p class="gp-section-copy">This example uses invented data in page memory. It does not change your account or saved Grove.</p><a class="gp-text" href="/">Return to live Grove</a>';
    var name=ui.editName?'<form data-name-form class="gp-name-edit"><label for="gp-name">Name</label><div class="gp-input-row"><input id="gp-name" name="name" value="'+esc(ui.nameDraft)+'" autocomplete="name" maxlength="100" required><button class="gp-small" type="submit">Save</button><button class="gp-text" type="button" data-action="cancel-name">Cancel</button></div></form>':'<div class="gp-setting-row"><span><strong>Name</strong><small>'+esc(shownName(state))+'</small></span><button class="gp-text" data-action="edit-name">Edit</button></div>';
    var method=state.auth&&state.auth.provider==='google'?'Google':state.auth&&state.auth.provider==='apple'?'Apple':'Email code';
    // Live mode shows only the address the sign-in knows; with none (Google), the sign-in method.
    var email=state.auth&&state.auth.email||(live?'':'anas@example.com');
    var emailRow=email?'<div class="gp-setting-row"><span><strong>Email</strong><small>'+esc(email)+'</small></span><span class="gp-positive">Verified</span></div>':'<div class="gp-setting-row"><span><strong>Email</strong><small>Signed in with '+esc(method)+'</small></span></div>';
    return '<h2>Account</h2><p class="gp-section-copy">Your identity and access</p><section class="gp-account-section"><h3>Profile</h3>'+name+emailRow+'</section><section class="gp-account-section"><h3>Sign-in</h3><div class="gp-setting-row"><span><strong>'+esc(method)+'</strong><small>This device</small></span><button class="gp-small" data-action="logout">Log out</button></div>'+(ui.logoutError?'<p class="gp-footnote" role="alert">'+esc(ui.logoutError)+'</p>':'')+'</section><section class="gp-account-section"><h3>Your data</h3><div class="gp-setting-row"><span><strong>'+(live?'Download browser settings':'Download my Grove')+'</strong><small>'+(live?'Keep this browser’s preferences and pending reminders':'Keep a copy of your sources and guidance')+'</small></span><button class="gp-text" data-action="download-data">Download ↗</button></div><div class="gp-setting-row"><span><strong>Delete brain</strong><small>Remove your Grove memories and connections</small></span><button class="gp-text gp-danger-text" data-action="delete-brain">Delete brain</button></div></section>';
  }
  function requestsPanel(){
    var items=(api.getState().requests||[]).filter(function(r){return r.status==='open';}),r=items.find(function(r){return r.id===ui.requestId;})||items[0],index=items.indexOf(r);
    if(r)ui.requestId=r.id;
    var question=r?r.title:'';
    if(r&&r.kind==='connection'&&question==='Does this connection still hold?')question='Is this still right: '+r.targetLabel.replace(/ → /g,' ')+'?';
    var body=r?'<form data-request-form data-request="'+r.id+'"><h2>'+esc(question)+'</h2><textarea name="answer" rows="3" required aria-label="Your answer" placeholder="'+esc(r.placeholder)+'">'+esc(ui.requestDrafts[r.id]||'')+'</textarea><footer><div class="gp-review-nav"><button class="gp-icon gp-prev-arrow" type="button" data-action="request-prev" aria-label="Previous question" '+(index===0?'disabled':'')+'>'+arrow()+'</button><span>'+(index+1)+' / '+items.length+'</span><button class="gp-icon" type="button" data-action="request-next" aria-label="Next question" '+(index===items.length-1?'disabled':'')+'>'+arrow()+'</button></div><button class="gp-small">Send answer</button></footer></form>':'<p class="gp-review-empty">'+(live?'Questions from your Grove appear here.':'You’re all caught up')+'</p>';
    return '<section class="gp-review-popover" role="dialog" aria-modal="false" aria-labelledby="gp-title"><header><span class="gp-review-agent" id="gp-title" tabindex="-1">'+(r?workerMark(r.agent.toLowerCase())+'From '+esc(r.agent):'Grove')+'</span>'+close()+'</header>'+body+'</section>';
  }

  function positionRequests(){
    var el=modalRoot.querySelector('.gp-review-popover'),button=document.getElementById('requests-button');if(!el||!button)return;
    var rect=button.getBoundingClientRect();el.style.top=(rect.bottom+12)+'px';el.style.left=Math.max(16,Math.min(innerWidth-el.offsetWidth-16,rect.right-el.offsetWidth))+'px';el.style.maxHeight=Math.max(160,innerHeight-rect.bottom-28)+'px';
  }
  function settingsFrame(content,tab,trail){
    var state=api.getState();
    return shell('<header class="gp-popup-top"><h1 id="gp-title" tabindex="-1">Settings</h1>'+close()+'</header><div class="gp-settings-layout"><nav aria-label="Settings sections">'+[['account','Account'],['sources','Sources'],['growth','My Grove'],['connections','Connections']].map(function(t){return '<button data-settings-tab="'+t[0]+'" class="'+(tab===t[0]?'is-current':'')+'" aria-current="'+(tab===t[0]?'page':'false')+'">'+t[1]+(t[0]==='sources'&&needsHistory(state)?'<i class="gp-notification-dot"></i>':'')+'</button>';}).join('')+'</nav><div class="gp-settings-content">'+(trail?breadcrumbs(trail):'')+content+'</div></div>','gp-settings',true);
  }
  function breadcrumbs(items){return '<nav class="gp-breadcrumbs" aria-label="Settings location"><h2>'+items.map(function(item,i){return (i?'<span aria-hidden="true">›</span>':'')+(item[1]?'<button data-action="'+item[1]+'">'+esc(item[0])+'</button>':'<span aria-current="page">'+esc(item[0])+'</span>');}).join('')+'</h2></nav>';}
  function sourceTrail(provider,upload){var trail=[['Sources','sources-back'],['Add a source','source-picker']];if(provider)trail.push([PROVIDERS[provider].label,upload?'export-stage':null]);if(upload)trail.push(['Upload']);return trail;}
  function profile(){
    var state=api.getState(),tab=ui.settingsTab,content='';
    if(tab==='account')content=accountSettings(state);
    else if(tab==='sources')content='<h2>Sources</h2><p class="gp-section-copy">The conversations and knowledge your Grove grows from</p>'+groupedSources(state)+'<p class="gp-source-retention">Disconnecting stops new imports. Your memories stay.</p>';
    else if(tab==='connections')content=connectionsContent();
    else content=growthForm(state);
    return settingsFrame(content,tab);
  }
  function careSummary(){return window.GroveCare?window.GroveCare.summary():{updated:0,connections:0,tended:0,fresh:0,areas:0,working:null,enabled:false};}
  function careWords(key,n){var terms={updated:['memory refreshed','memories refreshed'],tended:['area refreshed','areas refreshed'],connections:['connection found','connections found']};return terms[key][Number(n)===1?0:1];}
  function workerMark(id){return '<span class="gp-worker-mark gp-worker-'+id+'" aria-hidden="true"></span>';}
  function growthSparkline(kind){
    var s=api.getState(),history=s.growthHistory||[],current=kind==='nodes'?s.currentNodeCount:s.currentEdgeCount;
    if(!history.length)history=[{nodes:s.currentNodeCount,edges:s.currentEdgeCount}];
    var vals=history.map(function(p){return p[kind];}),max=Math.max.apply(Math,vals.concat([1])),points=vals.map(function(n,i){return (4+(vals.length===1?1:i/(vals.length-1))*126).toFixed(1)+','+(53-n/max*46).toFixed(1);}).join(' '),last=53-current/max*46;
    var line=vals.length===1?'4,'+last.toFixed(1)+' 130,'+last.toFixed(1):points;
    return '<svg viewBox="0 0 136 60" role="img" aria-label="'+(vals.length>1?'Recorded '+(kind==='nodes'?'node':'connection')+' growth, from '+vals[0]+' to '+current:'Growth history starts at '+current)+'"><path d="M4 54H132" fill="none" stroke="var(--border)" stroke-width="1"/><polyline points="'+line+'" fill="none" stroke="var(--gp-success)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/><circle cx="130" cy="'+last.toFixed(1)+'" r="2.5" fill="var(--gp-success)"/></svg>';
  }
  function healthSources(s){
    if(live&&liveData.sources===null)return '<p class="gp-section-copy">'+esc(coverageMessage())+'</p>';
    var accounts=sourceAccounts(s).filter(function(a){return a.status!=='disconnected';});
    return '<div class="gp-health-source-groups">'+[['chats','Chats'],['apps','Synced apps'],['files','Files & notes']].map(function(g){
      var rows=accounts.filter(function(a){return g[0]==='files'?a.kind==='file':a.kind!=='file'&&(g[0]==='chats'?AI_PROVIDERS.includes(a.id):!AI_PROVIDERS.includes(a.id));});
      var marks=g[0]==='files'?Array.from(new Set(rows.map(function(a){return (s.files[a.id]||{}).kind==='note'?'note':'file';}))).map(function(k){return '<span title="'+(k==='note'?'Notes':'Files')+'">'+icon('notes')+'</span>';}).join(''):rows.slice(0,5).map(function(a){return '<span title="'+esc(a.label)+'">'+appMark(a.id)+'</span>';}).join('');
      return '<div><div class="gp-health-source-label"><strong>'+rows.length+'</strong><span>'+g[1]+'</span></div><div class="gp-health-source-marks">'+(marks||'<small>None yet</small>')+(rows.length>5&&g[0]!=='files'?'<small>+'+(rows.length-5)+'</small>':'')+'</div></div>';
    }).join('')+'</div>';
  }
  function health(){
    var s=api.getState(),care=careSummary();
    if(live){
      var data=liveData.health;
      return shell('<header class="gp-popup-top"><h1 id="gp-title" tabindex="-1">Your Grove health</h1>'+close()+'</header><div class="gp-health-scroll"><section class="gp-health-overview"><div class="gp-health-counts">'+[['nodes','Nodes'],['edges','Connections']].map(function(row){return '<div class="gp-health-metric"><div><span class="gp-metric-label">'+row[1]+'</span><strong data-live-count="'+row[0]+'">'+(data?data[row[0]].toLocaleString():'—')+'</strong>'+((row[0]==='nodes'?s.nodeDelta:s.edgeDelta)?'<button class="gp-health-growth" data-growth-kind="'+row[0]+'">+'+(row[0]==='nodes'?s.nodeDelta:s.edgeDelta)+'</button>':'')+'</div><div class="gp-growth-chart"><small>Changes since previous graph check</small></div></div>';}).join('')+'</div></section><section class="gp-health-section"><h2><button class="gp-section-link" data-action="watch-care">Your Grove workers '+exploreArrow()+'</button></h2><div class="gp-health-workers">'+[['learner','Learner'],['curator','Curator'],['connector','Connector']].map(function(w){return '<div>'+workerMark(w[0])+'<span><strong>'+w[1]+'</strong><small>'+esc(care.workers&&care.workers[w[0]]||'No activity loaded')+'</small></span></div>';}).join('')+'</div><div class="gp-health-freshness"><span>Freshness</span><strong>—</strong></div></section><section class="gp-health-section"><h2><button class="gp-section-link" data-action="manage-sources">Your sources '+exploreArrow()+'</button></h2>'+healthSources(s)+'</section><p class="gp-health-observed" role="status">'+(liveData.pending?'Check in progress':data?'Last checked '+esc(liveData.checkedAt||'now'):'Health information could not be read. Try again.')+'</p><button class="gp-text" data-action="refresh-live-data">Check health</button></div>','gp-health-dialog',true);
    }
    function metric(kind,label,count,delta){return '<div class="gp-health-metric"><div><span class="gp-metric-label">'+label+'</span><strong data-count="'+kind+'">'+count.toLocaleString()+'</strong><button class="gp-health-growth" data-growth-kind="'+kind+'" aria-label="Show new '+label.toLowerCase()+'" '+(!delta?'hidden':'')+'><b data-growth-count="'+kind+'">+'+delta.toLocaleString()+'</b></button></div><div class="gp-growth-chart" data-growth-chart="'+kind+'">'+growthSparkline(kind)+'</div></div>';}
    var workers=[['learner','Learner','updated','Brings in new context'],['curator','Curator','tended','Keeps memories useful'],['connector','Connector','connections','Finds links across your world']];
    return shell('<header class="gp-popup-top"><h1 id="gp-title" tabindex="-1">Your Grove health</h1>'+close()+'</header><div class="gp-health-scroll"><section class="gp-health-overview"><div class="gp-health-counts">'+metric('nodes','Nodes',s.currentNodeCount,s.nodeDelta)+metric('edges','Connections',s.currentEdgeCount,s.edgeDelta)+'</div></section><section class="gp-health-section"><h2><button class="gp-section-link" data-action="watch-care">Your Grove workers '+exploreArrow()+'</button></h2><div class="gp-health-workers">'+workers.map(function(w){return '<div>'+workerMark(w[0])+'<span><strong>'+w[1]+' '+info(w[3])+'</strong><small><b data-care="'+w[2]+'">'+care[w[2]]+'</b> <span data-care-words="'+w[2]+'">'+careWords(w[2],care[w[2]])+'</span></small></span></div>';}).join('')+'</div><div class="gp-health-freshness"><span>Freshness '+info('Learner, Curator, and Connector revisit dusty areas first, refreshing context and keeping connections useful.')+'</span><strong data-care="fresh">'+care.fresh+'%</strong><div class="gp-freshness-track"><i style="width:'+care.fresh+'%" data-care-bar></i></div></div></section><section class="gp-health-section"><h2><button class="gp-section-link" data-action="manage-sources">Your sources '+exploreArrow()+'</button></h2>'+healthSources(s)+'</section></div>','gp-health-dialog',true);
  }
  var MCP_URL=live?live.mcpUrl():'https://mcp.grove.example/mcp';
  function mcpLink(){return '<label for="gp-mcp-url">Server URL</label><div class="gp-mcp-link"><input id="gp-mcp-url" value="'+esc(MCP_URL)+'" readonly aria-label="Grove MCP server URL"><button class="gp-small" data-action="copy-mcp">'+(ui.mcpCopied?'Copied':'Copy')+'</button></div>';}
  function mcpIcon(){return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="m8 5-6 7 6 7m8-14 6 7-6 7M14 3l-4 18"/></svg>';}
  function customMcp(){
    if(demo)return '<p class="gp-section-copy">These guides illustrate setup. No AI app receives access in this simulation. <a href="/">Return to live Grove</a> for real access.</p>';
    if(live)return '<section class="gp-mcp-section"><details '+(ui.mcpOpen?'open':'')+' data-mcp-details><summary tabindex="0" class="gp-host-choice"><span class="gp-provider-icon">'+mcpIcon()+'</span><span><strong>Custom MCP</strong><small>Set up another AI app</small></span><span class="gp-host-status" aria-hidden="true">+</span></summary><div class="gp-mcp-body"><p class="gp-section-copy">Add this server in your AI app’s MCP connector settings. Complete the connection in that app through Grove’s consent page.</p>'+mcpLink()+'</div></details></section>';
    var clients=api.getState().mcpClients||[];
    return '<section class="gp-mcp-section"><details '+(ui.mcpOpen?'open':'')+' data-mcp-details><summary class="gp-host-choice"><span class="gp-provider-icon">'+mcpIcon()+' </span><span><strong>Custom MCP</strong><small>Connect another AI app</small></span><span class="gp-host-status" aria-hidden="true">+</span></summary><div class="gp-mcp-body"><p class="gp-section-copy">Give any MCP-compatible AI access to your Grove. Add this server in its connector settings</p><label for="gp-mcp-url">Server URL</label><div class="gp-mcp-link"><input id="gp-mcp-url" value="'+MCP_URL+'" readonly aria-label="Grove MCP server URL"><button class="gp-small" data-action="copy-mcp">'+(ui.mcpCopied?'Copied':'Copy')+'</button></div><form data-mcp-form><label for="gp-mcp-name">AI app name</label><div class="gp-input-row"><input id="gp-mcp-name" name="client" placeholder="Your AI app" value="'+esc(ui.mcpDraft||'')+'" maxlength="60" required><button class="gp-small">Connect</button></div></form>'+clients.map(function(c){return '<div class="gp-mcp-client"><span><strong>'+esc(c.name)+'</strong><small>Custom MCP</small></span>'+sourceAction('disconnect','Disconnect '+c.name,'data-mcp-remove="'+esc(c.id)+'"')+'</div>';}).join('')+'</div></details></section>';
  }
  function connectionsContent(){return '<h2>Connections</h2><p class="gp-section-copy">Take your Grove to the AI you use</p>'+hostChoices()+customMcp();}
  function connections(){return settingsFrame(connectionsContent(),'connections');}
  function hostGuide(){
    if(live||demo){
      var name=ui.host==='muse'?'Muse Desktop':PROVIDERS[ui.host].label;
      var steps=ui.host==='muse'?[['Open a Muse conversation','Ask Muse to add Grove as a remote MCP connector.'],['Add the Grove server','Use the server URL below. Muse opens the Grove authorization page.'],['Review Grove access','Sign in to Grove, review the requested access, then return to Muse.']]:[['Open '+name,'Open the app’s custom connector settings.'],['Add the Grove server','Add a remote MCP connector with the server URL below.'],['Review Grove access','Sign in to Grove and review access. Return to '+name+' after approval.']];
      var visual='';
      if(ui.host!=='grok'){
        var frames=steps.map(function(step,i){return '<section class="film-frame" style="animation-delay:-'+(9-i*3).toFixed(2)+'s"><div class="film-instruction"><div class="film-logo">'+icon(ui.host)+'</div><span>0'+(i+1)+' / 03</span><h2>'+esc(step[0])+'</h2><p>'+esc(step[1])+'</p>'+(i===1?'<div class="film-server">'+esc(MCP_URL)+'</div>':'')+'</div></section>';}).join('');
        var doc='<!doctype html><html><head><meta charset="utf-8"><style>'+filmCSS(3,9)+'</style></head><body><div class="film-stage" data-frame-seconds="3" data-frame-count="3">'+frames+'</div></body></html>';
        visual='<div class="gp-app-frame" aria-label="Illustrated '+esc(name)+' connection guide"><iframe title="Automatic '+esc(name)+' connection guide" tabindex="-1" aria-hidden="true" inert sandbox srcdoc="'+esc(doc)+'"></iframe></div>'+readableGuide(steps.map(function(step){return {title:step[0],detail:step[1]};}))+'<div class="gp-film-tools"><button class="gp-text" data-action="film-toggle" aria-label="Pause guide">Ⅱ</button></div>';
      }
      return settingsFrame('<div class="gp-host-guide"><p class="gp-section-copy">'+(demo?'Illustration only. No app is connected here.':'Complete the connection in your AI app')+'</p>'+visual+'<div class="gp-mcp-body"><ol class="gp-section-copy">'+steps.map(function(step){return '<li>'+esc(step[1])+'</li>';}).join('')+'</ol>'+(demo?'<a class="gp-text" href="/">Return to live Grove for actual access</a>':mcpLink())+'<p class="gp-section-copy">Review access in Grove before approval.</p></div></div>','connections',[['Connections','host-back'],['Use Grove in '+name]]);
    }
    var id=ui.host,label=PROVIDERS[id].label,connected=(api.getState().connectedHosts||[]).includes(id);return shell('<header class="gp-popup-top"><button class="gp-text" data-action="host-back">← Back</button>'+close()+'</header>'+heading('',connected?'Grove is with you':'Use Grove in '+label,connected?'Your context is ready in '+label:'Your brain, wherever you think')+'<div class="gp-connection-route"><span class="gp-grove-mini">g<span></span></span><i></i><span>'+icon(id)+'</span></div><button class="gp-primary gp-wide" data-action="host-connect">'+(connected?'Done':'Connect Grove to '+label)+'</button>','gp-connect-dialog',true);}
  function sourcesPanel(){var tab=ui.sourceTab;return settingsFrame('<div class="gp-source-tabs" role="tablist">'+[['chats','Chats'],['apps','Synced apps'],['files','Files & notes']].map(function(t){return '<button role="tab" aria-selected="'+(tab===t[0])+'" data-source-tab="'+t[0]+'">'+t[1]+'</button>';}).join('')+'</div><div class="gp-add-content">'+(tab==='chats'?'<p class="gp-section-copy">Bring the conversations that know you</p><div class="gp-source-choices">'+AI_PROVIDERS.map(function(id){return sourceChoice(id,PROVIDERS[id].label);}).join('')+'</div>':tab==='apps'?integrationChoices():filesComposer())+'</div>','sources',[['Sources','sources-back'],['Add a source']]);}
  function setStep(n){ui.step=n;ui.guide=null;if(api.setStep)api.setStep(n);render(true);if(window.GroveView)window.GroveView.fit();}
  function finish(){ui.guide=null;ui.panel=null;var duration=reduceMotion.matches?0:1300;document.body.classList.add('gp-arriving');tourAfter=performance.now()+duration+900;api.completeOnboarding();render(true);if(window.GroveView)window.GroveView.fit(duration);clearTimeout(arrivalTimer);arrivalTimer=setTimeout(function(){document.body.classList.remove('gp-arriving');},duration);setTimeout(maybeTour,duration+950);}
  function importSource(id,file,provider){
    // Recording release: the chosen file triggers only the isolated invented-data page.
    // Do not read, serialize, upload, or retain that file. The real API remains unchanged.
    if(live&&file&&(provider==='claude'||provider==='chatgpt')){
      window.location.assign('/demo/onboarding?provider='+provider+'&start=1');
      return;
    }
    if(live){
      if(!canUpload(provider))return;
      var upload=liveUpload(provider);
      var context=api.pendingExportContext();
      function current(){return api.isPendingExportContext(context)&&liveUpload(provider)===upload;}
      if(upload.pending)return;
      upload.pending=true;upload.result=null;upload.error='';render(true);
      live.uploadExport(provider,file).then(function(res){
        if(!current())return;
        if(!res.ok){upload.error='HTTP '+res.status+': '+live.errorText(res.body,'The upload failed. Please try again.');return;}
        var result=res.body;
        if(!result||!['taken_in','nothing_new','kept_out','failed'].includes(result.state)){upload.error='The server returned an unrecognized upload result. Keep this export and try again.';return;}
        upload.result=result;
        if((result.state==='taken_in'||result.state==='nothing_new')&&result.failed_chats===0){
          upload.file=null;
          if(result.reminder&&result.reminder.status==='updated'&&result.reminder.snapshot)api.applyPendingExports(result.reminder.snapshot,context);
          if(result.reminder&&result.reminder.status==='unavailable')upload.error='The import succeeded. Grove could not update the export reminder. Keep the export and retry to clear it.';
          refreshLiveData(true);
        }
      }).catch(function(error){if(current())upload.error=error.message||'The upload could not reach Grove. Please try again.';}).finally(function(){upload.pending=false;if(current())render(true);});
      return;
    }
    ui.error='';api.importSource(id,{name:file.name,size:file.size,provider:provider,update:!!accountActive(provider),label:PROVIDERS[provider]&&PROVIDERS[provider].label}).then(function(){ui.guide=null;render(true);var el=modalRoot.querySelector('[data-source-choice="'+provider+'"] .gp-source-orb'),r=el&&el.getBoundingClientRect();if(window.GroveMotion)window.GroveMotion.source(provider,r?{x:r.left+r.width/2,y:r.top+r.height/2}:null);}).catch(function(){ui.error='That source could not be added. Please try again.';render(true);});
  }
  function requestPendingExport(){
    if(!live){if(api.requestExport)api.requestExport(ui.guide);ui.guideStage=1;ui.uploadFile=null;render(true);return;}
    if(ui.reminderRequest)return;
    var provider=ui.guide,context=api.pendingExportContext(),request={};
    ui.reminderRequest=request;ui.error='';render(true);
    api.requestExport(provider).then(function(applied){
      if(ui.reminderRequest!==request||!api.isPendingExportContext(context))return;
      if(applied&&ui.guide===provider){ui.guideStage=1;ui.uploadFile=null;ui.reminderError='';}
    }).catch(function(error){
      if(ui.reminderRequest===request&&api.isPendingExportContext(context))ui.error=error.message||'Grove could not save your export reminder. Try again.';
    }).finally(function(){if(ui.reminderRequest===request){ui.reminderRequest=null;render(true);}});
  }
  function focus(selector){requestAnimationFrame(function(){var el=root.querySelector(selector||'#gp-title')||root.querySelector('#gp-title');if(el)el.focus({preventScroll:true});});}
  function settleEntrance(){
    var surface=modalRoot.firstElementChild;
    // A background Safari tab can suspend its animation timeline. Essential
    // content must still reach its visible state; ongoing activity is untouched.
    setTimeout(function(){
      if(!surface||!surface.isConnected)return;
      surface.getAnimations({subtree:true}).forEach(function(animation){
        var timing=animation.effect&&animation.effect.getComputedTiming();
        if(timing&&Number.isFinite(timing.endTime)&&timing.endTime<=1800&&animation.playState!=='finished')animation.finish();
      });
    },1800);
  }
  function render(force){var s=api.getState(),onboard=!s.auth||!s.onboardingComplete;if(!s.auth&&!ui.authStage)ui.authStage='signin';if(!logoutPage()&&(ui.panel!=='profile'||ui.settingsTab!=='account'))ui.logoutError='';var shape=[onboard,ui.step,ui.panel,ui.guide,ui.guideStep,ui.guideStage,ui.settingsTab,ui.host,ui.authStage,ui.sourceTab,ui.filePicker,ui.noteEditor,ui.confirm&&JSON.stringify(ui.confirm),ui.requestTab,ui.requestId,(s.requests||[]).map(function(r){return r.id+':'+r.status;}).join(','),Object.keys(s.pendingExports).join(','),Object.keys(s.accountImports).map(function(id){return id+':'+s.accountImports[id].status;}).join(','),(s.connectedHosts||[]).join(','),(s.mcpClients||[]).map(function(c){return c.id;}).join(',')].join('|');
    if(force||shape!==lastShape){lastShape=shape;var content=ui.authStage?signIn():logoutPage()?logoutConfirmation():(live||demo)&&ui.host?hostGuide():ui.guide?guide():onboard?(ui.step===0?welcome():setup()):ui.panel==='profile'?profile():ui.panel==='health'?health():ui.panel==='connections'?connections():ui.panel==='sources'?sourcesPanel():ui.panel==='requests'?requestsPanel():'';if(ui.confirm)content='<div class="gp-confirm-context" inert aria-hidden="true">'+content.replace('aria-modal="true"','aria-modal="false"')+'</div>'+confirmation();modalRoot.innerHTML=content;if(ui.error&&content){var err=document.createElement('p');err.className='gp-footnote';err.setAttribute('role','alert');err.textContent=ui.error;var dialog=modalRoot.querySelector('.gp-auth-form')||modalRoot.querySelector('section');if(dialog)dialog.appendChild(err);}bind();if(ui.confirmScroll!=null){var pane=modalRoot.querySelector('.gp-settings-content');if(pane)pane.scrollTop=ui.confirmScroll;if(!ui.confirm)ui.confirmScroll=null;}fitGuide();positionRequests();if(window.GroveEntryScene)window.GroveEntryScene.mount(modalRoot.querySelector('.gp-entry-canvas'));settleEntrance();if(force&&content){focus(ui.confirm?'#gp-confirm-title':ui.confirmReturn||'#gp-title');if(!ui.confirm)ui.confirmReturn=null;}}
    document.body.classList.toggle('gp-onboarding-open',onboard);document.body.classList.toggle('gp-welcome-open',onboard&&ui.step===0&&!ui.authStage);document.body.classList.toggle('gp-auth-open',!!ui.authStage);document.body.classList.toggle('gp-modal-open',(!!ui.panel&&ui.panel!=='requests')||!!ui.guide||!!((live||demo)&&ui.host));document.body.classList.toggle('gp-tour-open',!!ui.tour);
    document.body.classList.toggle('gp-logout-open',!!logoutPage());
    var fullModal=onboard||logoutPage()||!!((live||demo)&&ui.host)||!!ui.guide||['profile','health','connections','sources'].includes(ui.panel);
    document.querySelectorAll('.topbar,#search-overlay,#selection-pill,#legend,#canvas,#detail').forEach(function(el){el.inert=fullModal;});
    document.getElementById('profile-button').hidden=onboard;document.getElementById('profile-button').setAttribute('aria-expanded',String(ui.panel==='profile'||ui.panel==='sources'));
    var exportCount=pendingExports(s).length,profileLabel=exportCount?'Settings, '+exportCount+' conversation '+(exportCount===1?'export':'exports')+' to upload':needsHistory(s)?'Settings, conversation history to add':'Profile and settings';
    var profileButton=document.getElementById('profile-button');profileButton.innerHTML='<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>';profileButton.classList.toggle('has-pending',needsHistory(s));profileButton.setAttribute('aria-label',profileLabel);profileButton.setAttribute('title',exportCount?exportCount+' conversation '+(exportCount===1?'export':'exports')+' to upload':needsHistory(s)?'Add your conversation history':'Settings');
    var bottom=onboard?'':'<button class="gp-accounts-trigger" data-action="connections" aria-label="Take Grove to your AI"><span class="gp-host-cta">Take Grove to your AI '+arrow()+'</span><span class="gp-provider-stack">'+HOST_ORDER.map(function(id){return '<span class="'+(!live&&s.connectedHosts.includes(id)?'is-connected':'')+'">'+icon(id)+'<small>'+PROVIDERS[id].label+'</small></span>';}).join('')+'</span></button>'+(s.currentNodeCount?'<button class="gp-types-toggle" data-action="types">Node types</button>':'');
    if(chrome.innerHTML!==bottom){chrome.innerHTML=bottom;bindChrome();}
    var rq=document.getElementById('requests-button'),pending=(s.requests||[]).filter(function(r){return r.status==='open';}).length;rq.hidden=onboard;rq.classList.toggle('has-pending',pending>0);rq.setAttribute('aria-label','Grove needs your input'+(pending?', '+pending+(pending===1?' question':' questions'):''));rq.setAttribute('aria-expanded',String(ui.panel==='requests'));
    sessionNote.textContent=ui.offline?SIGNIN_COPY.offline:(ui.reminderError||'');sessionNote.hidden=!sessionNote.textContent;
    if(live&&window.GroveMaterials)window.GroveMaterials.mount(modalRoot);
    updateLive();renderTour();
    if(live)refreshLiveData(false);
  }
  function updateLive(){
    if(live){
      progress.innerHTML='';document.getElementById('learning-slot').innerHTML='';
      var state=api.getState();
      root.querySelectorAll('[data-source-choice]').forEach(function(el){
        var id=el.dataset.sourceChoice,added=liveSourceAccounts().some(function(a){return a.id===id&&a.readCount>0;}),pending=!!state.pendingExports[id];
        el.classList.toggle('is-added',added);el.classList.remove('is-learning');
        var label=el.querySelector('strong small');if(label)label.textContent=added?'Added':pending?'Upload':'Connect';
        el.setAttribute('aria-label',(added?'Added ':pending?'Upload ':'Connect ')+(PROVIDERS[id]||{label:id}).label);
      });
      return;
    }
    var s=api.getState(),accounts=sourceAccounts(s).filter(function(a){return a.status==='importing';}),jobs=accounts.filter(function(a){return a.status==='importing';});
    document.getElementById('learning-slot').innerHTML='';
    var markup=(!s.onboardingComplete&&ui.step===0)?'':accounts.map(function(a){return '<span class="gp-account-progress '+(a.status==='ready'?'is-ready':'')+'" data-progress-id="'+a.id+'" aria-label="'+a.label+', '+(a.status==='ready'?'connected':a.progress+' percent')+'" style="--progress:'+a.progress/100+'"><svg class="gp-mini-ring" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="18"/></svg><span class="gp-mini-mark">'+(a.kind==='file'?icon('notes'):appMark(a.id))+'</span></span>';}).join('');
    if(progress.dataset.accounts!==accounts.map(function(a){return a.id;}).join('|')||!progress.firstChild){progress.innerHTML=markup;progress.dataset.accounts=accounts.map(function(a){return a.id;}).join('|');}
    accounts.forEach(function(a){var el=progress.querySelector('[data-progress-id="'+a.id+'"]');if(el){el.style.setProperty('--progress',a.progress/100);el.classList.toggle('is-ready',a.status==='ready');el.setAttribute('aria-label',a.label+', '+(a.status==='ready'?'connected':a.progress+' percent'));}root.querySelectorAll('[data-account-status="'+a.id+'"]').forEach(function(el){el.textContent=a.status==='ready'?'':'Adding · '+a.progress+'%';});});
    if(!accounts.length)progress.innerHTML='';
    sourceAccounts(s).forEach(function(a){root.querySelectorAll('[data-account-status="'+a.id+'"]').forEach(function(el){el.textContent=a.status==='disconnected'?'Disconnected':a.status==='ready'?'':'Adding · '+a.progress+'%';});});
    root.querySelectorAll('[data-source-state]').forEach(function(el){el.textContent=sourceState(el.dataset.sourceState);});
    root.querySelectorAll('[data-provider-state]').forEach(function(el){var id=el.dataset.providerState,p=PROVIDERS[id],so=source(p.source);el.textContent=(so.providers||[so.provider]).includes(id)&&so.status!=='idle'?sourceState(p.source):'';});
    root.querySelectorAll('[data-count]').forEach(function(el){el.textContent=(el.dataset.count==='nodes'?s.currentNodeCount:s.currentEdgeCount).toLocaleString();});
    root.querySelectorAll('[data-source-choice]').forEach(function(el){var id=el.dataset.sourceChoice,p=PROVIDERS[id],so=source(p?p.source:id),added=id==='calendar'?so.status!=='idle':(so.providers||[so.provider]).includes(id)&&so.status!=='idle';if(s.accountImports&&s.accountImports[id]){so=s.accountImports[id];added=so.status!=='disconnected';}el.classList.toggle('is-added',added);el.classList.toggle('is-learning',added&&so.status==='importing');el.style.setProperty('--source-progress',added?(so.progress||100)/100:0);var verb=added?(so.status==='ready'?'':'Adding'):((s.pendingExports||{})[id]?'Upload':id==='notes'?'Add':'Connect');el.querySelector('strong small').textContent=verb;el.setAttribute('aria-label',(verb||'Connected to')+' '+(p?p.label:'Calendar'));});
    root.querySelectorAll('[data-integration]').forEach(function(el){var id=el.dataset.integration,added=id==='google'?s.googleConnected:!!(s.accountImports&&s.accountImports[id]&&s.accountImports[id].status!=='disconnected');el.classList.toggle('is-connected',added);el.setAttribute('aria-pressed',String(added));});
    sourceAccounts(s).forEach(function(a){['nodes','edges'].forEach(function(k){if(!a.contribution)return;root.querySelectorAll('[data-contribution-'+k+'="'+a.id+'"]').forEach(function(el){el.textContent=a.contribution[k];});});});
    root.querySelectorAll('[data-growth-count]').forEach(function(el){var delta=el.dataset.growthCount==='nodes'?s.nodeDelta:s.edgeDelta;el.textContent='+'+delta.toLocaleString();el.closest('button').hidden=!delta;});
    root.querySelectorAll('[data-growth-chart]').forEach(function(el){el.innerHTML=growthSparkline(el.dataset.growthChart);});
    var care=careSummary();root.querySelectorAll('[data-care]').forEach(function(el){var k=el.dataset.care;el.textContent=k==='state'?(care.running?'At work':'Waiting for sources'):k==='fresh'?care.fresh+'%':care[k];});root.querySelectorAll('[data-care-words]').forEach(function(el){var k=el.dataset.careWords;el.textContent=careWords(k,care[k]);});var bar=root.querySelector('[data-care-bar]');if(bar)bar.style.width=care.fresh+'%';
    var healthState=document.getElementById('health-state');if(healthState)healthState.classList.toggle('is-learning',!!jobs.length);
  }
  function handle(action){
    if(action==='refresh-live-data'){refreshLiveData(true);if(ui.panel==='health'&&live&&window.GroveCare&&window.GroveCare.refresh)window.GroveCare.refresh();render(true);}
    else if(action==='start')move(function(){setStep(1);});
    else if(action==='auth-close'){authEpoch++;ui.authStage=null;render(true);}
    else if(action==='auth-back'){authEpoch++;ui.error='';ui.authStage='signin';ui.code='';render(true);}
    else if(action==='edit-name'){ui.editName=true;ui.nameDraft=api.getState().name||(live?'':'Anas');render(true);focus('#gp-name');}
    else if(action==='cancel-name'){ui.editName=false;render(true);}
    else if(action==='request-prev'||action==='request-next'){var items=api.getState().requests.filter(function(r){return r.status==='open';}),i=items.findIndex(function(r){return r.id===ui.requestId;});var next=Math.max(0,Math.min(items.length-1,i+(action==='request-prev'?-1:1)));if(items[next])ui.requestId=items[next].id;render(true);}
    else if(action==='copy-mcp'){var input=modalRoot.querySelector('#gp-mcp-url');function copied(){ui.mcpCopied=true;var b=modalRoot.querySelector('[data-action="copy-mcp"]');if(b)b.textContent='Copied';}function select(){if(input){input.select();try{if(document.execCommand('copy'))copied();}catch(e){}}}if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(MCP_URL).then(copied,select);else select();}
    else if(action==='choose-files'){if(live)return;ui.filePicker=true;ui.noteEditor=false;render(true);}
    else if(action==='cancel-files'){ui.filePicker=false;render(true);}
    else if(action==='write-note'){if(live)return;ui.noteEditor=true;ui.filePicker=false;render(true);}
    else if(action==='cancel-note'){ui.noteEditor=false;render(true);}
    else if(action==='connections-back'){ui.panel=ui.returnPanel;render(true);}
    else if(action==='source-picker'){ui.panel='sources';ui.settingsTab='sources';ui.guide=null;ui.query='';render(true);}
    else if(action==='sources-back'){ui.guide=null;ui.panel='profile';ui.settingsTab='sources';ui.filePicker=false;ui.noteEditor=false;render(true);}
    else if(action==='manage-sources'){ui.panel='profile';ui.settingsTab='sources';render(true);}
    else if(action==='watch-care'){ui.panel=null;render();var toggle=document.getElementById('agents-toggle');if(!toggle.checked)toggle.click();}
    else if(action==='delete-brain'){openConfirmation('brain','');ui.confirmReturn='[data-action="delete-brain"]';}
    else if(action==='cancel-confirm'){ui.confirm=null;render(true);}
    else if(action==='confirm-delete'){
      var c=ui.confirm;ui.confirm=null;
      if(c.type==='disconnect'){api.disconnectSource(c.id);render(true);}
      else if(c.type==='file'){api.removeFile(c.id);render(true);}
    }
    else if(action==='pick-export'){if(live){var input=modalRoot.querySelector('[data-pick-export]');if(input&&!liveUpload(ui.guide).pending)input.click();return;}var p=PROVIDERS[ui.guide];ui.uploadFile={name:p.file,size:240000};render(true);}
    else if(action==='back'&&ui.step>1)move(function(){setStep(ui.step-1);});
    else if(action==='next'||action==='skip')move(function(){if(ui.step<4)setStep(ui.step+1);else finish();});
    else if(action==='guide-up'){if(ui.guideStage===1){ui.guideStage=0;ui.uploadFile=null;}else{ui.guide=null;ui.guideStep=0;}render(true);}
    else if(action==='guide-back'){ui.guide=null;ui.guideStep=0;ui.uploadFile=null;render(true);}
    else if(action==='export-requested'){requestPendingExport();}
    else if(action==='upload-stage'){requestPendingExport();}
    else if(action==='export-stage'){ui.editName=false;ui.nameDraft='';ui.guideStage=0;ui.uploadFile=null;render(true);}
    else if(action==='upload-export'){var file=live?liveUpload(ui.guide).file:ui.uploadFile;if(!file)return;var p=PROVIDERS[ui.guide];importSource(p.source,file,ui.guide);if(!live)ui.uploadFile=null;}
    else if(action==='film-toggle'){
      var frame=root.querySelector('.gp-app-frame iframe'),button=root.querySelector('[data-action=film-toggle]'),track=root.querySelector('.gp-film-track i');
      if(frame){
        var paused=frame.dataset.paused!=='true',elapsed=Number(frame.dataset.elapsed)||0;
        if(paused)elapsed+=(performance.now()-Number(frame.dataset.started))/1000;
        frame.dataset.paused=String(paused);frame.dataset.elapsed=String(elapsed);frame.dataset.started=String(performance.now());
        var doc=frame.dataset.base.replace(/animation-delay:-([\d.]+)s/g,function(_,n){return 'animation-delay:-'+(Number(n)+elapsed)+'s';});
        if(paused){
          var count=Number((frame.dataset.base.match(/data-frame-count="(\d+)"/)||[])[1])||1;
          var seconds=Number((frame.dataset.base.match(/data-frame-seconds="([\d.]+)"/)||[])[1])||3;
          var selected=reduceMotion.matches?0:Math.floor(elapsed/seconds)%count;
          doc=doc.replace('</style>','*{animation-play-state:paused!important}.film-frame{animation:none!important;opacity:0!important;visibility:hidden!important}.film-frame:nth-child('+(selected+1)+'){opacity:1!important;visibility:visible!important}</style>');
        }
        frame.setAttribute('srcdoc',doc);if(track)track.style.animationPlayState=paused?'paused':'running';button.textContent=paused?'▷':'Ⅱ';button.setAttribute('aria-label',paused?'Play guide':'Pause guide');syncGuideText();
      }
    }
    else if(action==='guide-next')move(function(){ui.guideStep++;render(true);});
    else if(action==='guide-prev')move(function(){ui.guideStep=Math.max(0,ui.guideStep-1);render(true);});
    else if(action==='logout'){if(demo){window.location.assign('/');return;}if(live){logout();return;}endTour();ui.panel=null;ui.guide=null;ui.authStage='signin';ui.error='';ui.email='';api.signOut();render(true);}
    else if(action==='download-data'){var payload=JSON.stringify(api.getState(),null,2),a=document.createElement('a');a.href='data:application/json;charset=utf-8,'+encodeURIComponent(payload);a.download='my-grove.json';a.click();}
    else if(action==='close'){ui.confirm=null;ui.panel=null;ui.guide=null;ui.host=null;render();}
    else if(action==='calendar')importSource('calendar',{name:'Google Calendar',size:0},'calendar');
    else if(action==='connections'){endTour();ui.panel='profile';ui.settingsTab='connections';ui.guide=null;ui.host=null;render(true);}
    else if(action==='sources'){ui.panel='sources';ui.guide=null;render(true);}
    else if(action==='host-back'){ui.host=null;render(true);}
    else if(action==='host-connect'){if(live)return;if((api.getState().connectedHosts||[]).includes(ui.host)){ui.host=null;render(true);}else{api.connectHost(ui.host);render(true);}}
    else if(action==='types')document.body.classList.toggle('gp-types-open');
    else if(action==='tour'){ui.panel=null;ui.tour=1;ui.tourNode=null;render();}
  }
  function bindActions(scope){scope.querySelectorAll('[data-action]').forEach(function(el){el.onclick=function(){handle(el.dataset.action);};});}
  function bindChrome(){bindActions(chrome);}
  function connectInline(id){
    if(ui.connecting[id]||(id==='google'?api.getState().googleConnected:accountActive(id)))return;
    ui.connecting[id]=true;refreshIntegrations();var p=PROVIDERS[id],epoch=uiEpoch;
    var job=id==='google'?api.connectGoogle():api.importSource(p.source,{provider:id,name:p.label,label:p.label});
    job.then(function(){if(epoch!==uiEpoch)return;delete ui.connecting[id];refreshIntegrations();if(window.GroveMotion)window.GroveMotion.source(id);});
  }
  function refreshIntegrations(){var results=modalRoot.querySelector('#gp-integration-results');if(results){results.innerHTML=integrationResults();bindIntegrations();}}
  function bindIntegrations(){modalRoot.querySelectorAll('[data-integration]').forEach(function(el){el.onclick=function(){var id=el.dataset.integration,app=catalog.find(function(a){return a.id===id;});if(demo||!app||app.auth==='OAuth'||accountActive(id)){connectInline(id);return;}var slot=modalRoot.querySelector('#gp-inline-auth');if(!slot){connectInline(id);return;}slot.innerHTML='<form class="gp-inline-key"><label>'+esc(app.name)+' access key<input aria-label="Access key" type="password" value="grove-demo-key" autocomplete="off"></label><button class="gp-small">Connect</button></form>';slot.querySelector('form').onsubmit=function(e){e.preventDefault();slot.innerHTML='';connectInline(id);};};});}
  function bind(){bindActions(modalRoot);modalRoot.querySelectorAll('[data-guide]').forEach(function(el){el.onclick=function(){if((live||demo)&&el.dataset.guide==='muse'){ui.guide=null;ui.host='muse';ui.mcpCopied=false;render(true);return;}var snap=window.GroveMotion&&window.GroveMotion.capture(el.querySelector('.gp-choice-mark,.gp-provider-icon'));ui.guide=el.dataset.guide;ui.guideStep=0;ui.uploadFile=null;ui.guideStage=api.getState().pendingExports[ui.guide]?1:0;if(window.GroveMotion)window.GroveMotion.hover(null);render(true);if(window.GroveMotion)window.GroveMotion.morph(snap,modalRoot.querySelector('.gp-provider-icon'));};});modalRoot.querySelectorAll('[data-import]').forEach(function(el){el.onclick=function(){var p=PROVIDERS[el.dataset.import];importSource(p.source,{name:p.file,size:240000},el.dataset.import);};});modalRoot.querySelectorAll('[data-file]').forEach(function(el){el.onchange=function(){var f=el.files&&el.files[0],id=el.dataset.file;if(f)importSource(PROVIDERS[id].source,f,id);};});modalRoot.querySelectorAll('[data-host]').forEach(function(el){el.onclick=function(){ui.host=el.dataset.host;render(true);};});modalRoot.querySelectorAll('[data-pick-export]').forEach(function(el){el.onchange=function(){if(live){var upload=liveUpload(el.dataset.pickExport);if(upload.pending)return;upload.file=el.files&&el.files[0]||null;upload.result=null;upload.error='';}else ui.uploadFile=el.files&&el.files[0]||null;render(true);};});
    modalRoot.querySelectorAll('[data-settings-tab]').forEach(function(el){el.onclick=function(){ui.host=null;ui.settingsTab=el.dataset.settingsTab;ui.panel='profile';ui.guide=null;ui.editName=false;ui.confirm=null;ui.notice='';render(true);};});
    modalRoot.querySelectorAll('[data-upload-provider]').forEach(function(el){el.onclick=function(){ui.guide=el.dataset.uploadProvider;ui.guideStage=1;ui.uploadFile=null;render(true);};});
    modalRoot.querySelectorAll('[data-retry-asset]').forEach(function(el){el.onclick=function(){var key=el.dataset.retryAsset==='guides'?'guide':'catalog';ui[key+'Error']='';render(true);};});
    bindIntegrations();
    if(live&&window.GrovePipedream)window.GrovePipedream.mount(modalRoot);
    var mcpDetails=modalRoot.querySelector('[data-mcp-details]');if(mcpDetails)mcpDetails.ontoggle=function(){ui.mcpOpen=mcpDetails.open;};
    var mcpForm=modalRoot.querySelector('[data-mcp-form]');if(!live&&mcpForm){mcpForm.oninput=function(){ui.mcpDraft=mcpForm.elements.client.value;};mcpForm.onsubmit=function(e){e.preventDefault();var name=mcpForm.elements.client.value.trim();if(!name)return;ui.mcpDraft='';api.addMcpClient(name);render(true);};}
    modalRoot.querySelectorAll('[data-mcp-remove]').forEach(function(el){el.onclick=function(){if(live)return;api.removeMcpClient(el.dataset.mcpRemove);render(true);};});
    modalRoot.querySelectorAll('[data-growth-kind]').forEach(function(el){el.onclick=function(){ui.panel=null;render();if(window.GroveView)window.GroveView.showChanges(el.dataset.growthKind);};});
    var search=modalRoot.querySelector('[data-integration-search]');if(search)search.oninput=function(){ui.query=search.value;refreshIntegrations();};
    modalRoot.querySelectorAll('[data-host-inline]').forEach(function(el){el.onclick=function(){var id=el.dataset.hostInline;if(live||demo){ui.host=id;ui.mcpCopied=false;render(true);return;}if(api.getState().connectedHosts.includes(id)){api.disconnectHost(id);render(true);return;}api.connectHost(id);render(true);};});
    modalRoot.querySelectorAll('[data-source-toggle]').forEach(function(el){el.onclick=function(){var id=el.dataset.sourceToggle;if(accountActive(id)){openConfirmation('disconnect',id);}else {api.updateSource(id).then(function(){render(true);});}};});
    modalRoot.querySelectorAll('[data-signin]').forEach(function(el){el.onclick=function(){chooseSignIn(el.dataset.signin);};});
    modalRoot.querySelectorAll('[data-guide-device]').forEach(function(el){el.onclick=function(){ui.guideDevice=el.dataset.guideDevice;render(true);};});
    modalRoot.querySelectorAll('[data-request-tab]').forEach(function(el){el.onclick=function(){ui.requestTab=el.dataset.requestTab;ui.requestId=null;render(true);};});
    modalRoot.querySelectorAll('[data-request-id]').forEach(function(el){el.onclick=function(){ui.requestId=el.dataset.requestId;render(true);};});
    var requestForm=modalRoot.querySelector('[data-request-form]');if(requestForm){requestForm.oninput=function(){ui.requestDrafts[requestForm.dataset.request]=requestForm.elements.answer.value;};requestForm.onsubmit=function(e){e.preventDefault();var id=requestForm.dataset.request,answer=requestForm.elements.answer.value;if(!answer.trim())return;delete ui.requestDrafts[id];api.answerRequest(id,answer);render(true);};}
    var emailForm=modalRoot.querySelector('[data-email-form]');if(emailForm)emailForm.oninput=function(){ui.email=emailForm.elements.email.value;};if(emailForm)emailForm.onsubmit=function(e){e.preventDefault();startEmail(emailForm.elements.email.value);};
    var codeForm=modalRoot.querySelector('[data-code-form]');if(codeForm)codeForm.oninput=function(){ui.code=codeForm.elements.code.value;};if(codeForm)codeForm.onsubmit=function(e){e.preventDefault();verifyEmail(codeForm.elements.code.value);};
    modalRoot.querySelectorAll('[data-connect-source]').forEach(function(el){el.onclick=function(){var id=el.dataset.connectSource;if(id==='google'){api.connectGoogle().then(function(){ui.guide=null;render(true);if(window.GroveMotion)window.GroveMotion.source('gmail');});}else{var p=PROVIDERS[id];importSource(p.source,{name:p.label,size:0},id);}};});
    modalRoot.querySelectorAll('[data-source-tab]').forEach(function(el){el.onclick=function(){ui.sourceTab=el.dataset.sourceTab;ui.query='';render(true);};});
    modalRoot.querySelectorAll('[data-add-group]').forEach(function(el){el.onclick=function(){ui.sourceTab=el.dataset.addGroup;ui.panel='sources';ui.query='';ui.filePicker=false;ui.noteEditor=false;render(true);};});
    modalRoot.querySelectorAll('[data-remove-file]').forEach(function(el){el.onclick=function(){openConfirmation('file',el.dataset.removeFile);};});
    modalRoot.querySelectorAll('[data-source-value]').forEach(function(el){el.onclick=function(){ui.panel=null;render();if(window.GroveView)window.GroveView.showChanges('nodes',el.dataset.sourceValue);};});
    var filesForm=modalRoot.querySelector('[data-files-form]');if(!live&&filesForm)filesForm.onsubmit=function(e){e.preventDefault();var names=Array.from(filesForm.querySelectorAll('input:checked')).map(function(i){return i.value;});if(!names.length)return;ui.filePicker=false;Promise.all(names.map(function(name){return api.addFile(name);})).then(function(){render(true);if(window.GroveMotion)window.GroveMotion.source('notes');});};
    var noteForm=modalRoot.querySelector('[data-note-form]');if(noteForm)noteForm.oninput=function(){ui.noteDraft={title:noteForm.elements.title.value,body:noteForm.elements.body.value};};if(!live&&noteForm)noteForm.onsubmit=function(e){e.preventDefault();var name=noteForm.elements.title.value+'.md',body=noteForm.elements.body.value;ui.noteEditor=false;ui.noteDraft={title:'',body:''};api.addFile(name,body,'note').then(function(){render(true);});};
    var growthFormEl=modalRoot.querySelector('[data-growth-form]');if(growthFormEl)growthFormEl.oninput=function(){var f=growthFormEl.elements;ui.growthDraft={focus:f.focus.value,reading:f.reading.value,avoid:f.avoid.value,confirm:f.confirm.checked};};if(growthFormEl)growthFormEl.onsubmit=function(e){e.preventDefault();var f=growthFormEl.elements;api.saveGrowth({focus:f.focus.value,reading:f.reading.value,avoid:f.avoid.value,confirm:f.confirm.checked});ui.notice=live?'Saved on this browser':'Saved';ui.growthDraft=null;render(true);};
    var form=modalRoot.querySelector('[data-name-form]');if(form){form.oninput=function(){ui.nameDraft=form.elements.name.value;};form.onsubmit=function(e){e.preventDefault();var name=form.elements.name.value.trim();if(!name)return;ui.editName=false;api.setName(name);render(true);};}}
  function maybeTour(){if(performance.now()<tourAfter)return;var s=api.getState();if(s.onboardingComplete&&!s.tourComplete&&!ui.panel&&!ui.guide&&!ui.tour&&s.currentNodeCount){ui.tour=1;ui.tourNode=null;renderTour();}}
  function endTour(){if(ui.tour){ui.tour=0;document.body.classList.remove('gp-tour-open','gp-tour-workers','gp-tour-ai');ui.tourNode=null;lastTourShape='';if(api.completeTour)api.completeTour();tourRoot.innerHTML='';}}
  function tourTarget(){return ui.tour===2?document.getElementById('detail'):ui.tour===3?document.getElementById('profile-button'):ui.tour===4?document.getElementById('agents-toggle').closest('label'):ui.tour===5?document.querySelector('.hood-score'):ui.tour===6?chrome.querySelector('.gp-accounts-trigger'):null;}
  function tourBounds(){
    if(ui.tour===1){var n=window.GroveView&&window.GroveView.tourNode();if(!n)return null;ui.tourNode=n;return {left:n.x-n.r-16,top:n.y-n.r-12,width:(n.r+16)*2,height:(n.r+12)*2+20,round:12};}
    var target=tourTarget();if(!target)return null;var r=target.getBoundingClientRect();return {left:r.left-6,top:r.top-6,width:r.width+12,height:r.height+12,round:8};
  }
  function positionTour(){
    if(!ui.tour)return;var r=tourBounds(),hole=tourRoot.querySelector('#gp-tour-hole'),card=tourRoot.querySelector('.gp-tour-card'),spot=tourRoot.querySelector('.gp-node-spotlight');if(!r||!hole||!card)return;
    hole.setAttribute('x',Math.max(0,r.left));hole.setAttribute('y',Math.max(0,r.top));hole.setAttribute('width',r.width);hole.setAttribute('height',r.height);hole.setAttribute('rx',r.round);
    if(spot){spot.hidden=ui.tour!==1;spot.style.left=r.left+'px';spot.style.top=r.top+'px';spot.style.width=r.width+'px';spot.style.height=r.height+'px';}
    var cw=card.offsetWidth,ch=card.offsetHeight,gap=24,x,y,w=innerWidth,h=innerHeight;
    if(ui.tour===1){x=r.left+r.width+gap;y=r.top+r.height/2-ch/2;if(x+cw>w-20)x=r.left-cw-gap;if(x<20){x=(w-cw)/2;y=r.top+r.height+gap;}}
    else if(ui.tour===2){x=r.left-cw-gap;y=r.top+Math.min(140,Math.max(32,r.height/4));if(x<20){x=(w-cw)/2;y=h-ch-88;}}
    else if(ui.tour===3||ui.tour===4){x=r.left+r.width-cw;y=r.top+r.height+gap;}
    else if(ui.tour===5){x=r.left-cw-gap;y=r.top+r.height/2-ch/2;if(x<16){x=r.left+r.width-cw;y=r.top-ch-gap;}}
    else{x=r.left;y=r.top-ch-gap;}
    x=Math.max(16,Math.min(w-cw-16,x));y=Math.max(20,Math.min(h-ch-20,y));
    card.style.left=Math.round(x)+'px';card.style.top=Math.round(y)+'px';
  }
  function renderTour(){
    document.body.classList.toggle('gp-tour-open',!!ui.tour&&!ui.panel);
    document.body.classList.toggle('gp-tour-workers',ui.tour===5&&!ui.panel);
    document.body.classList.toggle('gp-tour-ai',ui.tour===6&&!ui.panel);
    if(!ui.tour||ui.panel||!api.getState().onboardingComplete){tourRoot.innerHTML='';lastTourShape='';return;}
    var v=window.GroveView,node=v&&v.tourNode();if(!v||!node)return;ui.tourNode=node;
    if(!tourRoot.firstChild){tourRoot.innerHTML='<svg class="gp-tour-veil" width="100%" height="100%" aria-hidden="true"><defs><mask id="gp-tour-mask"><rect width="100%" height="100%" fill="white"/><rect id="gp-tour-hole" fill="black"/></mask></defs><rect width="100%" height="100%" fill="rgba(60,64,61,.22)" mask="url(#gp-tour-mask)"/></svg><button class="gp-node-spotlight"></button><section class="gp-tour-card" role="dialog" aria-label="Grove tour"></section>';}
    var shape=ui.tour+'|'+node.id;if(lastTourShape!==shape){lastTourShape=shape;
      var copy=[['Your world, connected','Open a node to see what your Grove knows'],['Follow the connections','Every detail leads to the people and ideas around it'],['Account & settings','Manage your account and sources here. Upload any missing chat exports when they’re ready — your reminders stay until you add them.'],['Meet your Grove workers','Learner adds context, Curator keeps it useful, and Connector finds links. Turn on this view to see them at work.'],['Watch your Grove grow','This panel shows what each worker has done. Follow one to watch it work, and use Freshness to see what Grove will refresh next. Hiding the view doesn’t stop their work.'],['Connect your AI','Take Grove’s context to the AI you use. Connect a familiar app or add your own with Custom MCP.']][ui.tour-1];
      if(live&&ui.tour===4)copy=['Your Grove workers','Open this view to check recorded worker activity.'];
      if(live&&ui.tour===5)copy=['Worker status','Read each worker’s recorded status and recent activity. Refresh for a new check.'];
      var card=tourRoot.querySelector('.gp-tour-card');card.innerHTML='<p class="gp-kicker">0'+ui.tour+' / 06</p><h2>'+copy[0]+'</h2><p class="gp-copy">'+copy[1]+'</p><footer><button class="gp-text" data-tour-skip>Skip tour</button><button class="gp-small" data-tour-next>'+(ui.tour===6?'Done':ui.tour===4?'Show workers':'Next')+'</button></footer>';
      var spot=tourRoot.querySelector('.gp-node-spotlight');spot.setAttribute('aria-label','Open highlighted '+node.title+' node');spot.onclick=function(){v.openNode(ui.tourNode.id);};
      card.querySelector('[data-tour-skip]').onclick=endTour;card.querySelector('[data-tour-next]').onclick=nextTour;
    }positionTour();
  }
  function nextTour(){
    if(ui.tour===6){endTour();return;}
    var v=window.GroveView;
    if(ui.tour===1){v.openNode(ui.tourNode.id);return;}
    if(ui.tour===2)v.close();
    ui.tour++;
    if(ui.tour===5){var toggle=document.getElementById('agents-toggle');if(!toggle.checked)toggle.click();}
    renderTour();
  }
  document.getElementById('agents-toggle').addEventListener('change',function(){
    if(ui.tour===4&&this.checked){ui.tour=5;renderTour();}
    else if(ui.tour===5&&!this.checked){ui.tour=4;renderTour();}
    else if(ui.tour&&this.checked&&ui.tour!==5){endTour();}
  });
  document.getElementById('requests-button').onclick=function(){endTour();ui.panel=ui.panel==='requests'?null:'requests';ui.guide=null;ui.requestTab='open';ui.requestId=null;render(true);};
  document.getElementById('profile-button').onclick=function(){endTour();ui.panel=ui.panel==='profile'?null:'profile';ui.settingsTab=needsHistory(api.getState())?'sources':'account';ui.guide=null;render(true);};
  document.getElementById('health-link').onclick=function(e){e.preventDefault();if(!api.getState().onboardingComplete)return;endTour();refreshLiveData(true);ui.panel=ui.panel==='health'?null:'health';ui.guide=null;if(live&&ui.panel==='health'&&window.GroveCare&&window.GroveCare.refresh)window.GroveCare.refresh();render(true);};
  document.addEventListener('keydown',function(e){if(e.key==='Escape'){if(ui.confirm){handle('cancel-confirm');}else if((live||demo)&&ui.host){ui.host=null;render(true);}else if(ui.guide){ui.guide=null;render();}else if(ui.panel){ui.panel=null;ui.host=null;render();}else if(ui.tour)endTour();}if(e.key==='Tab'&&modalRoot.querySelector('[aria-modal="true"]')){var activeDialog=modalRoot.querySelector('[aria-modal="true"]'),els=Array.from(activeDialog.querySelectorAll('button:not([disabled]),input:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]'));var first=els[0],last=els[els.length-1];if(!els.length)return;if(!activeDialog.contains(document.activeElement)){e.preventDefault();(e.shiftKey?last:first).focus();}else if(e.shiftKey&&(document.activeElement===first||!els.includes(document.activeElement))){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
  modalRoot.addEventListener('click',function(e){if(e.target.classList.contains('gp-confirm-backdrop')){handle('cancel-confirm');return;}if(e.target.classList.contains('gp-backdrop')&&!ui.confirm&&api.getState().onboardingComplete){ui.panel=null;ui.guide=null;ui.host=null;render();}});
  window.addEventListener('grove:care',updateLive);
  window.addEventListener('grove:worker-status',function(){if(live&&ui.panel==='health')render(true);});
  window.addEventListener('grove:unauthenticated',function(){if(live){liveData={owner:null};endSession();}});
  window.addEventListener('grove:change',function(){render();maybeTour();});
  window.addEventListener('grove:graph-rendered',function(){maybeTour();renderTour();});
  window.addEventListener('grove:node-opened',function(){if(ui.tour===1){ui.tour=2;renderTour();}});
  window.addEventListener('resize',function(){renderTour();positionRequests();});
  document.addEventListener('pointerdown',function(e){if(ui.panel==='requests'&&!e.target.closest('.gp-review-popover,#requests-button')){ui.panel=null;render();}});
  // Keep the clear opening aligned with the actual node or control.
  setInterval(positionTour,100);
  if(live){
    document.title=(logoutPage()?'Log out':accountMode()==='signup'?'Create account':live.isAuthPage()?'Log in':'Grove')+' · Grove';
    window.addEventListener('grove:session-changed',function(){endSession();ui.authStage='checking';render(true);checkSession();});
    window.addEventListener('pagehide',function(){
      if(window.GroveMaterials)window.GroveMaterials.reset();
      authEpoch++;live.invalidateSession();
      if(window.GroveView&&window.GroveView.clear)window.GroveView.clear();
      document.body.classList.add('gp-session-hidden');
    });
    window.addEventListener('pageshow',function(event){
      if(!event.persisted)return;
      endSession();ui.authStage='checking';render(true);
      document.body.classList.remove('gp-session-hidden');checkSession();
    });
    window.addEventListener('focus',function(){checkSession();});
    readSigninError();ui.authStage='checking';checkSession();
  }
  render(true);maybeTour();
})();
