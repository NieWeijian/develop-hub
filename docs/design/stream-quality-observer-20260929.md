# uvc_host_stream capture with Hub metrics

The capture executable is uvc_host_stream-metrics: the versioned firmware uvc_host_stream source plus a Hub-owned observer. It retains the original RGB/Depth setup, four MMAP buffers per stream, one thread per stream, poll/drain-DQBUF/handle/QBUF loops and normal stream shutdown. It is not the untouched firmware-delivered binary. The original package is immutable.

Hub additions: configurable stream dimensions/rate; observe each dequeued frame; warmup markers and shared formal start time; fixed duration/stall termination; raw CSV and summary. Neither a second V4L2 client nor a replacement capture loop is started. Requested dimensions must be negotiated exactly. Both streams finish independently; parent waits and enforces timeout.

The adapter uses existing per-frame calculations: FPS=count/formal-duration; V4L2 sequence gaps and source sequence diagnostics; host dequeue minus synchronized device frame timestamp as estimated latency; interval and latency population standard deviation. One-way time sync bias and clock drift remain limitations, not an absolute latency guarantee. Reboot clears test sync and restores original mode. Warmup has a startup gate (3 full 1s windows within target ±20%, minimum configured duration); it is not a performance acceptance threshold.

Sources and patch are in bench/stream-observer; build.sh links unchanged firmware common/v4l2_capture.cpp. provenance.json records upstream commit, source/patch/observer/binary SHA-256. Runtime definitions snapshot the derived binary SHA and the package version; raw CSV and official tool logs remain round artifacts. Existing rounds keep their original results and packages.

Verification: synthetic C++ frame input exercises warmup exclusion, fixed-window FPS, missing sequence and timestamp latency through the real observer. Python metric regression tests and full server tests apply. No hardware round is created by deployment.
