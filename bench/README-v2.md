# QA v2 首轮联调

首轮使用 3588 上已连接的 RV1126B V2.0 模组和当前已安装固件。任务只执行两个 Case，不接受镜像，也不烧录。Develop Hub 的“架构联调”页面或 `python3 bench/demo_dispatch.py --wait` 可触发。

## 数据流

175 的 `qa_demo_api.py` 记录请求快照，调用 `demo_dispatch.py` 生成 schema v3 的 `test-run.json`，校验并推送到 3588 的 `demo-jobs/<round_id>`，最后原子发布 `READY`。3588 的 `qa_controller_v2.py` 校验配置、工具、Case 与运行时 SHA-256，定位唯一的 RMSL321 RGB UVC 设备，然后逐个启动 Case 进程。结果写到 `result.json`、`events.jsonl`、`cases/<case_id>/attempt-001/result.json` 和日志。175 读取结果并在 API 与页面展示。

- `test-demo1/1.0.1`：通过 `uvc_host_ctl` 读取 SN；首轮仅断言返回非空可打印标识，可选指定预期 SN 做精确匹配。当前实测值 `009`，这不代表量产 SN 规则合格。
- `test-demo2/1.0.1`：确认 `rgb+depth` 模式后通过 `uvc_host_stream` 采集至少两个 5 秒窗口，记录 RGB、DEPTH 帧率、收帧数和序号丢帧。首轮默认下限 1 fps，只验证链路和断言传递；发布阈值仍待定义。

## 部署与验证

Controller 服务：3588 `develop-hub-qa-v2.service`，根目录 `/home/linaro/develop-hub-qa`。旧 `develop-hub-qa-controller.service` 已停用；历史任务保留。

在 175 的 `/home/nwj/develop-hub`：

```sh
PYTHONPATH=server/src .venv/bin/python -m pytest -q server/tests bench/test_qa_controller_v2.py
python3 bench/demo_dispatch.py --wait
```

本轮真机结果：`TEST-DEMO-20260928T081632-576885`，两个 Case 均 passed；SN=`009`；RGB 两窗 9.35/9.83 fps，DEPTH 两窗 9.51/9.58 fps。当前固件为 `factory-V2.0.7`。此结果只证明已安装固件的测试链路走通，没有覆盖固件构建、烧录、IMU、时延、长时间稳定性或发布门禁。
