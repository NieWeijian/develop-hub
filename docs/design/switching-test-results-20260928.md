# 功能与模式切换测试结果（2026-09-28）

已补齐 3588 主机抓图及开发者快速测试模式。本次未编译或烧录模组固件。

## TEST-000009：全部 15 项

| 用例 | 自动结果 | 图片数 | 说明 |
| --- | --- | ---: | --- |
| SWITCH-ALGO-ASYNC | passed | 4 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-DENOISE | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-DISCONTINUE | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-FPS | passed | 4 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-FULLSIZE | passed | 4 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-ITER | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-MODEL | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-UNCERTAIN | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-ALGO-UNCERTAIN-DELTA | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-IR-BTNR | failed | 2 | Live BTNR 0 |
| SWITCH-PROJECTOR | passed | 6 | 配置、有效取流及恢复检查通过 |
| SWITCH-RGB-BTNR | failed | 2 | Live BTNR 0 |
| SWITCH-ROTATE | passed | 4 | 配置、有效取流及恢复检查通过 |
| SWITCH-DEVICE-MODE | error | 6 | Stable frames not received: artifacts/state-2-rgb_depth_ir/3 |
| SWITCH-RESOLUTION | passed | 10 | 配置、有效取流及恢复检查通过 |

共 12 项通过、2 项失败、1 项执行异常；78 张图片。所有用例恢复检查通过。

## TEST-000010：BTNR 定位补测

RGB 与 IR 均保留失败：保存的 BTNR=0 在开流后实时查询为 1。持续开流中的 1→0 切换、实时回读与恢复均通过。用户已确认这是固件问题，本次保留失败记录，不修改固件或放宽判定。补测另保存 12 张图片。

## 证据与验证

两轮合计 90 张图片、原始帧、元数据、XU 记录和断言已保存到 175 的 evidence/TEST-000009 与 evidence/TEST-000010；逐附件验证大小及 SHA-256。对应测试包副本也已冻结。

第 9 轮三流模式异常：RGB+Depth+IR 中 IR 在 50 秒内未达到连续 12 帧且至少 1 秒的稳定条件；RGB 和 Depth 已取得图片，原模式恢复成功。不能据此断言完全没有 IR 帧。

软件验证：80 项后端测试通过；前端 lint、TypeScript 和构建通过；实际图片 API 下载、图像显示及非法附件路径拒绝验证通过。浏览器自动化连接超时，未完成整页视觉验收。

使用入口：产品 → 新建测试轮次 → 勾选“开发者快速测试” → 选择模组和用例 → 测试当前固件。图片只展示结果；人工可在轮次中修正通过/失败分类，原自动结果继续保留。
