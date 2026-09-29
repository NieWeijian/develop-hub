// Hub-owned observer. uvc_host_stream alone owns capture, buffers and requeue.
#pragma once
#include <filesystem>
#include <fstream>
#include <cstring>
#include <cstdint>
#include <vector>
#include <array>
namespace hub_metrics {
namespace fs=std::filesystem;
inline std::string root;
inline int duration=18000,warmup=10,stall=5;
#ifndef HUB_METRICS_CLOCK_US
#define HUB_METRICS_CLOCK_US std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count()
#endif
inline int64_t now_us(){return HUB_METRICS_CLOCK_US;}
#pragma pack(push,1)
struct Header{uint16_t magic,version,size,type,width,height;int32_t sequence;int64_t timestamp;float temperature;};
#pragma pack(pop)
class Observer {
 fs::path dir; int type,w,h,fps; bool enabled,ready=false,stalled=false,io_error=false;
 int64_t opened=now_us(),begin=0,last=0,window=opened;
 uint64_t count=0,warm_frames=0;unsigned stable=0,part=0;std::ofstream csv;
 struct Snapshot {std::vector<char> bytes;Header header{};int64_t host=0;};
 std::array<Snapshot,2> snapshots;
 void retain(int slot,v4l2_buffer const& b,uvc_tools::MappedBuffer const& region,Header const& hdr,int64_t now){
  if(!snapshots[slot].bytes.empty())return;
  auto& shot=snapshots[slot];auto data=reinterpret_cast<const char*>(region.data());
  shot.bytes.assign(data,data+b.bytesused);shot.header=hdr;shot.host=now;
 }
 void save_snapshots(){
  for(int i=0;i<2;++i){
   auto const& shot=snapshots[i];if(shot.bytes.empty())continue;
   std::string name=i==0?"start":"end";
   std::ofstream raw(dir/(name+".raw"),std::ios::binary);raw.write(shot.bytes.data(),shot.bytes.size());raw.close();
   std::ofstream meta(dir/(name+".json"));
   meta<<"{\"width\":"<<w<<",\"height\":"<<h<<",\"type\":"<<type<<",\"header_size\":"<<shot.header.size<<",\"sequence\":"<<shot.header.sequence<<",\"device_timestamp\":"<<shot.header.timestamp<<",\"host_monotonic_us\":"<<shot.host<<",\"bytes\":"<<shot.bytes.size()<<"}";
   meta.close();if(!raw||!meta)io_error=true;
  }
 }
public:
 Observer(int t,int width,int height,int rate):type(t),w(width),h(height),fps(rate),enabled(!root.empty()){
  if(enabled){dir=fs::path(root)/(type==1?"rgb":"depth");fs::create_directories(dir);}
 }
 bool tick(){
  if(!enabled)return false;
  auto now=now_us();
  if(!ready&&now-window>=1000000){
   double rate=double(warm_frames)*1000000/(now-window);
   stable=rate>=fps*.8&&rate<=fps*1.2?stable+1:0;
   std::ofstream status(dir/"warmup.json");status<<"{\"fps\":"<<rate<<",\"stable_full_windows\":"<<stable<<",\"elapsed_seconds\":"<<double(now-opened)/1000000<<"}";
   if(stable>=3&&now-opened>=int64_t(warmup)*1000000){std::ofstream(dir/"ready")<<now;ready=true;}
   warm_frames=0;window=now;
  }
  if(!begin){std::ifstream(dir/"begin-us")>>begin;if(now-opened>120000000LL)return true;}
  if(begin&&now>=begin+int64_t(duration)*1000000)return true;
  if(begin&&now>=begin&&now-(last?last:begin)>int64_t(stall)*1000000){stalled=true;return true;}
  return io_error;
 }
 void frame(v4l2_buffer const& b,uvc_tools::MappedBuffer const& region){
  if(!enabled)return;
  auto now=now_us();Header hdr{};
  bool valid=b.bytesused>=sizeof(hdr)&&b.bytesused<=region.size();
  if(valid)std::memcpy(&hdr,region.data(),sizeof(hdr));
  valid=valid&&hdr.magic==0xA55A&&hdr.version>=1&&hdr.version<=2&&hdr.size>=sizeof(hdr)&&hdr.size<=b.bytesused&&hdr.type==type&&b.bytesused>=size_t(w)*h*(type==1?3:4)/2;
  if(!ready&&valid&&!(b.flags&V4L2_BUF_FLAG_ERROR))++warm_frames;
  if(!begin||now<begin||now>=begin+int64_t(duration)*1000000)return;
  if(count%50000==0){if(csv.is_open())csv.close();csv.open(dir/("frames-"+std::to_string(part++)+".csv"));csv<<"host_us,v4l2_sequence,source_sequence,device_us,valid,driver_error\n";}
  csv<<now<<','<<b.sequence<<','<<hdr.sequence<<','<<hdr.timestamp<<','<<(valid?1:0)<<','<<((b.flags&V4L2_BUF_FLAG_ERROR)?1:0)<<'\n';
  io_error=!csv;++count;last=now;
  if(valid&&!(b.flags&V4L2_BUF_FLAG_ERROR)){
   retain(0,b,region,hdr,now);
   if(now>=begin+int64_t(duration-1)*1000000)retain(1,b,region,hdr,now);
  }
 }
 ~Observer(){
  if(!enabled)return;
  if(csv.is_open()){csv.close();io_error=io_error||csv.fail();}
  auto ended=now_us();save_snapshots();bool complete=begin&&!stalled&&!io_error&&ended>=begin+int64_t(duration)*1000000&&count>0;
  std::ofstream out(dir/"summary.json");out<<"{\"completed\":"<<(complete?"true":"false")<<",\"stalled\":"<<(stalled?"true":"false")<<",\"received\":"<<count<<",\"start_us\":"<<begin<<",\"end_us\":"<<ended<<",\"duration_seconds\":"<<duration<<"}";
 }
};
}
