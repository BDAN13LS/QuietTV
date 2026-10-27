// The launcher's app pool and saved lists after the catalogue was cut from 31 apps to the 16 the TV can launch.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {launcherApps,initialSaved,uninstalledIds} from '../src/launcher-apps.mjs';
import {normalizeOrder,homeApps} from '../src/policy.mjs';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const catalog=JSON.parse(read('../src/catalog.json')),main=read('../src/main.jsx');
// The 15 IDs commit 433291a removed from the 31-app catalogue (no longer eligible on the TV).
const REMOVED=['com.samsung.tv.SecurityCenter','25emanual1.emanual','com.samsung.tv.samsung-health','com.samsung.tv.AICompanionUILauncher','ZmmGjO6VKO.slingtv','rJeHak5zRg.Spotify','com.samsung.tv.SmartThingsApp','com.samsung.tv.bixby-promotion','3KA0pm7a7V.TubiTV','4VlIBiGWKj.samsungshell','HupEmjIB4I.Glance','zigzSWa1hy.InstagramAirwave','Fsuqze47hn.PhyndGamesII','9TYrsHWbaB.klyq','yL49PNFmjW.PromotionApp','GHI3a0zMSx.XboxGamePass','CmeyUlnhYY.LunaWebApp'];
const HBO='5b8c3eb16b.BeamCTVDev',defaults=['org.tizen.netflix-app','9Ur5IzDKqV.TizenYouTube','com.samsung.tv.aria-video','org.tizen.primevideo','EYm8vc1St4.Philo',HBO];
const original=[...catalog,...REMOVED.map(Id=>({Id,Name:Id,Icon:''}))];
// What a TV that used the old build has saved: removed apps arranged first, favourited and recently opened.
const savedOnTv={order:['ZmmGjO6VKO.slingtv','org.tizen.netflix-app','3KA0pm7a7V.TubiTV',HBO,'quiet.iptv'],favorites:['rJeHak5zRg.Spotify',HBO,'GHI3a0zMSx.XboxGamePass'],recent:['3KA0pm7a7V.TubiTV',HBO,'CmeyUlnhYY.LunaWebApp']};
const leaks=lists=>REMOVED.filter(id=>lists.some(list=>list.indexOf(id)>=0));

test('original 32-app pool leaks removed IDs into every list; the new 15-app pool is clean',()=>{
 assert.equal(catalog.length,14);
 // Old main.jsx: allApps=[...catalog, IPTV]; order normalised against it; favorites and recents used as saved.
 const oldPool=[...original,{Id:'quiet.iptv',Name:'IPTV',Icon:''}],oldOrder=normalizeOrder(savedOnTv.order,oldPool);
 assert.equal(oldPool.length,32);
 assert.deepEqual(leaks([oldPool.map(a=>a.Id),oldOrder,savedOnTv.favorites,savedOnTv.recent]).sort(),[...REMOVED].sort(),'old flow offers all 15 removed apps');
 const pool=launcherApps(catalog),clean=initialSaved(pool,savedOnTv,defaults);
 assert.equal(pool.length,15);assert.equal(pool.at(-1).Id,'quiet.iptv');
 assert.deepEqual(leaks([pool.map(a=>a.Id),clean.order,clean.favorites,clean.recent]),[]);
 assert.deepEqual(clean.order.slice(0,3),['org.tizen.netflix-app',HBO,'quiet.iptv'],'surviving arranged order is kept');
 assert.deepEqual(clean.favorites,[HBO]);assert.deepEqual(clean.recent,[HBO]);
 assert.equal(clean.order.length,15);
 assert.ok(homeApps(pool.filter(a=>defaults.includes(a.Id)||clean.favorites.includes(a.Id)||a.Id==='quiet.iptv'),clean.favorites,7).every(a=>REMOVED.indexOf(a.Id)<0));
});

test('apps the TV can launch stay, including Browser and the streaming apps',()=>{
 const ids=launcherApps(catalog).map(a=>a.Id);
 for(const id of ['org.tizen.browser',HBO,...defaults])assert.ok(ids.includes(id),id);
 assert.equal(new Set(ids).size,ids.length);assert.ok(!ids.includes('com.samsung.tv.SecurityCenter'));assert.ok(!ids.includes('25emanual1.emanual'));
 assert.equal(launcherApps([...catalog,{Id:'quiet.iptv',Name:'x',Icon:''}]).filter(a=>a.Id==='quiet.iptv').length,1,'IPTV once');
});

test('saved-list fallbacks: missing favorites use the defaults, an emptied list stays empty, recents cap at 8',()=>{
 const pool=launcherApps(catalog);
 assert.deepEqual(initialSaved(pool,{order:null,favorites:null,recent:null},defaults).favorites,defaults);
 assert.deepEqual(initialSaved(pool,{order:[],favorites:[],recent:[]},defaults).favorites,[]);
 assert.equal(initialSaved(pool,{order:'x',favorites:{},recent:pool.map(a=>a.Id)},defaults).recent.length,8);
});

test('main.jsx builds every view from the clean pool and writes the clean lists back',()=>{
 assert.match(main,/const allApps=launcherApps\(catalog\);/);
 assert.match(main,/initialSaved\(allApps,\{order:saved\('quiet-order',\[\]\),favorites:saved\('quiet-favorites',null\),recent:saved\('quiet-recent',\[\]\)\},defaultFavorites\)/);
 assert.match(main,/useState\(start\.order\)/);assert.match(main,/useState\(start\.favorites\)/);assert.match(main,/useState\(start\.recent\)/);
 assert.doesNotMatch(main,/const allApps=\[\.\.\.catalog/,'no second, unfiltered pool');
});

const notFound=()=>{const e=new Error('not found');e.name='NotFoundError';throw e;};
test('uninstall check hides only apps the TV definitely reports as not found',()=>{
 const pool=launcherApps(catalog),asked=[];
 const tv=id=>{asked.push(id);if(id===HBO||id==='org.tizen.browser')notFound();return {id};};
 assert.deepEqual(uninstalledIds(pool,tv),['org.tizen.browser',HBO]);
 assert.equal(asked.length,14,'known catalogue IDs only, never IPTV');assert.ok(!asked.includes('quiet.iptv'));
 assert.deepEqual(uninstalledIds(pool,null),[],'no API: nothing hidden');
 assert.deepEqual(uninstalledIds(pool,id=>{if(id===HBO){const e=new Error('denied');e.name='SecurityError';throw e;}return {id};}),[],'other errors hide nothing');
 assert.deepEqual(uninstalledIds(pool,notFound),[],'every app "missing" is a broken API, not an empty TV');
});
