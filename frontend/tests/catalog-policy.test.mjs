import test from 'node:test';import assert from 'node:assert/strict';
import {filterCatalog,analyzeCatalog,pruneSavedIds,validateInventory,VIRTUAL_APPS} from '../src/catalog-policy.mjs';
import {normalizeOrder,recordRecent} from '../src/policy.mjs';
const rec=(Id,over={})=>({Id,Name:Id,Installed:true,Disabled:false,Launchable:true,...over});
const HBO='5b8c3eb16b.BeamCTVDev';
const catalog=[{Id:'org.tizen.netflix-app',Name:'Netflix',Icon:''},{Id:HBO,Name:'HBO Max',Icon:''},{Id:'fake.removed',Name:'Removed Fake',Icon:''},{Id:'fake.tvplus',Name:'TV Plus',Icon:''},{Id:'fake.held',Name:'Held',Icon:''},{Id:'fake.helper',Name:'Helper',Icon:''},{Id:'fake.gone',Name:'Not In Snapshot',Icon:''},{Id:'com.samsung.tv.SmartThingsApp',Name:'SmartThings',Icon:''}];
const inventory=[rec('org.tizen.netflix-app'),rec(HBO),rec('fake.removed',{Installed:false}),rec('fake.tvplus',{Disabled:true}),rec('fake.held',{Launchable:false}),rec('fake.helper',{Launchable:false}),rec('com.samsung.tv.SmartThingsApp',{Name:'SmartThings'})];
const hidden=['fake.removed','fake.tvplus','fake.held','fake.helper','fake.gone'];
const visibleIds=apps=>apps.map(a=>a.Id);
// Property the UI needs: nothing deleted, disabled, not launchable or unknown is offered.
const leaks=apps=>hidden.filter(id=>apps.some(a=>a.Id===id));

test('old static catalogue leaks tombstones; filtered catalogue does not (both ways)',()=>{
  assert.deepEqual(leaks(catalog).sort(),[...hidden].sort());
  assert.deepEqual(leaks(filterCatalog(catalog,inventory)),[]);
});
test('keeps current apps in catalogue order, including HBO Max and Samsung-branded launchables',()=>{
  assert.deepEqual(visibleIds(filterCatalog(catalog,inventory)),['org.tizen.netflix-app',HBO,'com.samsung.tv.SmartThingsApp']);
});
test('removal reasons are reported per app',()=>{
  const r=Object.fromEntries(analyzeCatalog(catalog,inventory).removed.map(x=>[x.Id,x.reason]));
  assert.deepEqual(r,{'fake.removed':'not-installed','fake.tvplus':'disabled','fake.held':'not-launchable','fake.helper':'not-launchable','fake.gone':'not-in-snapshot'});
});
test('a held package is hidden while its enabled sibling stays',()=>{
  const cat=[{Id:'pkg.a',Name:'A'},{Id:'pkg.b',Name:'A sibling'}];
  assert.deepEqual(visibleIds(filterCatalog(cat,[rec('pkg.a',{Launchable:false}),rec('pkg.b')])),['pkg.b']);
});
test('eligibility is not derived from icon or name',()=>{
  const cat=[{Id:'x',Name:'Looks Great',Icon:'/opt/share/icon.png'}];
  assert.deepEqual(filterCatalog(cat,[rec('x',{Name:'Looks Great',Installed:false})]),[]);
  assert.equal(filterCatalog([{Id:'y',Name:'',Icon:''}],[rec('y',{Name:''})]).length,1);
});
test('matches exact IDs only (case and prefix differ)',()=>{
  assert.deepEqual(filterCatalog([{Id:'App.One',Name:'a'}],[rec('app.one')]),[]);
  assert.deepEqual(filterCatalog([{Id:'app',Name:'a'}],[rec('app.one')]),[]);
});
test('virtual quiet.iptv survives without being in the snapshot, and is never taken from it',()=>{
  const cat=[...catalog,...VIRTUAL_APPS];
  assert.ok(visibleIds(filterCatalog(cat,inventory)).includes('quiet.iptv'));
  assert.deepEqual(visibleIds(filterCatalog(catalog,[...inventory,rec('quiet.iptv')],{appendNew:true})).filter(id=>id==='quiet.iptv'),[]);
});
test('appendNew adds only eligible unknown apps after retained ones, with a blank icon',()=>{
  const out=filterCatalog(catalog,[...inventory,rec('new.good',{Name:'New'}),rec('new.off',{Disabled:true})],{appendNew:true});
  assert.deepEqual(out.at(-1),{Id:'new.good',Name:'New',Icon:''});
  assert.ok(!out.some(a=>a.Id==='new.off'));
  assert.ok(!filterCatalog(catalog,[...inventory,rec('new.good')]).some(a=>a.Id==='new.good'));
});
test('malformed inventory fails closed instead of filtering loosely',()=>{
  for(const bad of [null,{},[],'x',[null],[rec('a'),rec('a')],[{...rec('a'),Installed:'true'}],[{...rec('a'),Disabled:undefined}],[{...rec('a'),Launchable:1}],[{...rec('a'),Id:' a'}],[{...rec('a'),Name:7}]])
    assert.throws(()=>filterCatalog(catalog,bad),Error,JSON.stringify(bad));
  assert.doesNotThrow(()=>validateInventory([],{allowEmpty:true}));
  assert.equal(filterCatalog(catalog,{records:inventory}).length,3);
});
test('malformed or duplicate catalogue throws',()=>{
  assert.throws(()=>filterCatalog([{Id:'a',Name:'a'},{Id:'a',Name:'b'}],inventory));
  assert.throws(()=>filterCatalog([{Name:'a'}],inventory));
  assert.throws(()=>filterCatalog(null,inventory));
});
test('extra unknown inventory fields are ignored',()=>{
  assert.equal(filterCatalog([{Id:'a',Name:'a'}],[{...rec('a'),Mystery:{x:1}}]).length,1);
});
test('pruneSavedIds drops hidden, unknown, non-string and duplicate IDs, keeping order',()=>{
  const vis=filterCatalog(catalog,inventory);
  assert.deepEqual(pruneSavedIds([HBO,'fake.removed',HBO,7,null,'org.tizen.netflix-app'],vis),[HBO,'org.tizen.netflix-app']);
  assert.deepEqual(pruneSavedIds(['a','b'],['b']),['b']);
  assert.deepEqual(pruneSavedIds(undefined,vis),[]);
  assert.deepEqual(pruneSavedIds(['a'],null),[]);
});
test('one clean pool feeds Apps/Search/Favorites/Arrange/recents',()=>{
  const pool=[...filterCatalog(catalog,inventory),...VIRTUAL_APPS];
  assert.deepEqual(leaks(normalizeOrder(['fake.removed',HBO],pool).map(Id=>({Id}))),[]);
  assert.deepEqual(recordRecent(['fake.tvplus',HBO],'fake.removed',pool),['fake.tvplus',HBO]);
  assert.deepEqual(pruneSavedIds(['fake.tvplus',HBO],pool),[HBO]);
});

// Shipped files against the public snapshot (build/ is untracked, so skip when absent).
import {readFileSync,existsSync} from 'node:fs';
const read=p=>JSON.parse(readFileSync(new URL(p,import.meta.url),'utf8'));
const snapshotUrl='../../build/catalog-inventory.public.json';
test('shipped catalogue files match the snapshot and the service allowlist keeps essential actions',{skip:!existsSync(new URL(snapshotUrl,import.meta.url))},()=>{
  const snap=read(snapshotUrl),shipped=read('../src/catalog.json'),native=read('../../app/apps.json'),allow=read('../../boot/widget/service/apps.json');
  const ok=new Set(snap.filter(r=>r.Installed&&!r.Disabled&&r.Launchable).map(r=>r.Id));
  assert.deepEqual(visibleIds(shipped).sort(),[...ok].sort());
  assert.deepEqual(visibleIds(native),visibleIds(shipped));
  assert.ok(shipped.every(a=>a.Icon===''),'public catalogue carries no TV icon paths');
  assert.ok(visibleIds(shipped).includes(HBO));
  for(const id of ['com.samsung.tv.quicksetting','com.samsung.tv.store','quiet.iptv',...ok])assert.ok(allow.includes(id),id);
  assert.equal(allow.length,new Set(allow).size);
  assert.deepEqual(allow.filter(id=>!ok.has(id)).sort(),['com.samsung.tv.quicksetting','com.samsung.tv.store','quiet.iptv']);
});
