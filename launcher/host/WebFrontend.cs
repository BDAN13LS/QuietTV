using System.Diagnostics;
using System.Text.Json;
internal sealed class WebFrontend : IDisposable {
 readonly HostConfig config;readonly IProcessStarter starter;readonly ISessionFiles files;readonly string Root;
 Process viewer;
 internal WebFrontend(HostConfig config,IProcessStarter starter,ISessionFiles files){this.config=config;this.starter=starter;this.files=files;Root=config.RuntimeRoot;}
 public bool Alive=>viewer!=null&&IptvSession.IsOurProcess(config,files,viewer.Id);
 public bool Ready=>Alive&&files.Exists(Root+"/ui-ready")&&files.ReadAllText(Root+"/ui-ready").Trim()==viewer.Id.ToString();
 public bool Hidden=>Alive&&files.Exists(Root+"/ui-hidden")&&files.ReadAllText(Root+"/ui-hidden").Trim()==viewer.Id.ToString();
 public void Start(bool hidden=false){if(!config.WebUiEnabled||!config.IptvEnabled)throw new InvalidOperationException("Web UI is disabled by the host config (it runs QuietIPTV.dll, so it needs webUiEnabled and iptvEnabled)"); // GUARD:webui-start
  foreach(string name in new[]{"ui-ready","ui-hidden","ui-close.request","ui-show.request","ui-hide.request","ui-action.request","ui-message.request"})if(files.Exists(Root+"/"+name))files.Delete(Root+"/"+name);var info=new ProcessStartInfo("/usr/bin/dotnet"){UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true};info.ArgumentList.Add(config.IptvDll);info.ArgumentList.Add("--dashboard");if(hidden)info.ArgumentList.Add("--start-hidden");viewer=starter.Start(info);if(viewer==null)throw new InvalidOperationException("Web UI process did not start");viewer.OutputDataReceived+=(s,e)=>{if(e.Data?.StartsWith("fatal=")==true)Console.WriteLine("web_ui_"+e.Data);};viewer.ErrorDataReceived+=(s,e)=>{};viewer.BeginOutputReadLine();viewer.BeginErrorReadLine();Console.WriteLine("web_ui_pid="+viewer.Id);}
 public void Show(){files.WriteAllText(Root+"/ui-show.request","");}
 public void Hide(){if(files.Exists(Root+"/ui-hidden"))files.Delete(Root+"/ui-hidden");files.WriteAllText(Root+"/ui-hide.request","");}
 public void Message(string text){files.WriteAllText(Root+"/ui-message.request",text);}
 public string ReadAction(){string path=Root+"/ui-action.request";if(!files.Exists(path))return null;if(files.Length(path)>2048){files.Delete(path);return null;}string text=files.ReadAllText(path);files.Delete(path);return ParseAction(text);}
 internal static string ParseAction(string text){if(text==null||text.Length>2048)return null;try{using var json=JsonDocument.Parse(text);var value=json.RootElement;if(value.ValueKind!=JsonValueKind.Object||!value.TryGetProperty("type",out var type)||type.ValueKind!=JsonValueKind.String)return null;string action=type.GetString();if(action=="home")return "@home";if(action=="close")return "@exit";if(action=="launch"&&value.TryGetProperty("id",out var id)&&id.ValueKind==JsonValueKind.String){string app=id.GetString();return !string.IsNullOrWhiteSpace(app)&&!app.StartsWith('@')?app:null;}return null;}catch(JsonException){return null;}}
 internal static void Tests(){int count=0;void Check(string input,string expected){count++;if(ParseAction(input)!=expected)throw new Exception("Web action validation failed");}Check("{\"type\":\"home\"}","@home");Check("{\"type\":\"close\"}","@exit");Check("{\"type\":\"launch\",\"id\":\"org.tizen.primevideo\"}","org.tizen.primevideo");foreach(string input in new[]{"null","[]","{broken}","{\"type\":4}","{\"type\":\"launch\",\"id\":1}","{\"type\":\"launch\",\"id\":\"@exit\"}","{\"type\":\"exec\",\"command\":\"reboot\"}",new string('x',2049)})Check(input,null);Console.WriteLine("PASS "+count+" web action assertions");}
 public void Dispose(){if(Alive)files.WriteAllText(Root+"/ui-close.request","");viewer?.Dispose();}
}
