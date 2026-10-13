# 3588 QA Controller

当前测试台使用 Python `qa_controller_v2.py` 和 `develop-hub-qa-v2.service`。入口、协议及部署位置见 [QA v2 说明](README-v2.md)，完整职责边界见 [QA v2 架构](../docs/design/qa-architecture-v2.md)。`README-v2.md` 中的首轮联调数据保留为历史记录，当前用例范围见 [产品测试平台](../README.md#产品测试平台)。

## 旧 C++ Controller 兼容说明

以下内容记录旧 `qa-controller.cpp`、`worker.py` 和 `develop-hub-qa-controller.service` 的 RK3572 烧录链路，供维护历史记录与兼容适配器时参考。旧 Controller 服务已停用；新部署使用上方 Python v2 服务。下面的“尚未接入”及测试结论均限于当时的旧链路。

qa-controller.cpp is the ARM64 supervisor; worker.py owns the version 2 file protocol and the upgrade_tool adapter using Python 3 standard library. No command from job JSON is executed. Only update.img and RK3572 chip tag 351A are enabled. Test Cases are snapshotted as not_run.

Build on 175 with the registered SDK aarch64-none-linux-gnu-g++: -std=c++17 -O2 -static qa-controller.cpp -o qa-controller.
Deploy binary, worker.py and tools/upgrade-tool to the bench root. The SDK full x86_64 upgrade_tool runs under Debian qemu-user-static because the preinstalled ARM tool lacks RCI/SFI. Existing /usr/bin/upgrade_tool is unchanged. Install the included systemd unit.

Protocol: transfer test-run.json and firmware/update.img, verify hashes, then atomically publish READY containing config_sha256. A single controller lock serializes jobs, and per-job locks prevent duplicate execution. Existing finished results never reflash. Restarted in-flight jobs become interrupted, requiring a new round.

Controller verifies JSON hash, image size/hash, exactly one Loader/Maskrom device, locked serial, RCI chip bytes 41 31 35 33 and SFI tag 351A before UF. Result passed requires exit 0 and the tool firmware-success marker. This establishes flashing only; boot_verified=false and all Test Cases remain not_run.

result.json is atomically replaced with heartbeat and bounded log tail; full command output stays in logs/. events.jsonl is append-only. Hub reads over SSH every 2 seconds and checks job identity/config hash. SSH failure does not stop an independently running flash.

Cancellation and execution of the existing /opt/rmsl_test/bin tools are not enabled in this phase. Do not kill a flashing controller manually. Preserve old job directories; create a new round to retry.

Command output is captured through a PTY to prevent static upgrade_tool stdio buffering; raw logs retain evidence while live status strips terminal control codes.

Verified live: TEST-000002, Gerrit 316200 PS6, serial c3d9b8674f4b94f6. Firmware UF exited 0, reported Download Firmware Success and Upgrade firmware ok; Hub reached flash_complete. Selected cases remain not_run.
