// Current weather for the Home panel, from Open-Meteo's free public APIs (no key). Pure apart from the injected
// fetch and timers, so the clock, network and storage are all test doubles. Rules the UI relies on:
// - nothing is fetched while weather is off, and at most one refresh starts per place per 30 minutes;
// - every request is aborted after LIMITS.requestMs, and a failure keeps the last good reading, never invents one;
// - a reading always carries the time it was fetched; old ones are labelled, very old ones are not shown as current.
// API facts verified 26 Oct 2026 against open-meteo.com/en/docs and /en/docs/geocoding-api (search accepts a postal
// code, countryCode filters; timeformat=unixtime gives epoch seconds and daily.time is local midnight). Free tier is
// non-commercial, 10,000 calls a day, CC BY 4.0: the panel shows the "Weather: Open-Meteo" credit.
export const WEATHER_LIMITS=Object.freeze({refreshMs:30*60e3,requestMs:8000,staleMs:45*60e3,expireMs:3*3600e3});
export const DEFAULT_WEATHER=Object.freeze({enabled:false,zip:'',unit:'F'});
// Unconfigured in the public source: no place is known until the owner enters a ZIP code, so the sunrise fallback uses fixed times. zip:null never equals a settings ZIP, so placeFor never returns this.
export const DEFAULT_PLACE=Object.freeze({zip:null,name:'',region:'',latitude:null,longitude:null});
export const validZip=zip=>typeof zip==='string'&&/^\d{5}$/.test(zip);
const finite=value=>typeof value==='number'&&isFinite(value);
const DAY_S=86400;

export function loadWeatherSettings(raw){
 const value=raw&&typeof raw==='object'?raw:{};
 return {enabled:typeof value.enabled==='boolean'?value.enabled:DEFAULT_WEATHER.enabled,zip:validZip(value.zip)?value.zip:DEFAULT_WEATHER.zip,unit:value.unit==='C'?'C':'F'};
}
export function loadWeatherCache(raw){
 const value=raw&&typeof raw==='object'?raw:{},place=value.place;
 const okPlace=place&&validZip(place.zip)&&typeof place.name==='string'&&finite(place.latitude)&&finite(place.longitude);
 const w=value.weather,okWeather=w&&validZip(w.zip)&&finite(w.fetchedAt)&&w.current&&finite(w.current.temperatureF)&&Array.isArray(w.days);
 const okAttempt=value.attempt&&validZip(value.attempt.zip)&&finite(value.attempt.at);
 return {place:okPlace?place:null,weather:okWeather?value.weather:null,attempt:okAttempt?value.attempt:null,error:value.error&&typeof value.error.message==='string'?value.error:null};
}
export function placeFor(zip,cache){return cache.place&&cache.place.zip===zip?cache.place:zip===DEFAULT_PLACE.zip?DEFAULT_PLACE:null;}

export function geocodeUrl(zip){return 'https://geocoding-api.open-meteo.com/v1/search?name='+encodeURIComponent(zip)+'&count=10&language=en&format=json&countryCode=US';}
// Only a result that lists this exact postal code counts: fuzzy name matching must never pick some other town.
export function pickPlace(json,zip){
 const hit=(json&&Array.isArray(json.results)?json.results:[]).find(r=>r&&r.country_code==='US'&&Array.isArray(r.postcodes)&&r.postcodes.indexOf(zip)>=0&&finite(r.latitude)&&finite(r.longitude)&&typeof r.name==='string');
 return hit?{zip,name:hit.name,region:typeof hit.admin1==='string'?hit.admin1:'',latitude:hit.latitude,longitude:hit.longitude}:null;
}
export function forecastUrl(place){
 return 'https://api.open-meteo.com/v1/forecast?latitude='+place.latitude+'&longitude='+place.longitude
  +'&current=temperature_2m,weather_code,is_day&daily=sunrise,sunset,temperature_2m_max,temperature_2m_min'
  +'&temperature_unit=fahrenheit&timezone=auto&timeformat=unixtime&forecast_days=7';
}
export function parseForecast(json){
 const c=json&&json.current,d=json&&json.daily,u=json&&json.current_units;
 if(!c||!finite(c.time)||!finite(c.temperature_2m)||!finite(c.weather_code))throw Error('Weather reply was incomplete');
 if(u&&u.temperature_2m&&u.temperature_2m!=='°F')throw Error('Weather reply was not in Fahrenheit');
 const days=[];
 if(d&&Array.isArray(d.time))d.time.forEach((time,i)=>{
  const day={start:time*1000,sunrise:d.sunrise&&d.sunrise[i]*1000,sunset:d.sunset&&d.sunset[i]*1000,high:d.temperature_2m_max&&d.temperature_2m_max[i],low:d.temperature_2m_min&&d.temperature_2m_min[i]};
  if(finite(time)&&finite(day.sunrise)&&finite(day.sunset))days.push(day);
 });
 return {current:{time:c.time*1000,temperatureF:c.temperature_2m,code:c.weather_code,isDay:c.is_day!==0},days};
}

// A clock that moved backwards (attempt "in the future") must not block refreshes until it catches up.
export function dueForFetch({enabled,zip,cache,now}){
 if(!enabled||!validZip(zip))return false;
 const last=cache.attempt;
 return !last||last.zip!==zip||now<last.at||now-last.at>=WEATHER_LIMITS.refreshMs;
}
export function beginAttempt(cache,zip,now){return {...cache,attempt:{zip,at:now}};}

async function getJson(url,{fetchFn,setTimer,clearTimer}){
 const controller=new AbortController(),timer=setTimer(()=>controller.abort(),WEATHER_LIMITS.requestMs);
 try{
  const response=await fetchFn(url,{signal:controller.signal,cache:'no-store'});
  if(!response.ok)throw Error('Weather service answered '+response.status);
  return await response.json();
 }catch(error){throw controller.signal.aborted?Error('Weather service timed out'):error;}
 finally{clearTimer(timer);}
}
// Never throws. Returns the next cache: a new place and reading on success, the previous ones plus an error otherwise.
export async function refreshWeather(cache,zip,deps){
 const now=deps.now;
 try{
  let place=cache.place&&cache.place.zip===zip?cache.place:null;
  if(!place){place=pickPlace(await getJson(geocodeUrl(zip),deps),zip);if(!place)return {...cache,error:{zip,at:now(),message:'ZIP '+zip+' was not found'}};}
  const reading=parseForecast(await getJson(forecastUrl(place),deps));
  return {...cache,place,weather:{zip,fetchedAt:now(),...reading},error:null};
 }catch(error){
  const message=error&&error.message&&!/fetch|network|load failed/i.test(error.message)?error.message:'No connection';
  return {...cache,error:{zip,at:now(),message}};
 }
}

const CONDITIONS=[[[0],'Clear','clear'],[[1],'Mainly clear','clear'],[[2],'Partly cloudy','partly'],[[3],'Overcast','cloud'],[[45,48],'Fog','fog'],
 [[51,53,55],'Drizzle','drizzle'],[[56,57],'Freezing drizzle','drizzle'],[[61],'Light rain','rain'],[[63],'Rain','rain'],[[65],'Heavy rain','rain'],
 [[66,67],'Freezing rain','rain'],[[71],'Light snow','snow'],[[73],'Snow','snow'],[[75],'Heavy snow','snow'],[[77],'Snow grains','snow'],
 [[80,81],'Rain showers','rain'],[[82],'Heavy showers','rain'],[[85,86],'Snow showers','snow'],[[95,97],'Thunderstorm','storm'],[[96,99],'Thunderstorm, hail','storm']];
// WMO weather interpretation codes, as listed in the Open-Meteo forecast docs.
export function conditionFor(code,isDay=true){
 const hit=CONDITIONS.find(([codes])=>codes.indexOf(code)>=0);
 if(!hit)return {label:'Current conditions',icon:'cloud'};
 const icon=!isDay&&(hit[2]==='clear'||hit[2]==='partly')?hit[2]+'-night':hit[2];
 return {label:hit[1],icon};
}
export const toUnit=(fahrenheit,unit)=>Math.round(unit==='C'?(fahrenheit-32)*5/9:fahrenheit);
export function clockText(ms){return new Date(ms).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true}).toLowerCase();}
// A day runs to the next day's local midnight, so the 23- and 25-hour clock-change days resolve correctly.
export function dayFor(days,now){
 const list=days||[];
 for(let i=0;i<list.length;i++){const end=i+1<list.length?list[i+1].start:list[i].start+DAY_S*1000;if(now>=list[i].start&&now<end)return list[i];}
 return null;
}
export function placeLabel(place){return place?place.name+(place.region?', '+place.region:''):'';}

// Everything the panel shows, as text. status: off | loading | fresh | stale | unavailable
export function weatherView({enabled,zip,unit,cache,now}){
 if(!enabled)return {status:'off'};
 const place=cache.place&&cache.place.zip===zip?cache.place:null,reading=cache.weather&&cache.weather.zip===zip?cache.weather:null;
 const error=cache.error&&cache.error.zip===zip?cache.error:null,where=place?placeLabel(place):'ZIP '+zip;
 if(!reading)return error?{status:'unavailable',place:where,condition:'Weather unavailable',detail:error.message}:{status:'loading',place:where,condition:'Checking weather…',detail:''};
 const age=now-reading.fetchedAt,at=clockText(reading.fetchedAt);
 if(age>WEATHER_LIMITS.expireMs||age<-WEATHER_LIMITS.staleMs)return {status:'unavailable',place:where,condition:'Weather unavailable',detail:'Last update '+at+(error?' · '+error.message:'')};
 const today=dayFor(reading.days,now),condition=conditionFor(reading.current.code,reading.current.isDay),mark=unit==='C'?'°C':'°F';
 const failedSince=error&&error.at>reading.fetchedAt;
 return {status:age>WEATHER_LIMITS.staleMs?'stale':'fresh',place:where,temp:toUnit(reading.current.temperatureF,unit)+mark,condition:condition.label,icon:condition.icon,
  hiLo:today&&finite(today.high)&&finite(today.low)?'H '+toUnit(today.high,unit)+'°  L '+toUnit(today.low,unit)+'°':'',
  detail:(age>WEATHER_LIMITS.staleMs?'As of ':'Updated ')+at+(failedSince?' · '+error.message:'')};
}
// Today's sunrise and sunset from the cached reading for this ZIP, if it covers today.
export function weatherSun(cache,zip,now){
 const reading=cache.weather&&cache.weather.zip===zip?cache.weather:null,day=reading&&dayFor(reading.days,now);
 return day?{sunrise:day.sunrise,sunset:day.sunset}:null;
}
