// Develop Hub host-side frame sampler; no device-control protocol implementation.
#include "v4l2_capture.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <cstring>
#include <csignal>
#include <chrono>
#include <thread>
namespace fs=std::filesystem;
using Clock=std::chrono::steady_clock;
volatile std::sig_atomic_t running=1;
#pragma pack(push,1)
struct Header { uint16_t magic,version,size,type,width,height; int32_t sequence; int64_t timestamp; float temperature; };
#pragma pack(pop)
int main(int argc,char**argv) {
  if(argc!=9) return 2;
  std::signal(SIGTERM,[](int){running=0;});std::signal(SIGINT,[](int){running=0;});
  int w=std::stoi(argv[2]),h=std::stoi(argv[3]),fps=std::stoi(argv[5]),type=std::stoi(argv[6]);
  std::string format=argv[4];fs::path dir=argv[7];int timeout=std::stoi(argv[8]);
  if(w<1||h<1||w>4096||h>4096||fps<1||fps>60||type<1||type>3) return 2;
  uint32_t fourcc=format=="NV12"?V4L2_PIX_FMT_NV12:V4L2_PIX_FMT_YUYV;
  uvc_tools::V4l2Capture cap;
  if(!cap.open(argv[1])||!cap.configure({fourcc,w,h,fps},4)||!cap.start()) return 3;
  fs::create_directories(dir);
  auto start=Clock::now(),seen=start,last=start;unsigned good=0,invalid=0;int generation=-1,saved=-1;int32_t seq=-1;
  while(running && std::chrono::duration_cast<std::chrono::seconds>(Clock::now()-start).count()<timeout) {
    int request=-1;std::ifstream(dir/"request")>>request;
    if(request!=generation){generation=request;good=0;seq=-1;seen=Clock::now();}
    if(!cap.poll_readable(std::chrono::milliseconds(250)))continue;
    v4l2_buffer b{};if(!cap.try_dequeue(b))continue;
    auto now=Clock::now();auto &region=cap.buffer(b.index);Header hdr{};
    size_t expected=static_cast<size_t>(w)*h*(format=="NV12"?3:4)/2;
    bool valid=b.bytesused>=sizeof(Header)&&b.bytesused<=region.size();
    if(valid)std::memcpy(&hdr,region.data(),sizeof(hdr));
    valid=valid&&hdr.magic==0xA55A&&hdr.version>=1&&hdr.version<=2&&hdr.size>=sizeof(Header)&&hdr.size<=b.bytesused&&hdr.type==type&&b.bytesused>=expected;
    if(!valid){++invalid;good=0;seq=-1;seen=now;cap.queue(b);continue;}
    if(seq>=0 && (hdr.sequence!=seq+1 || std::chrono::duration<double>(now-last).count()>1.0)){good=0;seen=now;}
    seq=hdr.sequence;last=now;++good;
    if(generation>=0&&generation!=saved&&good>=12&&std::chrono::duration<double>(now-seen).count()>=1.0){
      std::string name="g"+std::to_string(generation);
      std::ofstream raw(dir/(name+".raw.tmp"),std::ios::binary);raw.write(reinterpret_cast<const char*>(region.data()),b.bytesused);raw.close();
      fs::rename(dir/(name+".raw.tmp"),dir/(name+".raw"));
      std::ofstream meta(dir/(name+".json.tmp"));
      meta<<"{\"width\":"<<w<<",\"height\":"<<h<<",\"format\":\""<<format<<"\",\"type\":"<<type<<",\"header_size\":"<<hdr.size<<",\"header_width\":"<<hdr.width<<",\"header_height\":"<<hdr.height<<",\"sequence\":"<<hdr.sequence<<",\"device_timestamp\":"<<hdr.timestamp<<",\"host_monotonic_ns\":"<<std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()).count()<<",\"bytes\":"<<b.bytesused<<",\"consecutive_valid_frames\":"<<good<<",\"startup_or_invalid_frames\":"<<invalid<<",\"stable_window_seconds\":"<<std::chrono::duration<double>(now-seen).count()<<"}";meta.close();
      fs::rename(dir/(name+".json.tmp"),dir/(name+".json"));saved=generation;
    }
    cap.queue(b);
  }
  cap.stop();return 0;
}
