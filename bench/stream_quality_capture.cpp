// Host-only stream measurement. No module control protocol is implemented here.
#include "v4l2_capture.hpp"
#include <chrono>
#include <csignal>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <thread>
#include <cstdint>
namespace fs=std::filesystem;
using Clock=std::chrono::steady_clock;
volatile std::sig_atomic_t running=1;
static int64_t now_us(){return std::chrono::duration_cast<std::chrono::microseconds>(Clock::now().time_since_epoch()).count();}
#pragma pack(push,1)
struct Header{uint16_t magic,version,size,type,width,height;int32_t sequence;int64_t timestamp;float temperature;};
#pragma pack(pop)
int main(int argc,char**argv){
 if(argc!=11)return 2;
 std::signal(SIGTERM,[](int){running=0;});
 int w=std::stoi(argv[2]),h=std::stoi(argv[3]),fps=std::stoi(argv[5]),type=std::stoi(argv[6]);
 int duration=std::stoi(argv[8]),stall=std::stoi(argv[9]),warmup=std::stoi(argv[10]);
 if(w<1||h<1||w>4096||h>4096||fps<1||fps>60||duration<1||duration>86400||stall<1||stall>300)return 2;
 std::string format=argv[4];fs::path dir=argv[7];fs::create_directories(dir);
 uvc_tools::V4l2Capture cap;
 uint32_t fourcc=format=="NV12"?V4L2_PIX_FMT_NV12:V4L2_PIX_FMT_YUYV;
 if(!cap.open(argv[1])||!cap.configure({fourcc,w,h,fps},8)||!cap.start())return 3;
 int64_t opened=now_us(),begin=0,last=0,window=opened;uint64_t count=0,warm_frames=0;unsigned part=0,stable=0;bool stalled=false,ready=false;
 std::ofstream csv;
 while(running){
  int64_t now=now_us();
  if(!ready&&now-window>=1000000){
   double rate=double(warm_frames)*1000000/(now-window);
   stable=(rate>=fps*.8&&rate<=fps*1.2)?stable+1:0;
   std::ofstream status(dir/"warmup.json");status<<"{\"fps\":"<<rate<<",\"stable_full_windows\":"<<stable<<",\"elapsed_seconds\":"<<double(now-opened)/1000000<<"}";
   if(stable>=3&&now-opened>=int64_t(warmup)*1000000){std::ofstream(dir/"ready")<<now;ready=true;}
   warm_frames=0;window=now;
  }
  if(!begin){std::ifstream(dir/"begin-us")>>begin;if(now-opened>120000000LL)break;}
  if(begin&&now>=begin+int64_t(duration)*1000000)break;
  if(begin&&now>=begin&&now-(last?last:begin)>int64_t(stall)*1000000){stalled=true;break;}
  if(!cap.poll_readable(std::chrono::milliseconds(100)))continue;
  v4l2_buffer b{};if(!cap.try_dequeue(b))continue;
  now=now_us();
  if(!ready){
   Header warm{};auto &region=cap.buffer(b.index);
   if(b.bytesused>=sizeof(warm)&&b.bytesused<=region.size()){
    std::memcpy(&warm,region.data(),sizeof(warm));
    if(warm.magic==0xA55A&&warm.type==type&&!(b.flags&V4L2_BUF_FLAG_ERROR))++warm_frames;
   }
  }
  if(begin&&now>=begin&&now<begin+int64_t(duration)*1000000){
   Header hdr{};auto &region=cap.buffer(b.index);
   bool valid=b.bytesused>=sizeof(Header)&&b.bytesused<=region.size();
   if(valid)std::memcpy(&hdr,region.data(),sizeof(hdr));
   size_t expected=size_t(w)*h*(format=="NV12"?3:4)/2;
   valid=valid&&hdr.magic==0xA55A&&hdr.version>=1&&hdr.version<=2&&hdr.size>=sizeof(Header)&&hdr.size<=b.bytesused&&hdr.type==type&&b.bytesused>=expected;
   if(count%50000==0){if(csv.is_open())csv.close();csv.open(dir/("frames-"+std::to_string(part++)+".csv"));csv<<"host_us,v4l2_sequence,source_sequence,device_us,valid,driver_error\n";}
   csv<<now<<','<<b.sequence<<','<<hdr.sequence<<','<<hdr.timestamp<<','<<(valid?1:0)<<','<<((b.flags&V4L2_BUF_FLAG_ERROR)?1:0)<<'\n';
   if(!csv){cap.queue(b);cap.stop();return 6;}
   ++count;last=now;
  }
  cap.queue(b);
 }
 if(csv.is_open())csv.close();cap.stop();
 int64_t ended=now_us();bool complete=running&&begin&&!stalled&&ended>=begin+int64_t(duration)*1000000&&count>0;
 std::ofstream done(dir/"summary.json");
 done<<"{\"completed\":"<<(complete?"true":"false")<<",\"stalled\":"<<(stalled?"true":"false")<<",\"received\":"<<count<<",\"start_us\":"<<begin<<",\"end_us\":"<<ended<<",\"duration_seconds\":"<<duration<<"}";
 return complete?0:4;
}
