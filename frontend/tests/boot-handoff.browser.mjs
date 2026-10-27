// Optional end-to-end check of the BUILT page in headless Chromium (desktop Chromium, not the TV's engine).
// A stand-in for the native splash listens on 127.0.0.1:4196 and records what the dashboard sends.
// Usage, after `vite build --outDir <dir>`:  node tests/boot-handoff.browser.mjs <dir>
// Playwright is resolved from PLAYWRIGHT_MODULE or the global npm root; it is not a project dependency.
import http from 'node:http';import {readFile} from 'node:fs/promises';import {extname,join,resolve} from 'node:path';import {execSync} from 'node:child_process';import {createRequire} from 'node:module';
const dist=resolve(process.argv[2]||'dist');
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||join(execSync('npm root -g').toString().trim(),'playwright'));
const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml'};
const site=http.createServer(async(req,res)=>{try{const path=join(dist,decodeURIComponent(new URL(req.url,'http://x').pathname).replace(/^\/$/,'/index.html'));if(!path.startsWith(dist))throw Error('outside');res.writeHead(200,{'content-type':types[extname(path)]||'application/octet-stream'});res.end(await readFile(path));}catch{res.writeHead(404);res.end();}});
const signals=[];let page;
const splash=http.createServer(async(req,res)=>{
 signals.push({method:req.method,url:req.url,headers:req.headers,
  state:await page.evaluate(()=>({splash:!!document.getElementById('boot-splash'),dissolving:!!document.querySelector('.boot-splash-out'),tiles:document.querySelectorAll('.tile img').length,focused:document.querySelectorAll('button.focused').length}))});
 res.writeHead(204,{'access-control-allow-origin':'*','connection':'close'});res.end();
});
const fail=message=>{console.error('FAIL '+message);process.exitCode=1;};
await new Promise(r=>site.listen(0,'127.0.0.1',r));await new Promise((r,j)=>splash.once('error',j).listen(4196,'127.0.0.1',r));
const browser=await chromium.launch({headless:true});
try{
 page=await browser.newPage({viewport:{width:1920,height:1080}});
 const firstPaint=[];page.on('console',m=>{if(m.type()==='warning'||m.type()==='error')console.log('page '+m.type()+': '+m.text());});
 await page.goto('http://127.0.0.1:'+site.address().port+'/',{waitUntil:'commit'});
 firstPaint.push(await page.evaluate(()=>!!document.getElementById('boot-splash')));
 for(let i=0;i<50&&!signals.length;i++)await page.waitForTimeout(100);
 await page.waitForTimeout(600);
 const after=await page.evaluate(()=>({splash:!!document.getElementById('boot-splash'),boot:window.QuietTVBoot||null}));
 if(!firstPaint[0])fail('inline splash was not in the first document');
 if(signals.length!==1)fail('expected exactly one ready signal, got '+signals.length);
 const s=signals[0]||{state:{}};
 if(s.method!=='POST'||s.url!=='/ready')fail('unexpected request '+s.method+' '+s.url);
 if(s.headers&&s.headers['access-control-request-method'])fail('ready caused a CORS preflight');
 if(!(s.state.focused===1&&s.state.tiles>=6))fail('signal came before the home row and focus were rendered: '+JSON.stringify(s.state));
 if(after.splash)fail('HTML splash was not removed after the dissolve');
 if(!after.boot||!after.boot.signal.ok)fail('dashboard did not record a successful hand-off: '+JSON.stringify(after.boot));
 console.log(JSON.stringify({firstPaintSplash:firstPaint[0],signals:signals.map(x=>({method:x.method,url:x.url,origin:x.headers.origin,state:x.state})),after},null,1));
 // In-page Home returns (what the bridge does on the Home key) must never bring a splash back or signal again.
 await page.evaluate(()=>{window.__splashSeen=false;new MutationObserver(()=>{if(document.getElementById('boot-splash'))window.__splashSeen=true;}).observe(document.documentElement,{childList:true,subtree:true});});
 for(const key of ['ArrowRight','ArrowDown','Escape','Escape'])await page.keyboard.press(key);
 await page.evaluate(()=>window.QuietTVResetHome());await page.waitForTimeout(150);await page.evaluate(()=>window.QuietTVResetHome());await page.waitForTimeout(400);
 const home=await page.evaluate(()=>({seen:window.__splashSeen,present:!!document.getElementById('boot-splash'),focused:document.querySelectorAll('button.focused').length}));
 if(home.seen||home.present)fail('a Home return brought the splash back');if(signals.length!==1)fail('a Home return sent another ready signal');if(home.focused!==1)fail('Home return lost focus');
 // A fresh page load (the widget relaunched) DOES show the HTML splash again until ready; measure what it shows.
 await page.reload({waitUntil:'commit'});const t0=Date.now();let visibleMs=null,maxWord=0;
 while(Date.now()-t0<5000){const s=await page.evaluate(()=>{const el=document.getElementById('boot-splash');if(!el)return null;const letters=[...el.querySelectorAll('.bs-word span')];return {out:el.classList.contains('boot-splash-out'),word:Math.max(0,...letters.map(l=>+getComputedStyle(l).opacity))};});
  if(!s){visibleMs=Date.now()-t0;break;}if(!s.out)maxWord=Math.max(maxWord,s.word);await page.waitForTimeout(10);}
 if(visibleMs===null)fail('relaunched page never removed its splash');if(signals.length!==2)fail('relaunch should signal once more, got '+(signals.length-1));
 console.log(JSON.stringify({homeReturn:home,relaunch:{splashPresentMs:visibleMs,maxWordmarkOpacityBeforeDissolve:maxWord,signals:signals.length-1}}));
 if(!process.exitCode)console.log('PASS built dashboard signals ready once, after focus and the home row, then removes the HTML splash; Home returns never replay it; a relaunch shows it only until ready');
}finally{await browser.close();site.close();splash.close();}
