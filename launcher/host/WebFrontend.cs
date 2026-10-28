using System.Text.Json;
// The web dashboard viewer (QuietIPTV.dll --dashboard) as a LONG-LIVED Ecore-native helper. Like IptvSession
// it owns NO System.Diagnostics.Process: the viewer can die and be restarted while the ecore loop pumps, and
// a .NET-owned child exiting then aborts the controller (ProcessWaitState vs ecore SIGCHLD -> SIGABRT). Here
// ecore_exe owns the child, exit shows up as !Alive (ecore DEL), and nothing managed is waited on.
internal sealed class WebFrontend : IDisposable {
 readonly HostConfig config;readonly IHelperStarter helpers;readonly ISessionFiles files;readonly string Root;
 ILongLivedHelper viewer;
 internal WebFrontend(HostConfig config,IHelperStarter helpers,ISessionFiles files){this.config=config;this.helpers=helpers;this.files=files;Root=config.RuntimeRoot;}
 public bool Alive=>viewer!=null&&viewer.Alive&&IptvSession.IsOurProcess(config,files,viewer.Pid);
 public bool Ready=>Alive&&files.Exists(Root+"/ui-ready")&&files.ReadAllText(Root+"/ui-ready").Trim()==viewer.Pid.ToString();
 public bool Hidden=>Alive&&files.Exists(Root+"/ui-hidden")&&files.ReadAllText(Root+"/ui-hidden").Trim()==viewer.Pid.ToString();
 public void Start(bool hidden=false){if(!config.WebUiEnabled||!config.IptvEnabled)throw new InvalidOperationException("Web UI is disabled by the host config (it runs QuietIPTV.dll, so it needs webUiEnabled and iptvEnabled)"); // GUARD:webui-start
  foreach(string name in new[]{"ui-ready","ui-hidden","ui-close.request","ui-show.request","ui-hide.request","ui-action.request","ui-message.request"})if(files.Exists(Root+"/"+name))files.Delete(Root+"/"+name);var args=hidden?new[]{config.IptvDll,"--dashboard","--start-hidden"}:new[]{config.IptvDll,"--dashboard"};viewer=helpers.Start("/usr/bin/dotnet",args);if(viewer==null||!viewer.Started)throw new InvalidOperationException("Web UI process did not start");Console.WriteLine("web_ui_pid="+viewer.Pid);}
 public void Show(){files.WriteAllText(Root+"/ui-show.request","");}
 public void Hide(){if(files.Exists(Root+"/ui-hidden"))files.Delete(Root+"/ui-hidden");files.WriteAllText(Root+"/ui-hide.request","");}
 public void Message(string text){files.WriteAllText(Root+"/ui-message.request",text);}
 public string ReadAction(){foreach(var line in viewer?.DrainLines()??Array.Empty<string>())if(line.StartsWith("fatal="))Console.WriteLine("web_ui_"+line);string path=Root+"/ui-action.request";if(!files.Exists(path))return null;if(files.Length(path)>2048){files.Delete(path);return null;}string text=files.ReadAllText(path);files.Delete(path);return ParseAction(text);}
 internal static string ParseAction(string text){if(text==null||text.Length>2048)return null;try{using var json=JsonDocument.Parse(text);var value=json.RootElement;if(value.ValueKind!=JsonValueKind.Object||!value.TryGetProperty("type",out var type)||type.ValueKind!=JsonValueKind.String)return null;string action=type.GetString();if(action=="home")return "@home";if(action=="close")return "@exit";if(action=="launch"&&value.TryGetProperty("id",out var id)&&id.ValueKind==JsonValueKind.String){string app=id.GetString();return !string.IsNullOrWhiteSpace(app)&&!app.StartsWith('@')?app:null;}return null;}catch(JsonException){return null;}}
 internal static void Tests(){int count=0;void Check(string input,string expected){count++;if(ParseAction(input)!=expected)throw new Exception("Web action validation failed");}Check("{\"type\":\"home\"}","@home");Check("{\"type\":\"close\"}","@exit");Check("{\"type\":\"launch\",\"id\":\"org.tizen.primevideo\"}","org.tizen.primevideo");foreach(string input in new[]{"null","[]","{broken}","{\"type\":4}","{\"type\":\"launch\",\"id\":1}","{\"type\":\"launch\",\"id\":\"@exit\"}","{\"type\":\"exec\",\"command\":\"reboot\"}",new string('x',2049)})Check(input,null);Console.WriteLine("PASS "+count+" web action assertions");}
 // Cooperative close request; the helper's Dispose is non-blocking and never waits on or kills a tree.
 public void Dispose(){if(Alive)files.WriteAllText(Root+"/ui-close.request","");viewer?.Dispose();}
}
