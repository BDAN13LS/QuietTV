// Launcher catalogue policy: which installed apps may appear in the UI.
// Input is a sanitized, local inventory snapshot: [{Id,Name,Installed,Disabled,Launchable}].
// Eligibility is exact-ID and fail-closed: Installed===true && Disabled===false && Launchable===true.
// Icons, names and brand (e.g. Samsung) never decide eligibility.
export const VIRTUAL_APPS=Object.freeze([Object.freeze({Id:'quiet.iptv',Name:'IPTV',Icon:''})]);
const VIRTUAL_IDS=new Set(VIRTUAL_APPS.map(a=>a.Id));
const FLAGS=['Installed','Disabled','Launchable'];

export function validateInventory(inventory,{allowEmpty=false}={}){
  const records=Array.isArray(inventory)?inventory:inventory&&Array.isArray(inventory.records)?inventory.records:null;
  if(!records)throw new TypeError('inventory must be an array or {records:[...]}');
  if(!records.length&&!allowEmpty)throw new RangeError('inventory is empty; refusing to treat a failed snapshot as "no apps"');
  const seen=new Set();
  records.forEach((r,i)=>{
    if(!r||typeof r!=='object'||Array.isArray(r))throw new TypeError(`inventory[${i}] is not an object`);
    if(typeof r.Id!=='string'||!r.Id.trim()||r.Id!==r.Id.trim())throw new TypeError(`inventory[${i}].Id must be a trimmed non-empty string`);
    if(typeof r.Name!=='string')throw new TypeError(`inventory[${i}] (${r.Id}).Name must be a string`);
    for(const f of FLAGS)if(typeof r[f]!=='boolean')throw new TypeError(`inventory[${i}] (${r.Id}).${f} must be a boolean`);
    if(seen.has(r.Id))throw new RangeError(`duplicate inventory Id: ${r.Id}`);
    seen.add(r.Id);
  });
  return records;
}

export function validateCatalog(catalog){
  if(!Array.isArray(catalog))throw new TypeError('catalog must be an array');
  const seen=new Set();
  catalog.forEach((a,i)=>{
    if(!a||typeof a.Id!=='string'||!a.Id||typeof a.Name!=='string')throw new TypeError(`catalog[${i}] needs string Id and Name`);
    if(seen.has(a.Id))throw new RangeError(`duplicate catalog Id: ${a.Id}`);
    seen.add(a.Id);
  });
  return catalog;
}

// Reason an inventory record is hidden, or '' when it may be shown.
export function ineligibleReason(r){return !r.Installed?'not-installed':r.Disabled?'disabled':!r.Launchable?'not-launchable':'';}

// Detailed result: {apps, removed:[{Id,Name,reason}], appended:[...]}. Retained apps keep catalogue order.
export function analyzeCatalog(catalog,inventory,{appendNew=false}={}){
  validateCatalog(catalog);
  const byId=new Map(validateInventory(inventory).map(r=>[r.Id,r]));
  const apps=[],removed=[],appended=[];
  for(const a of catalog){
    if(VIRTUAL_IDS.has(a.Id)){apps.push(a);continue;}
    const rec=byId.get(a.Id);
    const reason=rec?ineligibleReason(rec):'not-in-snapshot';
    if(reason)removed.push({Id:a.Id,Name:a.Name,reason});else apps.push(a);
  }
  if(appendNew){
    const have=new Set(catalog.map(a=>a.Id));
    for(const r of byId.values())if(!have.has(r.Id)&&!VIRTUAL_IDS.has(r.Id)&&!ineligibleReason(r)){const a={Id:r.Id,Name:r.Name,Icon:''};apps.push(a);appended.push(a);}
  }
  return {apps,removed,appended};
}

export function filterCatalog(catalog,inventory,options){return analyzeCatalog(catalog,inventory,options).apps;}

// Drop saved IDs (order, favorites, recents) that are no longer visible. `visible` is apps or IDs.
export function pruneSavedIds(saved,visible){
  const ids=new Set((Array.isArray(visible)?visible:[]).map(v=>typeof v==='string'?v:v&&v.Id));
  return [...new Set(Array.isArray(saved)?saved:[])].filter(id=>typeof id==='string'&&ids.has(id));
}
