// Offline self-test: runs on Windows or Linux without Tizen libraries. It drives the
// real HostConfig/OwnerData/HostStartup/LaunchGate/IptvSession/WebFrontend code with
// recording fakes, so a disabled feature is shown not to start (and an enabled one,
// as a positive control, is shown to start exactly once with the exact arguments).
using System.Diagnostics;

internal static class SelfTest {
 internal const string ValidConfig="{\"schema\":\"quiettv-host-config/1\",\"runtimeRoot\":\"/home/owner/share/apps/quiet-tv\",\"ownerUid\":5001,\"ownAppId\":\"TestPkg001.QuietTV\",\"homeKeyCode\":63,\"observedInputPaths\":[\"/dev/input/event2\",\"/dev/input/event4\"]}";
 static int count;
 static void Check(bool ok,string claim){if(!ok)throw new Exception(claim);count++;}
 static void Rejects(string json,string claim){try{HostConfig.Parse(json);}catch(HostConfigException){count++;return;}throw new Exception("config accepted: "+claim);}
 static string With(string extra)=>ValidConfig[..^1]+","+extra+"}";
 static string Replace(string key,string value){var o=System.Text.Json.Nodes.JsonNode.Parse(ValidConfig).AsObject();o[key]=System.Text.Json.Nodes.JsonNode.Parse(value);return o.ToJsonString();}

 sealed class RecordingEffects : IHostEffects {
  internal readonly List<string> Calls=new();
  public void StartHomeObserver()=>Calls.Add("observe");
  public void StartRemoteGate()=>Calls.Add("grab");
  public void AddTimer(string purpose,double seconds,Native.TimerCallback callback)=>Calls.Add("timer:"+purpose);
  public void StartWebUi(bool hidden)=>Calls.Add("webui");
  public void Log(string line){}
 }
 sealed class RecordingStarter : IProcessStarter {internal readonly List<ProcessStartInfo> Calls=new();public Process Start(ProcessStartInfo info){Calls.Add(info);return null;}}
 sealed class MemorySessionFiles : ISessionFiles {
  internal readonly Dictionary<string,string> Files=new();internal readonly List<string> Ops=new();
  public bool Exists(string path){Ops.Add("exists:"+path);return Files.ContainsKey(path);}
  public string ReadAllText(string path){Ops.Add("read:"+path);return Files.TryGetValue(path,out var v)?v:throw new FileNotFoundException("memory session file missing");}
  public void WriteAllText(string path,string text){Ops.Add("write:"+path);Files[path]=text;}
  public void Delete(string path){Ops.Add("delete:"+path);Files.Remove(path);}
  public long Length(string path){Ops.Add("length:"+path);return Files.TryGetValue(path,out var v)?v.Length:throw new FileNotFoundException("memory session file missing");}
 }
 sealed class MemoryFiles : IOwnerFiles {internal readonly Dictionary<string,string> Files=new();public bool Exists(string path)=>Files.ContainsKey(path);public string ReadAllText(string path)=>Files[path];}

 internal static int Run(){
  try{
   State.Tests();RemoteGate.Tests();WebFrontend.Tests();CommandLifecycleTests.Run();
   var cfg=HostConfig.Parse(ValidConfig);IptvSession.Tests(cfg);
   ConfigTests(cfg);OwnerDataTests(cfg);StartupTests(cfg);LaunchTests(cfg);ProcessGateTests(cfg);InputLayoutTests();
   Console.WriteLine("PASS "+count+" host seam assertions");Console.WriteLine("SELF-TEST PASS");return 0;
  }catch(Exception e){Console.WriteLine("FAIL "+e.Message);return 1;}
 }

 static void ConfigTests(HostConfig cfg){
  Check(cfg.RuntimeRoot=="/home/owner/share/apps/quiet-tv"&&cfg.OwnerUid==5001&&cfg.OwnAppId=="TestPkg001.QuietTV"&&cfg.HomeKeyCode==63&&cfg.ObservedInputPaths.Count==2,"valid config accepted with its values");
  Check(!cfg.IptvEnabled&&!cfg.WebUiEnabled&&!cfg.SnapshotEnabled&&!cfg.RemoteGrabEnabled,"all optional features default OFF");
  Check(cfg.IptvDll=="/home/owner/share/apps/quiet-tv/QuietIPTV.dll","IPTV path derives from the root seam");
  var on=HostConfig.Parse(With("\"remoteDeviceNames\":[\"Test Remote\"],\"remoteGrabEnabled\":true,\"iptvEnabled\":true,\"webUiEnabled\":true,\"snapshotEnabled\":true"));
  Check(on.RemoteGrabEnabled&&on.IptvEnabled&&on.WebUiEnabled&&on.SnapshotEnabled,"explicit true flags accepted");
  Check(HostConfig.Parse(Replace("runtimeRoot","\"/opt/usr/home/owner/share/tmp/sdk_tools/quiet-tv\"")).RuntimeRoot.EndsWith("/quiet-tv"),"opt/usr owner share root accepted");
  foreach(var bad in new[]{"","null","[]","\"x\"","{","{}"})Rejects(bad,"malformed document "+bad);
  Rejects(ValidConfig.Replace("quiettv-host-config/1","quiettv-host-config/2"),"wrong schema");
  Rejects(With("\"extra\":1"),"unknown key");
  Rejects(With("\"ownerUid\":5002"),"duplicate key");
  Rejects(With("\"iptvEnabled\":\"true\""),"string flag");
  Rejects(With("\"iptvEnabled\":null"),"null flag");
  Rejects(With("\"remoteGrabEnabled\":true"),"grab without device names");
  foreach(var key in new[]{"runtimeRoot","ownerUid","ownAppId","homeKeyCode","observedInputPaths"}){var o=System.Text.Json.Nodes.JsonNode.Parse(ValidConfig).AsObject();o.Remove(key);Rejects(o.ToJsonString(),"missing "+key);}
  foreach(var root in new[]{"/","/etc/quiet-tv","/opt/usr/apps/quiet-tv","/home/owner/quiet-tv","/home/owner/share/../../etc/quiet-tv","/home/owner/share/./quiet-tv","/home/owner/share//quiet-tv","/home/owner/share/quiet-tv/","/home/owner/share/other","/home/owner/share/tmp\\quiet-tv","home/owner/share/quiet-tv","/home/owner/share/<ROOT>/quiet-tv","/home/owner/share/${ROOT}/quiet-tv","/home/owner/share/CHANGE_ME/quiet-tv","/home/owner/share/quiet-tv-EXAMPLE","/home/owner/share/x y/quiet-tv",null})Rejects(Replace("runtimeRoot",root==null?"null":System.Text.Json.JsonSerializer.Serialize(root)),"root "+root);
  foreach(var uid in new[]{"0","-1","5001.5","\"5001\"","65534","4999","6000"})Rejects(Replace("ownerUid",uid),"uid "+uid);
  foreach(var id in new[]{"\"QuietTV\"","\"XXXXXXXXXX.QuietTV\"","\"<PACKAGE>.QuietTV\"","\"Abc.QuietTV\"","\"TestPkg001.\"","\"TestPkg001.Quiet TV\"","\"TestPkg001.QuietTV;rm\""})Rejects(Replace("ownAppId",id),"app id "+id);
  foreach(var home in new[]{"0","-63","768","\"63\""})Rejects(Replace("homeKeyCode",home),"home key "+home);
  foreach(var paths in new[]{"[]","[\"/dev/input/event2\",\"/dev/input/event2\"]","[\"/dev/input/../mem\"]","[\"/dev/mem\"]","[\"/dev/input/event2;x\"]","[null]","\"/dev/input/event2\""})Rejects(Replace("observedInputPaths",paths),"input paths "+paths);
  foreach(var names in new[]{"[\"a\\\"b\"]","[\"\"]","[\"line\\nbreak\"]","[\"YOUR-REMOTE\"]","[\"x\",\"x\"]",null})Rejects(With("\"remoteDeviceNames\":"+(names??"null")),"device names "+names);
  bool threw=false;try{HostConfig.LoadFile(null);}catch(HostConfigException){threw=true;}Check(threw,"--config is required (no environment fallback)");
 }

 static void OwnerDataTests(HostConfig cfg){
  string apps=cfg.PathIn("apps.json"),settings=cfg.PathIn("settings.json");
  const string goodApps="[{\"Id\":\"org.tizen.netflix-app\",\"Name\":\"Netflix\"},{\"Id\":\"quiet.iptv\",\"Name\":\"IPTV\"},{\"Id\":\"org.tizen.primevideo\",\"Name\":\"Prime Video\",\"Icon\":\"/home/owner/share/apps/quiet-tv/icons/prime.png\"}]";
  const string goodSettings="{\"Order\":[\"org.tizen.primevideo\",\"org.removed.app\"],\"Favorites\":[\"org.tizen.netflix-app\"],\"Wallpaper\":1,\"HomeKeyCode\":0}";
  var files=new MemoryFiles();
  void Fails(string claim){var before=new Dictionary<string,string>(files.Files);bool threw=false;try{OwnerData.Load(cfg,files);}catch(HostConfigException){threw=true;}Check(threw,"owner data rejected: "+claim);Check(files.Files.Count==before.Count&&files.Files.All(kv=>before.TryGetValue(kv.Key,out var v)&&v==kv.Value),"rejected load mutates nothing: "+claim);}
  Fails("missing apps and settings");
  files.Files[apps]=goodApps;Fails("missing settings");
  files.Files.Remove(apps);files.Files[settings]=goodSettings;Fails("missing apps");
  files.Files[apps]=goodApps;var (list,prefs)=OwnerData.Load(cfg,files);
  Check(list.Count==3&&prefs.Order.SequenceEqual(new[]{"org.tizen.primevideo"})&&prefs.Favorites.SequenceEqual(new[]{"org.tizen.netflix-app"})&&prefs.Wallpaper==1,"valid owner data accepted; uninstalled ids dropped");
  foreach(var bad in new[]{"null","{}","[]","[null]","[{\"Id\":null,\"Name\":\"x\"}]","[{\"Id\":\"bad;id\",\"Name\":\"x\"}]","[{\"Id\":\"..\",\"Name\":\"x\"}]","[{\"Id\":\"a.b\",\"Name\":\"x\"},{\"Id\":\"a.b\",\"Name\":\"y\"}]","[{\"Id\":\"a.b\",\"Name\":\"\"}]","[{\"Id\":\"a.b\"}]","[{\"Id\":\"a.b\",\"Name\":\"x\",\"Token\":\"t\"}]","[{\"Id\":\"a.b\",\"Name\":\"x\",\"Icon\":\"/etc/passwd\"}]","[{\"Id\":\"a.b\",\"Name\":\"x\",\"Icon\":\"/home/owner/share/apps/quiet-tv/../../x.png\"}]","[{\"Id\":\"a.b\",\"Id\":\"c.d\",\"Name\":\"x\"}]","not json"}){files.Files[apps]=bad;Fails("apps "+bad);}
  files.Files[apps]=goodApps;
  foreach(var bad in new[]{"null","[]","{\"Order\":[null]}","{\"Order\":[\"a;b\"]}","{\"Order\":[\"org.tizen.primevideo\",\"org.tizen.primevideo\"]}","{\"Favorites\":\"x\"}","{\"Wallpaper\":99}","{\"Wallpaper\":\"1\"}","{\"Secret\":1}","{\"Wallpaper\":1,\"Wallpaper\":2}"}){files.Files[settings]=bad;Fails("settings "+bad);}
 }

 static void StartupTests(HostConfig cfg){
  var on=HostConfig.Parse(With("\"remoteDeviceNames\":[\"Test Remote\"],\"remoteGrabEnabled\":true,\"iptvEnabled\":true,\"webUiEnabled\":true,\"snapshotEnabled\":true"));
  List<string> Start(HostConfig c,params string[] args){var fx=new RecordingEffects();HostStartup.Run(c,HostStartup.FlagsFrom(args),fx,_=>1,_=>0,false);return fx.Calls;}
  var defaults=Start(cfg,"--web-ui");
  Check(!defaults.Contains("grab"),"defaults never grab the remote");
  Check(!defaults.Contains("timer:snapshot"),"defaults schedule no snapshot timer");
  Check(!defaults.Contains("webui"),"--web-ui alone does not start the web viewer");
  Check(!Start(HostConfig.Parse(With("\"webUiEnabled\":true")),"--web-ui").Contains("webui"),"IPTV off + Web UI on: startup does not start the web viewer");
  Check(defaults.Contains("observe")&&defaults.Contains("timer:heartbeat"),"defaults still observe Home and run the heartbeat");
  Check(!Start(cfg,"--registered-ui").Contains("grab")&&!Start(cfg,"--registered-ui").Contains("timer:snapshot"),"registered UI under defaults: no grab, no snapshot");
  var enabled=Start(on,"--web-ui");
  Check(enabled.Count(c=>c=="grab")==1&&!enabled.Contains("observe"),"positive control: grab starts once when enabled");
  Check(enabled.Count(c=>c=="timer:snapshot")==1,"positive control: snapshot timer when enabled");
  Check(enabled.Count(c=>c=="webui")==1,"positive control: web viewer when IPTV and Web UI are enabled and requested");
  Check(!Start(on,"--quiet-start").Contains("timer:snapshot"),"--quiet-start still suppresses snapshots");
  Check(!Start(on,"--system-home").Contains("grab"),"system-home mode never grabs");
 }

 static void LaunchTests(HostConfig cfg){
  var apps=new List<TvApp>{new("org.tizen.netflix-app","Netflix"),new("quiet.iptv","IPTV")};
  var off=new State(apps,new Preferences(),cfg.OwnAppId,cfg.IptvEnabled);
  var onCfg=HostConfig.Parse(With("\"iptvEnabled\":true"));var on=new State(apps,new Preferences(),onCfg.OwnAppId,onCfg.IptvEnabled);
  Check(LaunchGate.Route(cfg,off,"quiet.iptv")==LaunchRoute.Rejected,"IPTV off rejects quiet.iptv even when the catalogue lists it");
  Check(LaunchGate.Route(cfg,on,"quiet.iptv")==LaunchRoute.Rejected,"IPTV off rejects quiet.iptv whatever the state lists");
  Check(LaunchGate.Route(onCfg,on,"quiet.iptv")==LaunchRoute.Iptv,"positive control: IPTV route when enabled");
  Check(LaunchGate.Route(cfg,off,"org.tizen.netflix-app")==LaunchRoute.AppLauncher,"catalogue app launches");
  Check(LaunchGate.Route(cfg,off,"com.samsung.tv.store")==LaunchRoute.AppLauncher,"system store id launches");
  foreach(var id in new[]{"","org.unknown.app","@exit","quiet.inputs"})Check(LaunchGate.Route(cfg,off,id)==LaunchRoute.Rejected,"unknown id rejected: "+id);
  Check(LaunchGate.Route(onCfg,on,"quiet.inputs")==LaunchRoute.Rejected&&LaunchGate.Route(cfg,new State(new List<TvApp>{new("quiet.inputs","Inputs")},new Preferences(),cfg.OwnAppId,false),"quiet.inputs")==LaunchRoute.Rejected,"synthetic Inputs tile id is refused even if the catalogue lists it");
 }

 static void ProcessGateTests(HostConfig cfg){
  // Sessions run against MemorySessionFiles only: no disk read, write or delete, whatever root is configured.
  var mem=new MemorySessionFiles();var starter=new RecordingStarter();
  bool refused=false;try{new IptvSession(cfg,starter,mem).Start();}catch(InvalidOperationException e){refused=e.Message.Contains("disabled");}
  Check(refused&&starter.Calls.Count==0,"IPTV off: QuietIPTV.dll is never started");
  refused=false;try{new WebFrontend(cfg,starter,mem).Start();}catch(InvalidOperationException e){refused=e.Message.Contains("disabled");}
  Check(refused&&starter.Calls.Count==0,"Web UI off: QuietIPTV.dll --dashboard is never started");
  var webOnly=HostConfig.Parse(With("\"webUiEnabled\":true"));
  refused=false;try{new WebFrontend(webOnly,starter,mem).Start();}catch(InvalidOperationException e){refused=e.Message.Contains("disabled");}
  Check(refused&&starter.Calls.Count==0,"IPTV off + Web UI on: QuietIPTV.dll --dashboard is never started");
  Check(mem.Ops.Count==0,"refused sessions touch no session files");
  // Positive control with both switches on, on the ordinary root; stale session files exist only in memory.
  var onCfg=HostConfig.Parse(With("\"iptvEnabled\":true,\"webUiEnabled\":true"));
  foreach(var name in new[]{"iptv-close.request","iptv-status.json","ui-ready","ui-action.request"})mem.Files[onCfg.PathIn(name)]="stale";
  var onStarter=new RecordingStarter();
  try{new IptvSession(onCfg,onStarter,mem).Start();}catch(InvalidOperationException){}
  try{new WebFrontend(onCfg,onStarter,mem).Start(true);}catch(InvalidOperationException){}
  Check(onStarter.Calls.Count==2,"positive control: both starters reached once each when both are enabled");
  var a=onStarter.Calls[0];Check(a.FileName=="/usr/bin/dotnet"&&!a.UseShellExecute&&a.ArgumentList.SequenceEqual(new[]{onCfg.IptvDll}),"IPTV start uses an exact argument list under the root");
  var b=onStarter.Calls[1];Check(b.FileName=="/usr/bin/dotnet"&&!b.UseShellExecute&&b.ArgumentList.SequenceEqual(new[]{onCfg.IptvDll,"--dashboard","--start-hidden"}),"Web UI start uses an exact argument list");
  Check(mem.Files.Count==0&&mem.Ops.Contains("delete:"+onCfg.PathIn("iptv-status.json"))&&mem.Ops.Contains("delete:"+onCfg.PathIn("ui-action.request")),"stale-session cleanup goes through the session-file seam (memory only)");
 }

 static void InputLayoutTests(){
  var l64=InputEventLayout.For(8);var l32=InputEventLayout.For(4);
  Check(l64.Size==24&&l64.TypeOffset==16&&l32.Size==16&&l32.TypeOffset==8,"input_event layout follows pointer size");
  foreach(int size in new[]{0,2,16}){bool threw=false;try{InputEventLayout.For(size);}catch(PlatformNotSupportedException){threw=true;}Check(threw,"unsupported pointer size refused: "+size);}
  byte[] Events(InputEventLayout l,params (ushort Type,ushort Code,int Value)[] events){var b=new byte[l.Size*events.Length];for(int i=0;i<events.Length;i++){BitConverter.GetBytes(events[i].Type).CopyTo(b,i*l.Size+l.TypeOffset);BitConverter.GetBytes(events[i].Code).CopyTo(b,i*l.Size+l.TypeOffset+2);BitConverter.GetBytes(events[i].Value).CopyTo(b,i*l.Size+l.TypeOffset+4);}return b;}
  foreach(var l in new[]{l64,l32}){var b=Events(l,(1,63,1),(0,0,0),(1,63,0),(1,28,1),(1,63,2),(1,63,1));Check(l.CountHomePresses(b,b.Length,63)==2,"Home presses counted for "+l.Size+"-byte events");Check(l.CountHomePresses(b,b.Length-1,63)==1,"partial trailing event ignored for "+l.Size+"-byte events");}
  var wrong=Events(l32,(1,63,1),(1,63,1),(1,63,1));Check(l64.CountHomePresses(wrong,wrong.Length,63)!=3,"64-bit layout does not misread 32-bit events as Home");
 }
}
