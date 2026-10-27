// Render the real inline splash without its app bundle. A native-to-web handoff
// must not introduce an invisible wordmark at animation time zero.
// Desktop Chromium check only; physical cold-boot continuity is a separate test.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {join} from 'node:path';
import {execSync} from 'node:child_process';
const require=createRequire(import.meta.url);
const globalRoot=execSync('npm root -g',{windowsHide:true}).toString().trim();
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||join(globalRoot,'playwright'));
let html=await readFile(process.argv[2]||'index.html','utf8');
html=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');
if(process.argv.includes('--old-opacity'))html=html.replace('bs-letter{from{opacity:1;','bs-letter{from{opacity:0;');
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage({viewport:{width:1920,height:1080}});
 await page.setContent(html);
 for(const time of [0,450,1000]){
  const result=await page.evaluate(time=>{
   for(const animation of document.getAnimations()){animation.pause();animation.currentTime=time;}
   const letters=[...document.querySelectorAll('.bs-word span')];
   return {letters:letters.length,opacity:letters.map(el=>+getComputedStyle(el).opacity),background:getComputedStyle(document.getElementById('boot-splash')).backgroundColor};
  },time);
  assert.equal(result.letters,7,'real wordmark must be present');
  assert(result.opacity.every(value=>value>=.99),`wordmark disappears during handoff at ${time} ms: ${result.opacity}`);
 }
 console.log('PASS real inline wordmark stays visible at 0, 450 and 1000 ms');
}finally{await browser.close();}
