// Offline checks for the dashboard -> boot splash hand-off. A virtual clock drives every timer, so the limits are
// measured, not assumed. BOOT_READY_PATH lets the regression controls point these tests at a deliberately broken copy.
import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';import {pathToFileURL} from 'node:url';
const source=process.env.BOOT_READY_PATH?pathToFileURL(process.env.BOOT_READY_PATH):new URL('../src/boot-ready.mjs',import.meta.url);
const {createBootHandoff,dismissSplash,LIMITS}=await import(source);

function harness({fetchFn,loadImage=()=>Promise.resolve()}={}){
 let time=0,seq=0;const timers=new Map(),calls=[],warnings=[],dismissed=[];
 const setTimer=(fn,ms)=>{const id=++seq;timers.set(id,{at:time+ms,fn});return id;};
 const deps={now:()=>time,setTimer,clearTimer:id=>timers.delete(id),sleep:ms=>new Promise(r=>setTimer(r,ms)),nextFrame:()=>new Promise(r=>setTimer(r,16)),
  loadImage,warn:m=>warnings.push(m),dismiss:instant=>{dismissed.push({instant,at:time});return true;},
  fetchFn:(url,options)=>{calls.push({url,options,at:time});return (fetchFn||(()=>Promise.resolve({type:'opaque'})))(url,options,calls.length);}};
 async function settle(promise,limitMs=120000){
  let done=false,value;promise.then(v=>{done=true;value=v;});
  for(;;){for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));if(done)return value;
   const next=[...timers.entries()].sort((a,b)=>a[1].at-b[1].at)[0];if(!next||next[1].at>limitMs)throw Error('hand-off never finished; virtual time '+time);
   timers.delete(next[0]);time=next[1].at;next[1].fn();}
 }
 return {deps,calls,warnings,dismissed,settle,time:()=>time,start:createBootHandoff(deps)};
}
const ready={art:['wallpaper.png','netflix.svg'],focused:()=>true,reduced:false};

test('signals the native splash exactly once, as a simple loopback POST, and retires the HTML splash under it',async()=>{
 const h=harness();const first=h.start(ready),second=h.start(ready);assert.equal(first,second,'a second start must reuse the first run');
 const result=await h.settle(first);
 assert.equal(h.calls.length,1);const [{url,options}]=h.calls;
 assert.equal(url,'http://127.0.0.1:4196/ready');assert.equal(options.method,'POST');assert.equal(options.mode,'no-cors');assert.equal(options.headers,undefined,'custom headers would force a preflight');
 assert.deepEqual(h.dismissed.map(d=>d.instant),[true],'with the native splash in front the HTML copy goes at once, no double fade');assert.equal(result.nativeInFront,true);assert.equal(result.signal.ok,true);assert.equal(result.focused,true);assert.equal(result.art.loaded,2);
});
test('no artificial minimum: with art and focus ready, the signal goes two frames after the first commit',async()=>{
 const h=harness();await h.settle(h.start(ready));assert.ok(h.calls[0].at<=40,'signalled at '+h.calls[0].at+' ms');
});
test('never signals before spatial focus exists',async()=>{
 const h=harness();const result=await h.settle(h.start({...ready,focused:()=>h.time()>=500}));
 assert.ok(h.calls[0].at>=500&&h.dismissed[0].at>=500,'signalled at '+h.calls[0].at+' ms, before focus at 500 ms');assert.equal(result.focused,true);assert.deepEqual(h.warnings,[]);
});
test('focus that never arrives still releases the splash, bounded and logged',async()=>{
 const h=harness();const result=await h.settle(h.start({...ready,focused:()=>false}));
 assert.equal(result.focused,false);assert.equal(h.calls.length,1);assert.ok(h.calls[0].at>=LIMITS.focusMs&&h.calls[0].at<=LIMITS.focusMs+100);assert.match(h.warnings.join(),/without spatial focus/);
});
test('critical art is bounded: a hung image cannot hold the splash past the art limit',async()=>{
 const h=harness({loadImage:url=>url==='wallpaper.png'?new Promise(()=>{}):Promise.resolve()});const result=await h.settle(h.start(ready));
 assert.equal(result.art.timedOut,true);assert.equal(result.art.loaded,1);assert.ok(h.calls[0].at>=LIMITS.artMs&&h.calls[0].at<=LIMITS.artMs+100,'signalled at '+h.calls[0].at);
});
test('a broken image counts as settled, not as a reason to wait',async()=>{
 const h=harness({loadImage:url=>url==='netflix.svg'?Promise.reject(Error('404')):Promise.resolve()});const result=await h.settle(h.start(ready));
 assert.deepEqual([result.art.loaded,result.art.failed,result.art.timedOut],[1,1,false]);assert.ok(h.calls[0].at<=40);
});
test('reduced motion asks both splashes to leave without a dissolve',async()=>{
 const h=harness();await h.settle(h.start({...ready,reduced:true}));assert.equal(h.calls[0].url,'http://127.0.0.1:4196/ready?motion=off');assert.deepEqual(h.dismissed.map(d=>d.instant),[true]);
});
test('without a native splash the HTML splash dissolves itself, right after the refused first attempt',async()=>{
 const h=harness({fetchFn:()=>Promise.reject(TypeError('Failed to fetch'))});const result=await h.settle(h.start(ready));
 assert.deepEqual(h.dismissed.map(d=>d.instant),[false]);assert.equal(result.nativeInFront,false);assert.ok(h.dismissed[0].at-h.calls[0].at<=5,'dismissed '+(h.dismissed[0].at-h.calls[0].at)+' ms after the first attempt');
});
test('a first request that hangs cannot hold the HTML splash: it dissolves after the native answer window',async()=>{
 const h=harness({fetchFn:(u,o,n)=>n===1?new Promise((_,reject)=>o.signal.addEventListener('abort',()=>reject(Error('aborted')))):Promise.resolve({})});await h.settle(h.start(ready));
 const waited=h.dismissed[0].at-h.calls[0].at;assert.equal(h.dismissed[0].instant,false);assert.ok(waited>=200&&waited<=300,'HTML splash waited '+waited+' ms for an answer');
});
test('a splash that starts after the dashboard still gets the signal; retries stop at the first answer',async()=>{
 const h=harness({fetchFn:(u,o,n)=>n<=3?Promise.reject(TypeError('Failed to fetch')):Promise.resolve({type:'opaque'})});const result=await h.settle(h.start(ready));
 assert.equal(h.calls.length,4);assert.deepEqual(result.signal,{ok:true,attempts:4});
});
test('no splash installed: a bounded number of attempts, then a warning, and the dashboard is unaffected',async()=>{
 const h=harness({fetchFn:()=>Promise.reject(TypeError('Failed to fetch'))});const result=await h.settle(h.start(ready));
 assert.equal(h.calls.length,LIMITS.attempts);assert.equal(result.signal.ok,false);assert.ok(h.time()<=LIMITS.attempts*(LIMITS.retryMs+LIMITS.requestMs));assert.match(h.warnings.join(),/not reached/);assert.equal(h.dismissed.length,1);
});
test('a request that hangs is aborted and retried',async()=>{
 const h=harness({fetchFn:(u,o,n)=>n===1?new Promise((_,reject)=>o.signal.addEventListener('abort',()=>reject(Error('aborted')))):Promise.resolve({})});const result=await h.settle(h.start(ready));
 assert.equal(h.calls.length,2);assert.ok(h.calls[1].at>=LIMITS.requestMs);assert.equal(result.signal.ok,true);
});

function fakeSplash(){const classes=new Set();const el={removed:false,classList:{add:c=>classes.add(c)},remove(){el.removed=true;},classes};return el;}
test('HTML splash: dissolve then removal, instant removal when asked, and a missing splash is fine',()=>{
 const el=fakeSplash(),timers=[];assert.equal(dismissSplash({getElementById:()=>el},false,(fn,ms)=>timers.push({fn,ms})),true);
 assert.ok(el.classes.has('boot-splash-out')&&!el.removed);assert.ok(timers[0].ms>=600&&timers[0].ms<=1000,'removal after '+timers[0].ms+' ms: long enough for the dissolve, gone within a second');timers[0].fn();assert.equal(el.removed,true);
 const quick=fakeSplash();dismissSplash({getElementById:()=>quick},true,()=>assert.fail('an instant removal must not schedule a dissolve'));assert.equal(quick.removed,true);
 assert.equal(dismissSplash({getElementById:()=>null},false,()=>{}),false);
});
test('index.html paints the splash before the bundle and removes it by itself if the bundle never runs',()=>{
 const html=readFileSync(process.env.INDEX_HTML_PATH||new URL('../index.html',import.meta.url),'utf8');
 const splash=html.indexOf('id="boot-splash"'),root=html.indexOf('id="root"'),bundle=html.indexOf('type="module"');
 assert.ok(splash>0&&splash<root&&root<bundle,'splash markup must come before #root and the bundle');
 const inline=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);assert.equal(inline.length,1,'one inline classic script');
 const el={parentNode:{removeChild:node=>{el.gone=node===el;}}},pending=[];
 vm.runInNewContext(inline[0],{document:{getElementById:id=>id==='boot-splash'?el:null},setTimeout:(fn,ms)=>pending.push({fn,ms})});
 assert.equal(pending.length,1);assert.ok(pending[0].ms<=15000,'fail-open removal after '+pending[0].ms+' ms');pending[0].fn();assert.equal(el.gone,true);
});

test('legacy default still signals the native splash; nativeEnabled:true is the same',async()=>{
 for(const extra of [{},{nativeEnabled:true}]){
  const h=harness();const result=await h.settle(h.start({...ready,...extra}));
  assert.equal(h.calls.length,1);assert.equal(result.nativeInFront,true);assert.equal(result.signal.ok,true);
 }
});
test('nativeEnabled:false (preview) makes zero fetches, never retries, and still retires the HTML splash',async()=>{
 const h=harness({fetchFn:()=>{throw Error('preview must not fetch');}});
 const result=await h.settle(h.start({...ready,nativeEnabled:false}));
 assert.equal(h.calls.length,0);assert.deepEqual(h.warnings,[]);
 assert.deepEqual(result.signal,{ok:false,skipped:true,attempts:0});assert.equal(result.nativeInFront,false);assert.equal(result.htmlSplash,true);
 assert.deepEqual(h.dismissed.map(d=>d.instant),[false],'HTML splash dissolves itself with motion on');assert.ok(h.dismissed[0].at<=40,'after art, focus and two frames');
 const r=harness({fetchFn:()=>{throw Error('no');}});await r.settle(r.start({...ready,nativeEnabled:false,reduced:true}));
 assert.deepEqual(r.dismissed.map(d=>d.instant),[true]);assert.equal(r.calls.length,0);
 const f=harness({fetchFn:()=>{throw Error('no');}});await f.settle(f.start({...ready,nativeEnabled:false,focused:()=>false}));
 assert.equal(f.calls.length,0,'still waits for the focus limit, still no fetch');
});
test('main.jsx passes nativeEnabled from the preview flag',()=>{
 assert.match(readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8'),/startBootHandoff\(\{nativeEnabled:window\.QuietTVConfig\?\.preview!==true,/);
});
