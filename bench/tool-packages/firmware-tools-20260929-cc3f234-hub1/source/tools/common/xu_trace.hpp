#pragma once
#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <cerrno>
#include <cstdint>
#include <string>
#include <sys/ioctl.h>
#include <unistd.h>
namespace rmsl_xu {
inline std::string trace_hex(const void *ptr, unsigned size) {
    const auto *bytes=static_cast<const unsigned char *>(ptr);
    std::string result; const char *hex="0123456789abcdef";
    for(unsigned i=0;i<size;++i){ result+=hex[bytes[i]>>4]; result+=hex[bytes[i]&15]; }
    return result;
}
inline std::string trace_quote(const char *s) {
    std::string out="\"";
    for(;s && *s;++s){ unsigned char c=*s; if(c=='"'||c=='\\') out+='\\'; if(c>=32) out+=c; }
    return out+'"';
}
inline int traced_ioctl(int fd, unsigned long request, void *arg, uint8_t unit,
                        uint8_t selector,uint8_t query,void *data,unsigned size,bool usb=false) {
    using Clock=std::chrono::steady_clock;
    auto start=Clock::now();
    auto before=(query==0x01)?trace_hex(data,size):std::string();
    int rc=::ioctl(fd,request,arg); int saved=errno;
    const char *path=std::getenv("RMSL_XU_TRACE");
    if(path && *path) {
        static unsigned sequence=0; static uint32_t command=0;
        if(query==0x01 && size>=4) {
            auto b=static_cast<unsigned char *>(data);
            command=uint32_t(b[0])|(uint32_t(b[1])<<8)|(uint32_t(b[2])<<16)|(uint32_t(b[3])<<24);
        }
        unsigned actual=rc<0?0:(usb?static_cast<unsigned>(rc):size);
        if(actual>size) actual=size;
        auto raw=query==0x01?before:(rc>=0?trace_hex(data,actual):std::string());
        char device[256]={}; char fdpath[64];
        std::snprintf(fdpath,sizeof(fdpath),"/proc/self/fd/%d",fd);
        auto n=::readlink(fdpath,device,sizeof(device)-1); if(n>0) device[n]=0;
        auto elapsed=std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-start).count();
        auto time=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        FILE *f=std::fopen(path,"a");
        if(f) {
            auto step=trace_quote(std::getenv("RMSL_TRACE_STEP")); auto dev=trace_quote(device);
            std::fprintf(f,"{\"timestamp_us\":%lld,\"pid\":%d,\"sequence\":%u,\"step\":%s,\"device\":%s,\"transport\":\"%s\",\"unit\":%u,\"selector\":%u,\"query\":%u,\"direction\":\"%s\",\"requested_length\":%u,\"actual_length\":%u,\"hex\":\"%s\",\"command_u32_le\":%u,\"command_id\":%u,\"chunk_index\":%u,\"return_code\":%d,\"errno\":%d,\"elapsed_us\":%lld}\n",(long long)time,getpid(),++sequence,step.c_str(),dev.c_str(),usb?"usbfs":"uvcvideo",unit,selector,query,query==1?"TX":"RX",size,actual,raw.c_str(),command,command&65535,command>>16,rc,rc<0?saved:0,(long long)elapsed);
            std::fclose(f);
        }
    }
    errno=saved; return rc;
}
}
