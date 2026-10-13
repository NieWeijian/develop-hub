# Develop Hub QA 架构草案 v2

状态：待评审。适用目标：RV1126B 结构光固件测试，并允许以后接入其他芯片。本文定义目标架构，不表示功能已经实现或真机验证通过。

## 1. 目标与边界

一次 Test Round 必须回答：测试的是哪份源码和固件、哪台物理模组、哪套工具和用例、执行了哪些步骤、每条断言的实测值与证据是什么、失败后设备是否恢复、是否满足本轮发布规则。

175 是唯一的任务编排与审计源；3588 是可独立完成已接收任务的执行节点。网络短暂中断不得让烧录进程被 175 的 SSH 会话中止。3588 不决定产品发布，也不从任务 JSON 执行任意 shell 命令。测试工具提供通信和测量事实，Case 提供产品判定，Develop Hub 汇总并执行发布门禁。

## 2. 四层职责

| 层 | 职责 | 不承担 |
|---|---|---|
| 175 Develop Hub | 产品配置、源码选择、构建、镜像冻结、测试计划编译、任务下发、状态与证据归档、覆盖率和发布门禁 | 解析工具文本推断硬件指标、直接控制视频/HID 设备 |
| 3588 qa-controller v2 | 接收并校验不可变任务包、环境预检、资源锁、烧录适配器、启动身份核验、逐 Case 进程调度、超时/中断处理、证据索引与可靠上报 | 定义产品阈值、修改 Case 判定、自由执行 JSON 中的命令 |
| Test Case 包 | 前置条件、调用 tools、实测指标、逐条断言、必要的清理恢复、结构化结果 | Git 拉取、SDK 编译、发布结论 |
| Host tools | 稳定的 XU/UVC/HID/DFU/Loader 操作和原始测量；明确错误码、设备绑定和机器可读输出 | 固件发布策略、跨用例编排 |

### qa-controller v2 实现

以一个 Python 3 systemd 服务替换 C++ 轮询器和写死业务的 worker.py。主进程只做任务发现、锁和状态恢复；每个任务由独立 worker 进程执行，每个 Case 又是独立进程。继续使用 175 经 SSH 推送、原子 READY 发布、3588 本地 result.json/events.jsonl、175 轮询拉取；暂不新增常驻 HTTP API。

保留现有已经验证过的机制：配置/镜像 SHA-256 校验、Controller 独占锁和任务锁、原子结果文件、心跳、原始日志、同一物理 USB 端口回检、结束任务不可重复执行。移除硬编码的 RK3572、update.img、四个 BASIC Case 和固定工具路径。

## 3. 测试任务生命周期

1. 操作员可先触发独立的 preflight-only 检查：175 验证 SSH 与测试台服务，3588 验证工具、磁盘、USB 拓扑及模组当前状态。此检查不烧录，也不复用为稍后烧录时的安全结论。
2. 175 冻结 source：Gerrit 的 project/change/patchset/commit；或明确的本地 Git commit。若需要测试未提交修改，另建可校验源码快照和 dirty 标记，不把可变工作目录直接当作固定输入。
3. 175 在注册 SDK 上构建，冻结 Board Config、Manifest、产物 SHA-256。测试工具和 Case 包各自有独立版本与摘要，不默认使用固件源码树里的最新可执行文件。
4. 175 下发 plan.json、镜像、Case 包和工具包；3588 校验摘要与 schema 后接受任务。测试计划只引用已登记的 adapter_id、case_id/version 和参数，不包含任意命令行。
5. 3588 在烧录前重新执行 preflight：自身健康、磁盘/权限/依赖、USB 拓扑、设备数、硬件版本、模组当前 UVC/Loader/DFU 状态、烧录器能力。失败记录 BLOCKED，不烧录。
6. 若计划要求烧录，按产品配置选择 flash adapter。适配器再次校验镜像芯片标签、目标硬件版本、物理端口、序列号/可用身份、A/B 槽及目标分区；执行后核对工具结果、启动槽、运行固件 Commit 和实际枚举。烧录成功不能代替启动验证。
7. 逐 Case 检查前置条件和资源锁，运行版本化入口，读取 case-result.json 并验证结果 schema、退出码和证据索引。依赖失败的后续 Case 标记 BLOCKED；独立 Case 可继续。
8. 对有状态 Case 执行其恢复步骤，记录恢复实测。恢复失败时停止可能受污染的后续 Case，并把设备标为需人工处理。
9. 3588 原子写入最终 round-result.json；175 校验 round_id、任务摘要和单调 revision 后归档证据，按发布规则计算结论。

任务阶段：RECEIVED → PREFLIGHT → FLASHING（可选）→ VERIFY_BOOT → RUNNING_CASES → RECOVERY → COMPLETE。终态另有 BLOCKED、FAILED、ERROR、INTERRUPTED。烧录阶段中断后不自动重试烧录；新轮次必须重新核验设备状态。

## 4. Case 契约

每个 Case 是独立目录：case.yaml、run.py 或可执行程序、fixtures/。元数据至少包含 case_id、definition_version、适用产品/固件/硬件/模式、参数 schema、前置条件、资源需求、超时、风险级别、断言定义、证据要求和恢复策略。

Controller 传入 case-input.json，包含 round/attempt、模组绑定（USB 物理路径及当前动态节点映射）、固件身份、工具绝对路径与摘要、参数与阈值快照、输出目录。Case 输出 case-result.json，至少包含 outcome、开始/结束、每条断言的 expected/actual/unit/pass、指标、步骤及退出码、证据相对路径和 SHA-256、恢复结果。原始工具输出与采样数据保留为附件，数据库只保存摘要与索引。

Case outcome：PASS（全部必需断言成立）、FAIL（实测不满足）、ERROR（工具/用例/协议异常）、BLOCKED（前置条件不满足）、INCONCLUSIVE（数据不足以判定）、SKIPPED（计划明确不适用）、INTERRUPTED。不得把命令 exit 0、只有日志或采样不足转换成 PASS。

## 5. tools 接口要求

先维持进程级 CLI，不急于重写成共享库。每个工具必须：严格拒绝未知参数；支持明确设备绑定和超时；以稳定 JSON/JSONL 输出测量与错误；区分通信错误、采样超时和正常完成；不伪造失败 GET 响应；采集中断流返回失败；输出单调时钟和设备原始时间戳，并标记时间域。固定 /dev/videoN 和固定全局 /tmp 文件名不可作为正式接口。

把 uvc_host_ctl、uvc_host_ft、uvc_host_stream、uvc_host_imu 作为首批接口稳定化对象。preview/isp_cli 仅作辅助证据；配对、同步与 OTA 是专项能力。Case 只能调用本轮已校验摘要的工具包。

## 6. 资源与场景

设备标识采用 USB 物理路径 + 可验证的模组身份，UVC 节点在重启后重新发现。至少有 controller、USB hub、模组三个资源锁层级；同一模组不能同时被两个 Case 打开，三流和多模组 Case 可声明一组设备的原子占用。

Suite 只编排 Case 和参数，不复制断言。首批：preflight、烧录后冒烟、三流与帧头、模式/分辨率切换、厂测与持久化、30 分钟稳定性。OTA/A-B、故障注入、双机同步单列专项 Suite。功能适用矩阵按固件协议版本冻结；例如 2.0.7 电流边界与四种 device mode 不能使用旧阈值。

发布规则由 175 独立版本化：必需 P0 Case 全部得到 PASS，且无未测、阻塞、异常、证据不足或恢复失败；P1/P2 的要求由产品与发布类型指定。显示能力覆盖、执行覆盖和通过率，不能把未执行项目排除后显示“全部通过”。

## 7. 与当前实现的取舍

保留现有 jobs/TEST-NNNNNN 任务目录思想、SSH 文件交付、摘要校验、原子结果、事件、证据目录和锁。替换 qa-controller.cpp + worker.py 的业务逻辑；新协议可以直接升级，不需要兼容历史任务运行。历史 TEST-000001～000005 作为只读审计记录保留，不重新执行。

/opt/rmsl_test 一键脚本不进入新执行链；拆用其工具与测试步骤。rmsl-sdk-test 的 SDK/Kalibr 采集是专项测试能力，按需要包装成 Case，不承担通用任务调度。

## 8. 待确认的设计决策

1. RV1126B 默认测试固件交付是整包 Loader 烧录，还是 DFU A/B 分区烧录；两者可都实现，但需要明确首批默认路径及恢复手段。
2. “本地代码”仅指已提交的 Git commit，还是必须支持未提交工作区快照；后者须明确可复现与审计规则。
3. 首批发布门禁的产品模式、目标帧率/丢帧/时延阈值，以及 30 分钟或更长稳定性要求。
4. 图像空洞/闪烁等视觉问题的自动指标与人工复核边界。
