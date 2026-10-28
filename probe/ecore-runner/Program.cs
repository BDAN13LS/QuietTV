// Ecore-runner probe -- the ON-TV validation for the asynchronous app stop/launch fix.
//
// It compiles the SHIPPED runner (launcher/host/CommandLifecycle.cs -> EcoreCommandRunner) and drives
// it against FIXED, harmless children only, under a real ecore main loop. It is the primary evidence
// that the fix works, because the .NET-Process-under-ecore crash (SIGABRT from a ProcessWaitState /
// ecore SIGCHLD race) reproduces only on the real ARMv7 Tizen 9 .NET 6 TV, never on Windows.
//
// SAFETY: no UI/canvas, no window, no root, no app_launcher, no target apps, no network, no owner
// files. Only /bin/echo, /bin/sleep, /bin/cat and reads of our OWN children's /proc entries. It never
// takes arbitrary command input. The PARENT builds, deploys and runs this on the TV; nothing here
// deploys anything. A single final line (ECORE-PROBE: PASS|FAIL) and a non-zero exit on failure.
//
// FILE-DESCRIPTOR DIAGNOSIS (added Oct 2026): the earlier whole-run "no FD leak" check compared one count
// taken BEFORE the first runner use and BEFORE the live .NET coexist child, to one taken AFTER every case
// while that coexist child was STILL ALIVE -- so it charged one-time runtime/Process init and an active
// child's own handles as a "leak". It is replaced here by a regression-sensitive, warmup-based steady-
// state verdict (FdSteady) over 10/30/60-command batches, plus bounded per-stage evidence (own
// /proc/self/fd only, categorised, with the added link targets named) so the parent can see exactly what
// the +N is. The tolerance stays +2 and is NOT loosened; one-time init is simply paid before the baseline.
//
// Build (offline, pinned SDK) and cross-publish for the TV exactly as the app is published, e.g.:
//   dotnet publish probe/ecore-runner/EcoreRunnerProbe.csproj -c Release -r linux-arm --self-contained false -o out
// then copy `out` to the TV and run the dll with the device's dotnet. See README.md in this folder.
// The pure comparator can be exercised on Windows (no TV) with `dotnet EcoreRunnerProbe.dll fd-selftest`.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

static class Program {
 [DllImport("libecore.so.1")] static extern int ecore_init();
 [DllImport("libecore.so.1")] static extern void ecore_main_loop_begin();
 [DllImport("libecore.so.1")] static extern void ecore_main_loop_quit();
 [DllImport("libecore.so.1")] static extern IntPtr ecore_timer_add(double seconds,TimerCb cb,IntPtr data);
 // Used ONLY for the direct-exec identity check below (independent of the runner).
 [DllImport("libecore.so.1")] static extern IntPtr ecore_exe_pipe_run(string cmd,int flags,IntPtr data);
 [DllImport("libecore.so.1")] static extern int ecore_exe_pid_get(IntPtr exe);
 [DllImport("libc",SetLastError=true)] static extern int kill(int pid,int sig);
 [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int TimerCb(IntPtr data);

 static readonly List<string> results=new();
 static int failures;
 static void Rec(bool ok,string what){results.Add((ok?"PASS ":"FAIL ")+what);if(!ok)failures++;}

 static long ticks;                       // advanced by a 50 ms liveness timer; proves the loop stays responsive
 static readonly TimerCb Liveness=_=>{ticks++;return 1;};
 static readonly TimerCb Watchdog=_=>{Finish("watchdog timeout (loop or a child hung the probe itself)");return 0;};
 static readonly TimerCb Driver=Drive;

 // The fixed child plan. Each step dispatches one command through the shipped runner and validates the
 // bounded ProcessResult, OR (Action != null) runs a main-loop-only diagnostic step (an FD snapshot or the
 // coexist .NET child start) with no command dispatched. No step takes external input.
 sealed class Step{internal string Label;internal ProcessStartInfo Info;internal Func<ProcessResult,string> Check;internal bool RecordLiveness;internal int DeadlineMs=CommandRunner.DefaultDeadlineMs;internal Action Action;}
 static readonly Queue<Step> plan=new();
 static CommandRun current;static long livenessAtDispatch;static Step step;
 static Process coexist; // a live .NET child: installs .NET's SIGCHLD handler like IPTV/web do in the app
 static long coexistStart; // its /proc start-time, so even its cleanup kill is identity-guarded

 static ProcessStartInfo Psi(string file,params string[] args){var p=new ProcessStartInfo(file);foreach(var a in args)p.ArgumentList.Add(a);return p;}

 // ---- Bounded, own-process-only file-descriptor evidence -----------------------------------------------
 // A captured stage: the readlink target of every /proc/self/fd entry of OUR process (never env, cmdline
 // or another process), plus the live ecore handle-map sizes. Targets is null if /proc/self/fd is
 // unreadable on this device -- an unmeasurable stage, never asserted as a leak.
 sealed class FdStage{internal string Name;internal int Commands;internal List<string> Targets;internal (int ByHandle,int ById,int Orphans) Diag;}
 static readonly List<FdStage> stages=new();

 static List<string> FdTargets(){
  try{
   var list=new List<string>();
   foreach(var p in Directory.GetFileSystemEntries("/proc/self/fd")){
    string tgt;try{tgt=new FileInfo(p).LinkTarget;}catch{tgt=null;} // readlink only; never opens/reads the target
    list.Add(string.IsNullOrEmpty(tgt)?"unknown":tgt);
   }
   return list;
  }catch{return null;}
 }
 static void Capture(string name,int cmds)=>stages.Add(new FdStage{Name=name,Commands=cmds,Targets=FdTargets(),Diag=EcoreCommandRunner.Diagnostics});
 static FdStage Stage(string name)=>stages.Find(s=>s.Name==name);

 static string Cat(string t){
  if(string.IsNullOrEmpty(t)||t=="unknown")return "unknown";
  if(t.StartsWith("pipe:"))return "pipe";
  if(t.StartsWith("socket:"))return "socket";
  if(t.StartsWith("anon_inode:"))return "anon";       // eventpoll/eventfd/timerfd/signalfd/inotify
  if(t.StartsWith("/dev/"))return "dev";
  if(t.StartsWith("/"))return "file";
  return "other";
 }
 static string StageLine(FdStage s){
  if(s==null)return "FD-STAGE <missing>";
  if(s.Targets==null)return $"FD-STAGE {s.Name} cmds={s.Commands} total=unavailable";
  int pipe=0,sock=0,anon=0,dev=0,file=0,other=0,unk=0;
  foreach(var t in s.Targets)switch(Cat(t)){case "pipe":pipe++;break;case "socket":sock++;break;case "anon":anon++;break;case "dev":dev++;break;case "file":file++;break;case "unknown":unk++;break;default:other++;break;}
  return $"FD-STAGE {s.Name} cmds={s.Commands} total={s.Targets.Count} pipe={pipe} socket={sock} anon={anon} dev={dev} file={file} other={other} unknown={unk} maps(byHandle={s.Diag.ByHandle},byId={s.Diag.ById},orphans={s.Diag.Orphans})";
 }
 // The distinct link targets present in `to` but not in `from`, bounded. Names the actual additions (e.g.
 // pipe:[12345], anon_inode:[eventpoll]) so a one-time/active-child handle is told apart from a leak.
 static string AddedLine(FdStage from,FdStage to,int cap){
  if(from?.Targets==null||to?.Targets==null)return $"FD-ADDED {to?.Name} vs {from?.Name} unavailable";
  var baseCounts=new Dictionary<string,int>();
  foreach(var t in from.Targets){baseCounts.TryGetValue(t,out var c);baseCounts[t]=c+1;}
  var added=new List<string>();
  foreach(var t in to.Targets){if(baseCounts.TryGetValue(t,out var c)&&c>0)baseCounts[t]=c-1;else added.Add(t);}
  int shown=Math.Min(added.Count,cap);
  return $"FD-ADDED {to.Name} vs {from.Name} count={added.Count} [{string.Join(", ",added.GetRange(0,shown))}{(added.Count>shown?", ...":"")}]";
 }
 // The ordered /proc/self/fd totals for named stages, or null if any is unmeasurable.
 static int[] NamedTotals(params string[] names){
  var list=new List<int>();
  foreach(var n in names){var s=Stage(n);if(s?.Targets==null)return null;list.Add(s.Targets.Count);}
  return list.ToArray();
 }
 // Pure, Windows-testable ACCEPTANCE verdict for the warm-baseline FD samples. `fdTotals` are
 // /proc/self/fd counts sampled AFTER an explicit warmup (index 0 = the warm baseline) at increasing
 // command counts. FAILS CLOSED: null, fewer than `required` ordered samples, or any negative count =>
 // Unavailable (which the probe treats as a FAILED check, never a pass). A later sample more than
 // `tolerance` above the baseline => Leak (a per-command descriptor leak). Otherwise Accept. One-time init
 // and active-child handles are paid before the baseline, so they cannot trip it; only accumulation past
 // the tolerance does. Regression-sensitive: a slow leak still crosses the tolerance across the 60-batch.
 internal static FdVerdict FdClassify(IReadOnlyList<int> fdTotals,int tolerance,int required){
  if(fdTotals==null||fdTotals.Count<required)return FdVerdict.Unavailable; // missing baseline/batch samples
  foreach(var fd in fdTotals)if(fd<0)return FdVerdict.Unavailable;         // a negative count is not real data
  int baseline=fdTotals[0];
  foreach(var fd in fdTotals)if(fd-baseline>tolerance)return FdVerdict.Leak;
  return FdVerdict.Accept;
 }

 // Pure, Windows-testable ACCEPTANCE verdict for the between-command ecore handle maps. Each snapshot's
 // (byHandle,byId,orphans) MUST be (0,0,0): a nonzero size is a managed tracking-map leak that a native
 // /proc/self/fd count could miss. FAILS CLOSED: null list, fewer than `required` snapshots, or any
 // missing (null) snapshot => false. Only all-present, all-zero accepts.
 internal static bool DiagAccept(IReadOnlyList<(int ByHandle,int ById,int Orphans)?> diags,int required){
  if(diags==null||diags.Count<required)return false;
  foreach(var d in diags){if(d==null)return false;var v=d.Value;if(v.ByHandle!=0||v.ById!=0||v.Orphans!=0)return false;}
  return true;
 }
 // -------------------------------------------------------------------------------------------------------

 static int Main(string[] args){
  // Finite flood-child mode: write FIXED, bounded output and exit 0 BEFORE touching ecore. The ordinary
  // probe re-launches THIS assembly in this mode (device dotnet + our own dll + the literal "flood-child",
  // all safe args, no arbitrary input) to test the stdout/stderr caps with a child that ENDS on its own --
  // unlike an infinite /dev/zero -- so the caps are tested independently of the deadline/kill path.
  if(args.Length>0&&args[0]=="flood-child"){
   Console.Out.Write(new string('A',128*1024));Console.Error.Write(new string('B',32*1024));
   Console.Out.Flush();Console.Error.Flush();return 0;
  }
  // Pure comparator self-test (no ecore, no TV): proves the SHIPPED FdClassify and DiagAccept verdicts,
  // both ways AND their fail-closed (missing/negative) cases, so the FD acceptance cannot silently
  // regress. Returns before any native call, so it runs on Windows with the pinned SDK. The Python mirror
  // (tests/test_ecore_probe_fd.py) only corroborates these.
  if(args.Length>0&&args[0]=="fd-selftest"){
   int ok=0,bad=0;
   void T(bool cond,string what){if(cond){ok++;Console.WriteLine("PASS "+what);}else{bad++;Console.WriteLine("FAIL "+what);}}
   const int req=4; // the four required ordered stages: warmup-baseline + after-batch-10/30/60
   // FdClassify, both ways.
   T(FdClassify(new[]{50,50,51,50},2,req)==FdVerdict.Accept,"a stable warm set across 10/30/60 batches is ACCEPT");
   T(FdClassify(new[]{50,52,56,62},2,req)==FdVerdict.Leak,"a steadily leaking set is classified LEAK");
   T(FdClassify(new[]{50,51,52,52},2,req)==FdVerdict.Accept,"growth of exactly the tolerance is ACCEPT");
   T(FdClassify(new[]{50,51,52,53},2,req)==FdVerdict.Leak,"growth of tolerance+1 is LEAK");
   T(FdClassify(new[]{48,50,47,49},2,req)==FdVerdict.Accept,"transient dips below baseline are ACCEPT");
   // FdClassify fail-closed: null, too few samples, and a negative count must NOT accept.
   T(FdClassify(null,2,req)==FdVerdict.Unavailable,"null FD totals FAIL CLOSED (Unavailable, not Accept)");
   T(FdClassify(new[]{50,50,51},2,req)==FdVerdict.Unavailable,"fewer than the 4 required stages FAIL CLOSED");
   T(FdClassify(new[]{50,-1,50,50},2,req)==FdVerdict.Unavailable,"a negative (unreadable) count FAILS CLOSED");
   T(FdClassify(System.Array.Empty<int>(),2,req)==FdVerdict.Unavailable,"an empty sample FAILS CLOSED");
   // DiagAccept: between-command ecore maps must all be (0,0,0); nonzero and missing fail closed.
   var clean=new (int,int,int)?[]{(0,0,0),(0,0,0),(0,0,0)};
   var leaky=new (int,int,int)?[]{(0,0,0),(1,0,0),(0,0,0)};
   var hasNull=new (int,int,int)?[]{(0,0,0),null,(0,0,0)};
   T(DiagAccept(clean,3),"all-zero between-command maps ACCEPT");
   T(!DiagAccept(leaky,3),"a nonzero handle map is rejected (managed-map leak)");
   T(!DiagAccept(hasNull,3),"a missing snapshot FAILS CLOSED");
   T(!DiagAccept(clean,6),"fewer snapshots than required FAILS CLOSED");
   T(!DiagAccept(null,3),"null diagnostics FAIL CLOSED");
   Console.WriteLine("FD-SELFTEST: "+(bad==0?"PASS":"FAIL")+" ("+ok+"/"+(ok+bad)+")");
   return bad==0?0:1;
  }
  // LONG-LIVED HELPER probe (the IPTV player / web dashboard viewer path): its own ecore loop and plan,
  // separate from the ordinary command plan below. See RunHelperProbe. One final line, non-zero on failure.
  if(args.Length>0&&args[0]=="helper")return RunHelperProbe();
  if(ecore_init()==0){Console.WriteLine("FAIL ecore_init returned 0");Console.WriteLine("ECORE-PROBE: FAIL");return 1;}

  // Symbol/export presence: the runner needs these ecore_exe exports; resolve them up front for a clear check.
  try{var lib=NativeLibrary.Load("libecore.so.1");foreach(var sym in new[]{"ECORE_EXE_EVENT_DEL","ECORE_EXE_EVENT_DATA","ECORE_EXE_EVENT_ERROR"})NativeLibrary.GetExport(lib,sym);Rec(true,"ecore_exe event exports present");}
  catch(Exception e){Rec(false,"ecore_exe event exports present: "+e.Message);}

  // Direct-exec identity: spawn /bin/sleep via the SHIPPED command builder; the ecore_exe handle's pid
  // must BE the sleep process (comm "(sleep)"), proving no /bin/sh wrapper -- so a kill hits the CLI, not a shell.
  try{
   string line=CommandArgs.BuildLine(Psi("/bin/sleep","2"));
   IntPtr h=ecore_exe_pipe_run(line,1/*PIPE_READ*/,IntPtr.Zero);
   int pid=ecore_exe_pid_get(h);
   string stat=File.ReadAllText("/proc/"+pid+"/stat");
   int close=stat.LastIndexOf(')');string comm=stat.Substring(0,close+1);
   Rec(h!=IntPtr.Zero&&pid>0&&comm.EndsWith("(sleep)")&&int.Parse(stat.Split(' ')[0])==pid,"direct exec: ecore_exe_pid_get is the sleep CLI pid, not a /bin/sh wrapper (comm="+comm+")");
   // Clean up the probe's own 2 s sleep -- identity-guarded, exact pid only (no "reuse impossible" shortcut).
   long st=ProcStat.ParseStartTime(stat);if(ProcStat.IdentityMatches(pid,st,ProcStat.ReadStartTime(pid)))kill(pid,9);
  }catch(Exception e){Rec(false,"direct exec identity: "+e.Message);}

  // Build the runner-driven plan: 30 echo children (the race fired often -- many runs give confidence),
  // one hung child killed at the deadline with the loop still ticking, and a FINITE output-flood child.
  for(int i=0;i<30;i++){int n=i;plan.Enqueue(new Step{Label="echo"+n,Info=Psi("/bin/echo","QTV_PROBE_OK"),Check=r=>r.Started&&r.ExitCode==0&&!r.TimedOut&&!r.Killed&&r.Stdout.Contains("QTV_PROBE_OK")?null:"echo not clean exit0: "+Desc(r)});}

  // FD STEADY-STATE INSTRUMENTATION. The 30 echoes above warmed every one-time cost on the ecore_exe path
  // (handler registration, ecore's exe subsystem). From this WARM baseline, run batches of 10/30/60 more
  // echoes and snapshot /proc/self/fd between them: a per-command descriptor leak grows with the batch
  // size, a warm steady state does not. The .NET coexist child (the only .NET Process) is NOT started yet,
  // so its one-time init is excluded from this baseline on purpose.
  plan.Enqueue(Snap("warmup-baseline",0));
  int cum=0;
  foreach(int batch in new[]{10,30,60}){
   for(int i=0;i<batch;i++){int n=i;plan.Enqueue(new Step{Label="fdbatch"+batch+"_"+n,Info=Psi("/bin/echo","QTV_PROBE_OK"),Check=r=>r.Started&&r.ExitCode==0&&!r.TimedOut&&!r.Killed&&r.Stdout.Contains("QTV_PROBE_OK")?null:"fd-batch echo not clean exit0: "+Desc(r)});}
   cum+=batch;plan.Enqueue(Snap("after-batch-"+batch,cum));
  }

  // Separate, fixed deadline/kill test: a 60 s sleep must be killed at the ~2500 ms deadline, loop still ticking.
  plan.Enqueue(new Step{Label="hung",Info=Psi("/bin/sleep","60"),RecordLiveness=true,Check=r=>r.Started&&r.TimedOut&&r.Killed?null:"hung child not killed at the deadline: "+Desc(r)});
  // Finite flood: re-launch THIS assembly's flood-child mode (device dotnet + our own dll + a literal arg,
  // all safe). It writes 128 KiB stdout + 32 KiB stderr then exits 0, so the check is the CAPS (exactly
  // 64 KiB / 16 KiB) AND a clean exit 0 -- NOT a timeout. A larger deadline absorbs .NET cold start.
  string self=System.Reflection.Assembly.GetEntryAssembly().Location;string host=Environment.ProcessPath;
  plan.Enqueue(new Step{Label="flood",DeadlineMs=8000,Info=Psi(host,self,"flood-child"),Check=r=>r.Started&&r.ExitCode==0&&!r.TimedOut&&r.Stdout.Length==CommandRunner.StdoutCap&&r.Stderr.Length==CommandRunner.StderrCap?null:"flood child not cleanly capped at exit (outLen="+r.Stdout.Length+" errLen="+r.Stderr.Length+"): "+Desc(r)});
  // Coexist: install .NET's SIGCHLD handler (the IPTV/web shape), then 3 more echo children must still
  // get their ecore DEL. This is the one gap between an isolated probe and the real app. The FD snapshots
  // on either side isolate the .NET Process one-time init and its OWN still-live handles (expected, not a
  // leak) from any per-command growth.
  plan.Enqueue(Snap("before-coexist",cum));
  plan.Enqueue(new Step{Label="coexist-start",Info=null,Check=_=>null});
  for(int i=0;i<3;i++){int n=i;plan.Enqueue(new Step{Label="coexist-echo"+n,Info=Psi("/bin/echo","QTV_PROBE_OK"),Check=r=>r.Started&&r.ExitCode==0&&!r.TimedOut&&r.Stdout.Contains("QTV_PROBE_OK")?null:"DEL lost while a .NET Process (SIGCHLD handler) is alive: "+Desc(r)});}
  plan.Enqueue(Snap("coexist-alive",cum)); // coexist .NET child still alive here: its handles are counted

  Capture("pre-run",0); // before the first runner command and before the .NET coexist child -- the raw "before"
  ecore_timer_add(0.05,Liveness,IntPtr.Zero);
  ecore_timer_add(0.02,Driver,IntPtr.Zero);
  ecore_timer_add(60.0,Watchdog,IntPtr.Zero);
  ecore_main_loop_begin();

  // The loop has exited, so ecore is no longer pumping: only now may .NET wait on/reap its own child (the
  // ProcessWaitState/ecore-SIGCHLD FailFast race exists ONLY while the loop pumps). Clean up honestly:
  //  1. identity-guarded exact-pid SIGKILL -- signal ONLY the child we own, never a reused/mismatched pid;
  //  2. a BOUNDED WaitForExit so a stuck child cannot hang the probe;
  //  3. Dispose the Process AFTER the bookkeeping, so the final snapshot sees the handle actually released.
  // The snapshot proves reclaim only if cleanup actually completed, which is recorded and asserted below.
  string coexistCleanup="no-coexist-child";
  if(coexist!=null){
   try{
    bool owned=!coexist.HasExited&&ProcStat.IdentityMatches(coexist.Id,coexistStart,ProcStat.ReadStartTime(coexist.Id));
    if(owned)kill(coexist.Id,9); // exact owned pid only; an unknown/mismatched child is never signalled
    bool exited=coexist.WaitForExit(2000); // bounded reap; loop is not pumping so this cannot race ecore
    coexistCleanup=coexist.HasExited?(exited?"killed-and-reaped":"reaped")
                  :owned?"signalled-but-not-exited"
                  :"identity-mismatch-not-signalled";
    coexist.Dispose(); // release the Process handle AFTER bookkeeping, before the reclaim snapshot
   }catch(Exception e){coexistCleanup="cleanup-error="+e.Message;}
  }
  Capture("coexist-dead",cum); // taken AFTER the child is reaped and the Process disposed -- true reclaim point

  // Per-stage FD evidence (bounded; own /proc/self/fd only). The parent reads this to see what the +N is.
  foreach(var s in stages)Console.WriteLine(StageLine(s));
  Console.WriteLine(AddedLine(Stage("warmup-baseline"),Stage("after-batch-60"),20)); // per-command additions (expect ~0)
  Console.WriteLine(AddedLine(Stage("before-coexist"),Stage("coexist-alive"),20));   // the .NET Process init + its live handles
  Console.WriteLine(AddedLine(Stage("coexist-alive"),Stage("coexist-dead"),20));      // reclaimed on the child's death (negative delta)
  var pre=Stage("pre-run");var alive=Stage("coexist-alive");var dead=Stage("coexist-dead");
  Console.WriteLine($"FD-WHOLE-RUN before={pre?.Targets?.Count} after-loop(coexist alive)={alive?.Targets?.Count} after-coexist-cleanup={dead?.Targets?.Count} (the old whole-run check compared the first two across a DELIBERATELY live child)");

  // Coexist cleanup is recorded honestly and asserted: only a child that was actually reaped (or had
  // already exited) proves the reclaim snapshot; a stuck or unsignalled child must NOT pass.
  Rec(coexistCleanup=="no-coexist-child"||coexistCleanup=="killed-and-reaped"||coexistCleanup=="reaped","coexist .NET child cleaned up before the reclaim snapshot ("+coexistCleanup+")");

  // The FD verdict: a regression-sensitive, warm-baseline steady-state ACCEPTANCE over the 10/30/60
  // batches. The tolerance is +2 (unchanged, NOT loosened). It FAILS CLOSED: missing baseline/batch
  // samples or a negative count are Unavailable, which is a FAILED check here -- never a silent pass.
  var totals=NamedTotals("warmup-baseline","after-batch-10","after-batch-30","after-batch-60");
  var fdVerdict=FdClassify(totals,2,4);
  Rec(fdVerdict==FdVerdict.Accept,"no per-command file-descriptor leak across 10/30/60-command batches [verdict="+fdVerdict+"] "+(totals==null?"(/proc/self/fd unavailable -- FAIL CLOSED, acceptance not granted)":$"(warm baseline={totals[0]} -> {string.Join("/",totals)}, tol=2)"));

  // Between-command ecore handle maps must be empty at EVERY snapshot: a nonzero byHandle/byId/orphans is
  // a managed tracking-map leak that the native FD count could miss. Fail-closed on any missing snapshot.
  var diagStages=new[]{"warmup-baseline","after-batch-10","after-batch-30","after-batch-60","before-coexist","coexist-alive"};
  var diags=new List<(int,int,int)?>();foreach(var n in diagStages){var s=Stage(n);diags.Add(s==null?((int,int,int)?)null:(s.Diag.ByHandle,s.Diag.ById,s.Diag.Orphans));}
  Rec(DiagAccept(diags,diagStages.Length),"ecore handle maps empty between every command (no managed-map leak) "+string.Join(" ",diags.ConvertAll(d=>d==null?"missing":$"({d.Value.Item1},{d.Value.Item2},{d.Value.Item3})")));

  foreach(var r in results)Console.WriteLine(r);
  Console.WriteLine("ECORE-PROBE: "+(failures==0?"PASS":"FAIL")+" ("+(results.Count-failures)+"/"+results.Count+")");
  return failures==0?0:1;
 }

 static Step Snap(string name,int cmds)=>new Step{Label="snap:"+name,Action=()=>Capture(name,cmds)};

 static string Desc(ProcessResult r)=>$"started={r.Started} exit={r.ExitCode} timedOut={r.TimedOut} killed={r.Killed} abandoned={r.AbandonedReaders} stderr={r.Stderr}";

 static int Drive(IntPtr data){
  try{
   if(current!=null){
    if(!current.Done)return 1; // keep the loop turning; never block on the child
    var r=current.Result;current=null;
    if(step.RecordLiveness)Rec(ticks-livenessAtDispatch>=20,"the main loop stayed responsive during the hung child (ticks +"+(ticks-livenessAtDispatch)+")");
    string err=step.Check(r);Rec(err==null,step.Label+(err==null?" ok":": "+err));
   }
   if(plan.Count==0){Finish(null);return 0;}
   step=plan.Dequeue();
   // Main-loop-only diagnostic step (FD snapshot): no command, no current; just observe and keep turning.
   if(step.Action!=null){try{step.Action();}catch(Exception e){Rec(false,step.Label+": "+e.Message);}return 1;}
   // Long-lived on purpose: if this .NET child EXITED while the ecore loop pumps, ecore's waitpid(-1)
   // would reap it first and .NET's ProcessWaitState would FailFast -- aborting the PROBE with no final
   // line. 120 s is safely above the 60 s watchdog, so it cannot exit during the run; it is killed via
   // libc at loop-exit (and self-clears in 2 min if the probe itself ever aborts before that).
   if(step.Label=="coexist-start"){try{coexist=Process.Start(Psi("/bin/sleep","120"));coexistStart=coexist!=null?ProcStat.ReadStartTime(coexist.Id):0;Rec(coexist!=null,"started a live .NET Process to install the SIGCHLD handler");}catch(Exception e){Rec(false,"coexist .NET Process.Start: "+e.Message);}return 1;}
   livenessAtDispatch=ticks;
   current=EcoreCommandRunner.Begin(new CommandSpec{Label=step.Label,Info=step.Info,DeadlineMs=step.DeadlineMs});
   return 1;
  }catch(Exception e){Finish("driver crashed: "+e.Message);return 0;}
 }

 static bool finished;
 static void Finish(string fatal){if(finished)return;finished=true;if(fatal!=null)Rec(false,fatal);ecore_main_loop_quit();}

 // ---- Long-lived helper probe (ILongLivedHelper / EcoreHelperRunner) -----------------------------------
 // ON-TV validation of the LONG-LIVED helper path (the IPTV player and the web dashboard viewer). The crash
 // it guards against -- a .NET System.Diagnostics.Process child exiting WHILE the ecore loop pumps, racing
 // ecore's SIGCHLD reaper into Environment.FailFast/SIGABRT -- reproduces only on the real TV. Here ecore_exe
 // owns every child (via the SHIPPED EcoreHelperRunner): a helper may be force-killed OR exit on its own
 // while the loop pumps, and the probe proves the loop stays responsive, the exit shows up as !Alive, output
 // surfaces through DrainLines, the forced kill is an identity-guarded exact-pid SIGKILL that actually lands,
 // and the adapter map drains to empty (repeatedly, and with several helpers coexisting). It casts the
 // ILongLivedHelper to EcoreHelper only to READ its Killed/Disposed bookkeeping -- same assembly, no new API.
 // Harmless: only /bin/sleep and /bin/echo, our OWN children, no window/root/network/owner files, no
 // arbitrary input, bounded by a 45 s watchdog, a single final line (ECORE-HELPER-PROBE: PASS|FAIL).
 static ILongLivedHelper h1,h2,h3; static readonly ILongLivedHelper[] many=new ILongLivedHelper[3];
 static int phase; static long phaseTick;
 static readonly TimerCb HelperDriver=DriveHelper;
 static bool WaitTicks(long n)=>ticks-phaseTick>=n;   // elapsed since this phase began, in 50 ms liveness ticks
 static void Next(){phase++;phaseTick=ticks;}

 static int RunHelperProbe(){
  if(ecore_init()==0){Console.WriteLine("FAIL ecore_init returned 0");Console.WriteLine("ECORE-HELPER-PROBE: FAIL");return 1;}
  ticks=0;phase=0;phaseTick=0;
  ecore_timer_add(0.05,Liveness,IntPtr.Zero);   // proves the loop keeps turning across every child exit/kill
  ecore_timer_add(0.05,HelperDriver,IntPtr.Zero);
  ecore_timer_add(45.0,Watchdog,IntPtr.Zero);
  ecore_main_loop_begin();
  // The loop has exited, so nothing is pumping: release any helper still held (non-blocking Dispose).
  foreach(var h in new[]{h1,h2,h3,many[0],many[1],many[2]})h?.Dispose();
  foreach(var r in results)Console.WriteLine(r);
  Console.WriteLine("ECORE-HELPER-PROBE: "+(failures==0?"PASS":"FAIL")+" ("+(results.Count-failures)+"/"+results.Count+")");
  return failures==0?0:1;
 }

 static int DriveHelper(IntPtr data){
  try{
   switch(phase){
   case 0: // start a long-lived child (an 8 s sleep stands in for the always-on player/viewer)
    h1=EcoreHelperRunner.Start("/bin/sleep",new[]{"8"});
    Rec(h1!=null&&h1.Started&&h1.Alive&&h1.Pid>0,"long-lived helper started and is Alive (pid "+(h1?.Pid??0)+")");
    if(h1==null){Finish("long-lived helper did not start");return 0;}
    Next();return 1;
   case 1: // it must still be Alive PAST 2.5 s while the loop pumps (60 ticks = 3.0 s), not killed at a deadline
    if(!h1.Alive){Finish("long-lived helper exited before 2.5 s (did it inherit the 2.5 s command deadline?)");return 0;}
    if(!WaitTicks(60))return 1;
    Rec(h1.Alive,"long-lived helper still Alive past 2.5 s -- it did NOT inherit the ordinary command deadline (ticks +"+(ticks-phaseTick)+")");
    Rec(ticks-phaseTick>=50,"the main loop stayed responsive across the long-lived wait (ticks +"+(ticks-phaseTick)+")");
    h1.Kill();Next();return 1;
   case 2: // the identity-guarded SIGKILL must land, the child's DEL must end Alive, the map must drain
    if(h1.Alive){if(WaitTicks(60)){Finish("killed long-lived helper never reached DEL");return 0;}return 1;}
    Rec(((EcoreHelper)h1).Killed,"forced close sent an identity-guarded exact-pid SIGKILL that actually landed");
    Rec(!h1.Alive,"the killed child's ecore DEL ended Alive");
    Rec(EcoreHelperRunner.Tracked==0,"adapter map drained to empty after the killed child's DEL (tracked="+EcoreHelperRunner.Tracked+")");
    Next();return 1;
   case 3: // THE crash case: a child that EXITS ON ITS OWN while the loop pumps (a 1 s sleep)
    h2=EcoreHelperRunner.Start("/bin/sleep",new[]{"1"});
    Rec(h2!=null&&h2.Alive,"a self-exiting helper started and is Alive");
    if(h2==null){Finish("self-exiting helper did not start");return 0;}
    Next();return 1;
   case 4: // its own DEL must arrive (NO FailFast/SIGABRT), the loop still ticking, with no kill attempted
    if(h2.Alive){if(WaitTicks(120)){Finish("self-exiting helper never reached DEL");return 0;}return 1;}
    Rec(!h2.Alive&&!((EcoreHelper)h2).Killed,"a helper that EXITED ON ITS OWN while the loop pumped ended Alive with no kill and without aborting the loop (the ProcessWaitState/SIGABRT race)");
    Rec(ticks-phaseTick>=15,"the main loop kept ticking across the spontaneous exit (ticks +"+(ticks-phaseTick)+")");
    Rec(EcoreHelperRunner.Tracked==0,"adapter map drained to empty after the spontaneous exit (tracked="+EcoreHelperRunner.Tracked+")");
    Next();return 1;
   case 5: // output capture: a child whose complete stdout line must surface through DrainLines
    h3=EcoreHelperRunner.Start("/bin/echo",new[]{"QTV_HELPER_OK"});
    Rec(h3!=null&&h3.Started,"an output helper started");
    if(h3==null){Finish("output helper did not start");return 0;}
    Next();return 1;
   case 6: // after its DEL the captured complete line must be drainable (the fatal= scan's input path)
    if(h3.Alive){if(WaitTicks(120)){Finish("output helper never reached DEL");return 0;}return 1;}
    Rec(Array.IndexOf(h3.DrainLines(),"QTV_HELPER_OK")>=0,"the helper captured the child's complete stdout line through DrainLines");
    Next();return 1;
   case 7: // coexist: three concurrent long-lived helpers (under MaxHelpers), all tracked at once
    for(int i=0;i<3;i++)many[i]=EcoreHelperRunner.Start("/bin/sleep",new[]{"1"});
    Rec(many[0]!=null&&many[1]!=null&&many[2]!=null&&many[0].Alive&&many[1].Alive&&many[2].Alive,"three concurrent long-lived helpers coexist and are Alive");
    Rec(EcoreHelperRunner.Tracked==3,"adapter tracks all three concurrent helpers (tracked="+EcoreHelperRunner.Tracked+")");
    Next();return 1;
   case 8: // each gets its OWN DEL and the shared map drains to empty, loop still responsive
    if(many[0].Alive||many[1].Alive||many[2].Alive){if(WaitTicks(120)){Finish("a concurrent helper never reached DEL");return 0;}return 1;}
    Rec(EcoreHelperRunner.Tracked==0,"every concurrent helper reached DEL and the adapter map drained to empty (tracked="+EcoreHelperRunner.Tracked+")");
    Rec(ticks-phaseTick>=10,"the main loop stayed responsive across the concurrent teardown (ticks +"+(ticks-phaseTick)+")");
    Next();return 1;
   case 9: // repeat lifecycle after a full teardown: start a fresh helper, force-kill it
    h1=EcoreHelperRunner.Start("/bin/sleep",new[]{"8"});
    Rec(h1!=null&&h1.Alive,"a fresh helper starts cleanly after a full teardown (repeat lifecycle)");
    if(h1==null){Finish("repeat helper did not start");return 0;}
    h1.Kill();Next();return 1;
   case 10: // its DEL, the empty map, and an idempotent non-blocking Dispose
    if(h1.Alive){if(WaitTicks(60)){Finish("repeat helper never reached DEL");return 0;}return 1;}
    Rec(((EcoreHelper)h1).Killed&&EcoreHelperRunner.Tracked==0,"the repeat helper was killed and the map drained to empty (tracked="+EcoreHelperRunner.Tracked+")");
    h1.Dispose();h1.Dispose();
    Rec(((EcoreHelper)h1).Disposed,"Dispose is idempotent and non-blocking (never WaitForExit)");
    Finish(null);return 0;
   default: Finish(null);return 0;
   }
  }catch(Exception e){Finish("helper driver crashed: "+e.Message);return 0;}
 }
}

// The FD acceptance verdict. Unavailable and Leak both FAIL the probe's FD check (fail closed); only
// Accept passes it.
internal enum FdVerdict{Accept,Leak,Unavailable}
