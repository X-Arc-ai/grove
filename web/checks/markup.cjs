// Item text in the node panel goes through web/markup.js and the vendored marked: raw HTML shows
// as text, links and images keep only http, https and mailto, attributes are escaped. Both renderer
// calling forms are covered: marked v12's strings (vendored) and the tokens of v13 and later.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const web=path.join(__dirname,'..');
const read=f=>fs.readFileSync(path.join(web,f),'utf8');

function load(withMarked){
  const sandbox={};sandbox.window=sandbox;vm.createContext(sandbox);
  if(withMarked)vm.runInContext(read('vendor/marked.min.js'),sandbox,{filename:'marked.min.js'});
  vm.runInContext(read('markup.js'),sandbox,{filename:'markup.js'});
  return sandbox;
}
const decode=s=>s.replace(/&#x([0-9a-f]+);/gi,(_,h)=>String.fromCodePoint(parseInt(h,16))).replace(/&#(\d+);/g,(_,d)=>String.fromCodePoint(+d))
  .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
const TAGS=new Set(['p','a','img','strong','em','code','pre','ul','ol','li','br','hr','blockquote','h1','h2','h3','h4','h5','h6','del','table','thead','tbody','tr','th','td','input']);
const ATTRS=new Set(['href','title','src','alt','align','start','type','checked','disabled']);
// Every element in the output: an allowed tag, allowed attributes, no handler, and a safe address.
function assertInert(html,label){
  const tags=[...html.matchAll(/<\/?([a-zA-Z][\w-]*)((?:\s+[^\s=>]+(?:="[^"]*")?)*)\s*\/?>/g)];
  let rest=html;for(const t of tags)rest=rest.replace(t[0],'');
  assert(!rest.includes('<'),label+': every < outside a tag is escaped');
  for(const t of tags){
    const name=t[1].toLowerCase();assert(TAGS.has(name),label+': unexpected element '+name);
    for(const a of t[2].matchAll(/\s+([^\s=>]+)(?:="([^"]*)")?/g)){
      const attr=a[1].toLowerCase();assert(ATTRS.has(attr)&&!attr.startsWith('on'),label+': unexpected attribute '+attr);
      if(attr==='href'||attr==='src')assert(/^(https?|mailto):/i.test(decode(a[2]||'')),label+': unsafe address '+a[2]);
      assert(!/javascript:/i.test(decode(a[2]||'')),label+': javascript: in an attribute');
    }
  }
}

const S=load(true),render=S.GroveMarkup.render;
assert(/marked v12\./.test(read('vendor/marked.min.js')),'The vendored marked is v12: its renderers take strings');

// 1. Script, handlers and javascript: addresses render as text.
const hostile=[
  'A note <img src=x onerror=alert(1)> and <script>alert(2)</script> inline.',
  '<a href="javascript:alert(3)">a raw link</a> and <svg onload=alert(4)>',
  '[click me](javascript:alert(5)) and [again](  JaVaScRiPt:alert(6) "t") and [encoded](javascript&#58;alert(7))',
  '![picture](javascript:alert(8)) and ![other](data:image/svg+xml,<svg onload=alert(9)>)',
  'An autolink <javascript:alert(10)> and [data](data:text/html,<script>alert(11)</script>) and [vb](vbscript:msgbox(12))',
  '',
  '<div onclick="alert(13)">a block of raw HTML</div>',
  '',
  '<kbd>raw <img src=x onerror=alert(14)//',
  'more > text</kbd> and <pre><b onmouseover=alert(15)>x</b></pre>',
].join('\n');
const out=render(hostile);
assertInert(out,'hostile');
for(const bad of ['<img','<script','<svg','<div','<kbd','<pre','<b '])assert(!out.toLowerCase().includes(bad),'No '+bad+' element survives');
// Escaped text may spell onerror=; a real tag (every literal < starts one) never holds a handler.
const realTags=[...out.matchAll(/<[^>]*>/g)].map(m=>m[0]).join(' ');
assert(!/\son\w+\s*=/i.test(realTags),'No element carries an event handler');
assert(!/javascript:/i.test(decode(realTags)),'No javascript: address in an attribute');
assert(out.includes('&lt;img src=x onerror=alert(1)&gt;'),'Raw inline HTML shows as its own text');
assert(out.includes('&lt;div onclick=&quot;alert(13)&quot;&gt;a block of raw HTML&lt;/div&gt;'),'Raw block HTML shows as its own text');
assert(out.includes('click me')&&out.includes('picture')&&out.includes('again'),'An unsafe link or image keeps its words as plain text');
assert(!/<a\b/.test(out),'No unsafe address becomes a link');

// 2. Markdown and safe addresses still render, with escaped attributes.
const fine=render('**Bold** and *soft* with `a<b`\n\n- [a site](https://example.com/a?x=1&y=2 "Say \\"hi\\" & go") and <https://example.org/?q=1&r=2>\n- [mail](mailto:maya.chen@example.com) and ![a map](https://example.com/map.png "The \\"map\\"")\n- [relative](/somewhere) stays text');
assertInert(fine,'fine');
assert(fine.includes('<strong>Bold</strong>')&&fine.includes('<em>soft</em>')&&fine.includes('<code>a&lt;b</code>')&&fine.includes('<ul>'),'Markdown still renders');
assert(fine.includes('<a href="https://example.com/a?x=1&amp;y=2" title="Say &quot;hi&quot; &amp; go">a site</a>'),'A safe link keeps its address and an escaped title');
assert(fine.includes('<a href="https://example.org/?q=1&amp;r=2">https://example.org/?q=1&amp;r=2</a>'),'A safe autolink stays a link');
assert(fine.includes('<a href="mailto:maya.chen@example.com">mail</a>'),'mailto stays a link');
assert(fine.includes('<img src="https://example.com/map.png" alt="a map" title="The &quot;map&quot;">'),'A safe image stays, attributes escaped');
assert(fine.includes('relative')&&!fine.includes('href="/somewhere"'),'A relative address shows as plain text');

// 3. The token calling form of marked v13 and later, through the same renderer.
const R=S.GroveMarkup.renderer,ctx={parser:{parseInline:tokens=>tokens.map(t=>R.text.call(ctx,t)).join('')}};
assert.equal(R.html.call(ctx,{type:'html',text:'<img src=x onerror=alert(1)>',block:false}),'&lt;img src=x onerror=alert(1)&gt;');
assert.equal(R.html.call(ctx,{type:'html',text:'<script>alert(1)</script>\n',block:true}),'<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>\n');
assert.equal(R.link.call(ctx,{href:'javascript:alert(1)',title:null,tokens:[{type:'text',text:'<b>x</b>'}]}),'&lt;b&gt;x&lt;/b&gt;');
assert.equal(R.link.call(ctx,{href:'https://example.com/"x',title:'a "t"',tokens:[{type:'text',text:'go'}]}),'<a href="https://example.com/%22x" title="a &quot;t&quot;">go</a>');
assert.equal(R.image.call(ctx,{href:'javascript:alert(1)',title:null,text:'<i>alt</i>'}),'&lt;i&gt;alt&lt;/i&gt;');
assert.equal(R.image.call(ctx,{href:'https://example.com/p.png',title:null,text:'a "b"'}),'<img src="https://example.com/p.png" alt="a &quot;b&quot;">');
assert.equal(R.text.call(ctx,{type:'text',text:'<b> & "q"'}),'&lt;b&gt; &amp; &quot;q&quot;');
assert.equal(R.text.call(ctx,{type:'text',text:'already &amp; escaped &lt;'}),'already &amp; escaped &lt;','An entity marked wrote stays as written');

// 4. With no marked at all the text is escaped, never parsed.
assert.equal(load(false).GroveMarkup.render('<img src=x onerror=alert(1)>'),'&lt;img src=x onerror=alert(1)&gt;');

// 5. native-app.js renders item text only through markup.js, never marked directly.
const app=read('native-app.js');
assert(!/marked\.parse|window\.marked/.test(app),'native-app.js never calls marked itself');
assert((app.match(/GroveMarkup\.render\(/g)||[]).length===2,'Both the summary and its snippet go through markup.js');
const index=read('index.html');assert(index.indexOf('./vendor/marked.min.js')<index.indexOf('./markup.js')&&index.indexOf('./markup.js')<index.indexOf('./native-app.js'),'markup.js loads after marked and before the graph');
console.log('PASS: raw HTML as text, only http, https and mailto links and images, escaped attributes, markdown intact, both renderer forms, escaped text without marked, and the panel uses markup.js');
