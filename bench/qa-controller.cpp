#include <filesystem>
#include <iostream>
#include <vector>
#include <algorithm>
#include <thread>
#include <chrono>
#include <sys/file.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <unistd.h>
namespace fs = std::filesystem;
int main(int argc, char **argv) {
    if (argc != 3) { std::cerr << "usage: qa-controller JOBS WORKER\n"; return 2; }
    fs::path jobs=fs::canonical(argv[1]);
    int lock=open((jobs/".controller.lock").c_str(),O_CREAT|O_RDWR,0600);
    if(lock<0 || flock(lock,LOCK_EX|LOCK_NB)) return 3;
    for (;;) {
        std::vector<fs::path> queue;
        for (auto &entry: fs::directory_iterator(jobs))
            if (!entry.is_symlink() && entry.is_directory() && fs::exists(entry.path()/"READY")) queue.push_back(entry.path());
        std::sort(queue.begin(),queue.end());
        for(auto &job: queue) {
            // Worker owns JSON validation, durable idempotency and recovery.
            pid_t child=fork();
            if(child==0) { execl("/usr/bin/python3","python3",argv[2],job.c_str(),nullptr); _exit(127); }
            if(child>0) {int status; while(waitpid(child,&status,0)<0 && errno==EINTR) {}}
        }
        std::this_thread::sleep_for(std::chrono::seconds(2));
    }
}
