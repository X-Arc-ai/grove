/* A front-facing Grove, with quiet layers behind the working network.
 * Depth comes from scale, contrast and independent drift. The graph never rotates.
 * These example concepts preview the product; they are not imported user records. */
(function(){
  'use strict';
  function random(i){var n=Math.sin(i*127.1+71.7)*43758.5453;return n-Math.floor(n);}
  var hubs=[
    [.18,.20,'PRODUCT|LAUNCH',1], [.35,.09,'IDEAS',0], [.64,.12,'RESEARCH',2], [.85,.24,'NEXT|STEPS',4],
    [.12,.50,'DESIGN|NOTES',2], [.28,.69,'MAYA',0], [.16,.86,'DECISIONS',4], [.43,.91,'CHAT|HISTORY',2],
    [.66,.86,'PROJECTS',1], [.86,.73,'WEEKLY|PLAN',4], [.90,.48,'KNOWLEDGE',2], [.74,.54,'CONTEXT',3]
  ];
  // The working field continues beyond the viewport. Peripheral clusters fill
  // the corners instead of enclosing the message in a finite oval of nodes.
  var perimeter=[[.02,.03],[.24,-.03],[.49,-.04],[.75,-.03],[.98,.03],[-.03,.27],[1.03,.28],[-.03,.71],[1.03,.72],[.02,.98],[.25,1.03],[.50,1.04],[.75,1.03],[.98,.98]];
  var concepts=hubs.concat(perimeter.map(function(p,k){return [p[0],p[1],null,k%6];}));
  var nodes=[],links=[[],[],[]],layers=[[],[],[]],clusters=[[],[],[]];
  for(var layer=0;layer<3;layer++){
    concepts.forEach(function(h,k){
      var hub=nodes.length,outer=k>=hubs.length,spread=outer?.18:layer===2?.15:.20,count=outer?19:29;
      nodes.push({x:h[0],y:h[1],layer:layer,cluster:k,phase:k*.73,type:h[3],r:outer?6+k%5:layer===2?25+(k%3)*4:10+(k%4)*4,label:layer===2?h[2]:null,hub:true});
      layers[layer].push(hub);var members=[hub];
      for(var j=0;j<count;j++){
        var seed=layer*2100+k*31+j,angle=j*2.39996+k*.4,radius=Math.sqrt(random(seed+19))*spread,index=nodes.length;
        nodes.push({x:h[0]+Math.cos(angle)*radius,y:h[1]+Math.sin(angle)*radius*.90,layer:layer,cluster:k,phase:random(seed+2100)*Math.PI*2,type:j%7===0?h[3]:5,r:2.3+random(seed+1700)*2.7,hub:false});
        members.push(index);links[layer].push([hub,index]);if(j>0)links[layer].push([index-1,index]);if(j>6&&j%3===0)links[layer].push([index-6,index]);
      }
      clusters[layer].push(members);
    });
    // Short bridges weave the peripheral clusters into the same network.
    perimeter.forEach(function(p,k){
      var hub=layers[layer][hubs.length+k],near=0,best=Infinity;
      hubs.forEach(function(h,j){var d=Math.hypot(p[0]-h[0],p[1]-h[1]);if(d<best){best=d;near=j;}});
      links[layer].push([hub,layers[layer][near]],[hub,layers[layer][hubs.length+(k+1)%perimeter.length]]);
    });
  }
  var relations=[[0,1,'GROWS FROM'],[0,4,'SHAPED BY'],[4,5,'SHARED WITH'],[5,6,'INFORMS'],[6,7,'CAPTURED IN'],[7,8,'INFORMS'],[8,9,'GUIDES'],[3,10,'BUILDS ON'],[10,11,'CONNECTS'],[2,3,'INFORMS']];
  var workers=[],workerColors=['#5d9e6c','#9d66b4','#cba33b'];
  var existingLinks=new Set();links.forEach(function(group){group.forEach(function(pair){existingLinks.add(pair.slice().sort(function(a,b){return a-b;}).join(':'));});});
  function addWorker(layer,k,offset){
    var members=clusters[layer][k],hub=nodes[members[0]],id=workers.length;
    // Work happens among the small memories, away from the large named concepts.
    var leaves=members.slice(1).sort(function(a,b){return Math.hypot(nodes[b].x-hub.x,nodes[b].y-hub.y)-Math.hypot(nodes[a].x-hub.x,nodes[a].y-hub.y);});
    var a=leaves[(k*3+offset*7)%Math.ceil(leaves.length*.6)];
    // A little itinerary through nearby memories, with shared stops between
    // jobs. The workers move onward, rather than orbiting or returning to base.
    var kind=id%3,route=[a];
    for(var step=1;step<10;step++){
      var previous=route[step-1],available=leaves.filter(function(b){
        return !route.includes(b)&&(kind!==2||(!existingLinks.has([previous,b].sort(function(x,y){return x-y;}).join(':'))&&(step!==9||!existingLinks.has([b,a].sort(function(x,y){return x-y;}).join(':')))));
      });
      available.sort(function(x,y){return Math.hypot((nodes[x].x-nodes[previous].x)*1.6,nodes[x].y-nodes[previous].y)-Math.hypot((nodes[y].x-nodes[previous].x)*1.6,nodes[y].y-nodes[previous].y);});
      route.push(available[Math.floor(random(id*81+step*23)*Math.min(4,available.length))]);
    }
    workers.push({id:id,kind:kind,layer:layer,color:workerColors[kind],hub:members[0],route:route,phase:random(id+800),period:13+random(id+820)*6,angle:random(id+840)*Math.PI*2});
  }
  concepts.forEach(function(_,k){addWorker(2,k,0);addWorker(2,k,1);});
  [0,1,3,4,5,7,8,9,10,11].forEach(function(k){addWorker(1,k,2);});
  function smooth(t){t=Math.max(0,Math.min(1,t));return t*t*t*(t*(t*6-15)+10);}
  function mix(a,b,u){return {x:a.x+(b.x-a.x)*u,y:a.y+(b.y-a.y)*u};}
  window.GroveEntryVisual='<canvas class="gp-entry-canvas" aria-hidden="true"></canvas>';
  var stop=null,epoch=performance.now();
  window.GroveEntryScene={mount:function(canvas){
    if(stop)stop();stop=null;if(!canvas)return;
    var ctx=canvas.getContext('2d');if(!ctx)return;
    var raf=0,live=true,w=0,h=0,dpr=1,points=nodes.map(function(n){return {x:0,y:0,r:0,n:n};});
    var reduced=window.matchMedia('(prefers-reduced-motion: reduce)'),theme=getComputedStyle(document.documentElement);
    var paper=theme.getPropertyValue('--bg').trim()||'#f7f6f0',edge=theme.getPropertyValue('--text-muted').trim()||'#92958b',ink=theme.getPropertyValue('--text-secondary').trim()||'#6e736d';
    var colors=[theme.getPropertyValue('--ring-person').trim()||'#d4a52a','#86519d','#3985b0','#428d82','#9ba64b','#768b94'];
    function size(){var r=canvas.getBoundingClientRect();w=r.width;h=r.height;dpr=Math.min(window.devicePixelRatio||1,2);canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}
    function draw(seconds){
      if(!w||!h)return;
      var t=reduced.matches?0:seconds,unit=Math.max(.72,Math.min(w/1440,h/900)),small=w<720;
      // The layer and cluster waves have different periods and phases. Each
      // memory also drifts independently; no rigid rotation or perspective zoom.
      var layerX=[],layerY=[],clusterX=[],clusterY=[];
      for(var l=0;l<3;l++){
        layerX[l]=[-42,26,0][l]+Math.sin(t/10+l*1.8)*[28,24,20][l];
        layerY[l]=[24,-20,0][l]+Math.cos(t/12+l*1.6)*[23,20,17][l];
      }
      concepts.forEach(function(_,k){clusterX[k]=Math.sin(t/(7.5+k%4)+k*.73)*23;clusterY[k]=Math.cos(t/(9+k%3)+k*.91)*19;});
      nodes.forEach(function(n,i){
        var depth=[.90,.98,1.04][n.layer],dx=layerX[n.layer]+clusterX[n.cluster],dy=layerY[n.layer]+clusterY[n.cluster];
        if(!n.hub){dx+=Math.sin(t/(5.5+n.phase*.3)+n.phase)*12;dy+=Math.cos(t/(7+n.phase*.2)+n.phase)*10;}
        var p=points[i];p.x=w/2+(n.x-.5)*w*depth+dx*unit;p.y=h/2+(n.y-.5)*h*depth+dy*unit;p.r=n.r*unit;
      });
      ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
      for(var layer=0;layer<3;layer++){
        ctx.strokeStyle=edge;ctx.lineWidth=(layer===2?.7:.5)*unit;ctx.globalAlpha=[.055,.10,.17][layer];ctx.beginPath();
        links[layer].forEach(function(pair){var a=points[pair[0]],b=points[pair[1]];ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);});ctx.stroke();
        points.forEach(function(p){
          var n=p.n;if(n.layer!==layer)return;
          ctx.globalAlpha=[.13,.26,n.hub?.76:.58][layer];ctx.fillStyle=paper;ctx.strokeStyle=colors[n.type];ctx.lineWidth=(n.hub?1.7:.9)*unit;
          ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);ctx.fill();ctx.stroke();
          if(n.label&&!small){ctx.globalAlpha=.80;ctx.fillStyle=ink;ctx.font='500 '+(7.5*unit)+'px "JetBrains Mono", monospace';ctx.textAlign='center';ctx.textBaseline='middle';var lines=n.label.split('|');lines.forEach(function(line,i){ctx.fillText(line,p.x,p.y+(i-(lines.length-1)/2)*10*unit);});}
        });
      }
      // Meaningful cross-cluster relationships sit above the quieter rear layers.
      relations.forEach(function(rel){
        var a=points[layers[2][rel[0]]],b=points[layers[2][rel[1]]],dx=b.x-a.x,dy=b.y-a.y,len=Math.hypot(dx,dy),ux=dx/len,uy=dy/len;
        var ax=a.x+ux*a.r,ay=a.y+uy*a.r,bx=b.x-ux*b.r,by=b.y-uy*b.r;
        ctx.globalAlpha=.27;ctx.strokeStyle=edge;ctx.lineWidth=.9*unit;ctx.beginPath();ctx.moveTo(ax,ay);ctx.lineTo(bx,by);ctx.stroke();
        if(!small&&len>120*unit){ctx.save();ctx.translate((ax+bx)/2,(ay+by)/2);var angle=Math.atan2(dy,dx);if(angle>Math.PI/2||angle<-Math.PI/2)angle+=Math.PI;ctx.rotate(angle);ctx.font='500 '+(6.5*unit)+'px "JetBrains Mono",monospace';ctx.textAlign='center';ctx.textBaseline='middle';var labelWidth=ctx.measureText(rel[2]).width+8*unit;ctx.globalAlpha=.9;ctx.fillStyle=paper;ctx.fillRect(-labelWidth/2,-5*unit,labelWidth,10*unit);ctx.fillStyle=ink;ctx.globalAlpha=.65;ctx.fillText(rel[2],0,0);ctx.restore();}
      });
      function line(a,b,color,alpha,width){ctx.strokeStyle=color;ctx.globalAlpha=alpha;ctx.lineWidth=width*unit;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
      function ring(p,r,color,alpha,width){if(alpha<=0||r<=0)return;ctx.globalAlpha=alpha;ctx.fillStyle=paper;ctx.strokeStyle=color;ctx.lineWidth=width*unit;ctx.beginPath();ctx.arc(p.x,p.y,r*unit,0,Math.PI*2);ctx.fill();ctx.stroke();}
      function sparkle(p,strength,color,opacity){
        if(strength<=0)return;
        var r=(2+2*Math.sin(Math.PI*strength))*unit;
        line({x:p.x-r,y:p.y},{x:p.x+r,y:p.y},color,strength*opacity*.75,.9);
        line({x:p.x,y:p.y-r},{x:p.x,y:p.y+r},color,strength*opacity*.75,.9);
      }
      workers.forEach(function(worker){
        var cycle=t/worker.period+worker.phase,event=Math.floor(cycle),u=cycle-event,kind=worker.kind;
        var opacity=worker.layer===2?1:.40,bodyScale=worker.layer===2?1:.76,hub=points[worker.hub];
        function memory(n){return points[worker.route[((n%worker.route.length)+worker.route.length)%worker.route.length]];}
        function dock(p){var angle=worker.angle+p.n.phase*.25;return {x:p.x+Math.cos(angle)*(p.r+12*unit),y:p.y+Math.sin(angle)*(p.r+12*unit)};}
        function addition(n,j){
          var anchor=memory(n+1),direction=Math.atan2(anchor.y-hub.y,anchor.x-hub.x)+(random(worker.id*31+n*17)-.5)*1.6+j*1.1,distance=(29+j*17+random(worker.id+n*19)*10)*unit;
          return {x:anchor.x+Math.cos(direction)*distance,y:anchor.y+Math.sin(direction)*distance};
        }
        var a=memory(event),b=memory(event+1),start=dock(a),finish=dock(b),travel=smooth(u/.64),working=smooth((u-.64)/.36);
        var move=Math.sin(Math.PI*travel),heading=Math.atan2(finish.y-start.y,finish.x-start.x),bend=(8+random(worker.id+event*31)*10)*Math.sin(worker.angle+event)*unit;
        var position=mix(start,finish,travel);
        position.x-=Math.sin(heading)*bend*move;position.y+=Math.cos(heading)*bend*move;
        // A small asymmetric sway and a breathing pause, never a visible orbit.
        position.x+=Math.sin(t*1.7+worker.angle)*.65*unit;
        position.y+=Math.sin(t*2.1+worker.angle)*1.1*unit;
        var workBeat=Math.sin(Math.PI*working),contact=b,done=smooth((u-.88)/.06)*(1-smooth((u-.96)/.04));
        if(kind===0){
          contact=addition(event,0);
          var deliveryAngle=Math.atan2(contact.y-position.y,contact.x-position.x);
          var delivery={x:contact.x-Math.cos(deliveryAngle)*11*unit,y:contact.y-Math.sin(deliveryAngle)*11*unit};
          position=mix(position,delivery,workBeat*.8);
          var carried=1-smooth((u-.66)/.12);
          if(carried>0)ring({x:position.x+Math.cos(deliveryAngle)*9*unit,y:position.y+Math.sin(deliveryAngle)*9*unit},2.1,worker.color,carried*.8*opacity,1.1);
        }
        // Work leaves small, persistent changes. Old examples quietly recede
        // after several jobs so this ambient demonstration stays bounded.
        for(var past=Math.max(0,event-2);past<=event;past++){
          var current=past===event,retained=event-past===2?1-smooth(u):1,from=memory(past),target=memory(past+1);
          if(kind===2){
            var attached=current?smooth((u-.64)/.16):1;
            var end=current?mix(position,target,attached):target;
            var reveal=current?smooth(u/.12):1;
            line(from,end,edge,.24*retained*opacity*reveal,1);
            // Only a short strand near the worker glows, not a huge spoke.
            if(current){var glow=(1-smooth((u-.86)/.12))*reveal;line(mix(from,end,.86),end,worker.color,.72*glow*opacity,1.4);}
          }else if(kind===0){
            for(var j=0;j<2;j++){
              var born=current?smooth((u-(.67+j*.13))/.13):1;if(born<=0)continue;
              var node=addition(past,j),parent=j?addition(past,0):target,settle=current?1-smooth((u-.9)/.1):0;
              line(parent,mix(parent,node,born),edge,.27*born*retained*opacity,1);
              ring(node,(j?3.3:4.1)*born,colors[5],.65*born*retained*opacity,1);
              if(settle>0)ring(node,(j?3.3:4.1)*born,worker.color,.68*born*settle*opacity,1.2);
            }
          }
        }
        if(kind===1){
          // Tiny dust flecks lift off a single memory as the worker tends it.
          // The memory itself stays the same size; there are no territory rings.
          var clean=smooth((u-.66)/.23),dust=workBeat*(1-clean);
          ring(b,b.r/unit,worker.color,workBeat*.58*opacity,1.35);
          for(var j=0;j<3;j++){
            var angle=worker.angle+j*2.1,r=(b.r/unit+4+clean*9)*unit;
            var fleck={x:b.x+Math.cos(angle)*r,y:b.y+Math.sin(angle)*r-clean*4*unit};
            line(fleck,{x:fleck.x+1.7*unit,y:fleck.y-.8*unit},worker.color,dust*.85*opacity,1);
          }
        }
        sparkle({x:contact.x+6*unit,y:contact.y-7*unit},done,worker.color,opacity);
        // The same simple colored circles used in the product worker view.
        // Motion and changes to memories communicate the work.
        var radius=5.8*unit*bodyScale;
        ctx.fillStyle=worker.color;ctx.globalAlpha=.10*opacity;
        ctx.beginPath();ctx.arc(position.x,position.y,9*unit*bodyScale,0,Math.PI*2);ctx.fill();
        ctx.globalAlpha=.96*opacity;ctx.strokeStyle=paper;ctx.lineWidth=1.5*unit;
        ctx.beginPath();ctx.arc(position.x,position.y,radius,0,Math.PI*2);ctx.fill();ctx.stroke();
      });
      // Keep an unboxed reading space for the welcome message. The sign-in card is opaque.
      if(canvas.dataset.scene==='welcome'){
        ctx.save();ctx.globalCompositeOperation='destination-out';ctx.translate(w/2,h/2);ctx.scale(1,.60);
        var radius=Math.min(380,w*.46),gradient=ctx.createRadialGradient(0,0,radius*.48,0,0,radius);
        gradient.addColorStop(0,'rgba(0,0,0,1)');gradient.addColorStop(.55,'rgba(0,0,0,.96)');gradient.addColorStop(1,'rgba(0,0,0,0)');ctx.globalAlpha=1;ctx.fillStyle=gradient;ctx.fillRect(-radius,-radius,radius*2,radius*2);ctx.restore();
      }
      ctx.globalAlpha=1;
    }
    function tick(now){if(!live||!canvas.isConnected)return;draw((now-epoch)/1000);raf=requestAnimationFrame(tick);}
    function resize(){size();draw((performance.now()-epoch)/1000);}
    function motion(){cancelAnimationFrame(raf);draw(0);if(!reduced.matches)raf=requestAnimationFrame(tick);}
    size();draw((performance.now()-epoch)/1000);if(!reduced.matches)raf=requestAnimationFrame(tick);
    window.addEventListener('resize',resize);reduced.addEventListener('change',motion);
    stop=function(){live=false;cancelAnimationFrame(raf);window.removeEventListener('resize',resize);reduced.removeEventListener('change',motion);};
  }};
})();
