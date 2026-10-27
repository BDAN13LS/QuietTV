// Adapted from the pinned upstream remote input agent's EVIOCGRAB/uinput path:
// chris-ritsen/samsung-tv-root @ 4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b,
// payloads/common/SamsungTvRemoteInputAgent.cs (Unlicense). See NOTICE.md.
// Only Home is consumed. No kernel patch, firmware edit, or persistent input rule.
// Public host: device names come from the host config; constructed only when remoteGrabEnabled is true.
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
internal sealed class RemoteGate : IDisposable {
 const uint Grab=0x40044590,SetEvent=0x40045564,SetKey=0x40045565,Create=0x5501,Destroy=0x5502;
 [DllImport("libc",SetLastError=true)]static extern int open(string path,int flags);
 [DllImport("libc",SetLastError=true)]static extern int read(int fd,byte[] bytes,int length);
 [DllImport("libc",SetLastError=true)]static extern int write(int fd,byte[] bytes,int length);
 [DllImport("libc")]static extern int close(int fd);
 [DllImport("libc",EntryPoint="ioctl",SetLastError=true)]static extern int ioctl(int fd,uint request,int value);
 [DllImport("libc",EntryPoint="ioctl",SetLastError=true)]static extern int ioctl0(int fd,uint request);
 [StructLayout(LayoutKind.Sequential)]struct Poll {public int Fd;public short Events,Returned;}
 [DllImport("libc",SetLastError=true)]static extern int poll([In,Out]Poll[] fds,uint count,int timeout);
 readonly List<(int Input,int Output)> pairs=new();readonly Dictionary<int,HashSet<ushort>> held=new();
 readonly int eventSize=IntPtr.Size==8?24:16,typeOffset=IntPtr.Size==8?16:8;
 volatile bool stopped;
 public static bool Forward(ushort type,ushort code,int home)=>type==0||(type==1&&code!=home);
 // Device blocks from /proc/bus/input/devices whose Name exactly matches a configured remote name.
 internal static List<(string Event,string Block)> MatchDevices(string devices,IReadOnlyList<string> names){var found=new List<(string,string)>();foreach(string block in devices.Split("\n\n")){if(!names.Any(n=>block.Contains("Name=\""+n+"\"")))continue;var handler=Regex.Match(block,@"Handlers=.*\b(event\d+)\b");if(handler.Success)found.Add((handler.Groups[1].Value,block));}return found;}
 public RemoteGate(IReadOnlyList<string> deviceNames){try{
  if(deviceNames==null||deviceNames.Count==0)throw new Exception("No remote device names configured");
  foreach(var (eventName,block) in MatchDevices(File.ReadAllText("/proc/bus/input/devices"),deviceNames)){
   int input=open("/dev/input/"+eventName,2048|0x80000);if(input<0)throw new Exception("remote input open failed");int output=-1;
   try{output=open("/dev/uinput",2049|0x80000);if(output<0)throw new Exception("uinput open failed");Require(ioctl(output,SetEvent,0));Require(ioctl(output,SetEvent,1));for(int key=0;key<=0x2ff;key++)Require(ioctl(output,SetKey,key));var definition=new byte[1116];Encoding.ASCII.GetBytes("quiet-tv-forwarded-remote").CopyTo(definition,0);
    var id=Regex.Match(block,@"Bus=([0-9a-fA-F]+) Vendor=([0-9a-fA-F]+) Product=([0-9a-fA-F]+) Version=([0-9a-fA-F]+)");for(int j=0;j<4;j++)BitConverter.GetBytes(id.Success?Convert.ToUInt16(id.Groups[j+1].Value,16):(ushort)(j==0?6:0)).CopyTo(definition,80+j*2);
    RequireWrite(output,definition);Require(ioctl0(output,Create));Require(ioctl(input,Grab,1));pairs.Add((input,output));held[output]=new();Console.WriteLine("home_gate_grabbed="+eventName);
   }catch{if(output>=0){ioctl0(output,Destroy);close(output);}close(input);throw;}
  }if(pairs.Count==0)throw new Exception("No verified physical remote input devices");
 }catch{Dispose();throw;}}
 static void Require(int result){if(result<0)throw new Exception("input ioctl failed errno="+Marshal.GetLastWin32Error());}
 static void RequireWrite(int fd,byte[] bytes){if(write(fd,bytes,bytes.Length)!=bytes.Length)throw new Exception("uinput forward failed");}
 public void Run(int home,Action onHome){try{var polls=pairs.Select(p=>new Poll{Fd=p.Input,Events=1}).ToArray();var bytes=new byte[eventSize];while(!stopped){int n=poll(polls,(uint)polls.Length,200);if(n<0)throw new Exception("remote input poll failed");for(int i=0;i<polls.Length;i++){if((polls[i].Returned&24)!=0)throw new Exception("remote disconnected");if((polls[i].Returned&1)==0)continue;while(read(pairs[i].Input,bytes,bytes.Length)==eventSize){ushort type=BitConverter.ToUInt16(bytes,typeOffset),code=BitConverter.ToUInt16(bytes,typeOffset+2);int value=BitConverter.ToInt32(bytes,typeOffset+4);if(type==1&&code==home&&value==1)onHome();if(Forward(type,code,home)){RequireWrite(pairs[i].Output,bytes);if(type==1){if(value==0)held[pairs[i].Output].Remove(code);else held[pairs[i].Output].Add(code);}}}}}}
  catch(Exception e){Console.WriteLine("home_gate_released="+e.Message);}finally{Dispose();}}
 public void Dispose(){stopped=true;foreach(var p in pairs){try{foreach(ushort code in held[p.Output]){var release=new byte[eventSize];BitConverter.GetBytes((ushort)1).CopyTo(release,typeOffset);BitConverter.GetBytes(code).CopyTo(release,typeOffset+2);RequireWrite(p.Output,release);}RequireWrite(p.Output,new byte[eventSize]);}catch{}ioctl(p.Input,Grab,0);ioctl0(p.Output,Destroy);close(p.Input);close(p.Output);}pairs.Clear();}
 public static void Tests(){if(Forward(1,63,63))throw new Exception("Home must never reach Samsung");foreach(ushort key in new ushort[]{28,103,105,106,108,115,114,116,158,390})if(!Forward(1,key,63))throw new Exception("Non-Home key lost");if(!Forward(0,0,63))throw new Exception("SYN lost");if(Forward(4,4,63))throw new Exception("MSC must not bypass the Home filter");var devices="I: Bus=0019 Vendor=0001 Product=0001 Version=0100\nN: Name=\"Test Remote\"\nH: Handlers=kbd event3\n\nI: Bus=0003\nN: Name=\"Test Remote Clone\"\nH: Handlers=kbd event7\n\nN: Name=\"Keyboard\"\nH: Handlers=event9";var m=MatchDevices(devices,new[]{"Test Remote"});if(m.Count!=1||m[0].Event!="event3")throw new Exception("Remote device match must be exact-name only");if(MatchDevices(devices,Array.Empty<string>()).Count!=0)throw new Exception("No names must match no devices");Console.WriteLine("PASS 15 remote forwarding and device match assertions");}
}
