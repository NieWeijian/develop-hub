# 功能切换用例与固件 tools 交付要求

## 分工与资产

develop-hub 维护版本化用例源码、断言、参数、恢复、结果分类和证据展示。固件团队维护 tools 的控制协议和参数接口；平台维护 3588 UVC 采集和帧导出，不复制模组控制协议。

175 源码位于 `/home/nwj/develop-hub/bench/test-cases`；未具备工具条件的独立用例规格位于 `bench/case-specs`。3588 接收执行副本。现有 `bench/host-tools-source` 和 `xu-trace.patch` 是历史工具快照，不在本次继续修改；需要后续由固件方统一接管 trace 能力。

每次执行冻结用例定义、runtime、入口与 tools SHA-256。更新 tools 时需提供来源 commit、构建方式、架构、版本/能力清单、输出格式版本和摘要；用例与工具分开升级。工具与预期包不一致时，平台就绪检查阻止执行。

## 当前完成

`SWITCH-ROTATE-CONTROL/1.0.0`：保存原旋转值，切换相反值、原值，分别回读，finally 恢复原值并核验。设置失败、回读不符、格式错误、恢复失败均有测试覆盖。它只验证控制配置，不宣称图像效果通过。

2026-09-28 20:12 真机验证：模组 CPU `4d42583531310000000000000019050e`，当前固件 `b76b5cb`，旋转 `0→1→0`；五条断言通过、恢复原值 0。未烧录，属于执行适配器验证，不是新建平台轮次的完整验收。

Hub 证据：`var/artifacts/case-smoke/rotate-20260928-201223/`，包含输入、断言结果、stdout/stderr 和真实 XU 传输记录。3588 对应 `diagnostics/rotate-20260928-201223/`。

## 独立用例

以下 15 项各自有版本化 JSON 规格和可执行入口，已接入 3588 主机抓图：

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

## 接口边界（2026-09-28 修订）

稳定帧抓取、PNG 转换、附件和重枚举绑定由 develop-hub 的 3588 主机采集器实现。USB UVC 已提供的视频流不需要固件新增 dump 接口。

现有 get-param 可读取配置，BTNR 可在开流时查询 live 状态；投影仪 enable 是命令邮箱，不能等同实际灯状态。固件团队后续可完善机器可读能力枚举、真实生效值、物理灯状态及参数合法性/错误语义。缺少实际生效 getter 时，只声明配置/取流检查范围，不把图片留存当成算法效果判定。

执行方式、产物格式和限制见 [当前固件快速测试](current-firmware-testing.md)。以下历史旋转控制验证保留为背景；当前 15 个独立切换用例已经接入主机抓图。

## 人工修正结果分类

测试轮次中，已执行完成的用例可以修改为通过/不通过，也可以恢复自动分类。保存修改人（当前为操作员手工填写）、原因、时间、原自动结果和历次修改。并发修改用 revision 检测，过期编辑必须刷新。

修正保存在现有 TestRoundEvent 的独立事件中，不覆盖 Controller result.json 或断言；无数据库表结构变更。当前分类影响用例统计和筛选，报告提供 `case_classifications`、`case_classification_verdict`；既有 `verdict` 和轮次状态仍表示自动执行结果，界面分别标明，避免人工修正掩盖烧录/恢复错误。

API：`POST /api/qa/test-rounds/{round_id}/cases/{code}/classification`，字段 `outcome`（passed/failed/null）、`reason`、`operator`、`revision`。null 表示恢复自动分类。未测、运行中或不属于本轮的项目不能修改。

## 验证

后端回归 74 项通过；前端 lint、TypeScript、生产构建通过；部署后测试台 readiness 全部通过，新旋转控制用例已经注册。人工修正的 API 保存、读取、报告、历史、冲突与撤销均在隔离测试库验证，未改动历史真机轮次的分类。

卡片网格采用顶部对齐，展开卡片按自身内容增高；邻卡不会被拉成空白大卡片。浏览器自动化打开页面仍超时，页面视觉验收需区分于上述代码和接口验证。
