'use strict';
// Uses only the TV's documented source and picture APIs; no Samsung Source key.
function createTvInputs(api,changed){
 var sourceObjects={},inputs=[],active=false,current=null,lastError='';changed=changed||function(){};
 function notify(){changed({active:active,current:current,inputs:inputs,error:lastError});}
 function failure(error){lastError=error&&error.name||String(error||'TV input control unavailable');notify();return Error(lastError);}
 function load(){return new Promise(function(resolve,reject){try{api.systeminfo.getPropertyValue('VIDEOSOURCE',function(value){
  var seen={};sourceObjects={};inputs=[];[['connected',true],['disconnected',false]].forEach(function(group){(value[group[0]]||[]).forEach(function(source){var key=source.type+':'+source.number;if(seen[key])return;seen[key]=true;sourceObjects[key]=source;inputs.push({type:source.type,number:source.number,connected:group[1]});});});lastError='';notify();resolve(inputs.slice());
 },function(e){reject(failure(e));});}catch(e){reject(failure(e));}});}
 function show(source){return new Promise(function(resolve,reject){try{api.tvwindow.show(function(){current={type:source.type,number:source.number,signal:source.signal};active=true;lastError='';notify();resolve(current);},function(e){reject(failure(e));},['0%','0%','100%','100%'],'MAIN','FRONT');}catch(e){reject(failure(e));}});}
 function watch(source){if(!source){try{return show(api.tvwindow.getSource('MAIN'));}catch(e){return Promise.reject(failure(e));}}
  return load().then(function(){var found=inputs.find(function(v){return v.type===source.type&&v.number===source.number;});if(!found)throw Error('This input is not available on this TV.');return new Promise(function(resolve,reject){try{api.tvwindow.setSource(sourceObjects[found.type+':'+found.number],resolve,function(e){reject(failure(e));},'MAIN');}catch(e){reject(failure(e));}});}).then(function(actual){return show(actual||source);});
 }
 function hide(){if(!active)return Promise.resolve();return new Promise(function(resolve,reject){try{api.tvwindow.hide(function(){active=false;lastError='';notify();resolve();},function(e){reject(failure(e));},'MAIN');}catch(e){reject(failure(e));}});}
 return {load:load,watch:watch,hide:hide,state:function(){return {active:active,current:current,inputs:inputs.slice(),error:lastError};}};
}
if(typeof module==='object'&&module.exports)module.exports=createTvInputs;else window.QuietTVInputs=createTvInputs;
