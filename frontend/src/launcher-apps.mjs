// The one app pool every view uses, and the saved lists cleaned against it at start-up.
import {VIRTUAL_APPS,pruneSavedIds} from './catalog-policy.mjs';
import {normalizeOrder} from './policy.mjs';

export function launcherApps(catalog){const virtual=new Set(VIRTUAL_APPS.map(a=>a.Id));return [...catalog.filter(a=>!virtual.has(a.Id)),...VIRTUAL_APPS];}
// Saved order, favorites and recents lose any ID that is no longer in the pool; the caller writes them back.
// Missing or malformed favorites fall back to the defaults; an empty saved list stays empty.
export function initialSaved(apps,{order,favorites,recent},defaultFavorites){
 return {order:normalizeOrder(pruneSavedIds(order,apps),apps),favorites:pruneSavedIds(Array.isArray(favorites)?favorites:defaultFavorites,apps),recent:pruneSavedIds(recent,apps).slice(0,8)};
}
// Apps uninstalled since the catalogue was built. Asks tizen.application.getAppInfo for the known IDs only.
// Only a definite NotFoundError hides an app; no API, any other error, or "every app missing" (more likely a
// broken API than an empty TV) hides nothing.
export function uninstalledIds(apps,getAppInfo){
 if(typeof getAppInfo!=='function')return [];
 const virtual=new Set(VIRTUAL_APPS.map(a=>a.Id)),known=apps.filter(a=>!virtual.has(a.Id)),missing=[];
 for(const app of known){try{getAppInfo(app.Id);}catch(error){if(error&&error.name==='NotFoundError')missing.push(app.Id);}}
 return missing.length&&missing.length<known.length?missing:[];
}
