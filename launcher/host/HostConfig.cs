// The single configuration seam for the public host. Every device-specific value
// (runtime root, owner uid, own app id, input devices, Home key, feature flags)
// comes from one owner-generated JSON file named by --config, validated here
// before any native call or filesystem write. No environment fallback.
using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

internal sealed class HostConfigException : Exception {
 internal HostConfigException(string message):base(message){}
}

internal sealed class HostConfig {
 internal const string Schema="quiettv-host-config/1";
 internal static readonly string[] RootPrefixes={"/home/owner/share/","/opt/usr/home/owner/share/"};
 internal static readonly IReadOnlySet<string> SystemLaunchIds=new HashSet<string>{"com.samsung.tv.quicksetting","com.samsung.tv.store"};
 internal const string IptvAppId="quiet.iptv";
 static readonly string[] Known={"schema","runtimeRoot","ownerUid","ownAppId","homeKeyCode","observedInputPaths","remoteDeviceNames","remoteGrabEnabled","iptvEnabled","webUiEnabled","snapshotEnabled"};
 static readonly string[] Placeholders={"changeme","change_me","change-me","replace","placeholder","todo","example","xxxx","your-","your_"};

 internal string RuntimeRoot {get;}
 internal int OwnerUid {get;}
 internal string OwnAppId {get;}
 internal int HomeKeyCode {get;}
 internal IReadOnlyList<string> ObservedInputPaths {get;}
 internal IReadOnlyList<string> RemoteDeviceNames {get;}
 internal bool RemoteGrabEnabled {get;}
 internal bool IptvEnabled {get;}
 internal bool WebUiEnabled {get;}
 internal bool SnapshotEnabled {get;}
 internal string OwnerUidText=>OwnerUid.ToString(CultureInfo.InvariantCulture);
 internal string PathIn(string name)=>RuntimeRoot+"/"+name;
 internal string IptvDll=>PathIn("QuietIPTV.dll");

 HostConfig(string root,int uid,string appId,int home,IReadOnlyList<string> paths,IReadOnlyList<string> names,bool grab,bool iptv,bool web,bool snapshot){RuntimeRoot=root;OwnerUid=uid;OwnAppId=appId;HomeKeyCode=home;ObservedInputPaths=paths;RemoteDeviceNames=names;RemoteGrabEnabled=grab;IptvEnabled=iptv;WebUiEnabled=web;SnapshotEnabled=snapshot;}

 internal static HostConfig LoadFile(string path){
  if(string.IsNullOrWhiteSpace(path))throw new HostConfigException("--config PATH is required");
  string text;try{if(new FileInfo(path).Length>65536)throw new HostConfigException("config file too large");text=File.ReadAllText(path);}catch(HostConfigException){throw;}catch(Exception e){throw new HostConfigException("config file unreadable: "+e.GetType().Name);}
  return Parse(text);
 }

 internal static HostConfig Parse(string text){
  var root=StrictJson.ParseObject(text,"config");
  StrictJson.OnlyKnown(root,Known,"config");
  if(StrictJson.RequiredString(root,"schema")!=Schema)throw new HostConfigException("schema must be "+Schema);
  string runtimeRoot=ValidateRoot(StrictJson.RequiredString(root,"runtimeRoot"));
  int uid=StrictJson.RequiredInt(root,"ownerUid");if(uid<5000||uid>5999)throw new HostConfigException("ownerUid must be a Tizen owner uid (5000-5999)");
  string appId=StrictJson.RequiredString(root,"ownAppId");if(!Regex.IsMatch(appId,@"^[A-Za-z0-9]{10}\.[A-Za-z][A-Za-z0-9_]{0,49}$")||IsPlaceholder(appId)||appId.Split('.')[0].Distinct().Count()==1)throw new HostConfigException("ownAppId must be a real Tizen app id (10-char package id + name)");
  int home=StrictJson.RequiredInt(root,"homeKeyCode");if(home<1||home>0x2ff)throw new HostConfigException("homeKeyCode must be 1..767");
  var paths=StrictJson.StringList(root,"observedInputPaths",required:true);
  if(paths.Count<1||paths.Count>8)throw new HostConfigException("observedInputPaths needs 1-8 entries");
  foreach(var p in paths)if(!Regex.IsMatch(p,@"^/dev/input/event[0-9]{1,3}$"))throw new HostConfigException("observedInputPaths entries must be /dev/input/eventN");
  var names=StrictJson.StringList(root,"remoteDeviceNames",required:false);
  if(names.Count>8)throw new HostConfigException("remoteDeviceNames allows at most 8 entries");
  foreach(var n in names)if(n.Length<1||n.Length>64||n.Any(c=>c<0x20||c>0x7e||c=='"'||c=='\\')||IsPlaceholder(n))throw new HostConfigException("remoteDeviceNames entries must be 1-64 printable characters without quotes");
  bool grab=StrictJson.OptionalBool(root,"remoteGrabEnabled"),iptv=StrictJson.OptionalBool(root,"iptvEnabled"),web=StrictJson.OptionalBool(root,"webUiEnabled"),snapshot=StrictJson.OptionalBool(root,"snapshotEnabled");
  if(grab&&names.Count==0)throw new HostConfigException("remoteGrabEnabled requires remoteDeviceNames");
  return new HostConfig(runtimeRoot,uid,appId,home,paths.AsReadOnly(),names.AsReadOnly(),grab,iptv,web,snapshot);
 }

 // String-only validation: Path.GetFullPath would map these POSIX paths onto C:\ on Windows.
 internal static string ValidateRoot(string root){
  if(root.Length>200||root.Contains('\\')||root.Contains('\0'))throw new HostConfigException("runtimeRoot is not a plain POSIX path");
  if(IsPlaceholder(root))throw new HostConfigException("runtimeRoot is still a placeholder");
  string prefix=RootPrefixes.FirstOrDefault(p=>root.StartsWith(p,StringComparison.Ordinal));
  if(prefix==null)throw new HostConfigException("runtimeRoot must live below /home/owner/share/");
  var segments=root[prefix.Length..].Split('/');
  foreach(var s in segments)if(s.Length==0||s=="."||s==".."||!Regex.IsMatch(s,@"^[A-Za-z0-9._-]+$"))throw new HostConfigException("runtimeRoot has an empty, relative or unsafe segment");
  if(!Regex.IsMatch(segments[^1],@"^quiet-tv(-[a-z0-9]{1,24})?$"))throw new HostConfigException("runtimeRoot must end in the host's own quiet-tv folder");
  return root;
 }

 internal static bool IsPlaceholder(string value){string v=value.ToLowerInvariant();return v.IndexOfAny(new[]{'<','>','{','}','$','%'})>=0||Placeholders.Any(p=>v.Contains(p,StringComparison.Ordinal));}
}

// Owner-local data: apps.json (catalogue) and settings.json (preferences) under the
// validated root. Read-only: this type has no write path, so a rejected load mutates nothing.
internal interface IOwnerFiles {bool Exists(string path);string ReadAllText(string path);}
internal sealed class DiskOwnerFiles : IOwnerFiles {
 public bool Exists(string path)=>File.Exists(path);
 public string ReadAllText(string path){try{if(new FileInfo(path).Length>1048576)throw new HostConfigException("owner file too large: "+Path.GetFileName(path));return File.ReadAllText(path);}catch(HostConfigException){throw;}catch(Exception e){throw new HostConfigException("owner file unreadable: "+Path.GetFileName(path)+" ("+e.GetType().Name+")");}}
}

internal static class OwnerData {
 internal const int MaxApps=200;
 internal static (List<TvApp> Apps,Preferences Prefs) Load(HostConfig config,IOwnerFiles files){
  string appsPath=config.PathIn("apps.json"),settingsPath=config.PathIn("settings.json");
  if(!files.Exists(appsPath))throw new HostConfigException("apps.json missing in runtime root (owner must generate it)");
  if(!files.Exists(settingsPath))throw new HostConfigException("settings.json missing in runtime root (owner must generate it)");
  var apps=ParseApps(files.ReadAllText(appsPath),config);
  var prefs=ParseSettings(files.ReadAllText(settingsPath),apps);
  return (apps,prefs);
 }
 internal static bool ValidAppId(string id)=>Regex.IsMatch(id,@"^[A-Za-z0-9_.-]{1,160}$")&&!id.StartsWith('.')&&!id.Contains("..");
 internal static List<TvApp> ParseApps(string text,HostConfig config){
  using var doc=StrictJson.Parse(text,"apps.json");
  if(doc.RootElement.ValueKind!=JsonValueKind.Array)throw new HostConfigException("apps.json must be an array");
  var apps=new List<TvApp>();var seen=new HashSet<string>(StringComparer.Ordinal);
  foreach(var item in doc.RootElement.EnumerateArray()){
   if(item.ValueKind!=JsonValueKind.Object)throw new HostConfigException("apps.json entries must be objects");
   StrictJson.OnlyKnown(item,new[]{"Id","Name","Icon"},"apps.json entry");
   string id=StrictJson.RequiredString(item,"Id"),name=StrictJson.RequiredString(item,"Name"),icon=item.TryGetProperty("Icon",out var i)?StrictJson.AsString(i,"Icon"):"";
   if(!ValidAppId(id))throw new HostConfigException("apps.json has an unsafe app id");
   if(!seen.Add(id))throw new HostConfigException("apps.json has a duplicate app id");
   if(name.Length<1||name.Length>64||name.Any(char.IsControl))throw new HostConfigException("apps.json has an invalid app name");
   if(icon!=""){var rel=icon.StartsWith(config.RuntimeRoot+"/",StringComparison.Ordinal)?icon[(config.RuntimeRoot.Length+1)..]:null;if(rel==null||rel.Split('/').Any(s=>s.Length==0||s=="."||s==".."||!Regex.IsMatch(s,@"^[A-Za-z0-9._-]+$")))throw new HostConfigException("apps.json icon must be a file inside the runtime root");}
   apps.Add(new TvApp(id,name,icon));
  }
  if(apps.Count==0||apps.Count>MaxApps)throw new HostConfigException("apps.json must list 1-200 apps");
  return apps;
 }
 internal static Preferences ParseSettings(string text,List<TvApp> apps){
  var root=StrictJson.ParseObject(text,"settings.json");
  // HomeKeyCode is accepted for files written by the earlier host and ignored: the Home key lives in the host config.
  StrictJson.OnlyKnown(root,new[]{"Order","Favorites","Wallpaper","HomeKeyCode"},"settings.json");
  var order=StrictJson.StringList(root,"Order",required:false);var favorites=StrictJson.StringList(root,"Favorites",required:false);
  foreach(var list in new[]{order,favorites}){if(list.Any(id=>!ValidAppId(id)))throw new HostConfigException("settings.json has an unsafe app id");if(list.Distinct().Count()!=list.Count)throw new HostConfigException("settings.json has a duplicate app id");}
  int wallpaper=root.TryGetProperty("Wallpaper",out var w)?StrictJson.AsInt(w,"Wallpaper"):0;if(wallpaper<0||wallpaper>2)throw new HostConfigException("settings.json Wallpaper must be 0-2");
  if(root.TryGetProperty("HomeKeyCode",out var h))StrictJson.AsInt(h,"HomeKeyCode");
  // Well-formed ids for apps no longer installed are dropped (normal drift), not rejected.
  return new Preferences{Order=order.Where(id=>apps.Any(a=>a.Id==id)).ToList(),Favorites=favorites.Where(id=>apps.Any(a=>a.Id==id)).ToList(),Wallpaper=wallpaper};
 }
}

// System.Text.Json accepts duplicate keys (last wins); these helpers refuse them, unknown keys, nulls and wrong types.
internal static class StrictJson {
 internal static JsonDocument Parse(string text,string what){
  if(text==null)throw new HostConfigException(what+" is empty");
  JsonDocument doc;try{doc=JsonDocument.Parse(text,new JsonDocumentOptions{MaxDepth=16});}catch(JsonException){throw new HostConfigException(what+" is not valid JSON");}
  try{NoDuplicates(doc.RootElement,what);}catch{doc.Dispose();throw;}return doc;
 }
 internal static JsonElement ParseObject(string text,string what){using var doc=Parse(text,what);if(doc.RootElement.ValueKind!=JsonValueKind.Object)throw new HostConfigException(what+" must be a JSON object");return doc.RootElement.Clone();}
 static void NoDuplicates(JsonElement e,string what){
  if(e.ValueKind==JsonValueKind.Object){var names=new HashSet<string>(StringComparer.Ordinal);foreach(var p in e.EnumerateObject()){if(!names.Add(p.Name))throw new HostConfigException(what+" repeats the key "+p.Name);NoDuplicates(p.Value,what);}}
  else if(e.ValueKind==JsonValueKind.Array)foreach(var i in e.EnumerateArray())NoDuplicates(i,what);
 }
 internal static void OnlyKnown(JsonElement o,string[] known,string what){foreach(var p in o.EnumerateObject())if(!known.Contains(p.Name))throw new HostConfigException(what+" has an unknown key "+p.Name);}
 internal static string AsString(JsonElement e,string key)=>e.ValueKind==JsonValueKind.String?e.GetString():throw new HostConfigException(key+" must be a string");
 internal static int AsInt(JsonElement e,string key)=>e.ValueKind==JsonValueKind.Number&&e.TryGetInt32(out int v)?v:throw new HostConfigException(key+" must be an integer");
 internal static string RequiredString(JsonElement o,string key)=>o.TryGetProperty(key,out var e)?AsString(e,key):throw new HostConfigException(key+" is required");
 internal static int RequiredInt(JsonElement o,string key)=>o.TryGetProperty(key,out var e)?AsInt(e,key):throw new HostConfigException(key+" is required");
 internal static bool OptionalBool(JsonElement o,string key){if(!o.TryGetProperty(key,out var e))return false;return e.ValueKind switch{JsonValueKind.True=>true,JsonValueKind.False=>false,_=>throw new HostConfigException(key+" must be true or false")};}
 internal static List<string> StringList(JsonElement o,string key,bool required){
  if(!o.TryGetProperty(key,out var e)){if(required)throw new HostConfigException(key+" is required");return new();}
  if(e.ValueKind!=JsonValueKind.Array)throw new HostConfigException(key+" must be an array");
  var list=new List<string>();foreach(var i in e.EnumerateArray())list.Add(i.ValueKind==JsonValueKind.String?i.GetString():throw new HostConfigException(key+" entries must be strings"));
  if(list.Distinct(StringComparer.Ordinal).Count()!=list.Count)throw new HostConfigException(key+" has duplicates");
  return list;
 }
}
