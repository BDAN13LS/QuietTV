// Wallpaper choice and the day / dusk / night phase behind Auto. Pure: callers pass the clock and any sun times.
// Sun times come from, in order: today's sunrise and sunset in the cached weather reading, a NOAA solar estimate
// for the saved place (weather off or not fetched yet), then fixed 7:00 am / 6:30 pm if no place is known.
export const WALLPAPERS=Object.freeze(['Auto','Day','Dusk','Night']);
export const COLORS=Object.freeze([{name:'Onyx',hex:'#080a0c'},{name:'Smoke',hex:'#24282c'},{name:'Midnight',hex:'#0e1726'},{name:'Pine',hex:'#10201a'},{name:'Plum',hex:'#1f1524'},{name:'Clay',hex:'#2a1e18'}]);
// The choice lives in the existing quiet-appearance-v2 key because the TV host persists only listed keys, and stays
// a plain string so an older build can still read it. Older builds saved "Dusk" as their default before Auto
// existed, so "Dusk" means Auto; a Dusk picked here is stored as "Dusk-fixed".
export const APPEARANCE_KEY='quiet-appearance-v2';
const MINUTE=60e3,choices=new Set([...WALLPAPERS,...COLORS.map(c=>c.name)]);
export function loadAppearance(raw){if(raw==='Dusk-fixed')return 'Dusk';if(raw==='Dusk')return 'Auto';return typeof raw==='string'&&choices.has(raw)?raw:'Auto';}
export function storeAppearance(choice){return choice==='Dusk'?'Dusk-fixed':choices.has(choice)?choice:'Auto';}
// Twilight windows around sunrise and sunset use the dusk picture; night darkens it further.
export const TWILIGHT=Object.freeze({beforeSunrise:30*MINUTE,afterSunrise:30*MINUTE,beforeSunset:45*MINUTE,afterSunset:40*MINUTE});

// NOAA general solar position equations (sunrise/sunset at the 90.833 degree zenith). Accurate to a few minutes
// at mid latitudes; returns null where the sun does not rise or set that day.
export function solarTimes(now,latitude,longitude){
 const d=new Date(now),midnight=Date.UTC(d.getFullYear(),d.getMonth(),d.getDate());
 const day=Math.round((midnight-Date.UTC(d.getFullYear(),0,1))/864e5),rad=Math.PI/180,g=2*Math.PI/365*day;
 const eq=229.18*(.000075+.001868*Math.cos(g)-.032077*Math.sin(g)-.014615*Math.cos(2*g)-.040849*Math.sin(2*g));
 const decl=.006918-.399912*Math.cos(g)+.070257*Math.sin(g)-.006758*Math.cos(2*g)+.000907*Math.sin(2*g)-.002697*Math.cos(3*g)+.00148*Math.sin(3*g);
 const lat=latitude*rad,cos=Math.cos(90.833*rad)/(Math.cos(lat)*Math.cos(decl))-Math.tan(lat)*Math.tan(decl);
 if(cos<-1||cos>1)return null;
 const ha=Math.acos(cos)/rad;
 return {sunrise:midnight+(720-4*(longitude+ha)-eq)*MINUTE,sunset:midnight+(720-4*(longitude-ha)-eq)*MINUTE};
}
export function fixedSun(now){const d=new Date(now),at=(h,m)=>new Date(d.getFullYear(),d.getMonth(),d.getDate(),h,m).getTime();return {sunrise:at(7,0),sunset:at(18,30)};}
export function sunFor(now,weatherSun,place){
 if(weatherSun)return {...weatherSun,source:'weather'};
 const solar=place&&solarTimes(now,place.latitude,place.longitude);
 return solar?{...solar,source:'solar'}:{...fixedSun(now),source:'fixed'};
}
export function phaseAt(now,sun){
 if(now<sun.sunrise-TWILIGHT.beforeSunrise)return 'night';
 if(now<sun.sunrise+TWILIGHT.afterSunrise)return 'dusk';
 if(now<sun.sunset-TWILIGHT.beforeSunset)return 'day';
 if(now<sun.sunset+TWILIGHT.afterSunset)return 'dusk';
 return 'night';
}
// What the screen draws: a picture phase (day | dusk | night) or a plain colour.
export function resolveAppearance(choice,now,sun){
 const color=COLORS.find(c=>c.name===choice);
 if(color)return {kind:'color',name:color.name,hex:color.hex,art:null};
 const phase=choice==='Auto'?phaseAt(now,sun):choice.toLowerCase();
 return {kind:phase,name:phase,art:phase};
}
export function msToNextMinute(now){return MINUTE-now%MINUTE+50;}
