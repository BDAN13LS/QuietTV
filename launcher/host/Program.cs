using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using static Native;
static class Program {
 // Public host: every device value comes from the validated HostConfig (--config PATH).
 static HostConfig config;static string Root=>config.RuntimeRoot;static InputEventLayout inputLayout;static readonly IProcessStarter starter=new SystemProcessStarter();static readonly ISessionFiles sessionFiles=new DiskSessionFiles();
 static long homeEpoch;static string lastOrdinary="";static bool registeredUi;static bool systemHome;static WebFrontend web;static IptvSession iptv;static bool iptvVisible;static bool webReady;static long webStarted,handoffDeadline;static State state;static Canvas canvas;static bool hidden,animating,dismissed;static long lastKey,animationStart,launchDeadline;static string pendingApp="";static LaunchRoute pendingRoute;static readonly EventCallback EventHandler=OnEvent;static readonly Native.TimerCallback Heartbeat=Tick;static readonly Native.TimerCallback SnapshotStart=StartSnapshot;static readonly Native.TimerCallback SnapshotSave=SaveSnapshot;static readonly Native.TimerCallback FocusAnimation=Animate;static readonly Native.TimerCallback HomeActivation=ActivateLater;static readonly Native.TimerCallback LaunchWatch=CheckLaunch;
 [DllImport("libc",SetLastError=true)]static extern int open(string path,int flags);
 [DllImport("libc",SetLastError=true)]static extern int read(int fd,byte[] buffer,int count);
 [DllImport("libc")]static extern int close(int fd);
 [DllImport("/usr/lib/libvconf.so.0")]static extern IntPtr vconf_get_str(string key);
 [DllImport("libc")]static extern void free(IntPtr ptr);
 [DllImport("libecore.so.1")]static extern void ecore_main_loop_thread_safe_call_async(ActionCallback callback,IntPtr data);
 [UnmanagedFunctionPointer(CallingConvention.Cdecl)]delegate void ActionCallback(IntPtr data);
 static readonly ActionCallback ActivateCallback=Activate;
 static void Save(){var p=Root+"/settings.json";File.WriteAllText(p+".tmp",JsonSerializer.Serialize(state.Prefs));File.Move(p+".tmp",p,true);}
 static void Activate(IntPtr p){if(p==IntPtr.Zero){ecore_timer_add(0.25,HomeActivation,new IntPtr(1));return;}homeEpoch=DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
  // If Home cancels an in-flight ordinary-app (catalogue, app_launcher) launch, the OS may already have
  // accepted it and surface it ~3 s later over Home. Arm the one-shot guard on that exact runtime id.
  if(pendingApp!=""&&pendingRoute==LaunchRoute.AppLauncher&&state.Apps.Any(a=>a.Id==pendingApp))homeGuard.Arm(State.RuntimeId(pendingApp),config.OwnAppId,Environment.TickCount64+HomeReturnGuard.WindowMs);
  commands.Cancel();launchOwner.Retire();pendingApp="";iptvVisible=false;iptv.Stop();dismissed=false;hidden=false;state.Open("Home");canvas.Render(state);if(webReady)web.Show();else if(!registeredUi)canvas.Show();Console.WriteLine("home_activated");}
 static void ReassertHome(){Console.WriteLine("home_guard_reclaim="+config.OwnAppId);commands.Enqueue(new CommandSpec{Label="home_reclaim",TargetId=config.OwnAppId,Info=AppLauncher.Launch(config.OwnerUidText,config.OwnAppId)});commands.Pump(OnCommandDone);}
 static void Dismiss(){homeGuard.Cancel();commands.Cancel();launchOwner.Retire();pendingApp="";dismissed=true;hidden=true;canvas.Hide();if(webReady)web.Hide();Console.WriteLine("dashboard_dismissed");}
 static int ActivateLater(IntPtr p){Activate(p);return 0;}
 static void WatchHome(){// Observe only; never grab, block, or synthesize remote input.
  var fds=new List<int>();foreach(var p in config.ObservedInputPaths){int fd=open(p,2048|524288);if(fd>=0)fds.Add(fd);else Console.WriteLine("home_input_unavailable="+p);}
  var bytes=new byte[inputLayout.Size*32];while(true){foreach(int fd in fds){int n=read(fd,bytes,bytes.Length);for(int presses=inputLayout.CountHomePresses(bytes,n,config.HomeKeyCode);presses>0;presses--)ecore_main_loop_thread_safe_call_async(ActivateCallback,IntPtr.Zero);}Thread.Sleep(100);}
 }
 static int OnEvent(IntPtr d,int type,IntPtr info){try{if(hidden||webReady)return 1;string key=Marshal.PtrToStringAnsi(Marshal.ReadIntPtr(info))??"";long now=Environment.TickCount64;if(now-lastKey<95)return 0;lastKey=now;
  if(key is "Left" or "Right" or "Up" or "Down"){state.Move(key);animationStart=Environment.TickCount64;canvas.FocusProgress=0;if(!animating){animating=true;ecore_timer_add(1.0/60,FocusAnimation,IntPtr.Zero);}}else if(key is "Escape" or "XF86Back" or "BackSpace" or "XF86Exit")state.Back();else if(key is "Return" or "KP_Enter" or "Select"){
   string result=state.Select();Save();if(result=="@exit"){Dismiss();return 0;}if(result!=""){
    if(!LaunchApp(result))canvas.Render(state);return 0;
   }
  }else return 1;canvas.Render(state);return 0;
 }catch(Exception e){Console.WriteLine("input_error="+e.Message);state.Message="That action could not finish.";canvas.Render(state);return 0;}}
 static readonly Native.TimerCallback LaunchSubmit=SubmitLaunch;
 // Stop/launch run through the Ecore-native adapter (EcoreCommandRunner): spawned with ecore_exe and
 // completed from ecore's own ECORE_EXE_EVENT_DEL on the main loop, so no .NET Process is ever waited
 // on under an active ecore loop (that race aborts the controller with SIGABRT). launchOwner fences the
 // launch-submit and foreground-watch timers: each launch carries a token so an old timer that fires
 // after Home started a new launch cannot act on it. storeSettleUntil: the post-stop settle for the Store relaunch.
 static readonly LaunchOwner launchOwner=new();static long storeSettleUntil;
 // Reclaims our own registered home if an ordinary-app launch cancelled by Home surfaces late (the OS had
 // already accepted it). Armed in Activate for the cancelled app, observed in the heartbeat, reasserted
 // once via the existing app_launcher (-s own-id) command. See HomeReturnGuard.
 static readonly HomeReturnGuard homeGuard=new();
 static readonly CommandQueue commands=new(StartCommand);
 static CommandRun StartCommand(CommandSpec spec)=>EcoreCommandRunner.Begin(spec);
 // Runs on the ecore thread (called from a timer via commands.Pump), so UI calls here are safe. A stale
 // completion (generation moved on after Home/dismiss) touches nothing: no late launch, no dashboard hide.
 static void OnCommandDone(CommandSpec spec,ProcessResult result,bool fresh){
  if(!fresh){Console.WriteLine("stale_command_dropped="+spec.Label+"="+spec.TargetId);return;}
  string body=(result.Stdout+result.Stderr).Replace('\n',' ')+(result.PipeHeld?" pipe_held":"");
  if(!spec.IsLaunch){if(result.TimedOut||!result.Started)Console.WriteLine("stop_unconfirmed="+spec.TargetId+" "+body);else Console.WriteLine(spec.Label+"="+spec.TargetId+" exit="+result.ExitCode+" result="+body);return;}
  // A nonzero exit or failed start restores the UI now; exit 0, a held pipe or a deadline-kill (request may
  // have been accepted) hands off to the 15 s foreground verification, which decides success for real.
  if((result.Started&&result.ExitCode==0&&!result.TimedOut)||result.TimedOut){Console.WriteLine((result.TimedOut?"launch_cli_timeout=":"launch_app=")+spec.TargetId+" exit="+result.ExitCode+" result="+body);launchDeadline=Environment.TickCount64+15000;launchOwner.HandOff(spec.OwnerToken);ecore_timer_add(0.1,LaunchWatch,new IntPtr(spec.OwnerToken));return;}
  Console.WriteLine("launch_app_failed="+spec.TargetId+" exit="+result.ExitCode+" result="+body);pendingApp="";launchOwner.Retire();hidden=false;state.Message="Could not open this app. Try another.";canvas.Render(state);if(webReady){web.Message(state.Message);web.Show();}else if(!registeredUi)canvas.Show();
 }
 // Every launch source (remote, web action, launch.request) passes LaunchGate first; a rejected id starts nothing.
 static bool LaunchApp(string id){if(pendingApp!="")return true;var route=LaunchGate.Route(config,state,id);if(route==LaunchRoute.Rejected){Console.WriteLine("launch_rejected="+(id.Length<=160?id:"(long)"));state.Message=id==LaunchGate.InputsTileId?"Inputs are not available here. Use the Source button on the remote.":"This app is not available.";return false;}homeGuard.Cancel();state.Message="";iptvVisible=false;iptv.Stop();string previous=state.PreviousToClose(id,ReadSetting("memory/appfw/current_fullscreen_appid"),lastOrdinary);if(previous!="")StopApp("close_previous",previous);foreach(string stale in State.StoreStopTargets(id))StopApp("close_store",stale);pendingApp=id;pendingRoute=route;int gen=launchOwner.Begin();storeSettleUntil=0;hidden=true;canvas.YieldInput();if(webReady)web.Hide();handoffDeadline=Environment.TickCount64+3000;ecore_timer_add(State.StoreStopTargets(id).Length>0?0.05:0.01,LaunchSubmit,new IntPtr(gen));return true;}
 static void StopApp(string label,string id){commands.Enqueue(new CommandSpec{Label=label,TargetId=id,Info=AppLauncher.Stop(config.OwnerUidText,id)});}
 static int SubmitLaunch(IntPtr data){int tok=data.ToInt32();if(!launchOwner.Owns(tok))return 0;if(pendingApp=="")return 0;commands.Pump(OnCommandDone);if(!launchOwner.Owns(tok)||pendingApp=="")return 0;if(launchOwner.Submitted){if(launchOwner.ConsumeHandoff(tok))return 0;return 1;}if(webReady&&!web.Hidden){if(Environment.TickCount64<handoffDeadline)return 1;Console.WriteLine("web_ui_hide_timeout");commands.Cancel();launchOwner.Retire();pendingApp="";hidden=false;web.Show();return 0;}if(iptv.Alive){iptv.Poll();if(Environment.TickCount64<handoffDeadline)return 1;iptv.Poll();return 1;}if(commands.Busy)return 1;string id=pendingApp;if(State.StoreStopTargets(id).Length>0){if(storeSettleUntil==0)storeSettleUntil=Environment.TickCount64+500;if(Environment.TickCount64<storeSettleUntil)return 1;}if(pendingRoute==LaunchRoute.Iptv){iptv.Start();iptvVisible=true;launchDeadline=Environment.TickCount64+20000;ecore_timer_add(0.1,LaunchWatch,new IntPtr(tok));return 0;}if(launchOwner.TrySubmit(tok)){commands.Enqueue(new CommandSpec{Label="launch_app",TargetId=id,Info=AppLauncher.Launch(config.OwnerUidText,State.LaunchId(id)),IsLaunch=true,OwnerToken=tok});commands.Pump(OnCommandDone);}return 1;}
 static string ReadSetting(string key){IntPtr value=vconf_get_str(key);if(value==IntPtr.Zero)return "";try{return Marshal.PtrToStringAnsi(value)??"";}finally{free(value);}}
 static int lastClockMinute=-1;
 static int Tick(IntPtr d){try{if(homeGuard.Armed&&!dismissed&&pendingApp==""&&homeGuard.Observe(ReadSetting("memory/appfw/current_fullscreen_appid"),Environment.TickCount64))ReassertHome();if(registeredUi){File.WriteAllText(Root+"/native-state.tmp",JsonSerializer.Serialize(new{pid=Environment.ProcessId,homeEpoch,ready=true,iptv=iptvVisible&&iptv.Alive,iptvPid=iptv.Pid,foreground=ReadSetting("memory/appfw/current_fullscreen_appid"),partial=ReadSetting("memory/appfw/current_partial_appid"),pending=pendingApp,message=state.Message}));File.Move(Root+"/native-state.tmp",Root+"/native-state.json",true);}iptv.Poll();if(iptvVisible&&pendingApp==""&&!iptv.Alive)Activate(new IntPtr(1));if(web!=null&&!webReady&&web.Ready){webReady=true;canvas.Hide();Console.WriteLine("web_ui_ready");}if(webReady&&!web.Alive){webReady=false;web.Dispose();web=new WebFrontend(config,starter,sessionFiles);web.Start(hidden);webStarted=Environment.TickCount64;Console.WriteLine("web_ui_restarting");}if(web!=null&&!webReady&&!hidden&&Environment.TickCount64-webStarted>20000)canvas.Show();if(webReady){string action=web.ReadAction();if(action=="@home")Activate(new IntPtr(1));else if(action=="@exit"){Dismiss();}else if(action!=null){if(!LaunchApp(action)){web.Message("This app is not available.");Console.WriteLine("unknown_app_rejected");}}}if(File.Exists(Root+"/show.request")){File.Delete(Root+"/show.request");Activate(new IntPtr(1));}if(File.Exists(Root+"/launch.request")){string id=File.ReadAllText(Root+"/launch.request").Trim();File.Delete(Root+"/launch.request");LaunchApp(id);}if(hidden&&!dismissed&&!iptvVisible&&pendingApp==""){string full=ReadSetting("memory/appfw/current_fullscreen_appid"),partial=ReadSetting("memory/appfw/current_partial_appid");if((full=="com.samsung.tv.csfs"||(systemHome&&full==config.OwnAppId))&&(partial==""||partial==full))Activate(new IntPtr(1));}else if(!hidden&&lastClockMinute!=DateTime.Now.Minute){lastClockMinute=DateTime.Now.Minute;canvas.Render(state);}}catch(Exception e){Console.WriteLine("watch_error="+e.Message);}return 1;}
 static int CheckLaunch(IntPtr d){if(!launchOwner.Owns(d.ToInt32()))return 0;if(pendingApp=="")return 0;bool isIptv=pendingRoute==LaunchRoute.Iptv;string full=ReadSetting("memory/appfw/current_fullscreen_appid"),partial=ReadSetting("memory/appfw/current_partial_appid");if((isIptv&&iptv.Ready)||(!isIptv&&State.PresentationMatches(pendingApp,full,partial))){if(!isIptv&&state.Apps.Any(a=>State.RuntimeId(a.Id)==full)){lastOrdinary=full;File.WriteAllText(Root+"/last-ordinary-app",full);}Console.WriteLine((isIptv?"iptv_window_verified=":"foreground_verified=")+pendingApp+" actual="+full+" partial="+partial);pendingApp="";hidden=true;canvas.Hide();return 0;}if(Environment.TickCount64>launchDeadline){Console.WriteLine("launch_timeout="+pendingApp+" full="+full+" partial="+partial);if(isIptv){iptvVisible=false;iptv.Stop();}pendingApp="";hidden=false;state.Message="The app did not reach the foreground. Press Home to return.";canvas.Render(state);if(webReady){web.Message(state.Message);web.Show();}else if(!registeredUi)canvas.Show();return 0;}return 1;}
 static int Animate(IntPtr d){double t=Math.Min(1,(Environment.TickCount64-animationStart)/150.0);canvas.FocusProgress=1-Math.Pow(1-t,3);if(!hidden)canvas.Render(state);if(t>=1){animating=false;return 0;}return 1;}
 static int StartSnapshot(IntPtr d){canvas.BeginSnapshot();ecore_timer_add(1,SnapshotSave,IntPtr.Zero);return 0;}
 static int SaveSnapshot(IntPtr d){canvas.SaveSnapshot(Root+"/screen.png");return 0;}
 sealed class NativeHostEffects : IHostEffects {
  public void StartHomeObserver()=>new Thread(WatchHome){IsBackground=true}.Start();
  public void StartRemoteGate()=>new Thread(()=>{try{using var gate=new RemoteGate(config.RemoteDeviceNames);gate.Run(config.HomeKeyCode,()=>ecore_main_loop_thread_safe_call_async(ActivateCallback,new IntPtr(1)));}catch(Exception e){Console.WriteLine("home_gate_unavailable="+e.Message);WatchHome();}}){IsBackground=true}.Start();
  public void AddTimer(string purpose,double seconds,Native.TimerCallback callback)=>ecore_timer_add(seconds,callback,IntPtr.Zero);
  public void StartWebUi(bool startHidden){web=new WebFrontend(config,starter,sessionFiles);webStarted=Environment.TickCount64;web.Start(startHidden);}
  public void Log(string line)=>Console.WriteLine(line);
 }
 internal static string ConfigPath(string[] args){int i=Array.IndexOf(args,"--config");return i>=0&&i+1<args.Length?args[i+1]:null;}
 static int Main(string[] args){if(args.Contains("--self-test"))return SelfTest.Run();
  // Validate config, input layout and owner data before any native call or filesystem write.
  List<TvApp> apps;Preferences prefs;
  try{config=HostConfig.LoadFile(ConfigPath(args));inputLayout=InputEventLayout.For(IntPtr.Size);(apps,prefs)=OwnerData.Load(config,new DiskOwnerFiles());}
  catch(Exception e)when(e is HostConfigException or PlatformNotSupportedException){Console.WriteLine("config_rejected="+e.Message);return 2;}
  try{
  registeredUi=args.Contains("--registered-ui");systemHome=args.Contains("--system-home");var flags=HostStartup.FlagsFrom(args);state=new State(apps,prefs,config.OwnAppId,config.IptvEnabled);iptv=new IptvSession(config,starter,sessionFiles);File.WriteAllText(Root+"/dashboard.pid",Environment.ProcessId.ToString());if(File.Exists(Root+"/last-ordinary-app")){var remembered=File.ReadAllText(Root+"/last-ordinary-app").Trim();if(remembered.Length<=256)lastOrdinary=remembered;}canvas=new Canvas(config);var lib=NativeLibrary.Load("libecore_input.so.1");ecore_event_handler_add(Marshal.ReadInt32(NativeLibrary.GetExport(lib,"ECORE_EVENT_KEY_DOWN")),EventHandler,IntPtr.Zero);canvas.Render(state);string foreground=ReadSetting("memory/appfw/current_fullscreen_appid");hidden=args.Contains("--preserve-app")&&foreground!=""&&foreground!="com.samsung.tv.csfs"&&foreground!=config.OwnAppId;if(!registeredUi&&!hidden&&!flags.QuietStart)canvas.Show();HostStartup.Run(config,flags,new NativeHostEffects(),Heartbeat,SnapshotStart,hidden);Console.WriteLine("quiet_tv_ready");ecore_main_loop_begin();iptv.Dispose();web?.Dispose();canvas.Hide();Console.WriteLine("quiet_tv_closed");return 0;
 }catch(Exception e){Console.WriteLine("fatal="+e);return 1;}}
}
