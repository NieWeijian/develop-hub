# 信息上报用例判定修订（2026-09-29）

此版本取代 information-cases-20260928.md 中的判定说明。未新建测试轮次，未开流操作模组，未编译烧录固件。验证使用 TEST-000011 的历史状态文本及模拟异常。

## INFO-PING

空闲、开流各 10 次，全部响应且每次 RTT ≤ max_rtt_ms，默认 50 ms。展示每次 RTT、最小值、平均值、中位数、最大值及阈值。开流前后各抓一组图。取消 10 个样本下信息价值有限的 p95/p99。

## INFO-SENSORS

三个唯一 SC132GS，校验独立 subdev、I2C 地址与角色映射：左 IR m00_b_sc132gs / 3-0030；右 IR m01_b_sc132gs / 3-0032；RGB m02_b_sc132gs / 1-0030。角色依据 SDK src/temperature/temperature.cpp。

VICAP 预期 rkcif-mipi-lvds、rkcif-mipi-lvds1、rkcif-mipi-lvds2，分别对应右 IR、左 IR、RGB。两次读取，默认间隔 5 秒（window_seconds 可配置，最大 30 秒）。每路 frame amount 必须前进，所有已列出的 CSI/MIPI 错误及 frame loss 分量增量为零；计数器回退也失败，不忽略重置。实际 FPS 同时检查两次 fps 字段以及帧计数增量/读取间隔，当前 RV1126B 档位 10 fps，默认容差 ±10%（fps_tolerance_percent 可配置）。不以输入格式中的 @30 作为实际帧率。

ISP 预期 rkisp-vir0、rkisp-vir1、rkisp-vir2，校验与上述 CIF 的连接；帧计数前进，ErrCnt、frameloss 增量为零。

保留本轮开始前的历史错误累计值，本轮只比较增量。缺字段、缺节点、重复节点、错误角色均失败。保留完整两次状态原文，开流前后留图。节点映射绑定 RMSL321 当前板型，硬件变化需更新用例契约。

## INFO-TEMPERATURE

用户确认六路全部需要：SoC、IMU、TX、左 IR、右 IR、RGB。开流后从 t=0 到 t=30 秒按约 1 秒间隔采集，共 31 个计划读数；工具调用耗时会使实际时长延长，保存实际时间戳。各路必须有有限数值，不允许 na/缺失/NaN/Inf。逐路断言结果展示最低、最高、中位温度（℃）及样本数；完整序列保存 JSON。没有过温判定，不自行增加温度阈值。

先读取工具帮助验证 get-temperature 是否存在。当前 ctl-trace-1 冻结工具缺此接口时报告 blocked，不开启流、不宣称温度测量失败。SDK 当前源码已有接口，但工具与固件的配套更新和真机验证尚未执行。

## IMU 与历史记录

INFO-IMU 从可选用例和信息上报目录移除；保留定义、执行器和既有结果供历史查看。本类不再测试 IMU 样本频率、六轴数据。温度中的 IMU 通道保留。

## 参数与验证边界

快速模式和烧录模式均验证参数类型、有限性和范围。前端提供 ping 阈值、Sensor 窗口和容差参数输入。未修改图片展示。工具 SHA 和原始 XU 留存继续沿用。硬件能力及实际通过情况以用户后续执行的轮次为准。
