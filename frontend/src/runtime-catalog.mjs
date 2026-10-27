// Which app pool the launcher shows: the compiled catalogue (legacy) or an owner-generated runtime list.
// Contract: window.QuietTVApps = [{Id,Name,Icon?}] and window.QuietTVConfig = {iptvEnabled?,preview?,schema?}.
// Owner mode starts when QuietTVApps exists, or QuietTVConfig has preview===true or schema==='quiettv-widget/1'.
// In owner mode the list is validated whole and any fault fails closed: no apps and a setup message, never the compiled catalogue.
// IPTV is a virtual app: owner mode shows it only when QuietTVConfig.iptvEnabled===true.
import {VIRTUAL_APPS} from './catalog-policy.mjs';
import {launcherApps} from './launcher-apps.mjs';

export const OWNER_SCHEMA='quiettv-widget/1';
const ID=/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/,ICON=/^[A-Za-z0-9._\-/]+$/;
const virtualIds=new Set(VIRTUAL_APPS.map(a=>a.Id));

// Icons are local relative paths or empty: no scheme, no leading slash, no backslash, no '.' or '..' segment.
export function safeIcon(icon){return icon===''||(typeof icon==='string'&&icon.length<=256&&ICON.test(icon)&&!icon.startsWith('/')&&!icon.split('/').some(s=>s===''||s==='.'||s==='..'));}

// Returns {apps} with fresh copies, or {reason}. The caller's array is never changed.
export function validateRuntimeApps(list){
 if(!Array.isArray(list))return {reason:'the app list is not an array'};
 const seen=new Set(),apps=[];
 for(let i=0;i<list.length;i++){
  const a=list[i];
  if(!a||typeof a!=='object'||Array.isArray(a))return {reason:'app '+(i+1)+' is not an object'};
  if(typeof a.Id!=='string'||!ID.test(a.Id))return {reason:'app '+(i+1)+' has an unsafe or missing Id'};
  if(seen.has(a.Id))return {reason:'app '+a.Id+' is listed twice'};
  seen.add(a.Id);
  if(typeof a.Name!=='string'||a.Name.length>100)return {reason:'app '+a.Id+' needs a text Name'};
  const icon=a.Icon===undefined?'':a.Icon;
  if(!safeIcon(icon))return {reason:'app '+a.Id+' has an unsafe Icon'};
  apps.push({Id:a.Id,Name:a.Name,Icon:icon});
 }
 return {apps};
}

const isObject=v=>!!v&&typeof v==='object'&&!Array.isArray(v);
export const setupError=reason=>'Setup problem: '+reason+'. Open the widget setup and save it again.';

// Pure: pass window globals in. -> {apps, error, ownerMode}
export function resolveRuntimeApps({compiled,apps,config}={}){
 const cfg=isObject(config)?config:{};
 const present=apps!==undefined,marked=cfg.preview===true||cfg.schema===OWNER_SCHEMA;
 if(!present&&!marked)return {apps:launcherApps(Array.isArray(compiled)?compiled:[]),error:'',ownerMode:false};
 if(!present)return {apps:[],error:setupError('the widget did not provide its app list'),ownerMode:true};
 const checked=validateRuntimeApps(apps);
 if(!checked.apps)return {apps:[],error:setupError(checked.reason),ownerMode:true};
 const pool=checked.apps.filter(a=>!virtualIds.has(a.Id));
 if(cfg.iptvEnabled===true)for(const v of VIRTUAL_APPS)pool.push({...v});
 return {apps:pool,error:'',ownerMode:true};
}
