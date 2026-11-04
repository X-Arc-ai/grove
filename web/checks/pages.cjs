// The two public pages as build.py makes them: the homepage and the privacy page say what a person
// and a reviewer must find, the privacy table is every row of docs/data-flow.md, nothing needs a
// server or a script to read the fallback, and the build refuses a missing or broken generator.
const fs=require('fs'),os=require('os'),path=require('path'),assert=require('assert'),cp=require('child_process');
const web=path.join(__dirname,'..'),doc=path.join(web,'..','docs','data-flow.md');
cp.execFileSync('python3',[path.join(web,'build.py')],{stdio:'ignore'});
const home=fs.readFileSync(path.join(web,'dist','home.html'),'utf8'),privacy=fs.readFileSync(path.join(web,'dist','privacy.html'),'utf8'),flow=fs.readFileSync(doc,'utf8');
const decode=s=>s.replace(/&quot;/g,'"').replace(/&#x27;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
// The page as a person reads it: no style, no script, no comment, no tag.
const words=html=>decode(html.replace(/<style[\s\S]*?<\/style>/g,' ').replace(/<script[\s\S]*?<\/script>/g,' ').replace(/<svg[\s\S]*?<\/svg>/g,' ').replace(/<!--[\s\S]*?-->/g,' ').replace(/<[^>]+>/g,' ')).replace(/\s+/g,' ');
const homeText=words(home),privacyText=words(privacy);

// Homepage.
for(const href of ['href="/signin"','href="/privacy"','href="mailto:contact@x-arc.ai"','href="https://x-arc.ai"'])assert(home.includes(href),'The homepage links '+href);
for(const text of ['Give your AI a world to understand','Grove builds a live, time-aware map of your world from the sources you bring','Your AI host reads it and stores in it, with your consent','What you can bring','Sign in','contact@x-arc.ai','X-ARC','Why Grove asks Google for your name and address','The calendar is a separate request'])assert(homeText.includes(text),'The homepage says: '+text);

// Privacy page: every section, as a heading, in the design's order.
const headings=[...privacy.matchAll(/<h2>([^<]*)<\/h2>/g)].map(m=>decode(m[1]));
assert.deepStrictEqual(headings,['What Grove reads','What Grove asks Google for, and why','Where your data goes','Where it is stored','Which models see it','How to forget','Cookies','Contact'],'The privacy page has every section');
for(const scope of ['openid','email','profile','calendar.readonly'])assert(privacy.includes('<code>'+scope+'</code>'),'The privacy page names the scope '+scope);
for(const text of ['Only when you connect the calendar, in its own consent','Hetzner in Helsinki, Finland','daily offline dump','30 days','hashes','Muse Spark 1.3','Voice Transcribe','Muse Glimmer on Modal','Open the forget control','Each request requires a new approval','grove_session','grove_google','grove_calendar','no tracking and no analytics','Last updated 4 November 2026','Limited Use requirements','There is no local runner','Deployment is not checked','does not prove a successful connection'])assert(privacyText.includes(text),'The privacy page says: '+text);
assert(privacy.includes('href="/forget"'),'The privacy page links the forget control');
assert(!privacyText.includes('The forget control is not built yet'),'The obsolete manual-only instruction is gone');
assert(privacy.includes('https://developers.google.com/terms/api-services-user-data-policy'),'The privacy page links the Google data policy');
assert(privacy.includes('href="mailto:contact@x-arc.ai"'),'The privacy page links the contact');
assert(home.includes('/privacy#where-your-data-goes')&&privacy.includes('id="where-your-data-goes"'),'The homepage link lands on the table');

// Every row of the table, parsed here apart from build.py, in order and with nothing added.
const lines=flow.split('\n'),start=lines.findIndex(l=>l.startsWith('|'));
const mdRows=[];for(let i=start;lines[i]&&lines[i].startsWith('|');i++)mdRows.push(lines[i].replace(/^\||\|$/g,'').split('|').map(c=>c.trim()));
const mdHead=mdRows[0],mdBody=mdRows.slice(2);
assert.deepStrictEqual(mdHead,['Data','Where it goes','Terms','Status']);
const table=privacy.slice(privacy.indexOf('<table class="flow-table">'),privacy.indexOf('</table>',privacy.indexOf('<table class="flow-table">')));
const plain=c=>decode(c.replace(/<[^>]+>/g,'')).trim();
const htmlRows=[...table.matchAll(/<tr>([\s\S]*?)<\/tr>/g)].map(m=>[...m[1].matchAll(/<t[hd][^>]*>([\s\S]*?)<\/t[hd]>/g)].map(c=>plain(c[1])));
assert.deepStrictEqual(htmlRows,[mdHead,...mdBody].map(r=>r.map(c=>c.replace(/`/g,''))),'The table is every row of docs/data-flow.md');
assert(!privacy.includes('data-flow-table'),'The mark is gone');
assert(table.includes('<code>sources</code>'),'Backticks become code');
for(const [needle,status] of [['Resend','Deployment not checked'],['Google sign-in','Deployment not checked'],['Items, links, history','Deployment not checked'],['Backups','Deployment not checked'],['conversation exports','Deployment not checked'],['Calendar','Deployment not checked'],['Git repository','Deployment not checked'],['Pipedream','Deployment not checked'],['Screened text for extraction','Deployment not checked'],['Voice notes','Implementation pending'],['Muse Glimmer','Deployment not checked'],['Evidence returned','Deployment not checked'],['Forget targets','Deployment not checked']]){
  const row=mdBody.find(r=>r[0].includes(needle)||r[1].includes(needle));assert(row,'docs/data-flow.md has a row for '+needle);assert.equal(row[3],status,needle+' is '+status);
}
assert(mdBody.every(r=>r[3]==='Deployment not checked'||r[3]==='Implementation pending'),'A static build never asserts a deployed operation');
assert(!mdBody.some(r=>r[2].includes('Stated from configuration')),'Every Terms cell is filled');
assert(!/No path below runs/.test(flow),'The opening text no longer says no path runs');
assert(mdBody.find(r=>r[1].includes('Spark 1.3'))[2].includes('Provider terms apply'));
assert(mdBody.find(r=>r[1].includes('Voice Transcribe'))[2].includes('Audio leaves the server'));
assert(mdBody.find(r=>r[1].includes('Modal'))[2].includes('synthetic data only'));
assert(flow.includes('Generated from `server/grove/privacy.py`'));
assert(flow.includes('isolated CI proof'));
for(const text of ['opaque owner identifier','imports no source content','runs no provider actions','review its scopes on the hosted page'])assert(privacyText.includes(text),'The privacy page preserves the connection limit: '+text);
assert(mdBody.find(r=>r[1].includes('Pipedream'))[2].includes('executes no provider actions'));
assert(mdBody.find(r=>r[0].includes('Git repository'))[2].includes('without fetching'));

// For both: no em dash, none of the banned words, and nothing that needs a server or a script.
const banned=new RegExp([['ham','za'],['ham','py'],['aa','tar']].map(p=>p.join('')).join('|'),'i');
for(const [name,html] of [['home',home],['privacy',privacy]]){
  assert(!/—|&mdash;|&#8212;|&#x2014;/i.test(html),name+': no em dash');
  assert(!banned.test(html),name+': none of the banned words');
  assert(!/GROVE_FIXTURE|fixture\.js/.test(html),name+': no fixture');
  for(const [,attrs,body] of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)){
    assert(!/\bsrc=/.test(attrs),name+': no script file');
    if(name==='home')assert(!/fetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(|\/v1\//.test(body),name+': no script needs a server');
    else if(body.includes('fetch(')){assert.deepStrictEqual([...body.matchAll(/fetch\(([^,]+)/g)].map(m=>m[1]),["'/v1/privacy'"],'Privacy reads only the public configuration statement');assert(!/localStorage|sessionStorage|sendBeacon|XMLHttpRequest/.test(body),'The configuration script keeps no browser data');}
    else assert(!/XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(|\/v1\//.test(body),'The theme script makes no request');
  }
  assert(!/<(?:img|iframe|video|audio|source|embed|object)\b/i.test(html),name+': no outside media');
  assert(!/<link[^>]+rel="stylesheet"/.test(html),name+': every sheet is inside the page');
  for(const m of html.matchAll(/url\(([^)]{0,16})/g))assert(m[1].startsWith('data:'),name+': every asset is inside the page');
  assert(html.includes('@media (prefers-color-scheme:dark)')&&html.includes(':root[data-theme=dark]'),name+': light and dark');
  assert(html.includes('name="viewport"'),name+': made for a phone too');
  assert(Buffer.byteLength(html)<400000,name+': a public page stays small');
}

// The build refuses to make the page from a missing or broken file, and escapes what it copies.
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'grove-pages-'));
try{
  fs.cpSync(web,path.join(tmp,'web'),{recursive:true,filter:src=>!/[\\/](dist|checks)$/.test(src)});
  fs.mkdirSync(path.join(tmp,'docs'));
  const build=()=>cp.spawnSync('python3',[path.join(tmp,'web','build.py')],{encoding:'utf8'});
  const refused=(label,text,message)=>{const out=build();assert.notEqual(out.status,0,label+': the build fails');assert(out.stderr.includes(message),label+': the build says why: '+out.stderr);assert(!fs.existsSync(path.join(tmp,'web','dist','privacy.html')),label+': no page is written');};
  refused('no generator',null,'the data-flow generator is missing');
  fs.mkdirSync(path.join(tmp,'server','grove'),{recursive:true});
  const write=t=>fs.writeFileSync(path.join(tmp,'server','grove','privacy.py'),'def markdown_statement():\n    return '+JSON.stringify(t)+'\n');
  write('# Where the data goes\n\nNo table here.\n\n## Open\n\n| a | b |\n|---|---|\n| c | d |\n');refused('no table under the first heading','','no table under its first heading');
  write('# Where the data goes\n\n| Data | Status |\n|---|---|\n');refused('a table with no rows','','no table under its first heading');
  write('# Where the data goes\n\n| Data | Status |\n|---|---|\n| one cell |\n');refused('a short row','','another column count');
  write('# Where the data goes\n\n| Data | Status |\n|---|---|\n| <script>alert(1)</script> & `a<b` | Runs |\n');
  const made=build();assert.equal(made.status,0,made.stderr);
  const page=fs.readFileSync(path.join(tmp,'web','dist','privacy.html'),'utf8');
  assert(page.includes('&lt;script&gt;alert(1)&lt;/script&gt; &amp; <code>a&lt;b</code>'),'A cell is escaped, and backticks become code');
  assert(page.includes('<td data-label="Status" data-status="runs">Runs</td>'),'The status cell carries its word');
  fs.rmSync(path.join(tmp,'web','dist'),{recursive:true});
  const source=path.join(tmp,'web','privacy.html');fs.writeFileSync(source,fs.readFileSync(source,'utf8').replace('<!-- data-flow-table -->',''));
  refused('no mark','','the data-flow mark');
}finally{fs.rmSync(tmp,{recursive:true,force:true});}

console.log('PASS: public pages, privacy scope and policy disclosures, generated table, static deployment limits, bundled assets, and generator failure/escaping controls');
