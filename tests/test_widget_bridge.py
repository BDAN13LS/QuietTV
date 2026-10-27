"""launcher/widget/quiettv-bridge.js in a node VM with a fake Tizen: the packaged bytes, loaded as the TV would.

Proves: backend DISABLED makes zero requests (XHR and fetch spies, with an ENABLED positive control),
launches go straight to tizen.application.launch for catalogue apps only, IPTV is refused, Home and
Back reset our own UI, and inputs use the copied tv-inputs.js. Tokens are SYNTHETIC test strings.
Run: python -B -m unittest tests.test_widget_bridge
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper"))
sys.path.insert(0, str(REPO / "tests"))
import build_widget as bw  # noqa: E402
import owner_init as oi  # noqa: E402
from test_owner_init import SYN_TOKEN, inputs  # noqa: E402
from test_widget_build import write_dist  # noqa: E402

NODE = shutil.which("node")
NETFLIX, SETTINGS = "org.tizen.netflix-app", "com.samsung.tv.quicksetting"

HARNESS = r"""
const fs=require('fs'),vm=require('vm');
const [inputsPath,configPath,bridgePath,mode]=process.argv.slice(2);
const flush=()=>new Promise(r=>setImmediate(r));
function makeWindow(){
 const log={launch:[],exit:0,reset:0,messages:[],xhr:[],fetch:[],frontKeys:0,tv:[],errors:[]};
 const listeners=[],docListeners=[],classes=new Set(),intervals=[];
 const document={hidden:false,documentElement:{classList:{add:c=>classes.add(c),remove:c=>classes.delete(c)}},
  addEventListener:(t,f)=>docListeners.push({t,f}),removeEventListener(){}};
 function XHR(){const r={method:null,url:null,headers:{},body:undefined};log.xhr.push(r);
  this.open=(m,u)=>{r.method=m;r.url=u;};this.setRequestHeader=(k,v)=>{r.headers[k]=v;};
  this.send=b=>{r.body=b;this.status=200;this.responseText='{}';Promise.resolve().then(()=>this.onload&&this.onload());};}
 const tizen={application:{launch:(id,ok)=>{log.launch.push(id);ok();},getCurrentApplication:()=>({exit:()=>{log.exit++;}})},
  systeminfo:{getPropertyValue:(p,ok)=>{log.tv.push('get:'+p);ok({connected:[{type:'HDMI',number:1}],disconnected:[{type:'HDMI',number:2}]});}},
  tvwindow:{show:(ok)=>{log.tv.push('show');ok();},hide:(ok)=>{log.tv.push('hide');ok();},
   setSource:(s,ok)=>{log.tv.push('set:'+s.type+s.number);ok(s);},getSource:()=>({type:'TV',number:1})}};
 const win={tizen,document,XMLHttpRequest:XHR,fetch:(...a)=>{log.fetch.push(a);return Promise.reject(Error('blocked'));},
  setInterval:(fn)=>{intervals.push(fn);return intervals.length;},clearInterval(){},
  addEventListener:(t,f,c)=>listeners.push({t,f,c:!!c}),removeEventListener(){},
  console:{error:m=>log.errors.push(String(m))}};
 win.window=win;
 // the frontend's own globals and Back handler
 win.QuietTVResetHome=()=>{log.reset++;};win.QuietTVSetMessage=t=>log.messages.push(t);
 win.addEventListener('keydown',e=>{if([461,10009,27,8].includes(e.keyCode))log.frontKeys++;});
 const key=code=>{const ev={keyCode:code,stopped:false,preventDefault(){},stopImmediatePropagation(){this.stopped=true;}};
  for(const phase of [true,false])for(const l of listeners.filter(l=>l.t==='keydown'&&l.c===phase)){l.f(ev);if(ev.stopped)return;}};
 const tick=async()=>{intervals.forEach(f=>f());await flush();await flush();};
 return {win,log,classes,docListeners,key,tick};
}
async function run(){
 const t=makeWindow(),ctx=vm.createContext(t.win);
 vm.runInContext(fs.readFileSync(inputsPath,'utf8'),ctx);
 if(mode==='enabled')t.win.QuietTVWidgetConfig={schema:'quiettv-widget-config/1',packageId:'Qa7Rt2Lm9X',appName:'QuietTV',
  ownAppId:'Qa7Rt2Lm9X.QuietTV',version:'0.1.0',preview:true,onBoot:false,registerAsHome:false,iptvEnabled:false,
  apps:[{Id:'org.tizen.netflix-app',Name:'Netflix'}],backend:{enabled:true,port:47123,token:process.env.SYN_TOKEN}},
  t.win.QuietTVApps=[{Id:'org.tizen.netflix-app',Name:'Netflix',Icon:''}],
  t.win.QuietTVConfig={schema:'quiettv-widget/1',preview:true,iptvEnabled:false};
 else vm.runInContext(fs.readFileSync(configPath,'utf8'),ctx);
 vm.runInContext(fs.readFileSync(bridgePath,'utf8'),ctx);
 const w=t.win,out={installed:!!w.QuietTVBridge,errors:t.log.errors};
 w.QuietTVOutbox=[];  // the frontend replaces the outbox on mount
 const push=async a=>{w.QuietTVOutbox.push(a);await t.tick();};
 await push({type:'launch',id:'org.tizen.netflix-app'});out.afterLaunch=t.log.launch.slice();
 await push({type:'launch',id:'quiet.iptv'});out.afterIptv=t.log.launch.slice();
 await push({type:'launch',id:'com.unlisted.app'});await push({type:'launch',id:'http://evil/x'});
 await push({type:'launch',id:'com.samsung.tv.quicksetting'});
 await push({type:'home'});await push({type:'close'});await push({type:'shell',cmd:'rm'});await push(null);
 out.outboxEmpty=w.QuietTVOutbox.length===0;
 const list=await w.QuietTVGetInputs();out.inputs=list;
 await w.QuietTVSelectInput({type:'HDMI',number:1});out.watching=t.classes.has('watching-tv');
 t.key(10009);await t.tick();out.backWhileWatching={front:t.log.frontKeys,watching:t.classes.has('watching-tv')};
 t.key(10009);out.backAfter=t.log.frontKeys;
 t.docListeners.filter(l=>l.t==='visibilitychange').forEach(l=>l.f());await t.tick();
 if(mode==='enabled'){await w.QuietTVBridge.status();await w.QuietTVBridge.reportUiState();}
 Object.assign(out,{launch:t.log.launch,exit:t.log.exit,reset:t.log.reset,messages:t.log.messages,tv:t.log.tv,
  xhr:t.log.xhr,fetch:t.log.fetch.length});
 if(mode==='refusals'){const m=require(bridgePath);const base=JSON.parse(JSON.stringify(w.QuietTVWidgetConfig));
  const bad={port4190:{backend:{enabled:true,port:4190,token:process.env.SYN_TOKEN}},
   placeholderToken:{backend:{enabled:true,port:47123,token:'<CONFIGURE>'}},shortToken:{backend:{enabled:true,port:47123,token:'abc'}},
   disabledWithToken:{backend:{enabled:false,token:process.env.SYN_TOKEN}},onBoot:{onBoot:true},home:{registerAsHome:true},
   iptv:{apps:[{Id:'quiet.iptv',Name:'IPTV'}]},iptvOn:{iptvEnabled:true},noApps:{apps:[]}};
  out.refused={};for(const [k,v] of Object.entries(bad)){try{m.checkConfig(Object.assign({},base,v));out.refused[k]=false;}catch(e){out.refused[k]=true;}}
  try{m.checkConfig(base);out.baseAccepted=true;}catch(e){out.baseAccepted=false;}
  const rt={apps:JSON.parse(JSON.stringify(w.QuietTVApps)),config:JSON.parse(JSON.stringify(w.QuietTVConfig))};
  const badRt={appsMissing:{apps:undefined},configMissing:{config:undefined},extraApp:{apps:rt.apps.concat([{Id:'x.y',Name:'X'}])},
   reordered:{apps:rt.apps.slice().reverse()},iptvOn:{config:Object.assign({},rt.config,{iptvEnabled:true})},
   notPreview:{config:Object.assign({},rt.config,{preview:false})},wrongSchema:{config:Object.assign({},rt.config,{schema:'other'})}};
  out.runtimeRefused={};for(const [k,v] of Object.entries(badRt)){try{m.checkConfig(base,Object.assign({},rt,v));out.runtimeRefused[k]=false;}catch(e){out.runtimeRefused[k]=true;}}
  try{m.checkConfig(base,rt);out.runtimeAccepted=true;}catch(e){out.runtimeAccepted=false;}}
 process.stdout.write(JSON.stringify(out));
}
run().catch(e=>{process.stdout.write(JSON.stringify({fatal:String(e&&e.stack||e)}));});
"""


@unittest.skipUnless(NODE, "node is not installed")
class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        owner = tmp / "owner"
        oi.generate(owner, **inputs())
        files = bw.assemble(write_dist(tmp / "dist"), owner)  # the packaged bytes, not the source files
        cls.paths = []
        for name in ("tv-inputs.js", "quiettv-widget-config.js", "quiettv-bridge.js"):
            (tmp / name).write_bytes(files[name])
            cls.paths.append(str(tmp / name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_mode(self, mode):
        proc = subprocess.run([NODE, "-", *self.paths, mode], input=HARNESS, capture_output=True, text=True,
                              timeout=60, env={**os.environ, "SYN_TOKEN": SYN_TOKEN})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertNotIn("fatal", out, out.get("fatal"))
        self.assertTrue(out["installed"], out["errors"])
        return out

    def test_backend_disabled_makes_no_requests_and_launches_directly(self):
        out = self.run_mode("disabled")
        self.assertEqual(out["xhr"], [])
        self.assertEqual(out["fetch"], 0)
        self.assertEqual(out["afterLaunch"], [NETFLIX])  # drained after the frontend replaced the outbox
        self.assertEqual(out["afterIptv"], [NETFLIX])
        self.assertEqual(out["launch"], [NETFLIX, SETTINGS])
        self.assertTrue(out["outboxEmpty"])
        self.assertIn("IPTV is not available in this preview.", out["messages"])
        self.assertEqual(out["messages"].count("This app is not available."), 2)
        self.assertEqual(out["exit"], 1)

    def test_inputs_back_and_home(self):
        out = self.run_mode("disabled")
        self.assertEqual(out["inputs"], [{"type": "HDMI", "number": 1, "connected": True},
                                         {"type": "HDMI", "number": 2, "connected": False}])
        self.assertIn("set:HDMI1", out["tv"])
        self.assertIn("show", out["tv"])
        self.assertTrue(out["watching"])
        # Back while an input is showing: hidden, our Home reset, frontend handler NOT run
        self.assertEqual(out["backWhileWatching"], {"front": 0, "watching": False})
        self.assertIn("hide", out["tv"])
        # Back with no input showing: the frontend's own handler runs
        self.assertEqual(out["backAfter"], 1)
        # home action + Back-from-input + becoming visible each reset our own UI
        self.assertEqual(out["reset"], 3)

    def test_enabled_backend_uses_only_fixed_loopback_routes(self):
        out = self.run_mode("enabled")  # positive control: the request spy does see traffic
        self.assertEqual(out["launch"], [])
        urls = [(r["method"], r["url"]) for r in out["xhr"]]
        base = "http://127.0.0.1:47123"
        self.assertEqual(urls[0], ("POST", base + "/launch/" + NETFLIX))
        # catalogue app + system Settings only; IPTV, unlisted ids and URL-shaped ids never reach the wire
        allowed = {("POST", base + "/launch/" + NETFLIX), ("POST", base + "/launch/" + SETTINGS),
                   ("POST", base + "/home"), ("GET", base + "/status"), ("POST", base + "/ui-state")}
        self.assertEqual(set(urls), allowed)
        self.assertTrue(all(r["headers"].get("X-QuietTV-Token") == SYN_TOKEN for r in out["xhr"]))
        self.assertEqual(out["fetch"], 0)

    def test_config_refusals(self):
        out = self.run_mode("refusals")
        self.assertTrue(out["baseAccepted"])
        self.assertEqual({k: v for k, v in out["refused"].items() if not v}, {})
        self.assertTrue(out["runtimeAccepted"])
        self.assertEqual({k: v for k, v in out["runtimeRefused"].items() if not v}, {})
        self.assertEqual(len(out["runtimeRefused"]), 7)

    def test_bridge_source_has_no_root_startup_or_shell(self):
        src = (REPO / "launcher" / "widget" / "quiettv-bridge.js").read_text("utf-8")
        for banned in ("4190/", "exec(", "eval(", "Function(", "setAppControl", "application.kill", "home_appid",
                       "startup_appid", "registerKey", "/proc/", "sdb", "fetch("):
            self.assertNotIn(banned, src)

    TV_INPUTS_SHA256 = "261f8e2b824e3eec5adda91bac2d5aa3a88ea0d83cfaf124042464832e207fca"

    def test_tv_inputs_is_the_pinned_scanned_copy(self):
        ours = (REPO / "launcher" / "widget" / "tv-inputs.js").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(ours).hexdigest(), self.TV_INPUTS_SHA256)

    def test_tv_inputs_matches_private_origin_when_present(self):
        try:
            blob = subprocess.run(["git", "-C", str(REPO), "show", "main:boot/widget/tv-inputs.js"],
                                  capture_output=True, timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("main:boot/widget/tv-inputs.js not available")
        ours = (REPO / "launcher" / "widget" / "tv-inputs.js").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(ours).hexdigest(), hashlib.sha256(blob.replace(b"\r\n", b"\n")).hexdigest())


# The frontend's own resolver in this checkout; QUIETTV_RUNTIME_CATALOG may point at another lane for private checks.
RESOLVER = Path(os.environ.get("QUIETTV_RUNTIME_CATALOG") or REPO / "frontend" / "src" / "runtime-catalog.mjs")

RESOLVER_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),{pathToFileURL}=require('url');
const [configPath,resolverPath]=process.argv.slice(2);
const COMPILED=[{Id:'compiled.only.app',Name:'Compiled fallback',Icon:''}];
(async()=>{
 const {resolveRuntimeApps}=await import(pathToFileURL(resolverPath).href);
 const packaged=fs.readFileSync(configPath,'utf8');
 // run the emitted script as the TV page would (classic script, window === global), then resolve
 const page=src=>{const g={};g.window=g;vm.runInContext(src,vm.createContext(g));
  return resolveRuntimeApps({compiled:COMPILED,apps:g.QuietTVApps,config:g.QuietTVConfig});};
 const withoutApps=packaged.split('\n').filter(l=>!l.startsWith('window.QuietTVApps=')).join('\n');
 const malformed=packaged.replace('"Id":"org.tizen.netflix-app"','"Id":"../evil"');
 const withIptv=packaged.replace('window.QuietTVApps=Object.freeze([','window.QuietTVApps=Object.freeze([{"Icon":"","Id":"quiet.iptv","Name":"IPTV"},');
 process.stdout.write(JSON.stringify({packaged:page(packaged),missing:page(withoutApps),malformed:page(malformed),
  withIptv:page(withIptv),legacy:resolveRuntimeApps({compiled:COMPILED})}));
})().catch(e=>process.stdout.write(JSON.stringify({fatal:String(e&&e.stack||e)})));
"""


@unittest.skipUnless(NODE and RESOLVER.is_file(),
                     "node or frontend/src/runtime-catalog.mjs is absent (a toolkit export without --with-frontend)")
class RuntimeCatalogIntegrationTests(unittest.TestCase):
    """The packaged quiettv-widget-config.js feeds the frontend's runtime-catalog.mjs resolver (read-only)."""

    def test_emitted_globals_reach_the_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            oi.generate(tmp / "owner", **inputs())
            files = bw.assemble(write_dist(tmp / "dist"), tmp / "owner")
            owner_ids = [a["Id"] for a in json.loads((tmp / "owner" / "apps.json").read_text())]
            (tmp / "cfg.js").write_bytes(files["quiettv-widget-config.js"])
            proc = subprocess.run([NODE, "-", str(tmp / "cfg.js"), str(RESOLVER)], input=RESOLVER_HARNESS,
                                  capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertNotIn("fatal", out, out.get("fatal"))
        # positive control: without the globals the resolver really does use the compiled list
        self.assertIn("compiled.only.app", [a["Id"] for a in out["legacy"]["apps"]])
        packaged = out["packaged"]
        self.assertEqual((packaged["ownerMode"], packaged["error"]), (True, ""))
        self.assertEqual([a["Id"] for a in packaged["apps"]], owner_ids)
        for case in ("missing", "malformed"):
            self.assertEqual(out[case]["apps"], [], case)
            self.assertTrue(out[case]["ownerMode"], case)
            self.assertTrue(out[case]["error"].startswith("Setup problem:"), case)
        self.assertEqual([a["Id"] for a in out["withIptv"]["apps"]], owner_ids)  # IPTV off even if listed
        for case in ("packaged", "missing", "malformed", "withIptv"):
            ids = [a["Id"] for a in out[case]["apps"]]
            self.assertNotIn("compiled.only.app", ids, case)
            self.assertNotIn("quiet.iptv", ids, case)


if __name__ == "__main__":
    unittest.main()
