// Owner-generated runtime app list: strict validation, fail-closed, IPTV only when enabled, legacy untouched.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolveRuntimeApps,validateRuntimeApps,safeIcon} from '../src/runtime-catalog.mjs';
import {launcherApps} from '../src/launcher-apps.mjs';
import {homeApps} from '../src/policy.mjs';
const compiled=[{Id:'compiled.one',Name:'One',Icon:''},{Id:'compiled.two',Name:'Two',Icon:''}];
const good=[{Id:'own.a',Name:'A'},{Id:'own.b',Name:'B',Icon:'icons/b.png'}];
const ids=r=>r.apps.map(a=>a.Id);

test('legacy mode (no apps, no owner marker) keeps the compiled pool with IPTV',()=>{
 for(const config of [undefined,null,{},{token:'x'},{preview:false},{preview:'true'}]){
  const r=resolveRuntimeApps({compiled,config});
  assert.deepEqual(r.apps,launcherApps(compiled));assert.equal(r.ownerMode,false);assert.equal(r.error,'');
  assert.ok(ids(r).includes('quiet.iptv'));
 }
});

test('owner list is used as-is, IPTV off by default and only true enables it',()=>{
 const off=resolveRuntimeApps({compiled,apps:good});
 assert.deepEqual(ids(off),['own.a','own.b']);assert.equal(off.ownerMode,true);assert.equal(off.error,'');
 assert.equal(off.apps[0].Icon,'');
 for(const iptvEnabled of [false,'true',1,null,undefined])assert.ok(!ids(resolveRuntimeApps({compiled,apps:good,config:{iptvEnabled}})).includes('quiet.iptv'));
 const on=resolveRuntimeApps({compiled,apps:good,config:{iptvEnabled:true}});
 assert.deepEqual(ids(on),['own.a','own.b','quiet.iptv']);
});

test('virtual IPTV in the input is dropped when off and never duplicated when on',()=>{
 const withIptv=[...good,{Id:'quiet.iptv',Name:'IPTV'}];
 assert.ok(!ids(resolveRuntimeApps({compiled,apps:withIptv,config:{iptvEnabled:false}})).includes('quiet.iptv'));
 assert.equal(ids(resolveRuntimeApps({compiled,apps:withIptv,config:{iptvEnabled:true}})).filter(i=>i==='quiet.iptv').length,1);
});

test('an empty owner list is valid and gives an empty pool',()=>{
 const r=resolveRuntimeApps({compiled,apps:[]});
 assert.deepEqual(r,{apps:[],error:'',ownerMode:true});
 assert.deepEqual(homeApps(r.apps,[],6),[]);
});

test('guard off: a present but malformed list never falls back to the compiled apps',()=>{
 const bad=[null,'x',{},42,{0:1},[null],[{Id:'',Name:'x'}],[{Id:' a',Name:'x'}],[{Id:'a/b',Name:'x'}],[{Id:'../x',Name:'x'}],[{Id:'a',Name:5}],[{Name:'x'}],
  [{Id:'a',Name:'x'},{Id:'a',Name:'y'}],[{Id:'a',Name:'x',Icon:5}],[{Id:'a',Name:'x',Icon:'https://e.com/i.png'}],[{Id:'a',Name:'x',Icon:'/abs.png'}],
  [{Id:'a',Name:'x',Icon:'../up.png'}],[{Id:'a',Name:'x',Icon:'a\b.png'}],[{Id:'a',Name:'x',Icon:'data:image/png;base64,AA'}],[{Id:'a',Name:'x'},{Id:'b'}],[[]]];
 for(const apps of bad)for(const config of [undefined,{iptvEnabled:true}]){
  const r=resolveRuntimeApps({compiled,apps,config});
  assert.deepEqual(r.apps,[],JSON.stringify(apps));assert.equal(r.ownerMode,true);assert.match(r.error,/^Setup problem: /);
  assert.ok(!ids(r).some(i=>i.startsWith('compiled.')||i==='quiet.iptv'));
 }
 assert.equal(resolveRuntimeApps({compiled,apps:null}).apps.length,0);
});

test('owner-preview markers without a runtime list refuse the compiled fallback',()=>{
 for(const config of [{preview:true},{schema:'quiettv-widget/1'},{preview:true,iptvEnabled:true}]){
  const r=resolveRuntimeApps({compiled,config});
  assert.deepEqual(r.apps,[]);assert.equal(r.ownerMode,true);assert.match(r.error,/^Setup problem: /);
 }
});

test('icons: empty and local relative pass, everything else fails',()=>{
 for(const i of ['','a.png','icons/a.png','a-b_c.d/e.svg'])assert.ok(safeIcon(i),i);
 for(const i of ['/a','//h/a','a//b','./a','a/..','..','a/../b','http://x','a:b','a\b','a b','a?x','a#x','a%2e',undefined,null,7])assert.ok(!safeIcon(i),String(i));
});

test('callers arrays are never mutated and the result is a copy',()=>{
 const input=[{Id:'x',Name:'X'},{Id:'quiet.iptv',Name:'IPTV'}],snapshot=JSON.stringify(input),comp=JSON.stringify(compiled);
 const r=resolveRuntimeApps({compiled,apps:input,config:{iptvEnabled:true}});
 r.apps.push({Id:'z',Name:'Z'});r.apps[0].Name='changed';
 assert.equal(JSON.stringify(input),snapshot);assert.equal(JSON.stringify(compiled),comp);
 assert.equal(validateRuntimeApps(input).apps[0]!==input[0],true);
});

test('main.jsx wires the resolver to the window globals and guards empty pools',()=>{
 const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
 assert.match(main,/resolveRuntimeApps\(\{compiled:catalog,apps:window\.QuietTVApps,config:window\.QuietTVConfig\}\)/);
 assert.doesNotMatch(main,/(?<!length\?'home-'\+)first\[0\]\.Id/,'no unguarded first[0].Id');
 assert.match(main,/runtime\.error&&/);
});
