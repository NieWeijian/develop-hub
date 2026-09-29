import json
import shutil
import subprocess
from pathlib import Path
import pytest
from test_stream_quality import measure

def test_cpp_observer_excludes_warmup_and_records_gap_and_latency(tmp_path):
    compiler=shutil.which('g++')
    assert compiler, 'C++ compiler required to verify stream observer'
    header=Path(__file__).resolve().parents[2]/'bench/stream-observer'
    source=tmp_path/'test.cpp'
    source.write_text(r'''
#include <chrono>
#include <vector>
#include <cstdint>
#include <linux/videodev2.h>
int64_t test_now=0;
#define HUB_METRICS_CLOCK_US test_now
namespace uvc_tools { struct MappedBuffer {
 std::vector<unsigned char> data_=std::vector<unsigned char>(100);
 size_t size() const{return data_.size();}
 const void* data() const{return data_.data();}
}; }
#include "stream_metrics.hpp"
int main(int argc,char**argv){
 hub_metrics::root=argv[1];hub_metrics::duration=2;hub_metrics::warmup=1;
 hub_metrics::Observer observer(1,2,2,10);
 uvc_tools::MappedBuffer region;v4l2_buffer buffer{};buffer.bytesused=100;
 for(int i=0;i<=52;++i){
  test_now=int64_t(i)*100000;
  if(i==31)std::ofstream(std::filesystem::path(argv[1])/"rgb/begin-us")<<3200000;
  if(observer.tick())break;
  if(i==40)continue;
  hub_metrics::Header h{0xA55A,1,28,1,2,2,i,test_now-20000,25};
  std::memcpy(region.data_.data(),&h,sizeof(h));buffer.sequence=i;
  observer.frame(buffer,region);
 }
}
''')
    exe=tmp_path/'observer-test'
    subprocess.run([compiler,'-std=c++17','-I'+str(header),str(source),'-o',str(exe)],check=True,capture_output=True)
    subprocess.run([str(exe),str(tmp_path/'out')],check=True)
    folder=tmp_path/'out/rgb'
    m=measure(folder)
    assert m['completed'] and m['valid_frames']==19 and m['fps']==9.5
    assert m['v4l2_missing_frames']==1
    assert m['estimated_latency_ms']['mean']==20
    assert json.loads((folder/'warmup.json').read_text())['elapsed_seconds']==3

    for phase,sequence in [('start',32),('end',42)]:
        raw=(folder/(phase+'.raw')).read_bytes()
        meta=json.loads((folder/(phase+'.json')).read_text())
        assert len(raw)==100 and meta['sequence']==sequence
        assert meta['host_monotonic_us']>=3200000
        from frame_images import png
        png(raw,meta,folder/(phase+'.png'))
        assert (folder/(phase+'.png')).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
