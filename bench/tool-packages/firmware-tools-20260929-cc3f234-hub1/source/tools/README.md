# RMSL321 Host Test Tools

Linux 主机端结构光模组测试工具集。通过 USB UVC 采集 RGB / DEPTH / IR 三路视频流，覆盖厂测、ISP 压力、流监控、交互调试和实时预览。

**要求：** Linux (aarch64 / x86_64)，g++ / cmake，libdrm，UVC 设备驱动。适配 RK3576 / RK3588 及其他 Linux 主机。

## 构建

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

产物在 `build/` 下，共 9 个可执行文件。

## 工具一览

| 工具 | 说明 | 命令示例 |
|------|------|----------|
| `rmsl_single_module_test` | 单模组一键测试 | `./rmsl_single_module_test run --quick` |
| `rmsl_multi_module_test` | 多模组：枚举+逐台测试+同流验证 | `./rmsl_multi_module_test` |
| `uvc_host_ft` | 厂测 (设备/USB/双目/遥测) | `./build/uvc_host_ft --quick` |
| `uvc_host_isp_test` | ISP 3A 测试 (RGB+IR 出流 + 参数回读) | `./build/uvc_host_isp_test 10` |
| `uvc_host_stream` | 纯取流 (RGB+DEPTH fps / 丢帧) | `./build/uvc_host_stream` |
| `uvc_host_ctl` | 控制通道 (UVC XU) CLI/自检 | `./build/uvc_host_ctl --check` |
| `uvc_host_isp_cli` | 交互式 3A 调试 CLI | `./build/uvc_host_isp_cli` |
| `uvc_host_preview` | DRM 实时预览 | `./build/uvc_host_preview` |
| `uvc_host_ota` | DFU OTA 升级测试 | `./build/uvc_host_ota --image-dir ../output/image` |

---

## uvc_host_ctl — 控制通道 (UVC XU)

控制命令走 UVC 扩展单元（extension unit 6 / selector 1，SET_CUR 下发、GET_CUR
按 60B 分块回读），由设备端 `struct_light_ctl` 守护进程提供服务；IMU 数据走
HID（`/dev/hidraw`，report 1，48B=2×24B 样本）。主机侧功能由 `uvc_host_ctl`
提供（`uvc_host_stream` 只负责取流）。

**Linux 主机权限：** `hidraw` 节点默认 `root:root 0660`，安装 udev 规则后免 sudo
（多模块共挂时请用 `--device /dev/videoN` 指定模组）：

```bash
# /etc/udev/rules.d/60-rmsl-hid.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2207", MODE="0666"
sudo udevadm control --reload-rules && sudo udevadm trigger
```

```bash
./build/uvc_host_ctl ping 3                        # RTT 测量
./build/uvc_host_ctl get-version                   # 读取控制版本
./build/uvc_host_ctl get-sn                        # 读取 SN
./build/uvc_host_ctl get-board-sn                  # 读取上层板/下层板 SN
./build/uvc_host_ctl file-list /tmp                # 列出设备目录
./build/uvc_host_ctl file-get /tmp/a.txt out.txt   # 下载小文件 (≤60KB)
./build/uvc_host_ctl file-put local.txt /tmp/b.txt # 上传小文件 (≤60KB)
./build/uvc_host_ctl get-param                     # dump param 配置（调试）
./build/uvc_host_ctl set-algo-mode 512x448         # 设置算法分辨率并重启模组
./build/uvc_host_ctl get-algo-mode                 # 查询模式/切换状态
./build/uvc_host_ctl get-log                       # 拉取模组日志尾部（调试）
./build/uvc_host_ctl clean-log                     # 清理归档日志+截断当前日志
./build/uvc_host_ctl app-status                    # app 进程存活检查
./build/uvc_host_ctl sensor-status                 # v4l2 SC132GS 传感器枚举
./build/uvc_host_ctl vicap-status                  # dump /proc/rkcif* 节点
./build/uvc_host_ctl isp-status                    # dump /proc/rkisp* 节点
./build/uvc_host_ctl imu-status                    # imu app + IIO icm42607 状态
./build/uvc_host_ctl health                        # 生产健康汇总（一次看清各部件）
./build/uvc_host_ctl reboot                        # 重启模组（回 UVC 模式）
./build/uvc_host_ctl reboot-loader                 # 重启进 loader（DFU 待机）
./build/uvc_host_ctl adb-mode [SEC]                # 切 ADB 模式并等待 adb 设备
./build/uvc_host_ctl enter-dfu boot_b              # 进 DFU（目标分区，OTA 前置）
./build/uvc_host_ctl get-pro-current                # 读取投影仪目标电流
./build/uvc_host_ctl set-pro-current 1500           # 设置投影仪电流（100~2000 mA，步进 100）
./build/uvc_host_ctl get-usb-profile                # 读取 USB 带宽配置档位
./build/uvc_host_ctl set-usb-profile low           # 设置 USB 带宽档位（重启生效）
./build/uvc_host_ctl get-sync-role                  # 主从角色（0x1106）
./build/uvc_host_ctl get-cpu-serial                 # CPU 序列号（0x1107）
./build/uvc_host_ctl set-fov 63.06 / get-fov        # FOV 设置/读取
./build/uvc_host_ctl set-sn <SN>                    # 写 SN
./build/uvc_host_ctl set-board-sn <UPPER> <LOWER>   # 原子写入上层板/下层板 SN
./build/uvc_host_ctl set-algo-denoise 0             # 算法参数（denoise/uncertain/iter/fps 等）
./build/uvc_host_ctl set-ft-aging -2                # 老化结果写（FT 写，0x1126）
./build/uvc_host_ctl factory-reset                  # 出厂复位
./build/uvc_host_ctl time-sync <host_us>            # 时间同步（8B host us）
./build/uvc_host_ctl ... --device /dev/videoN      # 指定模组，默认自动探测
```

**排障状态命令：** `app-status` 报告 `struct_light_app` 是否存活及 pid；
`sensor-status` 通过 v4l2 子设备 sysfs 枚举 SC132GS（预期 3 个：
RGB + 双 IR），缺一个会标 `EXPECT 3!`；`vicap-status` / `isp-status`
分别原样 dump 设备端 `/proc/rkcif*` / `/proc/rkisp*` 的全部节点内容
（时钟、中断计数、输入格式等）；`imu-status` 报告 `struct_light_imu` 进程
存活 + IIO icm42607 设备信息（采样频率、buffer watermark）；`health` 一条命令
汇总 USB 链路 / app / sensor(0-3) / imu / algo mode，生产上出问题一眼定位
是哪个光学件（sensor 缺、IMU 缺 = 摄像头板问题）。

**param 模块：** 仿 rkipc 的分节键值存储（纯 C++ 实现），按模块划分
`system` / `algo` / `rgb` / `ir` / `aiq` / `fov` / `projector` / `switch`，
统一存于设备端 `/data/param.conf`（INI 风格、带注释、可手工编辑）。
ctl 修改、algo 读取，写入原子替换 + 版本号，读方轮询 mtime 也能感知手工改动。
模组日志当前段为 `/userdata/log/lasted.log`（序列号在 log_seq.txt）。

**算法分辨率切换：** 当前支持 640x352 / 512x448。`set-algo-mode WxH` 会把目标
分辨率写入设备端 `/data/param.conf`（`[algo] width/height` + `[fov] crop`）并
重启模组，下次开机按新分辨率初始化（模型加载约数秒）；`get-algo-mode` 轮询
status（1=loading → 2=ready）确认就绪后即可开流。不再做进程内运行时切换。

协议：`magic('RMSL') + ver + type + status + seq + plen + cmd + crc32 + payload`，
走 UVC XU 控制通道（ACM 已移除，控制+IMU 分别走 XU / HID）。当前设备端白名单目录：
`/calibration/calib_data`、`/usr/share/calib_data`、`/oem/usr/share/calib_data`、
`/userdata/log`、`/tmp`、`/data`。

控制通道自检（快速验证独立控制链路）：

```bash
./build/uvc_host_ctl --check                           # 控制通道自检
./build/uvc_host_ctl --check --device /dev/videoN      # 指定模组
```

输出末尾为 `CTL CHECK: PASS (node=..., rtt=..., sn=...)`，失败返回非 0。

---

## rmsl_single_module_test — 单模组自动化测试

单台模组一键测试：三种模式 + 可选跳过项。默认自动查找；多模组时用 `--device`
指定（`rmsl_multi_module_test` 会自动枚举并逐台调用本脚本）。

### 三种模式

| 模式 | 内容 | 预计时长 |
|------|------|----------|
| `run --quick`（默认） | 所有功能项 + 短稳定性 | ~3 分钟 |
| `run --long [H]` | 与 quick 相同的功能项，规模更大 + 长时间稳定性 | 小时级（默认 24h，可 `--long 48`） |
| `run --preview-only [SEC]` | 预览烤机：RGB+DEPTH 显示 + IMU + ctl 监控 | 默认 3600s |

### quick / long 测试项（按顺序执行）

1. **device check** — 按当前 device 模式枚举 UVC 节点（rgb+depth: RGB+DEPTH；calib/rgb+ir: RGB+IR）
2. **USB2 profile 自适应** — 链路为 480M 时自动 `set-usb-profile low` 并重启，
   等控制+出流恢复后继续（`--no-profile` 跳过）
3. **device 模式基线** — 归一化到 rgb+depth（产品模式；非基线则 `set-device-mode rgb+depth` + 重启）
4. **算法分辨率基线** — 归一化到 640x352（写 ini + 重启）
5. **ctl 链路测试** — clean-log → `--check` → ping 延迟统计 → version/sn →
   get-param → app/sensor/imu/vicap/isp 状态 → 32KB 文件上传下载往返（md5 校验）→
   get-log（`--no-ctl` 跳过；restart-app 仅 long 模式执行）
6. **stream IR-off** — RGB+DEPTH 各 20s（quick），帧率 ≥8 且丢包 ≤50 才 PASS
7. **stream IR-on** — 设备模式含 IR 时三路出流检查（RGB+DEPTH+IR）
8. **switch 测试** — 算法分辨率往返（640x352↔512x448）+ device 模式往返
   （calib → rgb+ir → rgb+depth+ir → rgb+depth），quick 默认 0 轮，`--switch-rounds`/`--mode-rounds` 开启，
   long 默认 10/5 轮；每轮各重启验证出流
9. **ISP 3A stress** — RGB+IR 出流 + 3A 参数读写回读（设备模式含 IR 时执行）
10. **FT 厂测** — `uvc_host_ft --quick`（`--no-ft` 跳过）
11. **OTA** — 完整 A/B：oem/system/boot 双槽各完整烧录一遍（boot 最后烧，带 `-R`），最终从 A 槽启动；
    flash 磨损保护每槽最多 3 次（计数在 `tools/.ota_counts`，达上限自动 SKIP）
12. **稳定性** — RGB+DEPTH 出流 + IMU + 每 50s ctl ping 监控
    （quick 默认 1 分钟，long 按小时计）

### 运行选项

```bash
./rmsl_single_module_test run --quick                 # 快速全功能（默认）
./rmsl_single_module_test run --long 48               # 长测 48 小时
./rmsl_single_module_test run --preview-only 3600     # 预览烤机 1 小时
./rmsl_single_module_test run --quick --device /dev/video21 --hidraw /dev/hidraw0  # 指定模组
./rmsl_single_module_test run --quick --switch-rounds 3 --mode-rounds 2            # 开启切换轮次
./rmsl_single_module_test run --quick --no-ota --no-ft --no-switch                # 跳过指定项
./rmsl_single_module_test run --quick --firmware-dir /path/to/firmware
./rmsl_single_module_test package      # 交叉编译打包（含 firmware 校验）
```

**输出：** 最后一行 `RESULT: PASS/FAIL`，逐项列出
`ctl / stream / switch / isp / ft / ota / stability` 的结果；报告与各阶段日志
写入当前目录（`report_<时间戳>.log`、`ctl_*.log`、`stability_*.log` 等）。

**USB2 低带宽 profile：** 480M 链路下自动切 `usb_profile=0`（low：RGB/DEPTH 均
maxpacket 1024 / burst 1）并重启。这是多模组 USB2 同时出流的关键——DEPTH 若保持
2048 会占满等时带宽预留，两台就起不来。SuperSpeed 下保持当前档位。

---

## rmsl_multi_module_test — 多模组测试

一个 hub 挂多台模组时用本脚本：自动枚举所有在线模组，逐台跑单模组套件，
最后验证所有模组同时出流。

```bash
./rmsl_multi_module_test --list        # 只枚举：端口/串号/各流节点
./rmsl_multi_module_test               # 默认：每台 quick + 多台同流验证
./rmsl_multi_module_test --long        # 每台 long 套件
./rmsl_multi_module_test --single --no-ota --no-ft   # 透传参数给单模组脚本
```

**底层选择机制：** 所有工具统一收 `--device /dev/videoN`（传 RGB 节点即可，
DEPTH/IR 节点由同一 USB 设备自动推导）、`uvc_host_imu --hidraw /dev/hidrawN`；
`uvc_host_ota --device` 自动带 `dfu-util -p <hub端口>` 防止多台烧错机。
USB 串号 = 设备序列号，唯一，作为选择键。

**验证结果：** 两台模组同挂 USB2 hub（480M），512→1024 修正后（DEPTH=1024）
双台 RGB+DEPTH 同时出流稳定：RGB ~10fps / DEPTH ~9fps。

---

## uvc_host_ft — 厂测

出厂质检工具，不与 isp_test 重叠。

**测试项：**

| 阶段 | 内容 |
|------|------|
| Device + USB | 3 路 UVC 探测、VID/PID/SN、USB 速度、供电 |
| Stream + IR | 三路出流验证 + IR 左右目有效性（标定用） |
| Module Telemetry | 通过 ctl（UVC XU）读取模组 ft_status (温度/外设/MIPI/同步/DDR) |
| Burn-in | 长时间流稳定性 (仅 --long) |

> 帧率/丢帧统计不在 FT 中做（打开 IR 会影响帧率；IR 仅供标定）。帧率监控由
> `uvc_host_stream` 负责（RGB/DEPTH，真实使用场景）。

```bash
./build/uvc_host_ft --quick         # 快速冒烟 (~22s)
./build/uvc_host_ft                  # 标准测试
./build/uvc_host_ft --long           # 含烧机
./build/uvc_host_ft --json           # JSON 输出
./build/uvc_host_ft --stream-sec 60  # 自定义流测试时长
./build/uvc_host_ft --burn-in-sec 3600
```

**输出格式：** 最后一行 `RESULT: PASS` 或 `RESULT: FAIL`。JSON 模式输出 `"overall":"PASS"`。

---

## uvc_host_isp_test — ISP 控制压力测试

打开 RGB + IR 两路流，循环读写 15 项 V4L2 控制。

```bash
./build/uvc_host_isp_test [depth_frames] [rounds]
# depth_frames: 深度帧数 (默认 30)
# rounds:       测试轮数 (默认 10)
```

**判定：** 最后一行 `summary: rgb=SUCCESS ir=SUCCESS => SUCCESS`

---

## uvc_host_stream — 流监控

打开 RGB + DEPTH 两路，持续监控 fps 和序列号丢帧。

```bash
./build/uvc_host_stream
# Ctrl+C 停止，每 5s 打印一次统计
```

---

## uvc_host_isp_cli — 交互式 3A CLI

打开 RGB + DEPTH + IR（IR 延迟启动），支持 DRM 预览和交互命令。

```bash
./build/uvc_host_isp_cli [--no-drm]
```

| 命令 | 说明 |
|------|------|
| `help` | 列出全部命令和参数范围 |
| `camera rgb\|ir` | 切换预览和默认 3A 目标 |
| `set [rgb\|ir] <param> <value>` | 设置控制项 |
| `get [rgb\|ir] <param>` | 读取控制项 |
| `get [rgb\|depth\|ir] fps` | 查看帧率 |
| `quit` | 退出 |

**参数列表：** `brightness` `contrast` `hue` `saturation` `sharpness` `gamma` `wb_temp` `exposure_mode` `gain` `exposure_time` `hue_auto` `power_line_frequency` `btnr`

---

## uvc_host_preview — DRM 实时预览

默认打开 RGB + DEPTH 两路；`--ir` 预览标定模式 RGB + IR，`--all`
同时打开 RGB + DEPTH + IR 三路。IR 仅使用双目拼接帧：标定预览为
1080x2560，三流预览为降采样的 540x1280。

```bash
./build/uvc_host_preview [--device /dev/dri/card0]
./build/uvc_host_preview --all
# Ctrl+C 停止
```

---

## uvc_host_ota — DFU OTA 升级测试

通过 ctl 控制通道（UVC XU）让模组进入 DFU 模式，再用 `dfu-util` 烧写另一槽的
oem/system/boot（默认升级集，boot 最后烧），`-R` 复位并回检运行模式设备。**仅验证 DFU 烧录
方式**（不涉及 adb OTA / 标定 OTA）。

流程：

1. 读取模组版本（ctl `get-version`）得到当前启动槽；
2. 发送 ctl `enter-dfu` 命令，payload 为目标槽分区名（如
   `uboot_b`），模组擦除该分区、`fw_setenv dfu_once` 后重启进入 U-Boot DFU；
3. 等待 DFU 设备 `2207:0107`，用 `dfu-util -l` 确认目标槽 alt（uboot_*/boot_*/system_*）；
4. 逐个 `dfu-util -d 2207:0107 -a <alt> -D <part>.img` 烧写（默认
   `oem,system,boot`，boot 最后烧），最后一条带 `-R`；
5. 等待运行模式设备 `2207:001a`（RMSL321 UVC）重新出现，读回版本，输出
   `RESULT: PASS/FAIL`。

```bash
./build/uvc_host_ota                              # 完整流程
./build/uvc_host_ota --flash-only                 # 模组已在 DFU 模式，直接烧写
./build/uvc_host_ota --enter-dfu-only             # 只发送进 DFU 命令（脚本化场景）
./build/uvc_host_ota --verify-only                # 只回检运行模式设备+版本
./build/uvc_host_ota --image-dir ../output/image  # 指定镜像目录
./build/uvc_host_ota --parts oem,system,boot --slot b --json
```

**参数：** `--image-dir DIR`（默认 `./output/image`）、`--parts a,b,c`（默认
`oem,system,boot`）、`--slot a|b`（目标槽覆盖，默认取当前槽的另一侧）、
`--timeout N`、`--no-reboot`、`--json`。

**依赖：** 主机需安装 `dfu-util`（`apt install dfu-util`），镜像文件按
`<分区名>.img`（`boot.img`/`system.img`/`oem.img`）放在 `--image-dir` 下。

> 注意：`oem` 分区（128MB，partubi）擦写较慢，stock dfu-util 固定 5s 的 USB
> 超时会导致传输中断（设备端报 `dfu_write: Wrong sequence number!`）。建议使用
> 支持 `DFU_TIMEOUT` 环境变量（默认 60s）的 dfu-util 构建；本工具烧写时自动带上
> `DFU_TIMEOUT=120000`。

---

## 目录结构

```
tools/
├── CMakeLists.txt          # 构建脚本
├── rmsl_single_module_test  # 单模组一键测试
├── rmsl_multi_module_test   # 多模组：枚举+逐台测试+同流验证
├── uvc_host_ft.cpp         # 厂测
├── uvc_host_isp_test.cpp   # ISP 压力
├── uvc_host_stream.cpp     # 流监控
├── uvc_host_isp_cli.cpp    # 交互 CLI
├── uvc_host_preview.cpp    # DRM 预览
├── uvc_host_ota.cpp        # DFU OTA 升级测试
├── common/
│   ├── v4l2_capture.{cpp,hpp}   # V4L2 采集库
│   └── drm_preview.{cpp,hpp}    # DRM 显示库
└── README.md
```
