# 功能切换用例与固件 tools 交付要求

## 分工与资产

develop-hub 维护版本化用例源码、断言、参数、恢复、结果分类和证据展示。固件团队维护 tools 的协议、设备发现、取流、参数接口和帧导出。平台不自行复制协议或补充底层 ioctl 来绕过工具缺口。

175 源码位于 `/home/nwj/develop-hub/bench/test-cases`；未具备工具条件的独立用例规格位于 `bench/case-specs`。3588 接收执行副本。现有 `bench/host-tools-source` 和 `xu-trace.patch` 是历史工具快照，不在本次继续修改；需要后续由固件方统一接管 trace 能力。

每次执行冻结用例定义、runtime、入口与 tools SHA-256。更新 tools 时需提供来源 commit、构建方式、架构、版本/能力清单、输出格式版本和摘要；用例与工具分开升级。工具与预期包不一致时，平台就绪检查阻止执行。

## 当前完成

`SWITCH-ROTATE-CONTROL/1.0.0`：保存原旋转值，切换相反值、原值，分别回读，finally 恢复原值并核验。设置失败、回读不符、格式错误、恢复失败均有测试覆盖。它只验证控制配置，不宣称图像效果通过。

2026-09-28 20:12 真机验证：模组 CPU `4d42583531310000000000000019050e`，当前固件 `b76b5cb`，旋转 `0→1→0`；五条断言通过、恢复原值 0。未烧录，属于执行适配器验证，不是新建平台轮次的完整验收。

Hub 证据：`var/artifacts/case-smoke/rotate-20260928-201223/`，包含输入、断言结果、stdout/stderr 和真实 XU 传输记录。3588 对应 `diagnostics/rotate-20260928-201223/`。

## 独立用例

以下 15 项各自有版本化 JSON 规格，当前标记等待 tools 接口，不注册为虚假的可执行用例：

| 编号 | 功能 |
| --- | --- |
| SWITCH-RESOLUTION | 分辨率切换 |
| SWITCH-DEVICE-MODE | 设备模式切换 |
| SWITCH-ROTATE | 图像旋转效果 |
| SWITCH-RGB-BTNR | RGB BTNR |
| SWITCH-IR-BTNR | IR BTNR |
| SWITCH-PROJECTOR | 投影仪开关 |
| SWITCH-ALGO-ASYNC | 算法异步 |
| SWITCH-ALGO-ITER | 迭代次数 |
| SWITCH-ALGO-FPS | 算法帧率 |
| SWITCH-ALGO-MODEL | 模型类型 |
| SWITCH-ALGO-DENOISE | 去噪 |
| SWITCH-ALGO-FULLSIZE | 全尺寸 |
| SWITCH-ALGO-UNCERTAIN | uncertain 阈值 |
| SWITCH-ALGO-DISCONTINUE | discontinue 阈值 |
| SWITCH-ALGO-UNCERTAIN-DELTA | uncertain-delta 阈值 |

本轮不实现 USB profile 遍历、USB 物理断连、RGB-IMU/Depth 配对或空洞闪烁；反复切换属于独立压测目录。RGB/Depth/IR 质量归并为一个可配置框架，常规 USB3、默认 5 小时；具体像素格式和 IR 完整尺寸待能力枚举确认。

## 固件侧需补齐的公共能力

下面是接口要求，不代表现有 tools 已有这些参数。

1. **机器可读能力枚举**：按当前设备身份列出模式、流、分辨率、格式、目标帧率以及参数允许范围/步进；不支持命令或参数须非零退出，不能忽略未知参数。
2. **状态 getter / setter 成对**：返回配置值和实际生效值、是否要求开流或重启。投影仪要能区分 off/on/idle；算法参数不能只有 set。若统一 `get-param`，需稳定、有版本的字段类型和错误语义。
3. **流生命周期**：精确绑定 CPU/USB 端口、按指定组合开流；返回实际配置、首帧/连续有效帧信息；支持有界超时和正常退出；重枚举后能重新绑定同一设备。
4. **稳定帧导出**：在指定状态、流配置下等待定义数量的连续有效帧或指定稳定窗口，然后导出一帧。超时、无帧、错尺寸不得成功退出；无需图形桌面/DRM 显示器。不要把“睡几秒”当成已检测帧稳定。
5. **输出附件**：提供原始帧、可展示的 PNG，以及 JSON 元数据（设备身份、模式、设置值/实际值、尺寸、stride、像素格式、帧号、时间戳及其时基、数据摘要）。Depth 保留原始数值；预览色标与范围记录在元数据中。Hub 将其作为结果附件显示，不引入人工复核状态。
6. **恢复接口**：可读取并恢复原配置；重启型设置返回预期掉线与重连状态。恢复失败须可识别，不能继续污染下一用例。

工具当前已提供旋转 getter/setter，BTNR getter 需要 ISP 初始化和开流。现有冻结 preview 没有稳定帧导出；当前固件源码 preview 增加限时与总结，但检查帮助和实现仍未找到帧导出命令。因此完整“切换→稳定帧→图像附件”的用例暂不能自动执行。

## 人工修正结果分类

测试轮次中，已执行完成的用例可以修改为通过/不通过，也可以恢复自动分类。保存修改人（当前为操作员手工填写）、原因、时间、原自动结果和历次修改。并发修改用 revision 检测，过期编辑必须刷新。

修正保存在现有 TestRoundEvent 的独立事件中，不覆盖 Controller result.json 或断言；无数据库表结构变更。当前分类影响用例统计和筛选，报告提供 `case_classifications`、`case_classification_verdict`；既有 `verdict` 和轮次状态仍表示自动执行结果，界面分别标明，避免人工修正掩盖烧录/恢复错误。

API：`POST /api/qa/test-rounds/{round_id}/cases/{code}/classification`，字段 `outcome`（passed/failed/null）、`reason`、`operator`、`revision`。null 表示恢复自动分类。未测、运行中或不属于本轮的项目不能修改。

## 验证

后端回归 74 项通过；前端 lint、TypeScript、生产构建通过；部署后测试台 readiness 全部通过，新旋转控制用例已经注册。人工修正的 API 保存、读取、报告、历史、冲突与撤销均在隔离测试库验证，未改动历史真机轮次的分类。

卡片网格采用顶部对齐，展开卡片按自身内容增高；邻卡不会被拉成空白大卡片。浏览器自动化打开页面仍超时，页面视觉验收需区分于上述代码和接口验证。
