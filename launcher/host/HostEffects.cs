// Seams between the host's decisions and the native side effects they cause.
// Program.Main runs HostStartup.Run with NativeHostEffects; --self-test runs the same
// code with a recording fake, so a feature that is off is proven not to start.
using System.Diagnostics;

internal interface IProcessStarter {Process Start(ProcessStartInfo info);}
internal sealed class SystemProcessStarter : IProcessStarter {public Process Start(ProcessStartInfo info)=>Process.Start(info);}

// Every IPTV/web-viewer session file (and /proc identity read) goes through this, so the
// self-test can drive the real sessions against memory only and never touch the disk.
internal interface ISessionFiles {bool Exists(string path);string ReadAllText(string path);void WriteAllText(string path,string text);void Delete(string path);long Length(string path);}
internal sealed class DiskSessionFiles : ISessionFiles {
 public bool Exists(string path)=>File.Exists(path);
 public string ReadAllText(string path)=>File.ReadAllText(path);
 public void WriteAllText(string path,string text)=>File.WriteAllText(path,text);
 public void Delete(string path)=>File.Delete(path);
 public long Length(string path)=>new FileInfo(path).Length;
}

internal interface IHostEffects {
 void StartHomeObserver();
 void StartRemoteGate();
 void AddTimer(string purpose,double seconds,Native.TimerCallback callback);
 void StartWebUi(bool hidden);
 void Log(string line);
}

internal sealed record StartupFlags(bool RegisteredUi,bool SystemHome,bool QuietStart,bool WebUiRequested);

internal static class HostStartup {
 internal static StartupFlags FlagsFrom(string[] args)=>new(args.Contains("--registered-ui"),args.Contains("--system-home"),args.Contains("--quiet-start"),args.Contains("--web-ui"));
 internal static void Run(HostConfig config,StartupFlags flags,IHostEffects fx,Native.TimerCallback heartbeat,Native.TimerCallback snapshot,bool startHidden){
  bool grab=!flags.SystemHome&&config.RemoteGrabEnabled; // GUARD:remote-grab
  if(grab)fx.StartRemoteGate();
  if(flags.RegisteredUi||(!flags.SystemHome&&!grab))fx.StartHomeObserver();
  fx.AddTimer("heartbeat",0.5,heartbeat);
  if(config.SnapshotEnabled&&!flags.QuietStart)fx.AddTimer("snapshot",3,snapshot); // GUARD:snapshot
  // The web viewer is QuietIPTV.dll --dashboard: with IPTV off it must not start either.
  if(flags.WebUiRequested&&!flags.RegisteredUi){if(config.WebUiEnabled&&config.IptvEnabled)fx.StartWebUi(startHidden);else fx.Log("web_ui_disabled_by_config");} // GUARD:webui
 }
}

// Which path a launch request takes. Rejection happens before any process or timer.
internal enum LaunchRoute {Rejected,Iptv,AppLauncher}
internal static class LaunchGate {
 // The native fallback's Inputs tile carries the synthetic id quiet.inputs, which is not a real
 // app id; it is refused rather than passed to app_launcher (unsupported in the native fallback).
 internal const string InputsTileId="quiet.inputs";
 internal static LaunchRoute Route(HostConfig config,State state,string id){
  if(string.IsNullOrEmpty(id))return LaunchRoute.Rejected;
  if(id==HostConfig.IptvAppId)return config.IptvEnabled?LaunchRoute.Iptv:LaunchRoute.Rejected; // GUARD:iptv-route
  if(id==InputsTileId)return LaunchRoute.Rejected;
  if(state.Apps.Any(a=>a.Id==id)||HostConfig.SystemLaunchIds.Contains(id))return LaunchRoute.AppLauncher;
  return LaunchRoute.Rejected;
 }
}

// struct input_event is { struct timeval; u16 type; u16 code; s32 value }; timeval is two longs.
internal readonly record struct InputEventLayout(int Size,int TypeOffset){
 internal static InputEventLayout For(int pointerSize)=>pointerSize switch{8=>new(24,16),4=>new(16,8),_=>throw new PlatformNotSupportedException("input_event layout unknown for pointer size "+pointerSize)};
 internal int CountHomePresses(byte[] bytes,int length,int homeKey){int presses=0;for(int i=0;i+Size<=length;i+=Size){ushort type=BitConverter.ToUInt16(bytes,i+TypeOffset),code=BitConverter.ToUInt16(bytes,i+TypeOffset+2);int value=BitConverter.ToInt32(bytes,i+TypeOffset+4);if(type==1&&value==1&&State.ObservedHome(homeKey,code))presses++;}return presses;}
}
