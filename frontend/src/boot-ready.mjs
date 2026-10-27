// Dashboard -> boot splash hand-off. Once per page load: after the first commit, wait (bounded) for the critical art
// and for spatial focus, let two frames paint, then tell the native splash on 127.0.0.1:4196 and retire the inline
// HTML splash. Every wait has a limit, so a missing image, focus or splash can delay nothing past these numbers.
export const READY_URL='http://127.0.0.1:4196/ready';
export const LIMITS=Object.freeze({artMs:2000,focusMs:1000,attempts:20,retryMs:1000,requestMs:1500,dissolveMs:700,nativeAnswerMs:250});
export function readyUrl(reduced){return READY_URL+(reduced?'?motion=off':'');}
export function waitForArt(urls,{loadImage,setTimer,clearTimer},timeoutMs=LIMITS.artMs){
 return new Promise(resolve=>{
  const result={loaded:0,failed:0,timedOut:false};let left=urls.length,done=false,timer=null;
  const finish=()=>{if(done)return;done=true;clearTimer(timer);resolve(result);};
  timer=setTimer(()=>{result.timedOut=true;finish();},timeoutMs);
  if(!left)finish();
  for(const url of urls)Promise.resolve().then(()=>loadImage(url)).then(()=>{result.loaded++;},()=>{result.failed++;}).then(()=>{if(--left===0)finish();});
 });
}
// A no-cors POST with no custom headers is a simple request: no preflight, and a refused connection rejects, so
// retries cover a splash that starts after the dashboard. It stops at the first answer or after LIMITS.attempts.
// onFirst(answered) reports the first attempt once, so the caller knows whether a native splash is listening.
export async function signalReady(url,{fetchFn,sleep,setTimer,clearTimer,warn,onFirst=()=>{}}){
 let last=null;
 for(let attempt=1;attempt<=LIMITS.attempts;attempt++){
  const controller=new AbortController(),timer=setTimer(()=>controller.abort(),LIMITS.requestMs);
  try{await fetchFn(url,{method:'POST',mode:'no-cors',cache:'no-store',signal:controller.signal});if(attempt===1)onFirst(true);return {ok:true,attempts:attempt};}
  catch(error){last=error;if(attempt===1)onFirst(false);}
  finally{clearTimer(timer);}
  if(attempt<LIMITS.attempts)await sleep(LIMITS.retryMs);
 }
 warn('Quiet TV boot splash was not reached after '+LIMITS.attempts+' attempts: '+(last&&last.message||last));
 return {ok:false,attempts:LIMITS.attempts};
}
export function dismissSplash(doc,instant,setTimer){
 const splash=doc.getElementById('boot-splash');if(!splash)return false;
 if(instant)splash.remove();else{splash.classList.add('boot-splash-out');setTimer(()=>splash.remove(),LIMITS.dissolveMs+80);}
 return true;
}
export function createBootHandoff(deps){
 let run=null;
 // nativeEnabled:false (owner preview) never contacts the native splash: no fetch, no retry; only the HTML splash leaves.
 async function go({art,focused,reduced,nativeEnabled=true}){
  const started=deps.now(),artResult=await waitForArt(art,deps);
  const focusStart=deps.now();while(!focused()&&deps.now()-focusStart<LIMITS.focusMs)await deps.nextFrame();
  const hasFocus=!!focused();if(!hasFocus)deps.warn('Quiet TV signalled ready without spatial focus after '+LIMITS.focusMs+' ms');
  await deps.nextFrame();await deps.nextFrame();
  const readyMs=Math.round(deps.now()-started);
  if(nativeEnabled===false){const splash=deps.dismiss(!!reduced);return {readyMs,art:artResult,focused:hasFocus,htmlSplash:splash,nativeInFront:false,signal:{ok:false,skipped:true,attempts:0}};}
  // Two splashes dissolving on top of each other would read as an uneven, late fade. If the native splash answers,
  // it is the one in front and does the dissolve, so the HTML copy underneath goes at once. With no answer within
  // nativeAnswerMs the HTML splash is the visible one and dissolves itself.
  let answered;const first=new Promise(resolve=>{answered=resolve;});
  const pending=signalReady(readyUrl(reduced),{...deps,onFirst:answered});
  const nativeInFront=await Promise.race([first,deps.sleep(LIMITS.nativeAnswerMs).then(()=>false)]);
  const splash=deps.dismiss(reduced||nativeInFront);
  return {readyMs,art:artResult,focused:hasFocus,htmlSplash:splash,nativeInFront,signal:await pending};
 }
 return function start(options){if(!run)run=go(options);return run;};
}
function loadImage(url){return new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>image.decode?image.decode().then(resolve,resolve):resolve();image.onerror=()=>reject(Error('Image failed: '+url));image.src=url;});}
export const startBootHandoff=createBootHandoff({
 loadImage,fetchFn:(url,options)=>fetch(url,options),now:()=>performance.now(),warn:message=>console.warn(message),
 setTimer:(fn,ms)=>setTimeout(fn,ms),clearTimer:id=>clearTimeout(id),sleep:ms=>new Promise(resolve=>setTimeout(resolve,ms)),
 // A hidden page gets no animation frames; the 100 ms cap keeps the hand-off moving so the splash still leaves.
 nextFrame:()=>new Promise(resolve=>{requestAnimationFrame(()=>resolve());setTimeout(resolve,100);}),dismiss:instant=>dismissSplash(document,instant,setTimeout),
});
