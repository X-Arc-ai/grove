/* Source feedback and icon handoffs. The shared entry scene owns both welcome backgrounds. */
(function () {
  'use strict';
  var reduced=window.matchMedia('(prefers-reduced-motion: reduce)'),api=window.GroveSimulation,state=api.getState();
  window.addEventListener('grove:change',function(e){state=e.detail||api.getState();});
  var canvas=document.createElement('canvas');canvas.id='gp-motion';canvas.setAttribute('aria-hidden','true');document.body.appendChild(canvas);
  var ctx=canvas.getContext('2d'),w=innerWidth,h=innerHeight,dpr=1,last=0,clock=0,hover=null,pointer={x:0,y:0},look={x:0,y:0};
  var core={x:w/2,y:h/2,scale:1,opacity:state.onboardingComplete||state.onboardingStep?0:1},packets=[],pulses=[],welcomeMix=state.onboardingStep?0:1;
  var colors={chatgpt:'#606864',claude:'#d97757',grok:'#84786b',muse:'#759ac8',calendar:'#4285f4',chats:'#2b9b76',notes:'#b8943b'};
  function size(){w=innerWidth;h=innerHeight;dpr=Math.min(devicePixelRatio||1,2);canvas.width=w*dpr;canvas.height=h*dpr;canvas.style.width=w+'px';canvas.style.height=h+'px';ctx.setTransform(dpr,0,0,dpr,0,0);}
  size();window.addEventListener('resize',size);
  // The backdrop does not react to pointer movement.
  function screenNode(){return window.GroveView&&window.GroveView.tourNode();}
  function dot(x,y,r,c,a){ctx.globalAlpha=a;ctx.fillStyle=c;ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();}
  function bead(p,u){var end=screenNode()||{x:core.x,y:core.y};var a=p.from,b={x:a.x+(end.x-a.x)*.42,y:a.y-60},c={x:end.x-(end.x-a.x)*.24,y:end.y+60},v=1-u;return {x:v*v*v*a.x+3*v*v*u*b.x+3*v*u*u*c.x+u*u*u*end.x,y:v*v*v*a.y+3*v*v*u*b.y+3*v*u*u*c.y+u*u*u*end.y};}
  function render(now){
    var dt=Math.min(50,now-(last||now));last=now;if(!reduced.matches)clock+=dt/1000;ctx.clearRect(0,0,w,h);
    var small=w<=820,step=state.onboardingStep,welcome=!state.onboardingComplete&&!step,ease=reduced.matches?1:1-Math.exp(-dt/420);
    var dest={x:welcome?w/2:small?w/2:w*.73,y:welcome?h/2:small?h*.22:h*.5,scale:welcome?1:small?.22:.42};
    var opacity=state.onboardingComplete?0:welcome?1:0;
    core.x+=(dest.x-core.x)*ease;core.y+=(dest.y-core.y)*ease;core.scale+=(dest.scale-core.scale)*ease;core.opacity+=(opacity-core.opacity)*ease;welcomeMix+=((welcome?1:0)-welcomeMix)*ease;
    look.x+=(pointer.x-look.x)*ease;look.y+=(pointer.y-look.y)*ease;
    // The empty setup keeps one quiet point of possibility until data arrives.
    if(!welcome&&!state.onboardingComplete&&!state.currentNodeCount&&!state.onboardingStep){var a=(1-core.opacity)*.65,r=7+Math.sin(clock*1.3)*1.2;ctx.globalAlpha=a;ctx.strokeStyle=hover?(colors[hover]||'#bfa15a'):'#bfa15a';ctx.lineWidth=1;ctx.beginPath();ctx.arc(dest.x,dest.y,r,0,Math.PI*2);ctx.stroke();dot(dest.x,dest.y,2,'#c9a341',a);}
    packets=packets.filter(function(p){var u=(now-p.at)/1100;if(u<0)return true;if(u>=1){if(!p.landed){var end=bead(p,1);pulses.push({x:end.x,y:end.y,at:now,color:p.color});p.landed=true;}return false;}ctx.strokeStyle=p.color;ctx.lineWidth=.8;ctx.globalAlpha=.2*Math.sin(u*Math.PI);ctx.beginPath();for(var i=0;i<=34;i++){var pp=bead(p,i/34);i?ctx.lineTo(pp.x,pp.y):ctx.moveTo(pp.x,pp.y);}ctx.stroke();var pp=bead(p,u*u*(3-2*u));dot(pp.x,pp.y,2.5,p.color,.9*Math.sin(u*Math.PI));return true;});
    pulses=pulses.filter(function(p){var u=(now-p.at)/950;if(u>1)return false;ctx.globalAlpha=(1-u)*.45;ctx.strokeStyle=p.color;ctx.lineWidth=.8;ctx.beginPath();ctx.arc(p.x,p.y,8+u*34,0,Math.PI*2);ctx.stroke();return true;});ctx.globalAlpha=1;
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);
  window.GroveMotion={
    hover:function(){hover=null;},
    source:function(id,from){if(reduced.matches)return;from=from||{x:w*.3,y:h*.58};for(var i=0;i<3;i++)packets.push({from:from,color:colors[id]||'#b8943b',at:performance.now()+i*140});},
    capture:function(el){return el?{rect:el.getBoundingClientRect(),html:el.innerHTML,color:getComputedStyle(el).color}:null;},
    morph:function(from,target){if(!from||!target||reduced.matches)return;var r=from.rect,d=target.getBoundingClientRect();if(!r.width||!d.width)return;var copy=document.createElement('span');copy.innerHTML=from.html;copy.className='gp-travelling-icon';Object.assign(copy.style,{left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px',color:from.color});document.body.appendChild(copy);target.style.opacity='0';var animation=copy.animate([{transform:'translate(0,0) scale(1)',opacity:1},{transform:'translate('+(d.left-r.left)+'px,'+(d.top-r.top)+'px) scale('+(d.width/r.width)+')',opacity:1}],{duration:520,easing:'cubic-bezier(.22,1,.36,1)',fill:'forwards'});var done=false;function finish(){if(done)return;done=true;target.style.opacity='';copy.remove();animation.cancel();}animation.finished.then(finish,function(){});setTimeout(finish,650);}
  };
})();
