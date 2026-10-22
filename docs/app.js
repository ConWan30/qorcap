(()=>{"use strict";
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const clamp=(v,a=0,b=1)=>Math.min(b,Math.max(a,v)),lerp=(a,b,t)=>a+(b-a)*t;
const ease=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2,eo=t=>1-Math.pow(1-t,3);
const CY='46,242,255',GD='255,194,61';
const enc=new TextEncoder();
// SHA-256: WebCrypto with a tiny pure-JS fallback (for non-secure contexts)
function sha256js(bytes){const K=new Uint32Array([0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);
 const l=bytes.length,n=((l+9+63)>>6)<<6,m=new Uint8Array(n);m.set(bytes);m[l]=0x80;const dv=new DataView(m.buffer);dv.setUint32(n-4,l*8>>>0);dv.setUint32(n-8,Math.floor(l/0x20000000));
 const H=new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]),W=new Uint32Array(64),r=(x,k)=>(x>>>k)|(x<<(32-k));
 for(let o=0;o<n;o+=64){for(let i=0;i<16;i++)W[i]=dv.getUint32(o+i*4);for(let i=16;i<64;i++){const a=W[i-15],b=W[i-2];W[i]=(W[i-16]+(r(a,7)^r(a,18)^(a>>>3))+W[i-7]+(r(b,17)^r(b,19)^(b>>>10)))>>>0}
  let [a,b,c,d,e,f,g,h]=H;for(let i=0;i<64;i++){const t1=(h+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+K[i]+W[i])>>>0,t2=((r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c)))>>>0;h=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0}
  H[0]+=a;H[1]+=b;H[2]+=c;H[3]+=d;H[4]+=e;H[5]+=f;H[6]+=g;H[7]+=h}
 return [...H].map(x=>x.toString(16).padStart(8,'0')).join('')}
async function sha(data){const b=typeof data==='string'?enc.encode(data):data;
 if(crypto.subtle){try{const d=await crypto.subtle.digest('SHA-256',b);return [...new Uint8Array(d)].map(x=>x.toString(16).padStart(2,'0')).join('')}catch(e){}}return sha256js(b)}
const rhex=n=>[...crypto.getRandomValues(new Uint8Array(n))].map(x=>x.toString(16).padStart(2,'0')).join('');
// visibility tracking
const vis=new Map();const io=new IntersectionObserver(es=>es.forEach(e=>vis.set(e.target,e.isIntersecting)),{rootMargin:'100px'});
const watch=el=>{if(el){vis.set(el,false);io.observe(el)}return el};const seen=el=>vis.get(el);
// canvas helper
function fit(cv){const r=cv.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);const w=Math.max(1,Math.round(r.width*d)),h=Math.max(1,Math.round(r.height*d));if(cv.width!==w||cv.height!==h){cv.width=w;cv.height=h}const x=cv.getContext('2d');x.setTransform(d,0,0,d,0,0);return {x,w:r.width,h:r.height}}
function rr(x,a,b,w,h,r){x.beginPath();x.roundRect?x.roundRect(a,b,w,h,r):x.rect(a,b,w,h)}

// wordmark
const wm=$('.wordmark span');if(wm){wm.innerHTML=[...wm.textContent].map((c,i)=>`<span class="ch" style="animation-delay:${.15+i*.07}s">${c}</span>`).join('')}
// reveal
const rio=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');rio.unobserve(e.target)}}),{threshold:.12,rootMargin:'0px 0px -6% 0px'});
$$('.rv').forEach((el,i)=>{el.style.transitionDelay=(i%3)*.08+'s';rio.observe(el)});
// cursor glow
const gc=$('.glow-cursor');if(matchMedia('(pointer:fine)').matches&&!RM){let tx=0,ty=0,cx=0,cy=0,on=false;addEventListener('pointermove',e=>{tx=e.clientX;ty=e.clientY;if(!on){on=true;document.body.classList.add('cur');cx=tx;cy=ty;loop()}},{passive:true});
 function loop(){cx+=(tx-cx)*.12;cy+=(ty-cy)*.12;gc.style.transform=`translate3d(${cx}px,${cy}px,0)`;requestAnimationFrame(loop)}}

/* ---------- HERO: HDMI signal resolving into a hash chain ---------- */
const sig=watch($('#sig')),hero=$('.hero');let hseq=0;const hSeq=$('#hSeq'),hHead=$('#hHead'),hState=$('#hState');
const blocks=[];let lastSpawn=-1e9,heroHead='0'.repeat(64);
async function heroSeal(){hseq++;heroHead=await sha(heroHead+hseq);hSeq.textContent='#'+String(hseq).padStart(4,'0');hHead.textContent=heroHead.slice(0,16)+'…';hState.textContent='chain intact · '+hseq+' seg'}
function drawHero(t){const {x,w,h}=fit(sig);x.clearRect(0,0,w,h);const mob=w<760;
 const band=mob?h*.9:h*.5,rx=w*(mob?.38:.6);// resolve point
 // perspective grid
 x.save();x.strokeStyle=`rgba(${CY},.06)`;x.lineWidth=1;const hz=band+20;
 for(let i=-14;i<=14;i++){x.beginPath();x.moveTo(w/2+i*30,hz);x.lineTo(w/2+i*(w/7),h);x.stroke()}
 for(let i=0;i<9;i++){const yy=hz+Math.pow(i/8,2.2)*(h-hz)+((t*.02)%1)*Math.pow(1/8,2)*(h-hz);x.beginPath();x.moveTo(0,yy);x.lineTo(w,yy);x.stroke()}x.restore();
 // signal waves
 const N=mob?5:7;x.globalCompositeOperation='lighter';
 for(let k=0;k<N;k++){const off=(k-(N-1)/2)*(mob?7:10);x.beginPath();
  for(let px=0;px<=rx;px+=4){const f=1-px/rx,amp=(mob?16:30)*f*f+1.5,tt=t*.0021;
   const y=band+off*f+Math.sin(px*.021+tt*3+k)*amp*.6+Math.sin(px*.067-tt*5+k*2)*amp*.35+(Math.sin(px*.31+tt*11+k*7)>.96?amp*.9:0);
   px?x.lineTo(px,y):x.moveTo(px,y)}
  x.strokeStyle=`rgba(${CY},${.1+.08*(k%3)})`;x.lineWidth=k===3?2:1.1;x.stroke()}
 // converge glow
 const g=x.createRadialGradient(rx,band,0,rx,band,mob?60:110);g.addColorStop(0,`rgba(${CY},.55)`);g.addColorStop(1,`rgba(${CY},0)`);x.fillStyle=g;x.fillRect(rx-120,band-120,240,240);
 x.globalCompositeOperation='source-over';
 // blocks
 const bs=mob?26:38,gap=mob?16:26,speed=(bs+gap)/2000;// one block per "2 s segment"
 if(t-lastSpawn>2000){lastSpawn=t;blocks.push({born:t,hex:rhex(4)});heroSeal()}
 while(blocks.length&&rx+(t-blocks[0].born)*speed>w+bs)blocks.shift();
 let prev=null;
 for(const b of blocks){const age=t-b.born,bx=rx+age*speed,a=eo(clamp(age/600)),sx=bs*a;const sealed=bx>w*(mob?.78:.8);
  if(prev){x.strokeStyle=sealed?`rgba(${GD},.6)`:`rgba(${CY},.55)`;x.lineWidth=2;x.beginPath();x.moveTo(prev+bs/2,band);x.lineTo(bx-sx/2,band);x.stroke()}
  x.save();x.shadowColor=sealed?`rgba(${GD},.8)`:`rgba(${CY},.8)`;x.shadowBlur=18;
  rr(x,bx-sx/2,band-sx/2,sx,sx,6);x.fillStyle=sealed?`rgba(${GD},.14)`:`rgba(${CY},.12)`;x.fill();x.strokeStyle=sealed?`rgba(${GD},.95)`:`rgba(${CY},.9)`;x.lineWidth=1.5;x.stroke();x.restore();
  if(a>.9&&!mob){x.fillStyle=sealed?`rgba(${GD},.9)`:'rgba(220,250,255,.8)';x.font='600 9px JetBrains Mono,monospace';x.textAlign='center';x.fillText(b.hex,bx,band+bs/2+14)}
  prev=bx}
 // scanning sparkle at resolve
 x.fillStyle=`rgba(255,255,255,${.5+.5*Math.sin(t*.01)})`;x.fillRect(rx-1,band-1,2,2)}
let heroT0=performance.now();
function heroLoop(now){if(seen(sig)!==false)drawHero(now-heroT0);requestAnimationFrame(heroLoop)}
if(RM){// static frame with a few blocks
 for(let i=0;i<8;i++)blocks.push({born:-i*2000,hex:rhex(4)});lastSpawn=1e12;drawHero(0);heroSeal();addEventListener('resize',()=>drawHero(0))}
else requestAnimationFrame(heroLoop);

/* ---------- PIPELINE: sticky scroll-scrubbed ---------- */
const pipe=$('#pipe'),pcv=$('#pipeCv'),steps=$$('#steps .step'),bars=$$('#prog b');let pP=-1,pTarget=0,pCur=0;
const tileHex=Array.from({length:12},()=>rhex(3));
function pipeProg(){const r=pipe.getBoundingClientRect(),tot=r.height-innerHeight;return clamp(-r.top/tot)}
function drawPipe(p,t){const {x,w,h}=fit(pcv);x.clearRect(0,0,w,h);const S=Math.min(w,h),cx=w/2,cy=h/2,u=S/100;
 const st=i=>clamp(p*6-i);// 0..1 per stage
 const s=[0,1,2,3,4,5].map(st);
 // backdrop rings
 x.save();x.translate(cx,cy);for(let i=1;i<=3;i++){x.beginPath();x.arc(0,0,i*15*u,0,7);x.strokeStyle=`rgba(${CY},${.05})`;x.lineWidth=1;x.stroke()}
 x.rotate(t*.0002);x.setLineDash([2*u,4*u]);x.beginPath();x.arc(0,0,46*u,0,7);x.strokeStyle=`rgba(${CY},.12)`;x.stroke();x.restore();x.setLineDash([]);
 // CARD (stage 0) — sits upper-left, shrinks after
 const cardA=1-ease(clamp((p*6-1.6)/1.2)),cardIn=eo(clamp(p*6/0.6+.35));
 if(cardA>0){x.save();x.globalAlpha=cardA*cardIn;const cw=lerp(56,30,ease(s[1]))*u,ch=cw*.55,ccx=lerp(cx,cx-30*u,ease(s[1])),ccy=lerp(cy,cy-32*u,ease(s[1]));
  x.shadowColor=`rgba(${CY},.6)`;x.shadowBlur=30;rr(x,ccx-cw/2,ccy-ch/2,cw,ch,cw*.08);const gg=x.createLinearGradient(0,ccy-ch/2,0,ccy+ch/2);gg.addColorStop(0,'#16203a');gg.addColorStop(1,'#0a0f1e');x.fillStyle=gg;x.fill();x.shadowBlur=0;x.strokeStyle=`rgba(${CY},.7)`;x.lineWidth=1.5;x.stroke();
  // ports
  rr(x,ccx-cw/2-cw*.06,ccy-ch*.15,cw*.1,ch*.3,3);x.fillStyle='#2a3658';x.fill();
  rr(x,ccx+cw/2-cw*.04,ccy-ch*.1,cw*.08,ch*.2,3);x.fill();
  x.fillStyle=`rgba(${CY},${.6+.4*Math.sin(t*.008)})`;x.beginPath();x.arc(ccx+cw*.36,ccy-ch*.28,cw*.022,0,7);x.fill();
  x.fillStyle='rgba(238,243,255,.85)';x.font=`700 ${cw*.07}px JetBrains Mono,monospace`;x.textAlign='center';x.fillText('USB3.0 VIDEO',ccx,ccy+ch*.08);
  x.fillStyle='rgba(151,160,186,.9)';x.font=`500 ${cw*.045}px JetBrains Mono,monospace`;x.fillText('HDMI → DirectShow',ccx,ccy+ch*.26);
  // HDMI cable signal
  x.strokeStyle=`rgba(${CY},.5)`;x.lineWidth=2;x.beginPath();const sx0=ccx-cw/2-cw*.06;x.moveTo(Math.max(0,sx0-40*u),ccy);x.lineTo(sx0,ccy);x.stroke();
  for(let i=0;i<4;i++){const ph=((t*.0012+i/4)%1);x.fillStyle=`rgba(${CY},${1-ph})`;x.fillRect(lerp(sx0-40*u,sx0,ph)-3,ccy-2,6,4)}
  x.restore()}
 // TILES (stages 1–4)
 const cols=4,ts=13*u,gap=5*u,gw=cols*ts+(cols-1)*gap,gx=cx-gw/2+ts/2,gy=cy-(3*ts+2*gap)/2+ts/2+4*u;
 const ord=i=>{const r=Math.floor(i/cols),c=i%cols;return {r,c:r%2?cols-1-c:c}};// snake
 const pos=[];const rec={x:cx,y:cy-3*u,w:54*u,h:76*u};
 for(let i=0;i<12;i++){const {r,c}=ord(i);const appear=eo(clamp(s[1]*14-i*1.0));// stage 1 spawn
  let X=gx+c*(ts+gap),Y=gy+r*(ts+gap);const cardX=cx-30*u,cardY=cy-32*u;
  X=lerp(cardX,X,appear);Y=lerp(cardY,Y,appear);
  const col=ease(clamp(s[4]*1.6-i*.05));// collapse into receipt
  const ty=rec.y-rec.h/2+20*u+Math.floor(i/4)*5*u,tx=rec.x-rec.w/2+12*u+(i%4)*8*u;
  X=lerp(X,tx,col);Y=lerp(Y,ty,col);const sz=lerp(ts*appear,4*u,col);pos.push({X,Y,sz,appear,col})}
 // links (stage 2)
 const lk=s[2];if(lk>0){x.lineWidth=2;for(let i=1;i<12;i++){const f=clamp(lk*12-i+1);if(f<=0)break;const a=pos[i-1],b=pos[i];if(a.col>.3)continue;x.strokeStyle=`rgba(${CY},${.7*(1-a.col)})`;x.beginPath();x.moveTo(a.X,a.Y);x.lineTo(lerp(a.X,b.X,f),lerp(a.Y,b.Y,f));x.stroke()}}
 // tiles draw
 const cpIdx=[2,6,10];
 for(let i=0;i<12;i++){const q=pos[i];if(q.appear<=0||q.col>=1&&s[4]>=1&&false)continue;if(q.sz<.5)continue;const linked=clamp(lk*12-i)>0;const alpha=1-ease(clamp((s[4]-.55)*2.4));if(alpha<=0)continue;
  x.save();x.globalAlpha=alpha;x.shadowColor=`rgba(${CY},.7)`;x.shadowBlur=linked?16:6;rr(x,q.X-q.sz/2,q.Y-q.sz/2,q.sz,q.sz,q.sz*.18);x.fillStyle=`rgba(${CY},${linked?.16:.06})`;x.fill();x.strokeStyle=`rgba(${CY},${linked?.95:.45})`;x.lineWidth=1.4;x.stroke();x.shadowBlur=0;
  if(q.sz>8*u){x.fillStyle='rgba(238,243,255,.9)';x.font=`600 ${2.6*u}px JetBrains Mono,monospace`;x.textAlign='center';x.fillText('seg '+String(i).padStart(2,'0'),q.X,q.Y-.6*u);
   if(linked){x.fillStyle=`rgba(${CY},.95)`;x.font=`500 ${2.2*u}px JetBrains Mono,monospace`;x.fillText(tileHex[i],q.X,q.Y+3.4*u)}else{x.fillStyle='rgba(151,160,186,.8)';x.font=`500 ${2.2*u}px JetBrains Mono,monospace`;x.fillText('2.0 s',q.X,q.Y+3.4*u)}}
  // checkpoints stage 3
  if(cpIdx.includes(i)&&s[3]>0&&q.col<.5){const k=clamp(s[3]*3-cpIdx.indexOf(i)*.7);if(k>0){const dx=q.X+q.sz/2,dy=q.Y-q.sz/2,dr=2.4*u*eo(k);
   x.save();x.translate(dx,dy);x.rotate(Math.PI/4);x.fillStyle='#7ff8ff';x.shadowColor=`rgba(${CY},1)`;x.shadowBlur=14;x.fillRect(-dr,-dr,dr*2,dr*2);x.restore();
   const ph=(t*.0009+i*.13)%1;x.beginPath();x.arc(dx,dy,dr+ph*9*u,0,7);x.strokeStyle=`rgba(${CY},${(1-ph)*.6*k})`;x.lineWidth=1.5;x.stroke();
   x.fillStyle=`rgba(${CY},.95)`;x.font=`600 ${2.1*u}px JetBrains Mono,monospace`;x.textAlign='left';x.fillText(['segment','score','final'][cpIdx.indexOf(i)],dx+3.4*u,dy-1.4*u)}}
  x.restore()}
 // RECEIPT (stage 4–5)
 const ra=ease(clamp((s[4]-.3)*1.6));if(ra>0){const vf=ease(s[5]);x.save();x.globalAlpha=ra;const W=rec.w,H=rec.h,X0=rec.x-W/2,Y0=rec.y-H/2+(1-ra)*8*u;
  x.shadowColor=vf>.5?`rgba(${GD},.5)`:`rgba(${CY},.45)`;x.shadowBlur=40;rr(x,X0,Y0,W,H,3*u);const g2=x.createLinearGradient(0,Y0,0,Y0+H);g2.addColorStop(0,'rgba(22,32,58,.96)');g2.addColorStop(1,'rgba(8,12,24,.96)');x.fillStyle=g2;x.fill();x.shadowBlur=0;
  x.strokeStyle=`rgba(${vf>.5?GD:CY},.8)`;x.lineWidth=1.5;x.stroke();
  x.textAlign='left';x.fillStyle='rgba(238,243,255,.95)';x.font=`700 ${3.4*u}px JetBrains Mono,monospace`;x.fillText('qor-receipt/v0',X0+5*u,Y0+9*u);
  // mini tiles inside
  for(let i=0;i<12;i++){x.fillStyle=`rgba(${vf>.5?GD:CY},.5)`;x.fillRect(X0+12*u+(i%4)*8*u-2*u,Y0+20*u+Math.floor(i/4)*5*u-2*u,4*u,4*u)}
  const lines=[['match_id','m-0001'],['segments','12'],['chain_head',tileHex[11]+'…'],['final_basis','clock_zero'],['signature',vf>.5?'signed ✓':'— none —']];
  x.font=`500 ${2.4*u}px JetBrains Mono,monospace`;lines.forEach((l,i)=>{const yy=Y0+40*u+i*4.8*u;x.fillStyle='rgba(151,160,186,.95)';x.fillText(l[0],X0+5*u,yy);x.fillStyle=i===4?(vf>.5?`rgba(${GD},1)`:'rgba(255,140,151,.95)'):'rgba(238,243,255,.95)';x.textAlign='right';x.fillText(l[1],X0+W-5*u,yy);x.textAlign='left'});
  // stamp
  x.save();x.translate(X0+W*.5,Y0+H-8*u);x.rotate(-.06);const lab=vf>.5?'SIGNED · CHAIN OK':'UNSIGNED DRAFT';x.font=`800 ${3.1*u}px JetBrains Mono,monospace`;const tw=x.measureText(lab).width+5*u;
  x.strokeStyle=vf>.5?`rgba(${GD},1)`:'rgba(151,160,186,.9)';x.lineWidth=1.6;x.setLineDash(vf>.5?[]:[3,3]);rr(x,-tw/2,-3.4*u,tw,6.4*u,1.2*u);x.stroke();x.setLineDash([]);x.fillStyle=x.strokeStyle;x.textAlign='center';x.fillText(lab,0,1.1*u);x.restore();
  x.restore();
  // verify seal
  if(vf>0){x.save();const sx=X0+W-2*u,sy=Y0+2*u,R=9*u*eo(vf);x.translate(sx,sy);x.rotate(t*.0006);x.shadowColor=`rgba(${GD},.9)`;x.shadowBlur=24;
   x.beginPath();for(let i=0;i<24;i++){const a=i/24*Math.PI*2,r2=i%2?R*.86:R;x.lineTo(Math.cos(a)*r2,Math.sin(a)*r2)}x.closePath();x.fillStyle=`rgba(${GD},.95)`;x.fill();x.restore();
   x.save();x.translate(sx,sy);x.strokeStyle='#1a1200';x.lineWidth=2*u*.6;x.beginPath();x.moveTo(-3*u*vf,0);x.lineTo(-.8*u,2.4*u*vf);x.lineTo(3.4*u*vf,-2.6*u*vf);x.stroke();x.restore();
   // verify page browser frame label
   x.globalAlpha=vf;x.fillStyle=`rgba(${GD},.95)`;x.font=`600 ${2.6*u}px JetBrains Mono,monospace`;x.textAlign='center';x.fillText('verify-bundle OK · qortime/verify',cx,Y0+H+7*u);x.globalAlpha=1}
 }}
let pipeLast=-1;
function pipeUpdate(now){pTarget=pipeProg();pCur+=(pTarget-pCur)*.14;if(Math.abs(pTarget-pCur)<.0005)pCur=pTarget;
 const i=Math.min(5,Math.floor(pCur*6+.0001));if(i!==pipeLast){steps.forEach((s,k)=>s.classList.toggle('on',k===i));pipeLast=i}
 bars.forEach((b,k)=>b.style.transform=`scaleX(${clamp(pCur*6-k)})`);drawPipe(pCur,now)}
if(RM){drawPipe(1,0);addEventListener('resize',()=>drawPipe(1,0));bars.forEach(b=>b.style.transform='scaleX(1)')}
else{watch(pipe);(function L(now){if(seen(pipe))pipeUpdate(now);requestAnimationFrame(L)})(0)}

/* ---------- SSE terminal (simulation) ---------- */
const sse=watch($('#sse'));let sseSeq=1200,sseHead='9f'+rhex(31),paused=false,lines=[],score=[7,3];
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function pushLine(html){lines.push(html);if(lines.length>36)lines.shift();sse.innerHTML=lines.join('\n')}
function jsonHtml(o){return '{'+Object.entries(o).map(([k,v])=>`<span class="k">"${k}"</span>:${typeof v==='string'?`<span class="s">"${esc(v)}"</span>`:`<span class="s">${v}</span>`}`).join(',')+'}'}
async function emit(kind,extra={}){sseSeq++;const seg=await sha('seg'+sseSeq+Math.random());sseHead=await sha(sseHead+seg);
 const wall=Date.now();const o=Object.assign({v:'qor-checkpoint/sim',kind,seq:sseSeq,seg_sha256:seg.slice(0,16)+'…',chain_head:sseHead.slice(0,16)+'…',wall_ms:wall},extra);
 pushLine(`<span class="d">id: ${sseSeq}</span>\n<span class="ev">event: ${kind==='segment'?'segment':'checkpoint'}</span>\n<span class="d">data:</span> ${jsonHtml(o)}${kind!=='segment'?'\n<span class="g">  ↳ awaiting referee co-sign (referee_ts_ms, sig)</span>':''}`)}
pushLine('<span class="d">: connected to qorcap 0.1.0 (simulated) · retry: 2000</span>');
let tick=0;(async()=>{for(let i=0;i<5;i++)await emit(i===2?'checkpoint_segment':'segment')})();
if(!RM){setInterval(()=>{if(paused||!seen(sse)||document.hidden)return;tick++;emit(tick%4===0?'checkpoint_segment':'segment')},1100)}else{emit('segment');emit('checkpoint_segment')}
$$('.term .cmd button').forEach(b=>b.addEventListener('click',()=>{const e=b.dataset.ev;
 if(e==='pause'){paused=!paused;b.textContent=paused?'resume':'pause';return}
 if(e==='score'){score[Math.random()<.5?0:1]+=Math.random()<.7?7:3;emit('score',{home:score[0],away:score[1]})}
 if(e==='nonce')emit('challenge',{nonce:rhex(16),phase:'kickoff',stamped:true});
 if(e==='final')emit('final',{home:score[0],away:score[1],final_basis:'clock_zero'})}));

/* ---------- Referee challenge ---------- */
const cc=watch($('#chalChain')),nonceEl=$('#nonce'),pat=$$('#pattern span'),kv=$$('#chalKv dd');let cSeq=310,cHead=rhex(32);
function addBlk(label,nc){const d=document.createElement('div');d.className='blk new'+(nc?' nc':'');d.innerHTML=label;if(cc.children.length)cc.insertAdjacentHTML('beforeend','<i class="link"></i>');cc.appendChild(d);
 while(cc.children.length>15){cc.firstElementChild.remove()}}
async function chainTick(){cSeq++;cHead=await sha(cHead+cSeq);addBlk(`#${cSeq}<br>${cHead.slice(0,4)}`)}
for(let i=0;i<5;i++)chainTick();
if(!RM)setInterval(()=>{if(seen(cc)&&!document.hidden)chainTick()},1600);
let chalBusy=false;
$('#chalBtn').addEventListener('click',async()=>{if(chalBusy)return;chalBusy=true;const n=rhex(16);const H='0123456789abcdef';let f=0;
 const done=()=>{nonceEl.innerHTML=[...n].map(c=>`<span class="hot">${c}</span>`).join('');setTimeout(()=>$$('span',nonceEl).forEach(s=>s.classList.remove('hot')),900)};
 if(RM)done();else await new Promise(r=>{const iv=setInterval(()=>{f++;nonceEl.textContent=[...n].map((c,i)=>i<f*2?c:H[Math.random()*16|0]).join('');if(f>=16){clearInterval(iv);done();r()}},40)});
 const b=parseInt(n.slice(0,8),16);pat.forEach((p,i)=>p.classList.toggle('on',(b>>(i*3))&1||i===(b%6)));
 const seq=cSeq;addBlk(`NONCE<br>${n.slice(0,4)}`,true);
 kv[0].textContent=Date.now();kv[1].textContent=seq;kv[2].textContent=cHead.slice(0,24)+'…';kv[3].textContent='nonce + pattern burned into video';kv[3].className='g';chalBusy=false});

/* ---------- Auto-clips with mock scorebug ---------- */
const scr=watch($('#screen')),sH=$('#sH'),sA=$('#sA'),sC=$('#sC'),bug=$('#bug'),fl=$('#flash'),tlEl=$('#tl'),tlFill=$('#tlFill'),log=$('#clipLog');
const plan=[{t:.12,s:[0,7]},{t:.34,s:[3,7]},{t:.55,s:[10,7]},{t:.78,s:[10,14]},{t:.9,s:[17,14]}];let cRun=0;
function fmt(sec){sec=Math.max(0,Math.ceil(sec));return Math.floor(sec/60)+':'+String(sec%60).padStart(2,'0')}
function clipAdd(pct,label,fin){const c=document.createElement('div');c.className='clip'+(fin?' fin':'');c.style.left=`calc(${pct*100}% - ${fin?24:12}px)`;c.style.width=fin?'24px':'18px';c.innerHTML=`<span>${label}</span>`;tlEl.appendChild(c)}
function logL(h){const a=log.innerHTML.split('<br>').filter(Boolean);a.push(h);log.innerHTML=a.slice(-4).join('<br>')}
function clipsStatic(){sH.textContent=17;sA.textContent=14;sC.textContent='0:00';tlFill.style.width='100%';plan.forEach((p,i)=>clipAdd(p.t,i%2?'':'clip_'+(i+1)));clipAdd(1,'final',1);log.innerHTML='<b>score</b> 17–14 → clips/score_005.mp4<br><span class="g">final</span> → clips/final.mp4 · final_basis=<span class="g">clock_zero</span>'}
async function clipLoop(){while(true){cRun++;const basis=cRun%2?'clock_zero':'scorebug_gone';$$('.clip',tlEl).forEach(c=>c.remove());log.innerHTML='<span style="color:#5d6788">reader: scorebug locked · AgreementWindow armed</span>';sH.textContent=0;sA.textContent=0;bug.classList.remove('gone');
 const D=17000;let t0=null,pi=0;await new Promise(res=>{function f(now){if(!seen(scr)||document.hidden){t0=null;requestAnimationFrame(f);return}
  if(t0===null)t0=now-(f.el||0);const el=now-t0;f.el=el;const p=clamp(el/D);tlFill.style.width=p*100+'%';
  const end=basis==='clock_zero'?1:.96;sC.textContent=fmt((1-p/end)*120);
  if(pi<plan.length&&p>=plan[pi].t){const e=plan[pi],old=[+sH.textContent,+sA.textContent];sH.textContent=e.s[0];sA.textContent=e.s[1];const w=e.s[0]!==old[0]?sH:sA;w.classList.remove('pop');void w.offsetWidth;w.classList.add('pop');fl.classList.remove('go');void fl.offsetWidth;fl.classList.add('go');
   const n=String(pi+1).padStart(3,'0');clipAdd(e.t,pi%2?'':'+'+(Math.abs(e.s[0]-old[0])||Math.abs(e.s[1]-old[1])));logL(`<b>score</b> ${e.s[0]}–${e.s[1]} confirmed → clips/score_${n}.mp4 (−20 s)`);pi++}
  if(basis==='scorebug_gone'&&p>=.96)bug.classList.add('gone');
  if(p>=1){clipAdd(1,'final',1);logL(`<span class="g">final</span> ${sH.textContent}–${sA.textContent} → clips/final.mp4 · final_basis=<span class="g">${basis}</span>`);res();return}
  requestAnimationFrame(f)}requestAnimationFrame(f)});
 await new Promise(r=>setTimeout(r,3800))}}
RM?clipsStatic():clipLoop();

/* ---------- Tamper demo ---------- */
const segsEl=$('#segs'),vd=$('#verdict'),vT=$('#vText');let segs=[],manifest=[];
async function initTamper(){segs=Array.from({length:6},()=>Array.from(crypto.getRandomValues(new Uint8Array(8))));manifest=[];let head='0'.repeat(64);
 for(let i=0;i<6;i++){const h=await sha(new Uint8Array(segs[i]));head=await sha(head+h);manifest.push({h,head})}
 segsEl.innerHTML=segs.map((s,i)=>`<div class="seg" data-i="${i}"><div class="t"><span>seq ${i}</span><span class="st">sealed</span></div><div class="bytes">${s.map((b,j)=>`<button data-j="${j}" aria-label="segment ${i} byte ${j}, value ${b.toString(16).padStart(2,'0')}, tap to flip">${b.toString(16).padStart(2,'0')}</button>`).join('')}</div><div class="h">${manifest[i].head.slice(0,14)}…</div></div>`).join('');
 render(-1)}
function render(bad,headNow){$$('.seg',segsEl).forEach((el,i)=>{el.classList.toggle('bad',i===bad);el.classList.toggle('tainted',bad>=0&&i>bad);$('.st',el).textContent=i===bad?'MISMATCH':bad>=0&&i>bad?'unverified':'sealed'});
 if(bad<0){vd.classList.remove('fail');vT.textContent='verify-bundle: OK · 6 segments · chain intact';$('.ic',vd).textContent='◆'}
 else{vd.classList.add('fail');$('.ic',vd).textContent='✕';vT.textContent=`verify-bundle: FAIL at seq ${bad} · sha256 ${headNow.slice(0,10)}… ≠ manifest ${manifest[bad].h.slice(0,10)}… · chain broken from here`}}
segsEl.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;const i=+b.closest('.seg').dataset.i,j=+b.dataset.j;segs[i][j]^=1<<(Math.random()*8|0);b.textContent=segs[i][j].toString(16).padStart(2,'0');b.classList.add('flip');
 for(let k=0;k<6;k++){const h=await sha(new Uint8Array(segs[k]));if(h!==manifest[k].h){render(k,h);return}}render(-1)});
$('#reset').addEventListener('click',initTamper);initTamper();

/* ---------- Watchdog ---------- */
const wcv=watch($('#wdCv')),wlog=$('#wdLog');const hist=[];let phase='flow',pt=0,part=1,lastT=0,wseq=88;
function wl(h){const a=wlog.innerHTML.split('<br>').filter(Boolean);a.push(h);wlog.innerHTML=a.slice(-3).join('<br>')}
function wdDraw(now){const {x,w,h}=fit(wcv);x.clearRect(0,0,w,h);const dt=Math.min(50,now-(lastT||now));lastT=now;pt+=dt;
 if(phase==='flow'&&pt>5200){phase='freeze';pt=0;wl('<span class="r">driver stall</span> · 0 bytes from ffmpeg stdout')}
 else if(phase==='freeze'&&pt>4000){phase='kill';pt=0;wl(`<b>watchdog</b> 4 s without data → kill ffmpeg child, reopen "USB3.0 Video"`)}
 else if(phase==='kill'&&pt>1300){phase='flow';pt=0;part=part>=9?2:part+1;wl(`<span class="g">reopened</span> → video.part${part}.mp4 · chain continues at seq ${wseq+1}`)}
 const rate=phase==='flow'?.55+.25*Math.sin(now*.004)+Math.random()*.15:phase==='kill'?.05:0;
 if(hist.length===0||now-hist[hist.length-1].t>60){hist.push({t:now,v:rate,ph:phase});if(phase==='flow'&&Math.random()<.05)wseq++}
 const n=Math.floor(w/6);while(hist.length>n)hist.shift();
 const base=h-30;x.strokeStyle='rgba(255,255,255,.06)';x.beginPath();x.moveTo(0,base+.5);x.lineTo(w,base+.5);x.stroke();
 hist.forEach((p,i)=>{const bh=p.v*(base-20);const X=w-(hist.length-i)*6;x.fillStyle=p.ph==='flow'?`rgba(${CY},${.35+p.v*.6})`:p.ph==='kill'?'rgba(255,77,94,.8)':'rgba(255,77,94,.25)';x.fillRect(X,base-bh,4,Math.max(2,bh))});
 x.font='600 11px JetBrains Mono,monospace';x.textAlign='left';x.fillStyle='rgba(151,160,186,.9)';x.fillText('bytes/s from ffmpeg',8,14);
 x.textAlign='right';x.fillStyle=phase==='flow'?`rgba(${CY},1)`:'rgba(255,140,151,1)';x.fillText(phase==='flow'?`● RECORDING · part${part}`:phase==='freeze'?'● NO DATA':'● REOPENING',w-8,14);
 if(phase==='freeze'){const k=pt/4000,cx=w/2,cy=base/2+8,R=Math.min(34,h*.18);x.lineWidth=4;x.strokeStyle='rgba(255,255,255,.1)';x.beginPath();x.arc(cx,cy,R,0,7);x.stroke();x.strokeStyle='rgba(255,77,94,.95)';x.beginPath();x.arc(cx,cy,R,-Math.PI/2,-Math.PI/2+k*Math.PI*2);x.stroke();x.textAlign='center';x.fillStyle='#fff';x.font='800 20px Big Shoulders Display,Impact,sans-serif';x.fillText((4-k*4).toFixed(1)+'s',cx,cy+7)}
 x.textAlign='left';x.fillStyle='rgba(93,103,136,1)';x.font='500 10px JetBrains Mono,monospace';x.fillText('watchdog = 4 s · backoff 1→8 s',8,h-8)}
for(let i=0;i<300;i++)hist.push({t:0,v:.5+.25*Math.sin(i*.25)+Math.random()*.12,ph:'flow'});
wl('<b>ffmpeg</b> child up · dshow → h264_mf · 1280x720@30 CFR');
if(RM){hist.length=0;for(let i=0;i<200;i++)hist.push({t:i,v:i>120&&i<170?0:.6,ph:i>120&&i<170?'freeze':'flow'});phase='flow';wdDraw(0);wl('<b>watchdog</b> 4 s without data → kill ffmpeg child, reopen');wl('<span class="g">reopened</span> → video.part2.mp4 · chain continues')}
else (function L(now){if(seen(wcv)&&!document.hidden)wdDraw(now);else lastT=0;requestAnimationFrame(L)})(0);

/* ---------- copy ---------- */
$('#copy').addEventListener('click',async e=>{const t=$('#ps').innerText;try{await navigator.clipboard.writeText(t);e.target.textContent='Copied ✓'}catch(_){e.target.textContent='Select & copy'}setTimeout(()=>e.target.textContent='Copy',1600)});
})();
/* ---------- version: one source (meta), refreshed from GitHub's latest release ---------- */
(()=>{const REPO='https://github.com/ConWan30/qorcap';const LATEST=REPO+'/releases/latest';const m=document.querySelector('meta[name="qorcap-version"]');
 function apply(v,zip){if(!v)return;document.querySelectorAll('[data-ver]').forEach(e=>e.textContent=v);
  document.querySelectorAll('[data-zipname]').forEach(e=>e.textContent=`qorcap-${v}-windows-x64.zip`);
  document.querySelectorAll('a[data-zip]').forEach(a=>a.href=zip||LATEST)}
 apply(m&&m.content);
 if(window.fetch)fetch('https://api.github.com/repos/ConWan30/qorcap/releases/latest',{headers:{Accept:'application/vnd.github+json'}}).then(r=>r.ok?r.json():null).then(j=>{
  if(!j||!/^v\d+(\.\d+)*$/.test(j.tag_name||''))return;const a=(j.assets||[]).find(x=>/windows-x64\.zip$/.test(x.name));apply(j.tag_name,a&&a.browser_download_url)}).catch(()=>{})})();
/* ---------- final-read ladder ---------- */
(()=>{const L=document.getElementById('ladder');if(!L)return;const li=[...L.querySelectorAll('li')];
 if(matchMedia('(prefers-reduced-motion: reduce)').matches){li.forEach(e=>e.classList.add('on'));return}
 let vis=false,i=0,tm=null;new IntersectionObserver(es=>{vis=es[0].isIntersecting;if(vis&&!tm)step()},{threshold:.3}).observe(L);
 function step(){if(!vis){tm=null;return}if(i>li.length){li.forEach(e=>e.classList.remove('on'));i=0;tm=setTimeout(step,900);return}
  if(i<li.length)li[i].classList.add('on');i++;tm=setTimeout(step,i>li.length?3200:1100)}})();
