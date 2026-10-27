using System.Runtime.InteropServices;
using static Native;
internal class Canvas {
 internal IntPtr Window,Surface;readonly List<IntPtr> objects=new();
 [DllImport("libevas.so.1")] static extern IntPtr evas_object_polygon_add(IntPtr canvas);
 [DllImport("libevas.so.1")] static extern void evas_object_polygon_point_add(IntPtr obj,int x,int y);
 [DllImport("libevas.so.1")] static extern void evas_object_del(IntPtr obj);
 [DllImport("libevas.so.1")] static extern int evas_object_text_horiz_advance_get(IntPtr obj);
 [DllImport("libevas.so.1")] static extern IntPtr evas_object_image_filled_add(IntPtr canvas);
 [DllImport("libevas.so.1")] static extern void evas_object_image_snapshot_set(IntPtr image,int enable);
 [DllImport("libevas.so.1")] static extern int evas_object_image_save(IntPtr image,string path,string key,string flags);
 [DllImport("libevas.so.1")] static extern void evas_object_image_file_set(IntPtr image,string path,string key);
 [DllImport("libevas.so.1")] static extern void evas_object_anti_alias_set(IntPtr obj,int enabled);
 [DllImport("libevas.so.1")] static extern IntPtr evas_object_image_add(IntPtr canvas);
 [DllImport("libevas.so.1")] static extern void evas_object_image_fill_set(IntPtr image,int x,int y,int w,int h);
 internal double FocusProgress=1;
 IntPtr snapshot;
 internal Canvas(HostConfig config){foreach(var p in new[]{("HOME","/opt/usr/home/owner"),("USER","owner"),("LOGNAME","owner"),("XDG_RUNTIME_DIR","/run/user/"+config.OwnerUidText),("WAYLAND_DISPLAY","wayland-0"),("ELM_PROFILE","tv"),("ELM_DISPLAY","wl"),("ELM_ENGINE","wayland_shm"),("ECORE_EVAS_ENGINE","wayland_shm"),("EVAS_WAYLAND_SHM_DISABLE_DMABUF","1"),("EINA_LOG_LEVEL","0")})Environment.SetEnvironmentVariable(p.Item1,p.Item2);
  if(elm_init(0,IntPtr.Zero)<1)throw new Exception("EFL init failed");Window=elm_win_add(IntPtr.Zero,"quiet-tv",12);if(Window==IntPtr.Zero)throw new Exception("Window failed");elm_win_borderless_set(Window,1);elm_win_prop_focus_skip_set(Window,0);elm_win_input_rect_set(Window,0,0,1920,1080);efl_util_set_notification_window_level(Window,40);elm_win_layer_set(Window,900);evas_object_resize(Window,1920,1080);Surface=evas_object_evas_get(Window);
 }
 internal void Show(){elm_win_prop_focus_skip_set(Window,0);elm_win_input_rect_set(Window,0,0,1920,1080);evas_object_show(Window);elm_win_activate(Window);}
 internal void YieldInput(){evas_object_hide(Window);}
 internal void Hide()=>evas_object_hide(Window);
 void Color(IntPtr o,int color){evas_object_color_set(o,(color>>16)&255,(color>>8)&255,color&255,255);}
 void Rect(int x,int y,int w,int h,int color){var o=evas_object_rectangle_add(Surface);objects.Add(o);evas_object_move(o,x,y);evas_object_resize(o,w,h);Color(o,color);evas_object_show(o);}
 void Round(int x,int y,int w,int h,int r,int color){var o=evas_object_polygon_add(Surface);objects.Add(o);for(int corner=0;corner<4;corner++){double start=corner*Math.PI/2;int cx=corner is 0 or 3?x+w-r:x+r,cy=corner<2?y+h-r:y+r;for(int j=0;j<=24;j++){double angle=start+j*Math.PI/48;evas_object_polygon_point_add(o,cx+(int)(r*Math.Cos(angle)),cy+(int)(r*Math.Sin(angle)));}}evas_object_anti_alias_set(o,1);Color(o,color);evas_object_show(o);}
 void Text(int x,int y,int size,string text,int color=0xeeeeee,int centerWidth=0,bool bold=false){var o=evas_object_text_add(Surface);objects.Add(o);evas_object_text_font_set(o,bold?"SamsungOne:style=Bold":"SamsungOne, Sans",size);evas_object_text_text_set(o,text);Color(o,color);if(centerWidth>0)x+=(centerWidth-evas_object_text_horiz_advance_get(o))/2;evas_object_move(o,x,y);evas_object_show(o);}
 void Tile(Control c,bool selected,bool favorite,string icon){int margin=selected?3+(int)(4*FocusProgress):0;int x=c.X-margin,y=c.Y-margin,w=c.W+margin*2,h=c.H+margin*2;bool youtube=c.Label=="YouTube";int fill=youtube||c.Label=="Netflix"?0xffffff:c.Label=="Prime Video"?0x087eff:c.Label=="Philo"?0x0f0e0d:0x242526;
  if(selected){Round(x-3,y-3,w+6,h+6,28,0xf0f0f0);}Round(x,y,w,h,25,fill);int size=c.W<250?30:43;string word=c.Label;int color=youtube?0x111111:word=="Netflix"?0xe50914:0xf5f5f5;
  if(icon!=""&&File.Exists(icon)){var image=evas_object_image_add(Surface);objects.Add(image);evas_object_image_file_set(image,icon,null);if(c.Label is "Netflix" or "YouTube" or "Philo"){int side=w-20;evas_object_move(image,x+10,y+10);evas_object_resize(image,w-20,h-20);evas_object_image_fill_set(image,0,(h-20-side)/2,side,side);}else{int side=h-16;evas_object_move(image,x+(w-side)/2,y+8);evas_object_resize(image,side,side);evas_object_image_fill_set(image,0,0,side,side);}evas_object_show(image);}
  else if(word=="Apps"){for(int i=0;i<4;i++)Round(x+w/2-42+(i%2)*48,y+h/2-42+(i/2)*48,36,36,8,0xf4f4f4);}
  else {if(word.Length>18)word=word[..17]+"…";Text(x,y+h/2-size/2-5,size,word,color,w,true);}
  if(favorite)Text(x+w-34,y+12,20,"★",0xe0d6be);Text(c.X,c.Y+c.H+17,c.W<250?23:27,c.Label.Length>24?c.Label[..23]+"…":c.Label,0xeaeaea,c.W);
 }
 internal void Render(State s){if(snapshot!=IntPtr.Zero){evas_object_del(snapshot);snapshot=IntPtr.Zero;}foreach(var o in objects)evas_object_del(o);objects.Clear();
  int bg=s.Prefs.Wallpaper==1?0:s.Prefs.Wallpaper==2?0x141414:0x070708;Rect(0,0,1920,1080,bg);var controls=s.Controls();
  if(s.ExitPrompt){Text(600,345,54,"Close dashboard?",bold:true);for(int i=0;i<controls.Count;i++){var c=controls[i];Round(c.X,c.Y,c.W,c.H,20,i==s.Focus?0xededed:0x262626);Text(c.X,c.Y+28,30,c.Label,i==s.Focus?0x111111:0xeeeeee,c.W);}return;}
  for(int i=0;i<3;i++){var c=controls[i];Text(c.X,c.Y,32,c.Label,i==s.Focus?0xffffff:0xbcbcbc,bold:c.Action==s.Screen);if(c.Action==s.Screen)Round(c.X,c.Y+49,c.Label.Length*18,3,1,0xdddddd);if(i==s.Focus)Round(c.X-9,c.Y-9,c.W,3,1,0xffffff);}
  Text(90,56,31,"QUIET",0xe4dfd5,bold:true);Text(1610,57,30,DateTime.Now.ToString("h:mm tt").ToLowerInvariant(),centerWidth:220);
  if(s.Screen=="Home"){Text(90,184,54,"Home",bold:true);Text(92,258,27,DateTime.Now.ToString("dddd, MMMM d"),0x9a9a9f);Text(90,701,32,"Your TV",0xe3e3e5,bold:true);}
  else {Text(90,176,54,s.Screen=="Arrange"?"Arrange apps":s.Screen=="Favorites"?"Favorites":s.Screen=="Settings"?"Settings":"All apps",bold:true);Text(95,249,25,s.Screen=="Favorites"?"OK adds or removes a favorite.":s.Screen=="Arrange"?"Select two apps to swap their positions.":s.Screen=="Settings"?"Customize your dashboard and TV.":$"Page {s.Page+1}  ·  {s.Apps.Count} installed apps",0x9a9a9f);}
  for(int i=3;i<controls.Count;i++){var c=controls[i];if(s.Screen=="Home"&&c.Y>=700){bool focus=i==s.Focus;Round(c.X,c.Y,c.W,c.H,18,focus?0xe9e5dc:0x191a1d);Text(c.X+27,c.Y+25,29,c.Label,focus?0x111111:0xeaeaec,bold:true);string sub=c.Action=="Apps"?$"{s.Apps.Count} available":c.Id=="com.samsung.tv.coba.setting"?"Display, audio & preferences":c.Id=="org.tizen.SMSourceAPP"?"HDMI & connected devices":"Install & manage apps";Text(c.X+27,c.Y+71,21,sub,focus?0x444444:0x96969d);}else if(s.Screen=="Settings"||c.Action is "Next" or "Previous"){Round(c.X,c.Y,c.W,c.H,17,i==s.Focus?0xe8e8e8:0x202122);Text(c.X+25,c.Y+21,31,c.Label,i==s.Focus?0x111111:0xeeeeee);}else Tile(c,i==s.Focus,s.Prefs.Favorites.Contains(c.Id)&&s.Screen=="Favorites",s.Apps.FirstOrDefault(a=>a.Id==c.Id)?.Icon??"");}
  Text(92,1015,23,s.Message!=""?s.Message:"OK  Open       Back  Return       Home  Dashboard",0x73737b);
 }
 internal void BeginSnapshot(){try{snapshot=evas_object_image_filled_add(Surface);evas_object_move(snapshot,0,0);evas_object_resize(snapshot,1920,1080);evas_object_image_snapshot_set(snapshot,1);evas_object_show(snapshot);}catch(Exception e){Console.WriteLine("snapshot_unavailable="+e.Message);}}
 internal void SaveSnapshot(string path){try{Console.WriteLine("snapshot_saved="+evas_object_image_save(snapshot,path,null,null));}catch(Exception e){Console.WriteLine("snapshot_error="+e.Message);}}
}
