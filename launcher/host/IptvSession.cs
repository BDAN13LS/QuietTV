using System.Diagnostics;
using System.Text.Json;
internal sealed class IptvSession : IDisposable {
 readonly HostConfig config;readonly IProcessStarter starter;readonly ISessionFiles files;readonly string Root;
 Process process;long closeDeadline;
 internal IptvSession(HostConfig config,IProcessStarter starter,ISessionFiles files){this.config=config;this.starter=starter;this.files=files;Root=config.RuntimeRoot;}
 public bool Alive=>process!=null&&IsOurProcess(config,files,process.Id);
 internal static bool IsOurProcess(HostConfig config,ISessionFiles files,int pid){try{return CommandMatches(config,files.ReadAllText("/proc/"+pid+"/cmdline"));}catch{return false;}}
 internal static bool CommandMatches(HostConfig config,string command){var parts=command.Split('\0');return parts.Length>=2&&parts[0]=="/usr/bin/dotnet"&&parts[1]==config.IptvDll;}
 public int? Pid=>Alive?process.Id:null;
 public bool Closing=>closeDeadline!=0;
 public bool Ready=>Alive&&files.Exists(Root+"/iptv-status.json")&&ReadyFromJson(process.Id,files.ReadAllText(Root+"/iptv-status.json"));
 internal static bool ReadyFromJson(int pid,string text){try{using var document=JsonDocument.Parse(text);var root=document.RootElement;return root.GetProperty("pid").GetInt32()==pid&&root.GetProperty("ready").GetString()=="complete"&&root.GetProperty("title").GetString()=="IPTV";}catch{return false;}}
 public void Start(){if(!config.IptvEnabled)throw new InvalidOperationException("IPTV is disabled by the host config"); // GUARD:iptv-start
  if(Alive)throw new InvalidOperationException("Previous IPTV session is still closing");process?.Dispose();closeDeadline=0;foreach(string name in new[]{"iptv-close.request","iptv-status.json"})if(files.Exists(Root+"/"+name))files.Delete(Root+"/"+name);var info=new ProcessStartInfo("/usr/bin/dotnet"){UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true};info.ArgumentList.Add(config.IptvDll);process=starter.Start(info);if(process==null)throw new InvalidOperationException("IPTV process did not start");process.OutputDataReceived+=(s,e)=>{if(e.Data?.StartsWith("fatal=")==true)Console.WriteLine("iptv_"+e.Data);};process.ErrorDataReceived+=(s,e)=>{};process.BeginOutputReadLine();process.BeginErrorReadLine();Console.WriteLine("iptv_session_pid="+process.Id);}
 public void Stop(){if(!Alive)return;files.WriteAllText(Root+"/iptv-close.request","");if(closeDeadline==0)closeDeadline=Environment.TickCount64+2500;}
 public void Poll(){if(Alive&&Closing&&Environment.TickCount64>closeDeadline){process.Kill();Console.WriteLine("iptv_session_forced_close="+process.Id);closeDeadline=Environment.TickCount64+2500;}if(!Alive)closeDeadline=0;}
 public void Dispose(){if(Alive){Stop();if(!process.WaitForExit(1500))process.Kill();}process?.Dispose();}
 internal static void Tests(HostConfig config){string Root=config.RuntimeRoot;string valid="{\"pid\":42,\"ready\":\"complete\",\"title\":\"IPTV\"}";if(!ReadyFromJson(42,valid)||ReadyFromJson(43,valid)||ReadyFromJson(42,valid.Replace("complete","loading"))||ReadyFromJson(42,"null")||ReadyFromJson(42,"{}")||ReadyFromJson(42,"bad"))throw new Exception("IPTV readiness must match the live child, not a stale file");if(!CommandMatches(config,"/usr/bin/dotnet\0"+Root+"/QuietIPTV.dll\0")||CommandMatches(config,"/usr/bin/dotnet\0/other/QuietIPTV.dll\0")||CommandMatches(config,""))throw new Exception("IPTV process identity mismatch");Console.WriteLine("PASS 9 IPTV readiness and process identity assertions");}
}
