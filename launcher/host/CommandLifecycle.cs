// Bounded, non-blocking app_launcher stop/launch runner shared by the public host
// (launcher/host/Program.cs) and the deployed controller (app/Program.cs via an explicit
// <Compile Link>). It depends on nothing project-specific (no HostConfig, no IProcessStarter,
// no State) so one source compiles into both assemblies.
//
// The freeze it removes: the old path did process.StandardOutput.ReadToEnd() then
// WaitForExit() with no timeout on the single-threaded ecore main loop. If app_launcher
// leaves a detached grandchild holding the stdout/stderr pipe, ReadToEnd() blocks until
// that fd closes -- which can be never -- freezing the heartbeat, Home observer and remote
// gate. Here the command runs on a background worker with bounded output buffers and a whole
// deadline; the ecore main loop only ever polls CommandRun.Done and reads the result, so no
// process or pipe wait ever lands on the UI thread.
//
// Borrowed shape (named, not copied): the in-repo bounded runner (async readback,
// WaitForExit(ms), Kill() on timeout, bounded join) and IptvSession's 2500 ms close-then-kill.
// Two things dropped from the borrow source on purpose: Kill(true) (never a tree kill -- we
// terminate only OUR app_launcher CLI child, never the target app) and the unbounded post-kill
// WaitForExit (bounded here to PostKillWaitMs).
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

// Outcome of one bounded command. PipeHeld means the child exited within the deadline but a
// grandchild kept the pipe open past the reader grace -- that is a clean, accepted stop/launch,
// not a failure, and nothing of ours is killed for it.
internal readonly record struct ProcessResult(
 bool Started, int ExitCode, bool TimedOut, bool Killed, bool PipeHeld,
 string Stdout, string Stderr, int AbandonedReaders);

// Seam (a): the child process as the runner needs it, so --self-test can feed the REAL runner a
// stream that never drains, a never-exiting child, a failed start, a nonzero exit or huge output.
// A real Process is wrapped by RealChild; a fake implements this directly.
internal interface IChildProcess : IDisposable {
 bool Started { get; }
 Stream StdOut { get; }
 Stream StdErr { get; }
 bool HasExited { get; }
 int ExitCode { get; }
 bool WaitForExit(int ms); // true if exited within ms; negative ms waits with no deadline (the hang)
 void Kill();              // single process only -- NEVER Kill(true)
}

internal sealed class RealChild : IChildProcess {
 readonly Process p;
 internal RealChild(Process p){this.p=p;}
 public bool Started=>p!=null;
 public Stream StdOut=>p.StandardOutput.BaseStream;
 public Stream StdErr=>p.StandardError.BaseStream;
 public bool HasExited=>p.HasExited;
 public int ExitCode=>p.ExitCode;
 public bool WaitForExit(int ms){if(ms<0){p.WaitForExit();return p.HasExited;}return p.WaitForExit(ms);}
 public void Kill()=>p.Kill(); // .NET 6 no-arg Kill terminates this process only, not the tree
 public void Dispose()=>p.Dispose();
}

// Drains one stream on its own background thread into a bounded buffer, discarding (but still
// reading, so the child never blocks on a full pipe) past the cap. Join(ms) returns false if the
// reader is still blocked -- that reader is abandoned, never waited on.
internal sealed class BoundedReader {
 readonly Stream stream; readonly int cap; readonly Thread thread; readonly MemoryStream buffer=new();
 internal BoundedReader(Stream stream,int cap){this.stream=stream;this.cap=cap;CommandRunner.ReaderStarted();thread=new Thread(Pump){IsBackground=true};thread.Start();}
 void Pump(){var chunk=new byte[8192];try{int n;while((n=stream.Read(chunk,0,chunk.Length))>0){lock(buffer){int room=cap-(int)buffer.Length;if(room>0)buffer.Write(chunk,0,Math.Min(room,n));}}}catch{}finally{CommandRunner.ReaderEnded();}}
 internal bool Join(int ms)=>thread.Join(ms);
 internal string Text{get{lock(buffer)return Encoding.UTF8.GetString(buffer.GetBuffer(),0,(int)buffer.Length);}}
}

// A single in-flight bounded command. The worker sets the result once; the ecore loop polls Done.
internal sealed class CommandRun {
 volatile bool done; ProcessResult result;
 internal int Gen; // generation stamp, compared on completion to drop stale results after Home/dismiss
 internal bool Done=>done;
 internal ProcessResult Result=>result;
 internal void Complete(ProcessResult r){result=r;done=true;} // worker and test fakes only
 internal static CommandRun Finished(ProcessResult r){var run=new CommandRun();run.Complete(r);return run;}
}

internal static class CommandRunner {
 // Production constants. DeadlineMs bounds the wait for the child to EXIT; it is not the total return
 // time. Worst case to return = DeadlineMs + PostKillWaitMs + 2*ReaderGraceMs (~3000 ms): after the
 // deadline we Kill() and wait a bounded moment for exit, then wait a bounded moment for each reader to
 // drain before abandoning it. Never unbounded (the freeze), never a strict 2500 ms total. Output is
 // capped. MaxLiveReaders is a HARD cap on reader threads/FDs blocked across all commands at once: if
 // closing a pipe does not unblock a reader (possible on Linux), its slot is held until the reader truly
 // ends, and while the cap is reached new commands are gracefully rejected instead of spawning unbounded
 // threads/FDs. The queue is serial, so this only accrues from past commands whose pipe is still held.
 internal const int DefaultDeadlineMs=2500;
 internal const int StdoutCap=65536; // 64 KiB
 internal const int StderrCap=16384; // 16 KiB
 internal const int PostKillWaitMs=200;
 internal const int ReaderGraceMs=150;
 internal const int MaxLiveReaders=8; // ~4 outstanding stuck commands (2 readers each) before reject
 static int liveReaders;
 internal static int LiveReaders=>Volatile.Read(ref liveReaders);
 internal static void ReaderStarted()=>Interlocked.Increment(ref liveReaders);
 internal static void ReaderEnded()=>Interlocked.Decrement(ref liveReaders);

 // Core seam-(a) entry: the worker ALWAYS completes the run with a bounded result. Every exception --
 // from spawn, stream access, reader setup or WaitForExit -- is caught; none escapes the thread entry
 // (an escaping exception would terminate the whole .NET controller and never complete the run).
 internal static CommandRun Begin(Func<IChildProcess> spawn,int deadlineMs,int outCap,int errCap){
  var run=new CommandRun();
  new Thread(()=>{ProcessResult r;try{r=Execute(spawn,deadlineMs,outCap,errCap);}catch(Exception e){r=new ProcessResult(false,-1,false,false,false,"","worker_crash="+e.Message,0);}run.Complete(r);}){IsBackground=true}.Start(); // worker_crash: a belt-and-braces backstop if Execute's own catch were ever removed
  return run;
 }

 static ProcessResult Execute(Func<IChildProcess> spawn,int deadlineMs,int outCap,int errCap){
  if(LiveReaders>MaxLiveReaders-2)return new ProcessResult(false,-1,false,false,false,"","rejected_reader_budget",0); // graceful reject while full; never spawns
  IChildProcess child=null;
  try{
   try{child=spawn();}catch(Exception e){return new ProcessResult(false,-1,false,false,false,"","start_error="+e.Message,0);}
   if(child==null||!child.Started)return new ProcessResult(false,-1,false,false,false,"","",0);
   var outReader=new BoundedReader(child.StdOut,outCap);
   var errReader=new BoundedReader(child.StdErr,errCap);
   bool exited=child.WaitForExit(deadlineMs);
   bool timedOut=false,killed=false;
   if(!exited){try{child.Kill();killed=true;}catch{}timedOut=true;child.WaitForExit(PostKillWaitMs);}
   int exitCode=-1;try{if(child.HasExited)exitCode=child.ExitCode;}catch{}
   bool outDone=outReader.Join(ReaderGraceMs),errDone=errReader.Join(ReaderGraceMs);
   int abandoned=(outDone?0:1)+(errDone?0:1);
   bool pipeHeld=!timedOut&&abandoned>0; // exited cleanly but a grandchild still holds the pipe
   return new ProcessResult(true,exitCode,timedOut,killed,pipeHeld,outReader.Text,errReader.Text,abandoned);
  }
  catch(Exception e){try{child?.Kill();}catch{}return new ProcessResult(false,-1,false,false,false,"","worker_error="+e.Message,0);} // bounded failure, our child only
  finally{try{child?.Dispose();}catch{}} // Dispose unblocks a blocked reader on Windows; an abandoned reader is counted, never awaited, and its slot (ReaderStarted/Ended) is freed only when it truly ends
 }
}

// One queued bounded command: a stop (IsLaunch false) or the single launch (IsLaunch true).
internal sealed class CommandSpec {
 internal string Label; internal string TargetId; internal ProcessStartInfo Info; internal bool IsLaunch;
 internal int DeadlineMs=CommandRunner.DefaultDeadlineMs, OutCap=CommandRunner.StdoutCap, ErrCap=CommandRunner.StderrCap;
 internal int Gen;         // queue generation stamp (set by the queue), compared on completion to drop stale results
 internal int OwnerToken;  // launch-owner token (launch commands only), carried so the handoff is fenced to its launch
}

// Seam (b): the serial, bounded command queue and its generation state machine, testable with no
// real process. Commands run strictly one at a time (stops first, then at most one launch). Home,
// dismiss and the web-hide timeout call Cancel(): queued-but-unstarted commands are dropped and the
// generation is bumped, so an in-flight command's completion is delivered as stale -- never as a late
// app launch or a dashboard hide after Home. An in-flight command is never a second app_launcher.
internal sealed class CommandQueue {
 readonly Func<CommandSpec,CommandRun> start;
 readonly Queue<CommandSpec> pending=new();
 CommandSpec current; CommandRun run; int gen; bool launchQueued;
 internal CommandQueue(Func<CommandSpec,CommandRun> start){this.start=start;}
 internal bool Busy=>current!=null||pending.Count>0;
 internal bool LaunchQueuedOrRunning=>launchQueued||(current!=null&&current.IsLaunch);
 // Returns false if a launch is refused because one is already queued or running (max one pending launch).
 internal bool Enqueue(CommandSpec spec){
  if(spec.IsLaunch){if(LaunchQueuedOrRunning)return false;launchQueued=true;}
  spec.Gen=gen;pending.Enqueue(spec);return true;
 }
 internal void Cancel(){gen++;pending.Clear();launchQueued=false;} // in-flight command keeps running; its result is now stale
 // Poll once: deliver a finished command (onComplete(spec,result,fresh)), then start the next if idle.
 // fresh is false when the generation moved on since the command started -- the caller must not touch the UI.
 internal void Pump(Action<CommandSpec,ProcessResult,bool> onComplete){
  if(current!=null){
   if(!run.Done)return;
   var spec=current;var result=run.Result;bool fresh=run.Gen==gen;
   current=null;run=null;if(spec.IsLaunch)launchQueued=false;
   onComplete(spec,result,fresh);
  }
  if(current==null&&pending.Count>0){current=pending.Dequeue();run=start(current);run.Gen=current.Gen;}
 }
}

// Single-owner fence for the launch state machine's ecore timers (SubmitLaunch, CheckLaunch). A launch
// claims a fresh token with Begin(); Home/dismiss Retire() the live launch. The launch flags (submitted,
// handedOff) live HERE, guarded by the token, not as loose statics -- so an OLD timer that fires after Home
// started a NEW launch cannot re-submit, enqueue a duplicate launch, or consume the new launch's handoff:
// every mutating method refuses a token it no longer owns. Each ecore timer carries its token in its data
// pointer. Testable with no process and no ecore (the Home->new-launch race is a self-test).
internal sealed class LaunchOwner {
 int current; bool submitted; bool handedOff;
 internal int Current=>current;
 internal bool Submitted=>submitted;
 internal int Begin(){submitted=false;handedOff=false;return ++current;}       // a new launch claims a fresh token and clean state
 internal void Retire(){submitted=false;handedOff=false;++current;}            // Home/dismiss: the live launch is invalidated
 internal bool Owns(int token)=>token==current;
 internal bool TrySubmit(int token){if(!Owns(token)||submitted)return false;submitted=true;return true;} // refuses a stale token or a second submit
 internal void HandOff(int token){if(Owns(token))handedOff=true;}
 internal bool ConsumeHandoff(int token){if(!Owns(token)||!handedOff)return false;handedOff=false;submitted=false;return true;} // one-shot; a stale token no-ops
}

// Pure, one-shot, bounded guard for the "late accepted launch after Home" race (no ecore, no PInvoke,
// Windows-testable). Proven on the TV: launch an ordinary app, press Home ~0.6 s later; Home shows and
// the pending launch is cancelled, but the OS had already ACCEPTED that launch and surfaces the app
// ~3 s later, stealing focus from the Home the user asked for. When Home cancels an ordinary-app
// launch, the controller arms this guard keyed to that exact runtime id with a short deadline; the
// heartbeat then observes the foreground and, if (and only if) that exact cancelled app surfaces late
// within the window, consumes the guard ONCE and asks the controller to reassert its OWN registered
// home via the existing app_launcher (-s own-id) command. It never polls or forces home forever (it
// expires), never targets an unrelated app, an empty/unknown id, IPTV, or our own home, and stands
// down on any newer launch or dismissal so it can never itself steal focus.
internal sealed class HomeReturnGuard {
 // Mirrors HostConfig.IptvAppId and the app's IPTV id: IPTV shows our own window, not an app_launcher
 // foreground, so there is nothing to reclaim against and it must never be a guard target.
 internal const string IptvId="quiet.iptv";
 // Pre-registered window: comfortably covers the measured ~3 s late surface with margin, yet expires
 // well before any later, unrelated user action, so the guard is strictly bounded.
 internal const int WindowMs=8000;
 string target="";   // the RuntimeId we will reclaim home against; "" = disarmed
 long deadline;      // Environment.TickCount64 value at/after which the guard has expired
 internal bool Armed=>target!="";
 internal string Target=>target;

 // Arm for exactly one cancelled ordinary-app launch. Refuses an empty/whitespace id, our own
 // registered home (never fight our own foreground) and IPTV. Re-arming replaces any prior watch so
 // the newest cancelled app wins; an ineligible target disarms rather than leaving a stale watch.
 internal void Arm(string runtimeTarget,string ownHomeId,long deadlineTick){
  if(string.IsNullOrWhiteSpace(runtimeTarget)||runtimeTarget==ownHomeId||runtimeTarget==IptvId){target="";return;}
  target=runtimeTarget;deadline=deadlineTick;
 }

 // Explicit newer launch, dismissal or other user intent: stand down, so a late surface can never be
 // reclaimed over what the user is now doing.
 internal void Cancel(){target="";}

 // Observe the current foreground once. Returns true EXACTLY ONCE -- when the armed, not-yet-expired
 // cancelled app is the one that actually surfaced late -- and disarms (one-shot). At/after the
 // deadline it expires with no reclaim. Our own home or any unrelated foreground leaves it armed until
 // it either matches or expires. Never reclaims twice for one arming.
 internal bool Observe(string foreground,long nowTick){
  if(target=="")return false;
  if(nowTick>=deadline){target="";return false;}   // expired: give up, never reclaim
  if(foreground==target){target="";return true;}    // the late launch landed: reclaim home once
  return false;                                      // not yet, or a different app: keep watching within the window
 }
}

// The single allowlist of app_launcher invocations this controller may build: stop by key (-k) and
// launch by id (-s), both under the owner uid (-u). No generic exec: nothing else is constructed here.
internal static class AppLauncher {
 internal const string Path="/usr/bin/app_launcher";
 internal static ProcessStartInfo Stop(string uid,string id)=>Build(uid,"-k",id);
 internal static ProcessStartInfo Launch(string uid,string launchId)=>Build(uid,"-s",launchId);
 static ProcessStartInfo Build(string uid,string verb,string id){
  var psi=new ProcessStartInfo(Path){UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true};
  psi.ArgumentList.Add("-u");psi.ArgumentList.Add(uid);psi.ArgumentList.Add(verb);psi.ArgumentList.Add(id);return psi;
 }
}

// ===========================================================================================
// Ecore-native, NON-BLOCKING execution path -- the real Tizen production runner.
//
// Why this exists (root cause, reproduced by the parent on the actual ARMv7 Tizen 9 .NET 6 TV):
// a .NET System.Diagnostics.Process waited on from a background worker WHILE the ecore main loop
// is pumping aborts the whole controller with SIGABRT. Ecore installs a SIGCHLD handler and reaps
// children with waitpid(-1, WNOHANG); when it reaps OUR child first, .NET's ProcessWaitState.
// TryReapChild then gets ECHILD and calls Environment.FailFast (a NATIVE-runtime abort -- no managed
// try/catch and no PID-reuse guard can stop it). The old synchronous wait avoided the race only by
// blocking the loop, which froze the UI. So the fix is to own NO .NET Process at all on this path:
// spawn through ecore_exe, let ECORE own the child and its reaping, and read the result from ecore's
// own ECORE_EXE_EVENT_DEL on the main loop. CommandRunner/IChildProcess below stay as the Windows
// fake/real-process test seam; production StartCommand selects EcoreCommandRunner instead.
//
// Primary ABI verified against Enlightenment/efl src/lib/ecore (master, read Oct 2026):
//  - Ecore_Exe *ecore_exe_pipe_run(const char*, Ecore_Exe_Flags, const void*)   [Ecore_Common.h:1155]
//  - pid_t ecore_exe_pid_get(const Ecore_Exe*)  (pid_t == int)                   [Ecore_Common.h:1239]
//  - ECORE_EXE_EVENT_DEL/DATA/ERROR are `extern int` exports                     [Ecore_Common.h:1017-1020]
//  - flags ECORE_EXE_PIPE_READ=1, ECORE_EXE_PIPE_ERROR=4                         [ecore_exe_eo.legacy.h:46,48]
//  - the command runs via DIRECT execvp (no /bin/sh) iff it contains none of
//    | & ; < > ( ) $ ` \ " ' * ? # , no token starts with '~' or '[', and the
//    first token has no '='                                   [ecore_exe_posix.c:1027,1043-1053]
//  - a PIPE_READ|PIPE_ERROR exe's DEL is DELAYED ~0.1 s after death              [ecore_signal.c:440-472]
//  - DEL auto-frees the handle (ecore_exe_free in _ecore_exe_event_del_free)     [ecore_exe.c:416]
//  - ecore_exe_kill() does kill(pid,SIGKILL) BUT also arms a 10 s "make sure it's really dead"
//    SIGKILL timer -- so we DO NOT use it; we kill the exact pid ourselves via libc kill()
//    guarded by a /proc start-time identity check                               [ecore_exe_posix.c:927-938]
// Tizen's EFL fork may differ; the on-TV probe (probe/ecore-runner) is the final ABI check.
// Everything here runs on the ecore MAIN thread only (Begin is called from CommandQueue.Pump in a
// timer; DATA/DEL handlers and timer callbacks are delivered by the main loop), so no locking is
// needed on the tracking maps or output buffers.

// The exact allowlisted argument validator: builds the single command STRING ecore needs while
// guaranteeing direct exec (never /bin/sh) and exact argv. Owner config is NOT automatically shell
// safe, so any argument with whitespace, a shell metacharacter, a leading '-' on a value, non-ASCII,
// or an empty string is REFUSED (fail-closed) rather than quoted -- quoting ' or " would force
// /bin/sh, which would make /bin/sh (not app_launcher) the child and defeat exact-pid kill. Every
// real launch/runtime id (org.tizen.netflix-app, 9Ur5Iz....TizenYouTube, org.volt.apps, com.samsung.
// tv.cobalt-yt, numeric uids, /usr/bin/app_launcher) is inside the safe set, so none is refused.
internal static class CommandArgs {
 static readonly string[] Flags={"-u","-k","-s"}; // the only arguments permitted to start with '-'
 static bool SafeChar(char c)=>(c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='.'||c=='_'||c=='-'||c=='/'||c==':'||c=='@'||c=='+';
 internal static bool SafeArg(string a){
  if(string.IsNullOrEmpty(a))return false;
  if(Array.IndexOf(Flags,a)>=0)return true;   // exact known flag
  if(a[0]=='-')return false;                  // option-injection guard on a value
  foreach(char c in a)if(!SafeChar(c))return false;
  return true;
 }
 // The exact command line, or null to REFUSE (unsafe argument). Safe args have no whitespace or shell
 // metacharacters, so a plain space join tokenises back to exactly these argv under ecore's direct exec.
 internal static string BuildLine(ProcessStartInfo info){
  if(info==null||!SafeArg(info.FileName))return null;
  var sb=new StringBuilder(info.FileName);
  foreach(var a in info.ArgumentList){if(!SafeArg(a))return null;sb.Append(' ').Append(a);}
  return sb.ToString();
 }
}

// Parses /proc/<pid>/stat field 22 (starttime) as a spawn identity, so a later kill targets only the
// process we spawned and never a reused pid. ParseStartTime is pure (Windows-testable); ReadStartTime
// touches /proc and is used only in production/probe on Linux.
internal static class ProcStat {
 // field 1 = pid, field 2 = comm (parenthesised, may contain spaces), field 3 = state, ...
 // field 22 = starttime. After the LAST ')', fields 3.. are space-separated; starttime is index 19.
 internal static long ParseStartTime(string stat){
  if(string.IsNullOrEmpty(stat))return 0;
  int close=stat.LastIndexOf(')');if(close<0||close+2>=stat.Length)return 0;
  var parts=stat.Substring(close+1).Split((char[])null,StringSplitOptions.RemoveEmptyEntries);
  return parts.Length>19&&long.TryParse(parts[19],out var t)?t:0;
 }
 internal static long ReadStartTime(int pid){try{return ParseStartTime(File.ReadAllText("/proc/"+pid+"/stat"));}catch{return 0;}}
 // Pure kill-identity guard. A signal is permitted ONLY when the pid is positive, BOTH the recorded and
 // the freshly-read /proc start-time exist (non-zero) and they are EXACTLY equal. Any missing (0),
 // denied, disappeared or reused identity returns false, so the kill path must not signal. No "reuse is
 // impossible" shortcut anywhere -- every kill in the new adapter calls this first.
 internal static bool IdentityMatches(long pid,long recordedStart,long currentStart)
  =>pid>0&&recordedStart>0&&currentStart>0&&recordedStart==currentStart;
}

// One in-flight ecore command as a PURE state machine over the native events, with no PInvoke -- so
// every transition (DEL before the deadline; deadline -> kill -> DEL; deadline -> kill -> no DEL ->
// bounded abandon; a late or duplicate DEL ignored; output capping) is driven and asserted offline.
// The owning adapter wires the real ecore events/timers/kill to these methods; the result it produces
// is the same ProcessResult shape the worker path returns, so OnCommandDone is unchanged.
internal sealed class EcoreCommand {
 readonly CommandRun run;readonly int outCap,errCap;readonly MemoryStream outB=new(),errB=new();
 bool completed,killRequested,timedOut,killed,abandoned;
 internal long Pid;internal long StartId;internal IntPtr Handle;internal int Id;
 internal EcoreCommand(CommandRun run,int outCap,int errCap){this.run=run;this.outCap=outCap;this.errCap=errCap;}
 internal CommandRun Run=>run;
 internal bool Completed=>completed;
 internal bool KillRequested=>killRequested;
 internal void OnData(bool err,byte[] data,int size){if(completed||size<=0)return;var b=err?errB:outB;int cap=err?errCap:outCap;int room=cap-(int)b.Length;if(room>0)b.Write(data,0,Math.Min(room,size));}
 // ecore DEL: a self-exit carries a real code; a signal-death carries exit=-1. A DEL after completion
 // (the late/kill DEL, or a duplicate) is ignored -- the run is completed exactly once.
 internal void OnDel(int exitCode,bool exited){if(completed)return;Finish(exited?exitCode:-1);}
 // The whole-command deadline: request the kill exactly once. Returns true if the caller must now try to
 // kill the exact pid. A command already completed (DEL beat the deadline) returns false -- no kill.
 // It marks TimedOut but NOT Killed: a signal may be denied (identity mismatch) or fail, and we must not
 // claim a kill that never happened. The caller records the kill with MarkKilled() only on success.
 internal bool Deadline(){if(completed||killRequested)return false;killRequested=true;timedOut=true;return true;}
 // Record that the exact-pid SIGKILL actually succeeded (kill() returned 0). If it was denied or failed,
 // this is never called, Killed stays false, and the run still finishes bounded (TimedOut) after grace.
 internal void MarkKilled(){killed=true;}
 // Post-kill grace elapsed with no DEL (child unkillable/zombie): give up on this handle, bounded.
 internal void Abandon(){if(completed)return;abandoned=true;Finish(-1);}
 void Finish(int exitCode){completed=true;run.Complete(new ProcessResult(true,exitCode,timedOut,killed,false,Encoding.UTF8.GetString(outB.GetBuffer(),0,(int)outB.Length),Encoding.UTF8.GetString(errB.GetBuffer(),0,(int)errB.Length),abandoned?1:0));}
}

// Production adapter (real Tizen): spawns through ecore_exe, tracks the handle, and completes the
// EcoreCommand from ecore's own DATA/DEL events plus a deadline timer -- all on the main loop, with no
// .NET Process anywhere. All PInvoke lives here and binds lazily on first use, so the Windows self-test
// (which never calls Begin) never loads libecore. Begin() and every callback run on the main thread.
internal static class EcoreCommandRunner {
 internal const int PostKillWaitMs=400;   // > the ~0.1 s piped-exe DEL delay, so a kill's DEL can still arrive
 internal const int MaxOrphanHandles=8;   // hard cap on killed-but-no-DEL handles before new commands are refused
 const int ECORE_EXE_PIPE_READ=1,ECORE_EXE_PIPE_ERROR=4,SIGKILL=9;

 [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int ExeEventCb(IntPtr data,int type,IntPtr info);
 [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int ExeTimerCb(IntPtr data);
 [DllImport("libecore.so.1")] static extern IntPtr ecore_exe_pipe_run(string exe_cmd,int flags,IntPtr data);
 [DllImport("libecore.so.1")] static extern int ecore_exe_pid_get(IntPtr exe);
 [DllImport("libecore.so.1")] static extern IntPtr ecore_event_handler_add(int type,ExeEventCb cb,IntPtr data);
 [DllImport("libecore.so.1")] static extern IntPtr ecore_event_handler_del(IntPtr handler);
 [DllImport("libecore.so.1")] static extern IntPtr ecore_timer_add(double seconds,ExeTimerCb cb,IntPtr data);
 [DllImport("libc",SetLastError=true)] static extern int kill(int pid,int sig);

 static readonly ExeEventCb OnDataCb=OnExeData,OnErrCb=OnExeError,OnDelCb=OnExeDel;
 static readonly ExeTimerCb DeadlineCb=OnDeadline,PostKillCb=OnPostKill;
 static readonly Dictionary<IntPtr,EcoreCommand> byHandle=new();
 static readonly Dictionary<int,EcoreCommand> byId=new();
 static readonly HashSet<IntPtr> orphans=new();
 static int errEventType,nextId,initState; // initState: 0 = not tried, 1 = ready, -1 = permanent failure
 static string initError="";

 // Read-only diagnostic seam for the on-TV FD probe only: the live sizes of the handle-tracking maps and
 // the orphan set. No behaviour, no mutation. Between commands all three must be 0 (ecore freed every
 // handle on its DEL), which tells a native-descriptor leak apart from a managed tracking-map leak.
 internal static (int ByHandle,int ById,int Orphans) Diagnostics=>(byHandle.Count,byId.Count,orphans.Count);

 // Pure init decision (Windows-testable): ready ONLY when all three event handlers registered non-null.
 internal static bool HandlersOk(IntPtr del,IntPtr dat,IntPtr err)=>del!=IntPtr.Zero&&dat!=IntPtr.Zero&&err!=IntPtr.Zero;

 // Fail-closed, memoized init. Resolves EVERY required symbol BEFORE registering anything; registers all
 // three handlers; if any returns null, deletes the ones that did register (no partial state) and records
 // a PERMANENT failure with the error. Only a complete success marks ready. Returns true iff ready.
 static bool EnsureInit(){
  if(initState!=0)return initState==1;
  try{
   var lib=NativeLibrary.Load("libecore.so.1");
   int del=Marshal.ReadInt32(NativeLibrary.GetExport(lib,"ECORE_EXE_EVENT_DEL"));
   int dat=Marshal.ReadInt32(NativeLibrary.GetExport(lib,"ECORE_EXE_EVENT_DATA"));
   int err=Marshal.ReadInt32(NativeLibrary.GetExport(lib,"ECORE_EXE_EVENT_ERROR"));
   IntPtr hDel=ecore_event_handler_add(del,OnDelCb,IntPtr.Zero);
   IntPtr hDat=ecore_event_handler_add(dat,OnDataCb,IntPtr.Zero);
   IntPtr hErr=ecore_event_handler_add(err,OnErrCb,IntPtr.Zero);
   if(!HandlersOk(hDel,hDat,hErr)){
    foreach(var h in new[]{hDel,hDat,hErr})if(h!=IntPtr.Zero)ecore_event_handler_del(h); // unwind partial registration
    throw new InvalidOperationException("ecore_event_handler_add returned null");
   }
   errEventType=err;initState=1;return true;
  }catch(Exception e){initError=e.Message;initState=-1;return false;} // memoize permanent failure; never retry/spawn
 }

 // The ONLY kill in this adapter. Signals a single exact pid, and ONLY when the recorded and freshly-read
 // /proc start-times both exist and match exactly (no reused/disappeared/denied pid is ever signalled).
 // Returns true iff an actual SIGKILL was sent AND kill() returned 0. Never a group/tree -- one pid.
 static bool KillExact(long pid,long recordedStart)
  =>ProcStat.IdentityMatches(pid,recordedStart,ProcStat.ReadStartTime((int)pid))&&kill((int)pid,SIGKILL)==0;

 // The CommandQueue.start seam. Returns a CommandRun completed from the ecore events/timers below.
 internal static CommandRun Begin(CommandSpec spec){
  var run=new CommandRun();
  string line=CommandArgs.BuildLine(spec.Info);
  if(line==null){run.Complete(new ProcessResult(false,-1,false,false,false,"","unsafe_arg",0));return run;}
  if(orphans.Count>=MaxOrphanHandles){run.Complete(new ProcessResult(false,-1,false,false,false,"","rejected_handle_budget",0));return run;}
  try{
   if(!EnsureInit()){run.Complete(new ProcessResult(false,-1,false,false,false,"","init_failed="+initError,0));return run;} // never spawn after a failed init
   IntPtr h=ecore_exe_pipe_run(line,ECORE_EXE_PIPE_READ|ECORE_EXE_PIPE_ERROR,IntPtr.Zero);
   if(h==IntPtr.Zero){run.Complete(new ProcessResult(false,-1,false,false,false,"","spawn_failed",0));return run;}
   int pid=ecore_exe_pid_get(h);
   var cmd=new EcoreCommand(run,spec.OutCap,spec.ErrCap){Handle=h,Pid=pid,StartId=ProcStat.ReadStartTime(pid),Id=++nextId};
   byHandle[h]=cmd;byId[cmd.Id]=cmd;
   // No deadline timer => a hung CLI is never killed and the queue stalls forever (run never completes).
   // Fail closed: try the identity-guarded exact-pid kill (no "reuse impossible" shortcut), then stop
   // tracking it in the maps but charge it against the orphan budget so repeated failures stay bounded --
   // its later DEL clears the orphan. Complete the run so the serial queue cannot stall.
   IntPtr timer=ecore_timer_add(Math.Max(spec.DeadlineMs,1)/1000.0,DeadlineCb,new IntPtr(cmd.Id));
   if(timer==IntPtr.Zero){KillExact(cmd.Pid,cmd.StartId);orphans.Add(h);Untrack(cmd);run.Complete(new ProcessResult(false,-1,false,false,false,"","timer_failed",0));return run;}
  }catch(Exception e){run.Complete(new ProcessResult(false,-1,false,false,false,"","spawn_error="+e.Message,0));}
  return run;
 }

 static void Untrack(EcoreCommand cmd){byHandle.Remove(cmd.Handle);byId.Remove(cmd.Id);}

 static void CopyData(IntPtr info,EcoreCommand cmd,bool err){
  IntPtr buf=Marshal.ReadIntPtr(info,IntPtr.Size);int size=Marshal.ReadInt32(info,2*IntPtr.Size); // Ecore_Exe_Event_Data{exe*,data*,size}
  if(buf==IntPtr.Zero||size<=0)return;
  int take=Math.Min(size,CommandRunner.StdoutCap); // bound the per-event copy; OnData caps the stream total
  var bytes=new byte[take];Marshal.Copy(buf,bytes,0,take);cmd.OnData(err,bytes,take);
 }
 static int OnExeData(IntPtr data,int type,IntPtr info){try{if(byHandle.TryGetValue(Marshal.ReadIntPtr(info,0),out var cmd))CopyData(info,cmd,false);}catch{}return 1;}
 static int OnExeError(IntPtr data,int type,IntPtr info){try{if(byHandle.TryGetValue(Marshal.ReadIntPtr(info,0),out var cmd))CopyData(info,cmd,true);}catch{}return 1;}
 static int OnExeDel(IntPtr data,int type,IntPtr info){
  try{
   int exitCode=Marshal.ReadInt32(info,4);IntPtr exe=Marshal.ReadIntPtr(info,8);int flags=Marshal.ReadInt32(info,12+IntPtr.Size); // {pid,exit_code,exe*,exit_signal,exited:1...}
   bool exited=(flags&1)!=0;
   if(byHandle.TryGetValue(exe,out var cmd)){cmd.OnDel(exitCode,exited);Untrack(cmd);}
   orphans.Remove(exe); // a killed-but-abandoned handle's DEL finally arrived: ecore now frees it
  }catch{}
  return 1; // the handle is auto-freed by ecore after this; never touch it again
 }
 // A failed grace timer must not leave the serial queue waiting forever. Keep this
 // allocator seam pure so null, throwing and successful registrations are testable.
 internal static void ScheduleGrace(Func<IntPtr> register,Action fallback){
  bool scheduled=false;try{scheduled=register()!=IntPtr.Zero;}catch{}
  if(!scheduled)fallback();
 }
 static int OnDeadline(IntPtr data){
  // Deadline reached: request the kill once, then attempt an identity-guarded exact-pid SIGKILL. Record
  // Killed ONLY if the signal actually succeeded. If the identity is missing/denied/reused or kill fails,
  // we do NOT signal and do NOT claim a kill; the post-kill grace still runs and the run finishes bounded
  // (TimedOut=true, Killed=false) via Abandon. Either way, schedule the one-shot grace timer.
  try{if(byId.TryGetValue(data.ToInt32(),out var cmd)&&cmd.Deadline()){
   try{if(KillExact(cmd.Pid,cmd.StartId))cmd.MarkKilled();}catch{}
   ScheduleGrace(()=>ecore_timer_add(PostKillWaitMs/1000.0,PostKillCb,data),()=>OnPostKill(data));
  }}catch{}
  return 0; // one-shot
 }
 static int OnPostKill(IntPtr data){
  try{if(byId.TryGetValue(data.ToInt32(),out var cmd)&&!cmd.Completed){cmd.Abandon();orphans.Add(cmd.Handle);Untrack(cmd);}}catch{}
  return 0; // one-shot
 }
}

// Offline acceptance (no TV, no PInvoke): drives the real runner and queue with fakes in both
// directions. Hang-prone cases run under an in-test wall-clock watchdog that polls Done exactly as
// the ecore loop does, so a deadline/generation regression FAILS a named claim instead of hanging.
internal static class CommandLifecycleTests {
 static int count;
 static void Check(bool ok,string claim){if(!ok)throw new Exception(claim);count++;}

 sealed class FakeStream:Stream{
  readonly byte[] data;int pos;readonly ManualResetEventSlim gate; // gate!=null => block at EOF until set (grandchild holds pipe)
  internal FakeStream(byte[] data,ManualResetEventSlim gate){this.data=data;this.gate=gate;}
  public override int Read(byte[] b,int o,int c){if(pos<data.Length){int n=Math.Min(c,data.Length-pos);Array.Copy(data,pos,b,o,n);pos+=n;return n;}gate?.Wait();return 0;}
  public override bool CanRead=>true;public override bool CanSeek=>false;public override bool CanWrite=>false;
  public override long Length=>data.Length;public override long Position{get=>pos;set{}}
  public override void Flush(){}public override long Seek(long a,SeekOrigin b)=>0;public override void SetLength(long a){}public override void Write(byte[] a,int b,int c){}
 }

 sealed class FakeChild:IChildProcess{
  internal bool started=true;internal int exitCode;internal bool neverExits;internal byte[] outBytes=Array.Empty<byte>(),errBytes=Array.Empty<byte>();
  internal bool holdPipe;internal int killCount;
  internal bool throwOnStream;    // StdOut/StdErr access throws (reader setup fails inside the worker)
  internal bool throwOnWait;      // WaitForExit throws (the deadline wait fails inside the worker)
  internal ManualResetEventSlim pipeGate; // if set, the pipe is held by THIS external gate and Dispose does NOT release it (Linux close-does-not-unblock)
  readonly ManualResetEventSlim exited=new(false),released=new(false);
  internal void SignalExit(){exited.Set();}
  // Which event, if any, a reader parks on at EOF. pipeGate (external, never released by Dispose) models a
  // reader that Dispose cannot free; released models a Windows-style close that unblocks it.
  ManualResetEventSlim Gate=>holdPipe?(pipeGate??released):null;
  public bool Started=>started;
  public Stream StdOut{get{if(throwOnStream)throw new IOException("stdout unavailable");return new FakeStream(outBytes,Gate);}}
  public Stream StdErr{get{if(throwOnStream)throw new IOException("stderr unavailable");return new FakeStream(errBytes,Gate);}}
  public bool HasExited=>exited.IsSet;
  public int ExitCode=>exitCode;
  public bool WaitForExit(int ms){if(throwOnWait)throw new InvalidOperationException("wait failed");if(ms<0){exited.Wait();return true;}return exited.Wait(ms);}
  public void Kill(){killCount++;exited.Set();}
  public void Dispose(){released.Set();} // unblocks a reader parked on 'released' (Windows close); a reader parked on an external pipeGate is NOT freed
 }

 static FakeChild Child(Action<FakeChild> set){var c=new FakeChild();set(c);if(!c.neverExits)c.SignalExit();return c;}

 // Poll Done like the ecore loop; never block on the process. Fails the claim if the worker hangs.
 static ProcessResult RunWatched(Func<IChildProcess> spawn,int deadlineMs,string claim){
  var run=CommandRunner.Begin(spawn,deadlineMs,CommandRunner.StdoutCap,CommandRunner.StderrCap);
  var sw=Stopwatch.StartNew();while(!run.Done&&sw.ElapsedMilliseconds<deadlineMs*8L)Thread.Sleep(5);
  Check(run.Done,claim);return run.Result;
 }

 internal static int Run(){
  const int fast=150; // short test deadline; production constants are asserted separately

  // Production constants are the pre-registered thresholds.
  Check(CommandRunner.DefaultDeadlineMs==2500&&CommandRunner.StdoutCap==65536&&CommandRunner.StderrCap==16384,"bounded thresholds are 2500 ms / 64 KiB / 16 KiB");

  // Exact allowlisted CLI arguments, nothing else.
  var stop=AppLauncher.Stop("1234","org.tizen.netflix-app");
  Check(stop.FileName=="/usr/bin/app_launcher"&&!stop.UseShellExecute&&stop.RedirectStandardOutput&&stop.RedirectStandardError&&stop.ArgumentList.SequenceEqual(new[]{"-u","1234","-k","org.tizen.netflix-app"}),"stop uses the exact allowlisted app_launcher arguments");
  var launch=AppLauncher.Launch("1234","org.volt.apps");
  Check(launch.ArgumentList.SequenceEqual(new[]{"-u","1234","-s","org.volt.apps"}),"launch uses the exact allowlisted app_launcher arguments");

  // Normal exit 0 with ordinary output.
  var ok=RunWatched(()=>Child(c=>c.outBytes=Encoding.UTF8.GetBytes("done")),fast,"a normal stop returns");
  Check(ok.Started&&ok.ExitCode==0&&!ok.TimedOut&&!ok.Killed&&!ok.PipeHeld&&ok.Stdout=="done","normal exit 0 is reported with its output");

  // Nonzero exit.
  var bad=RunWatched(()=>Child(c=>{c.exitCode=5;c.errBytes=Encoding.UTF8.GetBytes("no such app");}),fast,"a nonzero stop returns");
  Check(bad.ExitCode==5&&!bad.TimedOut&&bad.Stderr=="no such app","nonzero exit is reported, not treated as a timeout");

  // Failed start: null child and throwing spawn are both Started=false, never a crash.
  var noStart=RunWatched(()=>null,fast,"a failed start returns");
  Check(!noStart.Started&&!noStart.TimedOut,"a null start is reported as not started");
  var threwStart=RunWatched(()=>throw new InvalidOperationException("boom"),fast,"a throwing start returns");
  Check(!threwStart.Started&&threwStart.Stderr.Contains("start_error="),"a throwing start is caught and reported as not started");

  // Large / malformed output is bounded and still returns.
  var big=RunWatched(()=>Child(c=>c.outBytes=new byte[CommandRunner.StdoutCap*2]),fast,"a large-output stop returns");
  Check(big.Stdout.Length<=CommandRunner.StdoutCap&&!big.TimedOut,"stdout is capped at 64 KiB and the stop still returns");

  // Exit 0 but a grandchild holds the pipe: clean, accepted, bounded, nothing killed.
  var held=RunWatched(()=>Child(c=>{c.outBytes=Encoding.UTF8.GetBytes("ok");c.holdPipe=true;}),fast,"a pipe-held stop returns promptly without waiting on the grandchild");
  Check(held.Started&&held.ExitCode==0&&!held.TimedOut&&!held.Killed&&held.PipeHeld&&held.AbandonedReaders>0,"exit 0 with a held pipe is PipeHeld, not a failure, and nothing of ours is killed");

  // Child never exits and never drains: must be killed at the deadline (this is the freeze case).
  FakeChild hung=null;
  var timedOut=RunWatched(()=>{hung=new FakeChild{neverExits=true,holdPipe=true};return hung;},fast,"a stop whose child never exits is killed at the deadline and returns after bounded cleanup");
  Check(timedOut.TimedOut&&timedOut.Killed&&hung.killCount==1,"a never-exiting child is killed once at the deadline and reported TimedOut");

  // --- Issue 1: NO worker exception escapes the thread and kills the controller; every one completes the run ---
  // Reader-setup failure (stream access throws): caught, run completes with a bounded worker_error.
  var threwStream=RunWatched(()=>Child(c=>c.throwOnStream=true),fast,"a child whose stream access throws still returns");
  Check(!threwStream.TimedOut&&threwStream.Stderr.Contains("worker_error="),"an exception from stream/reader setup is caught and completes the run, never escaping the worker thread");
  // WaitForExit failure: caught, run completes with a bounded worker_error.
  var threwWait=RunWatched(()=>Child(c=>c.throwOnWait=true),fast,"a child whose WaitForExit throws still returns");
  Check(threwWait.Stderr.Contains("worker_error="),"an exception from WaitForExit is caught and completes the run, never escaping the worker thread");

  // --- Issue 3: a HARD cap on live readers/FDs even when closing the pipe does NOT unblock the reader ---
  // Each child here holds BOTH pipes on an external gate that Dispose never releases (Linux close-does-not-unblock).
  // Run many more than the cap; live readers must never grow past MaxLiveReaders, and once full, commands are
  // gracefully rejected (rejected_reader_budget) rather than spawning unbounded reader threads/FDs.
  var stuckGate=new ManualResetEventSlim(false);
  int readersBefore=CommandRunner.LiveReaders;bool everRejected=false;
  var stuckRuns=new List<CommandRun>();
  for(int i=0;i<12;i++){
   var r=CommandRunner.Begin(()=>new FakeChild{neverExits=true,holdPipe=true,pipeGate=stuckGate},fast,CommandRunner.StdoutCap,CommandRunner.StderrCap);
   stuckRuns.Add(r);
   var sw=Stopwatch.StartNew();while(!r.Done&&sw.ElapsedMilliseconds<fast*8L)Thread.Sleep(5);
   Check(r.Done,"a stuck-pipe command still returns bounded (never hangs the caller)");
   Check(CommandRunner.LiveReaders<=CommandRunner.MaxLiveReaders,"live readers never exceed the hard cap even when Dispose cannot unblock them");
   if(r.Result.Stderr.Contains("rejected_reader_budget"))everRejected=true;
  }
  Check(everRejected,"once the reader budget is full, further commands are gracefully rejected, not spawned unbounded");
  stuckGate.Set(); // release every parked reader; the slots must drain back to the starting level
  var drainSw=Stopwatch.StartNew();while(CommandRunner.LiveReaders>readersBefore&&drainSw.ElapsedMilliseconds<4000)Thread.Sleep(5);
  Check(CommandRunner.LiveReaders<=readersBefore,"every reader slot is released once the pipe truly closes (no permanent leak)");

  // --- Issue 2: the LaunchOwner single-owner fence -- the Home->new-launch race, before the old callback retires ---
  var owner=new LaunchOwner();
  int tA=owner.Begin();                // launch A
  Check(owner.TrySubmit(tA),"launch A's own timer submits once");
  Check(!owner.TrySubmit(tA),"launch A cannot submit twice (no duplicate enqueue from a repeating timer)");
  owner.Retire();                      // Home pressed (or dismiss)
  int tB=owner.Begin();                // a new launch B starts immediately, before A's old timers have retired
  Check(!owner.TrySubmit(tA),"after Home starts a new launch, A's old timer cannot submit (no stale launch on the new app)");
  Check(!owner.Owns(tA)&&owner.Owns(tB),"only the new launch B owns the live generation");
  Check(owner.TrySubmit(tB),"launch B submits under its own token");
  owner.HandOff(tB);                   // B's command completed and handed off to the foreground watch
  Check(!owner.ConsumeHandoff(tA),"A's old timer cannot consume B's handoff (no reset of the new launch after handoff)");
  Check(owner.Submitted,"B stays submitted -- the stale timer did not clear it");
  Check(owner.ConsumeHandoff(tB),"B's owning timer consumes its own handoff exactly once");
  Check(!owner.ConsumeHandoff(tB),"a handoff is consumed only once");
  Check(!owner.Owns(0),"a zero/default token never owns a live generation");

  // --- Queue + generation state machine (seam b), no real process ---
  var started=new List<string>();var done=new List<string>();
  var s1=new CommandSpec{Label="stop1"};var s2=new CommandSpec{Label="stop2"};var lx=new CommandSpec{Label="launch",IsLaunch=true};
  CommandRun R1=null,R2=null,LR=null;
  var q=new CommandQueue(spec=>{started.Add(spec.Label);var r=new CommandRun();if(spec.Label=="stop1")R1=r;else if(spec.Label=="stop2")R2=r;else LR=r;return r;});
  q.Enqueue(s1);q.Enqueue(s2);
  Check(q.Enqueue(lx),"the single launch enqueues when none is pending");
  Check(!q.Enqueue(new CommandSpec{Label="launch2",IsLaunch=true}),"a second launch is refused (max one pending launch)");
  void Pump()=>q.Pump((spec,res,fresh)=>done.Add(spec.Label+":"+fresh));
  Pump();Check(started.SequenceEqual(new[]{"stop1"}),"only the first command starts");
  Pump();Check(started.Count==1,"the next command does not start until the current one completes (serial)");
  R1.Complete(default);Pump();Check(started.SequenceEqual(new[]{"stop1","stop2"})&&done.SequenceEqual(new[]{"stop1:True"}),"stop2 starts only after stop1 completes");
  R2.Complete(default);Pump();Check(started.SequenceEqual(new[]{"stop1","stop2","launch"}),"the launch starts only after every stop completes");
  LR.Complete(default);Pump();Check(done.SequenceEqual(new[]{"stop1:True","stop2:True","launch:True"}),"the launch completes fresh in order");

  // Home/dismiss cancel: a queued-but-unstarted launch is dropped and an in-flight command's result is stale.
  var started2=new List<string>();CommandRun aRun=null;var fresh2=new List<bool>();
  var q2=new CommandQueue(spec=>{started2.Add(spec.Label);var r=new CommandRun();if(spec.Label=="stopA")aRun=r;return r;});
  var sa=new CommandSpec{Label="stopA"};var lb=new CommandSpec{Label="launchB"};lb.IsLaunch=true;
  q2.Enqueue(sa);q2.Enqueue(lb);
  q2.Pump((s,r,f)=>fresh2.Add(f));
  Check(started2.SequenceEqual(new[]{"stopA"}),"only stopA has started before Home");
  q2.Cancel(); // Home pressed
  aRun.Complete(default);
  q2.Pump((s,r,f)=>{if(s.Label=="stopA")fresh2.Add(f);});
  Check(fresh2.SequenceEqual(new[]{false}),"stopA's completion after Home is delivered as stale (never fresh)");
  Check(!started2.Contains("launchB"),"the queued launch is dropped after Home and never starts (no late app launch)");

  // --- Ecore-native path: argument safety (owner config is not shell-safe) ---
  // Every real launch/runtime id must pass (refusing one would silently shrink the allowlist).
  foreach(var id in new[]{"org.tizen.netflix-app","org.tizen.primevideo","org.volt.apps","com.samsung.tv.store","com.samsung.tv.quicksetting","com.samsung.tv.cobalt-yt","1234","/usr/bin/app_launcher"})
   Check(CommandArgs.SafeArg(id),"a legitimate id/arg is accepted by the validator: "+id);
  Check(CommandArgs.SafeArg("-u")&&CommandArgs.SafeArg("-k")&&CommandArgs.SafeArg("-s"),"the exact app_launcher flags are accepted");
  // Adversarial appId text must be REFUSED, never quoted (quoting ' or \" would force /bin/sh and break exact-pid kill).
  foreach(var uarg in new[]{"a b","a;b","a&b","a|b","a$b","a`b","a>b","a<b","a(b","a'b","a\"b","a\\b","a*b","a?b","a#b","a~b","a=b","a[b","-rf","","xé","a\nb"})
   Check(!CommandArgs.SafeArg(uarg),"an unsafe argument is refused: "+uarg.Replace("\n","\\n"));
  var safeLine=CommandArgs.BuildLine(AppLauncher.Launch("1234","org.tizen.netflix-app"));
  Check(safeLine=="/usr/bin/app_launcher -u 1234 -s org.tizen.netflix-app","a safe launch builds the exact space-joined command line (direct exec, no shell)");
  Check(CommandArgs.BuildLine(AppLauncher.Stop("1234","evil;rm -rf /"))==null,"an unsafe target id refuses the whole command (null line, nothing spawned)");

  // --- /proc start-time identity parser (guards kill against a reused pid) ---
  Check(ProcStat.ParseStartTime("1234 (app launcher) S 1 1 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 55512 0 0")==55512,"start-time is field 22, parsed after the comm parens even when comm has spaces/parens");
  Check(ProcStat.ParseStartTime("")==0&&ProcStat.ParseStartTime("garbage")==0,"a malformed /proc stat line parses to 0 (identity unknown), never throws");
  // --- pure kill-identity guard: only a positive pid with matching, present start-times may signal ---
  Check(ProcStat.IdentityMatches(100,7,7),"kill-identity: a known pid whose recorded and current start-times match may be signalled");
  Check(!ProcStat.IdentityMatches(100,7,0),"kill-identity: a disappeared process (current start unknown) must NOT be signalled");
  Check(!ProcStat.IdentityMatches(100,0,7),"kill-identity: a missing recorded start must NOT be signalled");
  Check(!ProcStat.IdentityMatches(100,7,9),"kill-identity: a reused pid (different start-time) must NOT be signalled");
  Check(!ProcStat.IdentityMatches(0,7,7)&&!ProcStat.IdentityMatches(-1,7,7),"kill-identity: a non-positive pid must NOT be signalled");
  // --- fail-closed init decision: ready ONLY when all three handlers registered non-null ---
  Check(EcoreCommandRunner.HandlersOk(new IntPtr(1),new IntPtr(2),new IntPtr(3)),"init: three non-null handlers => ready");
  Check(!EcoreCommandRunner.HandlersOk(IntPtr.Zero,new IntPtr(2),new IntPtr(3))&&!EcoreCommandRunner.HandlersOk(new IntPtr(1),IntPtr.Zero,new IntPtr(3))&&!EcoreCommandRunner.HandlersOk(new IntPtr(1),new IntPtr(2),IntPtr.Zero),"init: any null handler => NOT ready (fail closed, no partial registration)");

  // --- EcoreCommand state machine (pure; no ecore), the real defect transitions ---
  static EcoreCommand MakeCmd(out CommandRun r){r=new CommandRun();return new EcoreCommand(r,CommandRunner.StdoutCap,CommandRunner.StderrCap){Pid=100,StartId=7};}
  // 1. A clean self-exit before the deadline: exit code reported, not timed out, not killed.
  var c1=MakeCmd(out var r1);c1.OnData(false,Encoding.UTF8.GetBytes("done"),4);c1.OnDel(0,true);
  Check(r1.Done&&r1.Result.Started&&r1.Result.ExitCode==0&&!r1.Result.TimedOut&&!r1.Result.Killed&&r1.Result.Stdout=="done","ecore: a self-exit before the deadline reports its exit code and output");
  // 2. Deadline -> SUCCESSFUL kill (MarkKilled) -> DEL (signal death): requested once, TimedOut+Killed.
  var c2=MakeCmd(out var r2);Check(c2.Deadline(),"ecore: the deadline requests a kill");Check(!c2.Deadline(),"ecore: the deadline never requests a second kill");c2.MarkKilled();c2.OnDel(-1,false);
  Check(r2.Done&&r2.Result.TimedOut&&r2.Result.Killed&&r2.Result.ExitCode==-1&&r2.Result.AbandonedReaders==0,"ecore: deadline then a SUCCESSFUL kill DEL is TimedOut+Killed, not abandoned");
  // 3. Deadline -> SUCCESSFUL kill -> NO DEL -> bounded abandon: completes TimedOut+Killed with the flag.
  var c3=MakeCmd(out var r3);c3.Deadline();c3.MarkKilled();c3.Abandon();
  Check(r3.Done&&r3.Result.TimedOut&&r3.Result.Killed&&r3.Result.AbandonedReaders==1,"ecore: a successful kill with no DEL is abandoned after the post-kill grace (bounded), not a hang");
  // 3b. Deadline -> kill DENIED/failed (never MarkKilled) -> grace -> abandon: finishes bounded, TimedOut but NOT Killed.
  var c3b=MakeCmd(out var r3b);c3b.Deadline();c3b.Abandon();
  Check(r3b.Done&&r3b.Result.TimedOut&&!r3b.Result.Killed&&r3b.Result.AbandonedReaders==1,"ecore: an identity-denied or failed signal still finishes bounded (TimedOut) and never claims Killed");
  // 4. A late DEL after abandon, and a duplicate DEL, are ignored -- the run completes exactly once.
  var c4=MakeCmd(out var r4);c4.OnDel(0,true);c4.OnDel(5,true);c4.Deadline();c4.Abandon();
  Check(r4.Done&&r4.Result.ExitCode==0&&!r4.Result.TimedOut,"ecore: a duplicate/late DEL (and a post-completion deadline) cannot re-complete or overwrite the run");
  // 5. Output past the cap is bounded and the command still completes.
  var c5=MakeCmd(out var r5);c5.OnData(false,new byte[CommandRunner.StdoutCap*2],CommandRunner.StdoutCap*2);c5.OnData(true,new byte[CommandRunner.StderrCap*2],CommandRunner.StderrCap*2);c5.OnDel(0,true);
  Check(r5.Result.Stdout.Length<=CommandRunner.StdoutCap&&r5.Result.Stderr.Length<=CommandRunner.StderrCap,"ecore: stdout/stderr are capped at 64 KiB / 16 KiB");
  Check(EcoreCommandRunner.PostKillWaitMs>100,"ecore: the post-kill grace exceeds the ~100 ms piped-exe DEL delay");
  int graceFallbacks=0;
  EcoreCommandRunner.ScheduleGrace(()=>IntPtr.Zero,()=>graceFallbacks++);
  Check(graceFallbacks==1,"ecore: a missing grace timer completes cleanup immediately exactly once");
  graceFallbacks=0;
  EcoreCommandRunner.ScheduleGrace(()=>throw new InvalidOperationException("timer unavailable"),()=>graceFallbacks++);
  Check(graceFallbacks==1,"ecore: a throwing grace timer completes cleanup immediately exactly once");
  graceFallbacks=0;
  EcoreCommandRunner.ScheduleGrace(()=>new IntPtr(1),()=>graceFallbacks++);
  Check(graceFallbacks==0,"ecore: an allocated grace timer retains delayed cleanup");

  // --- HomeReturnGuard: the proven TV edge (an ordinary app cancelled by Home surfaces ~3 s later) ---
  const string ownHome="TestPkg001.QuietTV";
  // 1. The REGRESSION. The exact late-surfacing cancelled app reclaims home EXACTLY ONCE; our own and
  //    unrelated foregrounds are ignored while it waits. Neuter the arm or the match branch and this fails.
  var g=new HomeReturnGuard();
  g.Arm("org.tizen.netflix-app",ownHome,1000);
  Check(g.Armed&&g.Target=="org.tizen.netflix-app","guard arms for a cancelled ordinary app, keyed to its runtime id");
  Check(!g.Observe("com.samsung.tv.csfs",10),"guard ignores the system/own Home foreground while it waits");
  Check(!g.Observe("org.tizen.primevideo",20),"guard ignores an unrelated app in the foreground");
  Check(g.Armed,"an unrelated or own foreground never disarms the guard before the deadline");
  Check(g.Observe("org.tizen.netflix-app",900),"the exact late-surfacing cancelled app reclaims home once (the proven TV edge)");
  Check(!g.Armed,"the guard is one-shot: it disarms the moment it reclaims");
  Check(!g.Observe("org.tizen.netflix-app",950),"the guard never reclaims twice for one arming");
  // 2. Expiry: a late surface AT or AFTER the deadline must NOT reclaim (bounded; never forces home forever).
  var gx=new HomeReturnGuard();gx.Arm("org.tizen.netflix-app",ownHome,1000);
  Check(!gx.Observe("org.tizen.netflix-app",1000)&&!gx.Armed,"at the deadline the guard expires and never reclaims");
  var gx2=new HomeReturnGuard();gx2.Arm("org.tizen.netflix-app",ownHome,1000);
  Check(!gx2.Observe("org.tizen.netflix-app",5000)&&!gx2.Armed,"well past the deadline the guard expires and never reclaims");
  // 3. Explicit newer launch / dismissal cancels the guard so a stale launch can never steal focus.
  var gc=new HomeReturnGuard();gc.Arm("org.tizen.netflix-app",ownHome,1000);gc.Cancel();
  Check(!gc.Armed&&!gc.Observe("org.tizen.netflix-app",10),"a newer launch or dismissal cancels the guard (no reclaim afterwards)");
  // 4. Newer launch suppresses the OLD target: re-arming watches only the newest cancelled app.
  var gr=new HomeReturnGuard();gr.Arm("org.tizen.netflix-app",ownHome,1000);gr.Arm("org.tizen.primevideo",ownHome,2000);
  Check(gr.Target=="org.tizen.primevideo"&&!gr.Observe("org.tizen.netflix-app",10),"re-arming watches only the newest cancelled app, not the old one");
  Check(gr.Observe("org.tizen.primevideo",20),"the newest cancelled app reclaims home");
  // 5. Empty/unknown, our own registered home, and IPTV are refused (never arm against those).
  var ge=new HomeReturnGuard();
  ge.Arm("",ownHome,1000);Check(!ge.Armed,"an empty target never arms the guard");
  ge.Arm("   ",ownHome,1000);Check(!ge.Armed,"a whitespace target never arms the guard");
  ge.Arm(ownHome,ownHome,1000);Check(!ge.Armed,"the guard never arms against our own registered home");
  ge.Arm(HomeReturnGuard.IptvId,ownHome,1000);Check(!ge.Armed,"the guard never arms against IPTV (no app_launcher foreground to reclaim)");
  // An ineligible re-arm disarms a previously-armed guard rather than leaving a stale watch.
  ge.Arm("org.tizen.netflix-app",ownHome,1000);ge.Arm("",ownHome,1000);Check(!ge.Armed,"an ineligible re-arm stands the guard down");
  // 6. Pre-registered window.
  Check(HomeReturnGuard.WindowMs==8000,"the Home-return guard window is the pre-registered 8000 ms");

  Console.WriteLine("PASS "+count+" command lifecycle assertions");return count;
 }
}
