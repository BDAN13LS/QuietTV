using System.Text.Json;
// The IPTV player (QuietIPTV.dll) as a LONG-LIVED Ecore-native helper. It owns NO System.Diagnostics.Process:
// the old path's Process.Kill()/WaitForExit()/BeginOutputReadLine on a child that exits while the ecore loop
// pumps races ecore's SIGCHLD reaper -> SIGABRT. Here ecore_exe owns the child, exit arrives as ecore DEL,
// a forced close is an identity-guarded exact-pid kill, and Dispose never blocks. Session files still go
// through ISessionFiles; the readiness/identity rules are unchanged.
internal sealed class IptvSession : IDisposable {
 readonly HostConfig config;readonly IHelperStarter helpers;readonly ISessionFiles files;readonly string Root;
 ILongLivedHelper process;long closeDeadline;
 internal IptvSession(HostConfig config,IHelperStarter helpers,ISessionFiles files){this.config=config;this.helpers=helpers;this.files=files;Root=config.RuntimeRoot;}
 public bool Alive=>process!=null&&process.Alive&&IsOurProcess(config,files,process.Pid);
 internal static bool IsOurProcess(HostConfig config,ISessionFiles files,int pid){try{return CommandMatches(config,files.ReadAllText("/proc/"+pid+"/cmdline"));}catch{return false;}}
 internal static bool CommandMatches(HostConfig config,string command){var parts=command.Split('\0');return parts.Length>=2&&parts[0]=="/usr/bin/dotnet"&&parts[1]==config.IptvDll;}
 public int? Pid=>Alive?process.Pid:null;
 public bool Closing=>closeDeadline!=0;
 public bool Ready=>Alive&&files.Exists(Root+"/iptv-status.json")&&ReadyFromJson(process.Pid,files.ReadAllText(Root+"/iptv-status.json"));
 internal static bool ReadyFromJson(int pid,string text){try{using var document=JsonDocument.Parse(text);var root=document.RootElement;return root.GetProperty("pid").GetInt32()==pid&&root.GetProperty("ready").GetString()=="complete"&&root.GetProperty("title").GetString()=="IPTV";}catch{return false;}}
 public void Start(){if(!config.IptvEnabled)throw new InvalidOperationException("IPTV is disabled by the host config"); // GUARD:iptv-start
  if(Alive)throw new InvalidOperationException("Previous IPTV session is still closing");process?.Dispose();closeDeadline=0;foreach(string name in new[]{"iptv-close.request","iptv-status.json"})if(files.Exists(Root+"/"+name))files.Delete(Root+"/"+name);process=helpers.Start("/usr/bin/dotnet",new[]{config.IptvDll});if(process==null||!process.Started)throw new InvalidOperationException("IPTV process did not start");Console.WriteLine("iptv_session_pid="+process.Pid);}
 public void Stop(){if(!Alive)return;files.WriteAllText(Root+"/iptv-close.request","");if(closeDeadline==0)closeDeadline=Environment.TickCount64+2500;}
 // Non-blocking poll on the ecore loop: drain the helper's bounded output for fatal lines, then (cooperative
 // close having had its grace) force the exact-pid kill. No managed wait ever lands on the UI loop.
 public void Poll(){Scan();if(Alive&&Closing&&Environment.TickCount64>closeDeadline){process.Kill();Console.WriteLine("iptv_session_forced_close="+process.Pid);closeDeadline=Environment.TickCount64+2500;}if(!Alive)closeDeadline=0;}
 void Scan(){if(process==null)return;foreach(var line in process.DrainLines())if(line.StartsWith("fatal="))Console.WriteLine("iptv_"+line);}
 // Cooperative close then a bounded exact-own-child kill; never WaitForExit (non-blocking).
 public void Dispose(){if(Alive){Stop();process.Kill();}process?.Dispose();}
 internal static void Tests(HostConfig config){string Root=config.RuntimeRoot;string valid="{\"pid\":42,\"ready\":\"complete\",\"title\":\"IPTV\"}";if(!ReadyFromJson(42,valid)||ReadyFromJson(43,valid)||ReadyFromJson(42,valid.Replace("complete","loading"))||ReadyFromJson(42,"null")||ReadyFromJson(42,"{}")||ReadyFromJson(42,"bad"))throw new Exception("IPTV readiness must match the live child, not a stale file");if(!CommandMatches(config,"/usr/bin/dotnet\0"+Root+"/QuietIPTV.dll\0")||CommandMatches(config,"/usr/bin/dotnet\0/other/QuietIPTV.dll\0")||CommandMatches(config,""))throw new Exception("IPTV process identity mismatch");Console.WriteLine("PASS 9 IPTV readiness and process identity assertions");}
}
