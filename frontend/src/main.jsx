import React,{useEffect,useRef,useState,createContext,useContext} from 'react';
import {createRoot} from 'react-dom/client';
import {init,useFocusable,FocusContext,setFocus,getCurrentFocusKey} from '@noriginmedia/norigin-spatial-navigation';
import {animate} from 'motion/mini';
import {Grid2X2,SlidersHorizontal,Plug,ShoppingBag,ArrowLeft,House,Star,Check,Wallpaper,Move,ChevronLeft,ChevronRight,Search,Settings,UserRound,Tv,Delete,Sun,Moon,CloudSun,CloudMoon,Cloud,CloudFog,CloudDrizzle,CloudRain,CloudSnow,CloudLightning,CloudOff,MapPin,Thermometer} from 'lucide-react';
import catalog from './catalog.json';
import netflix from './assets/brand-netflix.svg';import youtube from './assets/brand-youtube.svg';import apple from './assets/brand-appletv.svg';import hbo from './assets/brand-hbomax.svg';import hulu from './assets/brand-hulu.svg';import paramount from './assets/brand-paramountplus.svg';import youtubeTv from './assets/brand-youtubetv.svg';import browserIcon from './assets/browser.svg';
import iptv from './assets/iptv.svg';import dusk from './assets/cinematic-dusk.png';import day from './assets/cinematic-day.png';import night from './assets/cinematic-night.png';
import {allowedAction,normalizeOrder,homeApps,homeFocus,recordRecent,searchApps,recentApps,SYSTEM_APPS} from './policy.mjs';
import {startBootHandoff} from './boot-ready.mjs';
import {initialSaved,uninstalledIds} from './launcher-apps.mjs';
import {resolveRuntimeApps} from './runtime-catalog.mjs';
import {WALLPAPERS,COLORS,APPEARANCE_KEY,loadAppearance,storeAppearance,sunFor,resolveAppearance,msToNextMinute} from './appearance.mjs';
import {validZip,loadWeatherSettings,loadWeatherCache,placeFor,placeLabel,dueForFetch,beginAttempt,refreshWeather,weatherView,weatherSun} from './weather.mjs';
import './style.css';
init({debug:false,visualDebug:false,throttle:95,throttleKeypresses:false});
const NETFLIX='org.tizen.netflix-app',YOUTUBE='9Ur5IzDKqV.TizenYouTube',APPLE='com.samsung.tv.aria-video',HBO='5b8c3eb16b.BeamCTVDev';
const iconMap={[NETFLIX]:netflix,[YOUTUBE]:youtube,[APPLE]:apple,[HBO]:hbo,'quiet.iptv':iptv,'LBUAQX1exg.Hulu':hulu,'rJyOSqC6Up.CBSAllAccess':paramount,'PvWgqxV3Xa.YouTubeTV':youtubeTv,'org.tizen.browser':browserIcon};
const extraBrandKinds={'LBUAQX1exg.Hulu':'brand-hulu','rJyOSqC6Up.CBSAllAccess':'brand-paramount','PvWgqxV3Xa.YouTubeTV':'brand-youtube-tv','org.tizen.browser':'browser'};
const wallArt={day,dusk,night},weatherIcons={clear:Sun,'clear-night':Moon,partly:CloudSun,'partly-night':CloudMoon,cloud:Cloud,fog:CloudFog,drizzle:CloudDrizzle,rain:CloudRain,snow:CloudSnow,storm:CloudLightning};
const wallDetail={Day:'Bright daylight',Dusk:'Golden hour',Night:'Dimmed for evening'},capital=word=>word.charAt(0).toUpperCase()+word.slice(1);
// Weather never gates the first paint or the boot hand-off: it starts after the hand-off or this delay, whichever is first.
const WEATHER_START_MS=6000,weatherDeps={fetchFn:(url,options)=>fetch(url,options),now:()=>Date.now(),setTimer:(fn,ms)=>setTimeout(fn,ms),clearTimer:id=>clearTimeout(id)};
// An owner-generated list (window.QuietTVApps) replaces the compiled catalogue; a bad one gives an empty pool and a setup message.
const runtime=resolveRuntimeApps({compiled:catalog,apps:window.QuietTVApps,config:window.QuietTVConfig});
const allApps=runtime.apps;
const defaultFavorites=[NETFLIX,YOUTUBE,APPLE,'org.tizen.primevideo','EYm8vc1St4.Philo',HBO];
const liveIds=new Set(['quiet.iptv','EYm8vc1St4.Philo','PvWgqxV3Xa.YouTubeTV','ZmmGjO6VKO.slingtv','XtU8fQk2gN.ESPN']);
const movieIds=new Set([NETFLIX,APPLE,'org.tizen.primevideo','MCmYXNxgcu.DisneyPlus','LBUAQX1exg.Hulu','gzcc4LRFBF.Peacock','3KA0pm7a7V.TubiTV','rJyOSqC6Up.CBSAllAccess','Ob3XhAVMu6.amcplus',HBO]);
const Context=createContext(null),reduced=()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function saved(key,fallback){try{return JSON.parse(localStorage.getItem(key))??fallback;}catch{return fallback;}}
const artFor=id=>iconMap[id]||'native-icons/'+encodeURIComponent(id)+'.png';
function FocusButton({id,onPress,className='',children,label,onFocusItem}){
 const context=useContext(Context);
 const {ref,focusSelf}=useFocusable({focusKey:id,onEnterPress:onPress,onFocus:()=>{const current=getCurrentFocusKey();context?.setVisualFocus(current);if(current===id)onFocusItem?.(id);}});
 const focused=context?.visualFocus===id;
 useEffect(()=>{if(!ref.current||!context?.motion||reduced())return;const animation=animate(ref.current,{transform:focused?'translateY(-3px) scale(1.025)':'translateY(0px) scale(1)'},{duration:.16,ease:'easeOut'});return()=>animation.stop();},[focused,context?.motion]);
 return <button ref={ref} tabIndex={-1} data-focus-key={id} aria-label={label} className={className+' '+(focused?'focused':'')} onMouseEnter={focusSelf} onFocus={focusSelf} onKeyDown={e=>{if(e.key==='Enter'||e.keyCode===13)e.preventDefault();}} onClick={onPress}>{children}</button>;
}
function Tile({app,prefix='app',onPress}){
 const [failed,setFailed]=useState(false);
 const kind=app.Id===NETFLIX?'netflix':app.Id===YOUTUBE?'youtube':app.Id===APPLE?'apple':app.Id==='org.tizen.primevideo'?'prime':app.Id===HBO?'hbo':app.Id==='quiet.iptv'?'iptv':extraBrandKinds[app.Id]||(!iconMap[app.Id]?'native-art':'');
 return <FocusButton id={prefix+'-'+app.Id} className={'tile '+kind} label={app.Name} onPress={()=>onPress(app)} onFocusItem={id=>{window.QuietTVLastAppFocus=id;}}>
  <span className="tile-face">{!failed?<img src={artFor(app.Id)} alt="" draggable="false" onError={()=>setFailed(true)}/>:<Grid2X2 size={56} strokeWidth={1.25}/>}</span><span className="tile-label">{app.Name}</span>
 </FocusButton>;
}
function Shortcut({id,Icon,title,detail,onPress}){return <FocusButton id={id} className="shortcut" onPress={onPress} label={title}><Icon size={40} strokeWidth={1.4}/><span><strong>{title}</strong><small>{detail}</small></span><ChevronRight className="shortcut-arrow" size={22}/></FocusButton>;}
function CloseDialog({onCancel,onClose}){
 const {ref,focusKey}=useFocusable({focusKey:'CLOSE_DIALOG',trackChildren:true,isFocusBoundary:true,autoRestoreFocus:false,focusBoundaryDirections:['left','right','up','down']});
 useEffect(()=>{setFocus('dialog-cancel');},[]);
 return <FocusContext.Provider value={focusKey}><div className="modal-shade"><div ref={ref} role="dialog" aria-modal="true" aria-labelledby="exit-title" className="dialog"><h2 id="exit-title">Return to TV?</h2><p>Press Home whenever you want to return.</p><div><FocusButton id="dialog-cancel" onPress={onCancel}>Keep watching</FocusButton><FocusButton id="dialog-close" onPress={onClose}>Return to TV</FocusButton></div></div></div></FocusContext.Provider>;
}
function WeatherPanel({view}){
 const Icon=view.status==='unavailable'?CloudOff:weatherIcons[view.icon]||Cloud;
 return <aside className={'weather-panel weather-'+view.status} aria-label="Weather"><div className="weather-now"><Icon/>{view.temp?<span className="weather-temp">{view.temp}</span>:<span className="weather-condition-only">{view.condition}</span>}</div>
  {view.temp&&<p className="weather-condition">{view.condition}</p>}<p className="weather-place"><MapPin size={18}/>{view.place}{view.hiLo&&<span>{view.hiLo}</span>}</p>
  <p className="weather-status"><span>{view.detail}</span><span className="weather-credit">Weather: Open-Meteo</span></p></aside>;
}
// ZIP entry for the remote: on-screen digits (or the remote's own number keys), Delete, Clear, Cancel and Save.
function ZipDialog({zip,place,onCancel,onSave}){
 const {ref,focusKey}=useFocusable({focusKey:'ZIP_DIALOG',trackChildren:true,isFocusBoundary:true,autoRestoreFocus:false,focusBoundaryDirections:['left','right','up','down']});
 const [digits,setDigits]=useState(''),[hint,setHint]=useState('');
 const add=digit=>{setHint('');setDigits(value=>value.length<5?value+digit:value);};
 useEffect(()=>{setFocus('zip-1');function key(e){const code=e.keyCode>=96&&e.keyCode<=105?e.keyCode-48:e.keyCode;if(code>=48&&code<=57){e.preventDefault();add(String(code-48));}}document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key);},[]);
 const ready=validZip(digits),save=()=>ready?onSave(digits):setHint('Enter all five digits first.');
 return <FocusContext.Provider value={focusKey}><div className="modal-shade"><div ref={ref} role="dialog" aria-modal="true" aria-labelledby="zip-title" className="dialog zip-dialog"><h2 id="zip-title">Weather ZIP code</h2><p>Now showing {zip}{place?' · '+placeLabel(place):''}</p>
  <div className="zip-slots" aria-label={'New ZIP code '+digits}>{[0,1,2,3,4].map(i=><span key={i} className={i===digits.length?'next':''}>{digits.charAt(i)}</span>)}</div>
  <div className="zip-keys">{'123456789'.split('').map(d=><FocusButton key={d} id={'zip-'+d} onPress={()=>add(d)}>{d}</FocusButton>)}<FocusButton id="zip-delete" label="Delete digit" onPress={()=>setDigits(value=>value.slice(0,-1))}><Delete size={24}/></FocusButton><FocusButton id="zip-0" onPress={()=>add('0')}>0</FocusButton><FocusButton id="zip-clear" onPress={()=>{setHint('');setDigits('');}}>Clear</FocusButton></div>
  <p className="zip-hint" role="status">{hint||'Back cancels. US ZIP codes only.'}</p>
  <div className="zip-actions"><FocusButton id="zip-cancel" onPress={onCancel}>Cancel</FocusButton><FocusButton id="zip-save" className={ready?'':'waiting'} onPress={save}>Save</FocusButton></div></div></div></FocusContext.Provider>;
}
function Choice({id,name,detail,selected,swatch,className='',onPress}){return <FocusButton id={id} className={'choice '+className+(selected?' selected':'')} label={name+(selected?', selected':'')} onPress={onPress}><span className="choice-swatch" style={swatch}/><span className="choice-text"><strong>{name}</strong>{detail&&<small>{detail}</small>}</span>{selected&&<Check className="choice-check" size={24}/>}</FocusButton>;}
function App(){
 const [visualFocus,setVisualFocus]=useState(null),[page,setPage]=useState('Home'),[time,setTime]=useState(new Date());
 // Saved lists are cleaned against the current catalogue once, and the effects below write the clean lists back.
 const [start]=useState(()=>initialSaved(allApps,{order:saved('quiet-order',[]),favorites:saved('quiet-favorites',null),recent:saved('quiet-recent',[])},defaultFavorites));
 const [order,setOrder]=useState(start.order);
 const [favorites,setFavorites]=useState(start.favorites);
 const [appearance,setAppearance]=useState(()=>loadAppearance(saved(APPEARANCE_KEY,null))),[motion,setMotion]=useState(()=>saved('quiet-motion',true));
 const [weatherSettings,setWeatherSettings]=useState(()=>loadWeatherSettings(saved('quiet-weather',null))),[weatherCache,setWeatherCache]=useState(()=>loadWeatherCache(saved('quiet-weather-cache',null)));
 const [weatherArmed,setWeatherArmed]=useState(false),[zipOpen,setZipOpen]=useState(false);const weatherRef=useRef(null),weatherBusy=useRef(false),preloaded=useRef({});
 const [message,setMessage]=useState(''),[dialog,setDialog]=useState(false),[pendingSwap,setPendingSwap]=useState(null),[index,setIndex]=useState(0),[query,setQuery]=useState('');
 const [inputs,setInputs]=useState([]),[inputsError,setInputsError]=useState('');
 const [recent,setRecent]=useState(start.recent),[uninstalled,setUninstalled]=useState([]);const content=useRef(null);
 const {ref,focusKey}=useFocusable({focusKey:'ROOT',trackChildren:true,saveLastFocusedChild:true,autoRestoreFocus:false});
 const apps=order.map(id=>allApps.find(a=>a.Id===id)).filter(a=>a&&uninstalled.indexOf(a.Id)<0);
 const recentlyOpened=recentApps(recent,apps,5);
 const picked=apps.filter(a=>defaultFavorites.includes(a.Id)||favorites.includes(a.Id)||a.Id==='quiet.iptv'),homePool=picked.some(a=>a.Id!=='quiet.iptv')?picked:apps;
 const navHome=list=>list.length?null:'nav-Home',homeKey=(last,list)=>navHome(list)||homeFocus(last,list);
 const first=homeApps(homePool,favorites,apps.some(a=>a.Id===HBO)?7:6);
 const now=time.getTime(),place=placeFor(weatherSettings.zip,weatherCache),sunAt=at=>sunFor(at,weatherSun(weatherCache,weatherSettings.zip,at),place);
 const look=resolveAppearance(appearance,now,sunAt(now)),weather=weatherView({...weatherSettings,cache:weatherCache,now});weatherRef.current={settings:weatherSettings,cache:weatherCache};
 const autoPhase=resolveAppearance('Auto',now,sunAt(now)).name,choiceFocus=WALLPAPERS.includes(appearance)?'wall-'+appearance:'color-'+appearance;
 const shown=page==='Movies'?apps.filter(a=>movieIds.has(a.Id)):page==='Live TV'?apps.filter(a=>liveIds.has(a.Id)):page==='Search'?searchApps(apps,query):apps;
 function open(next,restore=false){setDialog(false);setPage(next);setIndex(0);setPendingSwap(null);setMessage('');requestAnimationFrame(()=>setFocus(next==='Settings'?'setting-appearance':next==='Appearance'?choiceFocus:next==='Home'?homeKey(restore?window.QuietTVLastAppFocus:null,first):next==='Inputs'?'nav-Home':next==='Search'?'key-A':(l=>l.length?'grid-'+l[0].Id:'nav-Home')(next==='Movies'?apps.filter(a=>movieIds.has(a.Id)):next==='Live TV'?apps.filter(a=>liveIds.has(a.Id)):apps)));}
 function send(action){if(!allowedAction(action,allApps))return;window.QuietTVOutbox??=[];window.QuietTVOutbox.push(action);setMessage(action.type==='launch'?'Opening app…':'');}
 function select(app){
  if(page==='Favorites'){setFavorites(old=>old.includes(app.Id)?old.filter(id=>id!==app.Id):[...old,app.Id]);return;}
  if(page==='Arrange'){if(!pendingSwap){setPendingSwap(app.Id);setMessage('Choose the second app.');}else{setOrder(old=>{const next=[...old],a=next.indexOf(pendingSwap),b=next.indexOf(app.Id);[next[a],next[b]]=[next[b],next[a]];return next;});setPendingSwap(null);setMessage('App order saved.');}return;}
  setRecent(old=>recordRecent(old,app.Id,allApps));send({type:'launch',id:app.Id});
 }
 function cancelDialog(){setDialog(false);requestAnimationFrame(()=>setFocus(page==='Settings'?'setting-close':homeKey(window.QuietTVLastAppFocus,first)));}
 useEffect(()=>{if(page!=='Inputs')return;let canceled=false;setInputs([]);setInputsError('');const request=window.QuietTVGetInputs?window.QuietTVGetInputs():Promise.reject(Error('Input controls are available on the TV.'));request.then(list=>{if(canceled)return;setInputs(list);requestAnimationFrame(()=>{if(list.length)setFocus('source-'+list[0].type+'-'+list[0].number);});}).catch(error=>{if(!canceled)setInputsError(error.message);});return()=>{canceled=true;};},[page]);
 useEffect(()=>{window.QuietTVOutbox=[];setFocus(first.length?'home-'+first[0].Id:'nav-Home');const arm=setTimeout(()=>setWeatherArmed(true),WEATHER_START_MS);startBootHandoff({nativeEnabled:window.QuietTVConfig?.preview!==true,art:[...(look.art?[wallArt[look.art]]:[]),...first.map(app=>artFor(app.Id))],focused:()=>!!getCurrentFocusKey(),reduced:reduced()||!motion}).then(result=>{window.QuietTVBoot=result;setWeatherArmed(true);});
  // The clock and Auto wallpaper move on each minute boundary, and at once when the TV wakes or the page is shown again.
  let timer=null;const tick=()=>{setTime(new Date());timer=setTimeout(tick,msToNextMinute(Date.now()));};timer=setTimeout(tick,msToNextMinute(Date.now()));
  const wake=()=>{if(!document.hidden)setTime(new Date());};document.addEventListener('visibilitychange',wake);window.addEventListener('pageshow',wake);window.addEventListener('focus',wake);
  return()=>{clearTimeout(arm);clearTimeout(timer);document.removeEventListener('visibilitychange',wake);window.removeEventListener('pageshow',wake);window.removeEventListener('focus',wake);};},[]);
 // Apps removed since the catalogue was built drop out of every view: checked after start-up and whenever the page is shown.
 useEffect(()=>{if(!weatherArmed)return;const check=()=>{if(document.hidden)return;let getAppInfo=null;try{const api=window.tizen&&window.tizen.application;if(api&&api.getAppInfo)getAppInfo=id=>api.getAppInfo(id);}catch{}const next=uninstalledIds(allApps,getAppInfo);setUninstalled(old=>old.join()===next.join()?old:next);};check();document.addEventListener('visibilitychange',check);return()=>document.removeEventListener('visibilitychange',check);},[weatherArmed]);
 // At most one refresh in flight; dueForFetch enforces Off and the 30-minute spacing. Re-checked on every minute
 // tick, on wake, on a settings change and when a refresh lands (a ZIP changed mid-flight is then fetched).
 useEffect(()=>{
  if(!weatherArmed||weatherBusy.current)return;const {settings,cache}=weatherRef.current,at=Date.now();
  if(!dueForFetch({enabled:settings.enabled,zip:settings.zip,cache,now:at}))return;
  weatherBusy.current=true;const started=beginAttempt(cache,settings.zip,at);setWeatherCache(started);
  refreshWeather(started,settings.zip,weatherDeps).then(next=>{weatherBusy.current=false;setWeatherCache(next);},()=>{weatherBusy.current=false;});
 },[weatherArmed,time,weatherSettings,weatherCache]);
 // Load the other picture a few minutes before Auto switches to it, so the swap does not flash the base colour.
 useEffect(()=>{const ahead=resolveAppearance(appearance,now+5*60e3,sunAt(now+5*60e3));if(ahead.art&&ahead.art!==look.art&&!preloaded.current[ahead.art]){preloaded.current[ahead.art]=new Image();preloaded.current[ahead.art].src=wallArt[ahead.art];}},[time,appearance]);
 useEffect(()=>{window.QuietTVResetHome=()=>open('Home',true);window.QuietTVSetMessage=text=>setMessage(String(text));});
 useEffect(()=>{localStorage.setItem('quiet-order',JSON.stringify(order));},[order]);useEffect(()=>{localStorage.setItem('quiet-favorites',JSON.stringify(favorites));},[favorites]);
 useEffect(()=>{localStorage.setItem(APPEARANCE_KEY,JSON.stringify(storeAppearance(appearance)));},[appearance]);
 useEffect(()=>{localStorage.setItem('quiet-weather',JSON.stringify(weatherSettings));},[weatherSettings]);useEffect(()=>{localStorage.setItem('quiet-weather-cache',JSON.stringify(weatherCache));},[weatherCache]);useEffect(()=>{localStorage.setItem('quiet-motion',JSON.stringify(motion));},[motion]);useEffect(()=>{localStorage.setItem('quiet-recent',JSON.stringify(recent));},[recent]);
 function closeZip(){setZipOpen(false);requestAnimationFrame(()=>setFocus('weather-zip'));}
 useEffect(()=>{function key(e){if(![461,10009,27,8].includes(e.keyCode))return;e.preventDefault();if(zipOpen)closeZip();else if(dialog)cancelDialog();else if(page==='Appearance')open('Settings');else if(page!=='Home')open('Home');else setDialog(true);}document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key);},[page,dialog,zipOpen]);
 useEffect(()=>{if(content.current&&motion&&!reduced()){const a=animate(content.current,{opacity:[0,1],transform:['translateY(8px)','translateY(0px)']},{duration:.2});return()=>a.stop();}},[page,index]);
 useEffect(()=>{window.QuietTVState=()=>({page,focus:getCurrentFocusKey(),dialog,apps:apps.length,index,query});},[page,dialog,index,apps.length,query]);
 const pages=Math.max(1,Math.ceil(shown.length/10));
 return <FocusContext.Provider value={focusKey}><Context.Provider value={{open,dialog,visualFocus,setVisualFocus,motion}}><div ref={ref} className={'screen appearance-'+look.name.toLowerCase()+(look.kind==='color'?' appearance-plain':'')+(motion?'':' motion-off')}><div className="wallpaper" style={look.kind==='color'?{backgroundColor:look.hex}:undefined}/><div className="shade"/>
 <header><nav>{['Home','Apps','Movies','Live TV'].map(name=><FocusButton key={name} id={'nav-'+name} onPress={()=>open(name)} className={'nav-link '+(page===name?'active':'')} label={name}>{name==='Home'&&<House size={27} fill="currentColor" strokeWidth={1.5}/>}<span>{name}</span></FocusButton>)}</nav><div className="header-tools"><FocusButton id="nav-Search" className="icon-button" label="Search" onPress={()=>open('Search')}><Search/></FocusButton><FocusButton id="nav-Settings" className={'icon-button '+(page==='Settings'?'selected':'')} label="Settings" onPress={()=>open('Settings')}><Settings/></FocusButton><FocusButton id="nav-Favorites" className="profile-button" label="Favorites" onPress={()=>open('Favorites')}><UserRound/></FocusButton><time>{time.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true}).toLowerCase()}</time></div></header>
 <main ref={content} className={page==='Home'?'home-content'+(recentlyOpened.length?' has-recents':''):'library-content'}>
 {page==='Home'?<>
  {weather.status!=='off'&&<WeatherPanel view={weather}/>}
  {runtime.error&&<p className="empty-state" role="alert">{runtime.error}</p>}
  <section className="home-apps"><h2>Your apps</h2><div className="app-row">{first.map(app=><Tile key={app.Id} app={app} prefix="home" onPress={select}/>)}</div></section>
  {recentlyOpened.length>0&&<section className="recent-section"><h2>Recently opened</h2><div className="recent-row">{recentlyOpened.map(app=><FocusButton key={app.Id} id={'recent-'+app.Id} className="recent-chip" label={'Open '+app.Name+' again'} onPress={()=>select(app)}><span className="recent-art"><img src={artFor(app.Id)} alt="" loading="lazy" onError={e=>{e.currentTarget.style.display='none';}}/></span><span>{app.Name}</span></FocusButton>)}</div></section>}
  <section className="quick-section"><h2>Your TV, your way</h2><div className="quick-row"><Shortcut id="quick-apps" Icon={Grid2X2} title="All your apps" detail={apps.length+' apps. One place.'} onPress={()=>open('Apps')}/><Shortcut id="quick-live" Icon={Tv} title="Live TV" detail="Your channels & streaming" onPress={()=>open('Live TV')}/><Shortcut id="quick-inputs" Icon={Plug} title="Connected devices" detail="HDMI & TV inputs" onPress={()=>open('Inputs')}/><Shortcut id="quick-settings" Icon={SlidersHorizontal} title="Picture & sound" detail="Fine-tune your screen" onPress={()=>send({type:'launch',id:SYSTEM_APPS.settings})}/></div></section>
 </>:<>
  <h1>{page==='Apps'?'Your app library':page==='Arrange'?'Arrange apps':page==='Search'?'Find your favorites':page==='Appearance'?'Appearance & weather':page}</h1><div className="subheading">{page==='Apps'?apps.length+' apps · Page '+(index+1)+' of '+pages:page==='Appearance'?'Choose a wallpaper or a plain color, and the weather on Home.':page==='Movies'?'Your installed movie & series apps.':page==='Live TV'?'Live channels and the services you watch.':page==='Favorites'?'Select favorites to put them first on Home.':page==='Arrange'?'Select two apps to swap their positions.':page==='Search'?'Search installed apps with your remote.':page==='Inputs'?'Choose a TV or HDMI input. Home brings you back.':'Make your TV feel like yours.'}</div>
  {page==='Inputs'?<div className="settings-grid input-grid">{inputs.map(source=><Shortcut key={source.type+source.number} id={'source-'+source.type+'-'+source.number} Icon={source.type==='TV'?Tv:Plug} title={source.type==='TV'?'TV':source.type+' '+source.number} detail={source.type==='TV'?'Live TV input':source.connected?'Device connected':'No device detected'} onPress={()=>window.QuietTVSelectInput(source).catch(error=>setMessage(error.message))}/>)}{inputsError&&<p className="empty-state">{inputsError}</p>}</div>:page==='Appearance'?<div className="appearance-page">
   <section><h2>Wallpaper</h2><div className="choice-row">{WALLPAPERS.map(name=><Choice key={name} id={'wall-'+name} name={name} className={'wall-'+name.toLowerCase()} selected={appearance===name} detail={name==='Auto'?capital(autoPhase)+' now · follows the sun':wallDetail[name]} onPress={()=>setAppearance(name)}/>)}</div></section>
   <section><h2>Plain color</h2><div className="choice-row color-row">{COLORS.map(color=><Choice key={color.name} id={'color-'+color.name} name={color.name} swatch={{backgroundColor:color.hex}} selected={appearance===color.name} onPress={()=>setAppearance(color.name)}/>)}</div></section>
   <section><h2>Weather on Home</h2><div className="choice-row weather-row"><Shortcut id="weather-toggle" Icon={weatherSettings.enabled?CloudSun:CloudOff} title={'Weather · '+(weatherSettings.enabled?'On':'Off')} detail={weatherSettings.enabled?'Current conditions on Home':'Nothing is fetched'} onPress={()=>setWeatherSettings(s=>({...s,enabled:!s.enabled}))}/><Shortcut id="weather-zip" Icon={MapPin} title={'ZIP code · '+weatherSettings.zip} detail={place?placeLabel(place):weather.status==='unavailable'?weather.detail:'Not looked up yet'} onPress={()=>setZipOpen(true)}/><Shortcut id="weather-unit" Icon={Thermometer} title={'Units · °'+weatherSettings.unit} detail={weatherSettings.unit==='F'?'Fahrenheit':'Celsius'} onPress={()=>setWeatherSettings(s=>({...s,unit:s.unit==='F'?'C':'F'}))}/></div></section>
  </div>:page==='Settings'?<div className="settings-grid"><Shortcut id="setting-appearance" Icon={Wallpaper} title="Appearance & weather" detail={(appearance==='Auto'?'Auto · '+capital(look.name)+' now':appearance)+' · Weather '+(weatherSettings.enabled?'on':'off')} onPress={()=>open('Appearance')}/><Shortcut id="setting-tv" Icon={SlidersHorizontal} title="Picture & sound" detail="Open TV settings" onPress={()=>send({type:'launch',id:SYSTEM_APPS.settings})}/><Shortcut id="setting-favorites" Icon={Star} title="Favorites" detail={favorites.length+' saved apps'} onPress={()=>open('Favorites')}/><Shortcut id="setting-arrange" Icon={Move} title="Arrange apps" detail="Choose your app order" onPress={()=>open('Arrange')}/><Shortcut id="setting-motion" Icon={Check} title={'Motion · '+(motion?'On':'Off')} detail="Short, gentle transitions" onPress={()=>setMotion(!motion)}/><Shortcut id="setting-store" Icon={ShoppingBag} title="App store & updates" detail="Manage your installed apps" onPress={()=>send({type:'launch',id:SYSTEM_APPS.store})}/><Shortcut id="setting-inputs" Icon={Plug} title="Inputs" detail="Connected devices" onPress={()=>open('Inputs')}/><Shortcut id="setting-close" Icon={House} title="Return to TV" detail="Your TV or HDMI picture" onPress={()=>setDialog(true)}/></div>:<>
   {page==='Search'&&<div className="search-panel"><div className="search-value"><Search size={26}/>{query||<span>App name</span>}</div><div className="keyboard">{'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'.split('').map(char=><FocusButton id={'key-'+char} key={char} onPress={()=>{setQuery(q=>q+char);setIndex(0);}}>{char}</FocusButton>)}<FocusButton id="key-space" label="Space" onPress={()=>setQuery(q=>q+' ')}>Space</FocusButton><FocusButton id="key-delete" label="Delete letter" onPress={()=>{setQuery(q=>q.slice(0,-1));setIndex(0);}}><Delete size={22}/></FocusButton><FocusButton id="key-clear" onPress={()=>{setQuery('');setIndex(0);}}>Clear</FocusButton></div></div>}
   <div className={'app-grid '+(page==='Search'?'search-results':'')}>{shown.slice(index*10,index*10+10).map(app=><div key={app.Id} className="grid-cell"><Tile app={app} prefix="grid" onPress={select}/>{page==='Favorites'&&favorites.includes(app.Id)&&<span className="star"><Star size={22} fill="currentColor"/></span>}{pendingSwap===app.Id&&<span className="star">Selected</span>}</div>)}</div>
   {!shown.length&&<p className="empty-state">No apps match “{query}”. Try another name.</p>}
   <div className="pagination">{index>0&&<FocusButton id="previous" onPress={()=>{setIndex(index-1);requestAnimationFrame(()=>setFocus('grid-'+shown[(index-1)*10].Id));}}><ChevronLeft/>Previous</FocusButton>}{(index+1)*10<shown.length&&<FocusButton id="next" onPress={()=>{setIndex(index+1);requestAnimationFrame(()=>setFocus('grid-'+shown[(index+1)*10].Id));}}>Next<ChevronRight/></FocusButton>}</div>
  </>}
 </>}
 </main><footer>{message?<span role="status" className="toast">{message}</span>:page!=='Home'&&<><span><kbd>OK</kbd>Select</span><span><kbd><ArrowLeft size={18}/></kbd>Return</span><span className="footer-brand">QUIET TV</span></>}</footer>{dialog&&<CloseDialog onCancel={cancelDialog} onClose={()=>send({type:'close'})}/>}{zipOpen&&<ZipDialog zip={weatherSettings.zip} place={place} onCancel={closeZip} onSave={zip=>{setWeatherSettings(s=>({...s,zip,enabled:true}));closeZip();}}/>}
 </div></Context.Provider></FocusContext.Provider>;
}
function fit(){const root=document.getElementById('root');root.style.transform='scale('+Math.min(innerWidth/1920,innerHeight/1080)+')';root.style.transformOrigin='top left';}
window.addEventListener('resize',fit);fit();createRoot(document.getElementById('root')).render(<App/>);

