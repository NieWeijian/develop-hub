# 1.0 基线遥测问题记录（2026-10-14）

真实运行 `abb54d17-ed3d-40c8-b504-fca099129bc2` 保存了 24 项通过、1 项未通过和 1 项执行异常。报告优化不改变这些结论。

## 温度命令

`case-007/output/execution/protocol.jsonl` 的 `temperature-0` 发送 `get-temperature`（0x1135）。设备返回错误帧 `13 80`，其 19 字节载荷为 `unsupported command`。工具仅显示 `command failed: no response`，因此平台报告从已校验的原始证据展示具体原因。

当时 SDK 的 `src/ctl/ctl_service.cpp` 未提交修改删除了温度命令处理实现，以及 `is_plain_get` 和 `is_kcmd` 中对应路由，与设备行为一致。被测固件版本是 `factory-V2.1.1-dirty`；仅凭版本名不能证明它与当前工作区逐字节相同。

## VICAP 状态

`case-008/output/execution/protocol.jsonl` 的 `vicap-1` 接收块 0 末尾为 `...rkcif-mipi-lv`，块 1 开头为 `vds1`，拼接结果为 `rkcif-mipi-lvvds1`，不同于预期 `rkcif-mipi-lvds1`。异常已存在于原始 USB 数据，不是前端或测试解析器造成。

当时固件 `ctl_service.cpp` 的 `handle_set` 每选择一个块都重新调用 `dispatch`，替换动态状态文本；`handle_get` 再按 `chunk * (out_len - 2)` 截取。动态字段长度变化会使分块偏移不一致，这与接收记录中的重复字符吻合。

修复方向是在命令首块生成快照，后续块沿用同一份内容，并处理生命周期和并发隔离。不要自动把错误节点名纠正成期望值，因为其他字段也可能错位。

三个传感器身份与映射、第一次 VICAP/ISP 文本结构通过；第二次 VICAP 节点名校验失败后没有完成其余计数和错误增量比较，因此不能直接判断硬件异常。

## 本次范围

1.0 固定平台代码与报告功能，未修改或重新烧录上述固件。SDK 编译、烧录修复和专用真机联调属于后续工作；隔离开发环境维持仅模拟设备。
