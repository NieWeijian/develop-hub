# 固件 tools 固化与接入（2026-09-29）

- Hub 包版本：2026.09.29-cc3f234-hub1（Hub 导入构建编号，不冒充固件团队版本号）。
- 固件源码 commit：cc3f2345245b3d8347d4ef604bb80b8a9e330546；仅 git archive 中已提交的 tools 文件。
- 目标：linux-arm64 / RK3588。包含九个 uvc_host 工具。
- 原始源码、原始源码 SHA、Hub XU trace 补丁与头文件、构建产物及逐文件 SHA、capability 清单均留存。
- 构建：3588 GNU C++ 10.2.1 / libdrm 2.4.104；cmake Release；-DCMAKE_CXX_FLAGS=-pthread。旧系统需要显式 pthread 链接选项；没有修改固件源码。
- capability 依据已提交 CLI dispatch 和编译产物；temperature.read.v1 对应 get-temperature / XU 0x1135。尚未真机验证新工具与当前固件的所有接口。
- tools.lock.json 指定新轮次默认包。用例 adapter 在创建轮次时冻结完整包信息，执行配置及原始证据继续保存；运行使用冻结二进制路径及 SHA。
- 旧 ctl-trace-1 二进制不覆盖，旧锁文件复制到 tool-packages/ctl-trace-1/manifest.json；旧轮次记录不修改。
- 导入不更新 Hub 自有 host-capture、host-imu 助手。新包附带的固件工具保留在版本目录内。
- CLI 的 --help 也会连接模组，不能作为离线 capability 检查。温度用例改读已校验的包清单。
- 不新增 Test round，不编译烧录模组固件，不执行开流或模式切换。

更新流程：在新版本目录导入源码及构建产物 → 校验架构/命令/依赖/校验值 → 固化 manifest → 验证适配器 → 同步版本目录到测试台 → 更新默认锁文件 → 重启 Hub 加载定义。历史包不可覆盖。能力缺失在创建时拒绝，在执行时仍有 blocked 防线。
