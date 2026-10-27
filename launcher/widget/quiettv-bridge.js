'use strict';
// QuietTV preview widget bridge (D5b). Connects the frontend globals to documented Tizen web APIs.
// Backend DISABLED (default): no network request at all; launches go straight to tizen.application.launch
// for apps in the owner catalogue (plus Settings and Store), Home resets the frontend's own UI.
// Backend ENABLED (future loopback service only): fixed http://127.0.0.1:<port>, a token header and four
// finite routes (GET /status, POST /home, POST /launch/<id>, POST /ui-state). No other URL can be built.
// No root, startup, Home registration, key registration or shell access lives here.
(function(root){
 var SYSTEM_IDS=['com.samsung.tv.quicksetting','com.samsung.tv.store'],IPTV_ID='quiet.iptv';
 var LEGACY_PORTS=[4190,4196],BACK_KEYS=[461,10009,27,8],POLL_MS=100,BACKEND_TIMEOUT_MS=4000;
 var APP_ID=/^[A-Za-z0-9_.-]{1,160}$/,TOKEN=/^[A-Za-z0-9_-]{32,128}$/;
 function validId(id){return typeof id==='string'&&APP_ID.test(id)&&id.charAt(0)!=='.'&&id.indexOf('..')<0;}
 function keys(o){return Object.keys(o).sort().join(',');}
 function fail(text){throw new Error('QuietTV widget config: '+text);}

 // Re-validates what build_widget.py already checked: a hand-edited stage fails closed here too.
 // runtime: the window.QuietTVApps / window.QuietTVConfig globals the frontend resolver reads; they must agree.
 function checkConfig(c,runtime){
  if(!c||typeof c!=='object')fail('missing');
  if(c.schema!=='quiettv-widget-config/1')fail('wrong schema');
  if(c.preview!==true||c.onBoot!==false||c.registerAsHome!==false||c.iptvEnabled!==false)fail('preview flags are wrong');
  if(!Array.isArray(c.apps)||!c.apps.length)fail('no apps');
  var allowed={};
  c.apps.forEach(function(a){if(!a||!validId(a.Id)||a.Id===IPTV_ID||allowed[a.Id])fail('bad app entry');allowed[a.Id]=true;});
  if(runtime){
   var r=runtime.config,list=runtime.apps,ids=function(l){return l.map(function(a){return a&&a.Id;}).join('\n');};
   if(!r||r.schema!=='quiettv-widget/1'||r.preview!==true||r.iptvEnabled!==false)fail('QuietTVConfig must be preview with IPTV off');
   if(!Array.isArray(list)||ids(list)!==ids(c.apps))fail('QuietTVApps does not match the bridge catalogue');
  }
  SYSTEM_IDS.forEach(function(id){allowed[id]=true;});
  var b=c.backend,backend;
  if(b&&keys(b)==='enabled'&&b.enabled===false)backend={enabled:false};
  else if(b&&keys(b)==='enabled,port,token'&&b.enabled===true){
   if(typeof b.port!=='number'||b.port%1!==0||b.port<1024||b.port>65535||LEGACY_PORTS.indexOf(b.port)>=0)fail('bad backend port');
   if(typeof b.token!=='string'||!TOKEN.test(b.token))fail('bad backend token');
   backend={enabled:true,base:'http://127.0.0.1:'+b.port,token:b.token};
  }else fail('backend must be {enabled:false} or {enabled:true,port,token}');
  return {allowed:allowed,backend:backend};
 }

 // env: {window, tizen, config, runtime?, createInputs, setInterval, clearInterval, XMLHttpRequest}
 function createBridge(env){
  var w=env.window,cfg=checkConfig(env.config,env.runtime),tizen=env.tizen,timer=null;
  var inputs=typeof env.createInputs==='function'&&tizen?env.createInputs(tizen,onInputs):null;
  function onInputs(state){var el=w.document&&w.document.documentElement;if(el&&el.classList)el.classList[state.active?'add':'remove']('watching-tv');}
  function message(text){if(typeof w.QuietTVSetMessage==='function')w.QuietTVSetMessage(text);}
  function resetHome(){if(typeof w.QuietTVResetHome==='function')w.QuietTVResetHome();}
  function hideInputs(){return inputs?inputs.hide().then(null,function(){}):Promise.resolve();}

  function request(method,route,body){
   if(!cfg.backend.enabled)return Promise.reject(new Error('backend disabled'));
   return new Promise(function(resolve,reject){
    var xhr=new env.XMLHttpRequest();
    xhr.open(method,cfg.backend.base+route,true);
    xhr.setRequestHeader('X-QuietTV-Token',cfg.backend.token);
    if(body!==undefined)xhr.setRequestHeader('Content-Type','application/json');
    xhr.timeout=BACKEND_TIMEOUT_MS;
    xhr.onload=function(){if(xhr.status>=200&&xhr.status<300)resolve(xhr.responseText);else reject(new Error('backend '+xhr.status));};
    xhr.onerror=xhr.ontimeout=function(){reject(new Error('backend unreachable'));};
    xhr.send(body===undefined?null:JSON.stringify(body));
   });
  }

  function launch(id){
   if(id===IPTV_ID){message('IPTV is not available in this preview.');return Promise.resolve(false);}
   if(!validId(id)||!cfg.allowed[id]){message('This app is not available.');return Promise.resolve(false);}
   return hideInputs().then(function(){
    if(cfg.backend.enabled)return request('POST','/launch/'+encodeURIComponent(id)).then(function(){return true;});
    return new Promise(function(resolve,reject){tizen.application.launch(id,function(){resolve(true);},reject);});
   }).then(null,function(){message('Could not open the app.');return false;});
  }
  function home(){
   return hideInputs().then(function(){resetHome();if(cfg.backend.enabled)return request('POST','/home').then(null,function(){});});
  }
  function close(){try{tizen.application.getCurrentApplication().exit();}catch(e){}}
  function handle(action){
   if(!action||typeof action!=='object')return;
   if(action.type==='launch')launch(action.id);else if(action.type==='home')home();else if(action.type==='close')close();
  }
  // The frontend replaces window.QuietTVOutbox on mount, so always drain the CURRENT array.
  function drain(){var box=w.QuietTVOutbox;if(!Array.isArray(box)||!box.length)return;box.splice(0,box.length).forEach(handle);}
  function status(){return request('GET','/status');}
  function reportUiState(){var s=typeof w.QuietTVState==='function'?w.QuietTVState():null;return request('POST','/ui-state',s);}

  // Back while a TV input fills the screen returns to our Home; otherwise the frontend's own Back handling runs.
  function onKey(e){
   if(!inputs||!inputs.state().active||BACK_KEYS.indexOf(e.keyCode)<0)return;
   e.preventDefault();e.stopImmediatePropagation();home();
  }
  function onVisible(){if(!w.document.hidden)home();}

  w.QuietTVGetInputs=function(){return inputs?inputs.load():Promise.reject(new Error('Input controls are available on the TV.'));};
  w.QuietTVSelectInput=function(source){
   if(!inputs)return Promise.reject(new Error('Input controls are available on the TV.'));
   if(!source||typeof source.type!=='string'||typeof source.number!=='number')return Promise.reject(new Error('This input is not available on this TV.'));
   return inputs.watch({type:source.type,number:source.number});
  };
  w.addEventListener('keydown',onKey,true);
  if(w.document)w.document.addEventListener('visibilitychange',onVisible);
  timer=env.setInterval(drain,POLL_MS);
  return {drain:drain,launch:launch,home:home,close:close,status:status,reportUiState:reportUiState,
   stop:function(){env.clearInterval(timer);w.removeEventListener('keydown',onKey,true);if(w.document)w.document.removeEventListener('visibilitychange',onVisible);}};
 }

 if(typeof module==='object'&&module.exports){module.exports={createBridge:createBridge,checkConfig:checkConfig};return;}
 try{
  root.QuietTVBridge=createBridge({window:root,tizen:root.tizen,config:root.QuietTVWidgetConfig,
   runtime:{apps:root.QuietTVApps,config:root.QuietTVConfig},createInputs:root.QuietTVInputs,
   setInterval:function(f,ms){return root.setInterval(f,ms);},clearInterval:function(t){root.clearInterval(t);},XMLHttpRequest:root.XMLHttpRequest});
 }catch(e){if(root.console)root.console.error(String(e&&e.message||e));}
})(this);
