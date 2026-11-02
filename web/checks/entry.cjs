// Exercise the actual canvas renderer with a deterministic clock.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
let frame,arcs=[],labels=[],ellipses=[],strokes=[],currentPath=[],now=0,width=1440,height=900,matrix=[1,0,0,1,0,0],stack=[];
const handlers={};
function point(x,y){return [matrix[0]*x+matrix[2]*y+matrix[4],matrix[1]*x+matrix[3]*y+matrix[5]];}
const ctx={
 setTransform(){matrix=[1,0,0,1,0,0];},clearRect(){arcs=[];labels=[];ellipses=[];strokes=[];},beginPath(){currentPath=[];},
 moveTo(x,y){assert(Number.isFinite(x)&&Number.isFinite(y));currentPath.push(point(x,y));},lineTo(x,y){assert(Number.isFinite(x)&&Number.isFinite(y));currentPath.push(point(x,y));},
 quadraticCurveTo(cx,cy,x,y){assert([cx,cy,x,y].every(Number.isFinite));currentPath.push(point(x,y));},
 stroke(){strokes.push({points:currentPath.slice(),color:this.strokeStyle,alpha:this.globalAlpha});},fill(){},
 ellipse(x,y,rx,ry){assert([x,y,rx,ry].every(Number.isFinite)&&rx>0&&ry>0);ellipses.push({p:point(x,y),rx,ry,color:this.fillStyle});},
 arc(x,y,r){assert([x,y,r].every(Number.isFinite)&&r>0);arcs.push([...point(x,y),r]);},
 save(){stack.push(matrix.slice());},restore(){matrix=stack.pop();},translate(x,y){const p=point(x,y);matrix[4]=p[0];matrix[5]=p[1];},
 scale(x,y){matrix[0]*=x;matrix[1]*=x;matrix[2]*=y;matrix[3]*=y;},rotate(a){const [m0,m1,m2,m3]=matrix,c=Math.cos(a),s=Math.sin(a);matrix[0]=m0*c+m2*s;matrix[1]=m1*c+m3*s;matrix[2]=m2*c-m0*s;matrix[3]=m3*c-m1*s;},
 fillRect(){},measureText:s=>({width:s.length*5}),fillText(s){labels.push(s);},createRadialGradient:()=>({addColorStop(){}})
};
const canvas={isConnected:true,dataset:{},getContext:()=>ctx,getBoundingClientRect:()=>({width,height,left:0,top:0})};
const media={matches:false,addEventListener(){},removeEventListener(){}};
const c={Math,performance:{now:()=>now},document:{documentElement:{}},getComputedStyle:()=>({getPropertyValue:()=>''}),matchMedia:()=>media,requestAnimationFrame:fn=>(frame=fn,1),cancelAnimationFrame(){frame=null;},devicePixelRatio:2,addEventListener:(name,fn)=>handlers[name]=fn,removeEventListener:name=>delete handlers[name]};c.window=c;
vm.createContext(c);let source=fs.readFileSync(path.join(__dirname,'../entry-visual.js'),'utf8');source=source.replace('  window.GroveEntryVisual=', '  window.entryTest={nodes,links,workers,setWorkers:value=>{workers=value;}};window.GroveEntryVisual=');vm.runInContext(source,c);
const count=1920;
function snapshot(ms){now=ms;frame(now);return arcs.slice(0,count);}
function workers(){return arcs.slice(count).filter(a=>a[2]===5.8||a[2]===5.8*.76).map(a=>a.slice(0,2));}
function coverage(points){
 for(const right of [false,true])for(const bottom of [false,true]){
  const inside=points.filter(p=>p[0]>=0&&p[0]<=width&&p[1]>=0&&p[1]<=height&&(right?p[0]>width*.85:p[0]<width*.15)&&(bottom?p[1]>height*.85:p[1]<height*.15));
  assert(inside.length>=12,`The ${right?'right':'left'} ${bottom?'bottom':'top'} corner must stay populated (${inside.length})`);
 }
 assert(points.some(p=>p[0]<-20)&&points.some(p=>p[0]>width+20)&&points.some(p=>p[1]<-20)&&points.some(p=>p[1]>height+20),'The graph continues beyond every edge');
}
c.GroveEntryScene.mount(canvas);const before=arcs.slice(0,count),workersBefore=workers();coverage(before);
assert.equal(workersBefore.length,62);assert(workersBefore.filter(p=>p[0]>0&&p[0]<width&&p[1]>0&&p[1]<height&&!(p[0]>width/2-220&&p[0]<width/2+220&&p[1]>height/2-260&&p[1]<height/2+260)).length>=25,'Many helpers are immediately visible outside the sign-in card');
const near=snapshot(3000),distances=near.slice(1280).map((p,i)=>Math.hypot(p[0]-before[1280+i][0],p[1]-before[1280+i][1])).sort((a,b)=>a-b);
assert(distances[320]>6,'Foreground drift is visible within three seconds');for(let i=0;i<count;i++)assert.equal(near[i][2],before[i][2],'2D node radii stay fixed');
assert(Math.abs((near[1281][0]-near[1282][0])-(before[1281][0]-before[1282][0]))>.1,'Memories drift independently');
snapshot(12000);assert(workers().filter((p,i)=>Math.hypot(p[0]-workersBefore[i][0],p[1]-workersBefore[i][1])>20).length>50,'Most helpers make visible progress to another memory within twelve seconds');
assert(!labels.some(s=>/Learner|Curator|Connector|Reading context|Refreshing memories|Finding connections/.test(s)));assert(labels.includes('SHAPED BY')&&labels.includes('PRODUCT'));
// Every work pause, next destination and route-loop seam stays continuous.
snapshot(0);let previous=workers();for(let ms=100;ms<=210000;ms+=100){snapshot(ms);workers().forEach((p,i)=>assert(Math.hypot(p[0]-previous[i][0],p[1]-previous[i][1])<12,'Helpers must not teleport between jobs'));previous=workers();assert.equal(ellipses.length,0,'Workers remain circles, without faces, limbs or zone outlines');if(ms%30000===0)coverage(arcs.slice(0,count));}
const all=c.entryTest.workers,basePairs=new Set(c.entryTest.links.flat().map(pair=>[...pair].sort((a,b)=>a-b).join(':')));
for(const worker of all)assert(worker.route.every(id=>Number.isInteger(id)&&!c.entryTest.nodes[id].hub),'Jobs target small memories only');
for(const worker of all.filter(w=>w.kind===2))worker.route.forEach((from,i)=>assert(!basePairs.has([from,worker.route[(i+1)%worker.route.length]].sort((a,b)=>a-b).join(':')),'Gold helpers create previously absent connections'));
function phase(worker,u){snapshot((u-worker.phase)*worker.period*1000);}
const learner=all.find(w=>w.kind===0&&w.layer===2);c.entryTest.setWorkers([learner]);phase(learner,.6);assert.equal(arcs.length-count,3,'A green helper carries a small seed before placing a memory');assert.equal(arcs[count][2],2.1);phase(learner,.85);assert.equal(arcs.length-count,6,'Two new memories appear with brief birth marks');phase(learner,1.2);assert.equal(arcs.length-count,5,'Two new memories remain as the helper carries its next seed');
const connector=all.find(w=>w.kind===2&&w.layer===2);c.entryTest.setWorkers([connector]);phase(connector,.3);const strand=strokes.find(s=>s.color===connector.color&&s.alpha>0&&s.points.length===2),edge=strokes.find(s=>s.color==='#92958b'&&s.alpha===.24);assert(strand&&edge);function length(line){return Math.hypot(line.points[1][0]-line.points[0][0],line.points[1][1]-line.points[0][1]);}assert(length(strand)<length(edge)*.2,'Only the short strand next to the gold helper glows');phase(connector,1.2);assert(strokes.filter(s=>s.color==='#92958b'&&s.alpha===.24).length===2,'The completed connection remains as the next job starts');
const curator=all.find(w=>w.kind===1&&w.layer===2);c.entryTest.setWorkers([curator]);phase(curator,.8);assert.equal(arcs.length-count,3,'Purple tends one memory directly');const mark=arcs[count],target=arcs[curator.route[1]];assert(Math.hypot(mark[0]-target[0],mark[1]-target[1])<.001);assert.equal(mark[2],target[2],'Tending does not draw a bigger ring around the memory');assert.equal(ellipses.length,0,'The worker stays a plain circle while tending a memory');
c.entryTest.setWorkers(all);snapshot(24000);const large=arcs.slice(0,count).filter(p=>p[2]>=25);for(const mark of arcs.slice(count))assert(!large.some(p=>Math.hypot(p[0]-mark[0],p[1]-mark[1])<.01),'Activity leaves large named nodes plain');
assert.equal(canvas.width,2880);c.GroveEntryScene.mount(null);assert(!frame);assert.equal(Object.keys(handlers).length,0,'Unmount removes its resize handler');
canvas.dataset.scene='welcome';width=393;height=852;media.matches=true;c.GroveEntryScene.mount(canvas);assert(!frame);coverage(arcs.slice(0,count));assert.equal(canvas.width,786);c.GroveEntryScene.mount(null);
console.log('PASS: full-field 2D drift, 62 circular workers, no zones, smooth travel/work/loop joins over 210 seconds, persistent new nodes and edges, localized tending, short connector strands, reduced motion and cleanup');
