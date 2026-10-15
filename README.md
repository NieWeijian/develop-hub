# RK Test Workbench 1.0

面向 Rockchip 芯片设备与固件的可配置自动化测试平台。测试人员用表单创建方案、选择固件和用例、填写人工记录、查看报告；开发人员通过版本化用例包扩展测试能力。

当前默认入口是新测试平台。新安装使用独立 `testing_*` 数据表，不导入旧定时任务、发布验证或历史测试数据。旧研发模块暂留在源码中，默认不注册路由、不启动任务；新平台不需要 Codex 账号。

## 1.0 交付范围

本版固定为 `1.0.0`，版本标签为 `v1.0.0`。主要交付手动创建方案、测试当前固件或已有镜像烧录、版本化用例库、参数覆盖、执行恢复与报告。详见 [版本说明](CHANGELOG.md)。

源码快照与构建已有实现和软件测试，但完整真实 SDK 自动编译烧录验收不属于本版承诺；自定义定时与 Gerrit 触发、开源交付整理均降低优先级，后续按需求推进。

当前开发环境与稳定环境独立：开发仅允许模拟设备；版本提交和打标签不代表已发布到稳定平台。稳定发布需要单独安排维护、备份和回滚流程，开发数据库不得覆盖稳定业务数据。

## 已实现

- 统一测试方案：设备当前固件、导入镜像、SDK 源码构建；模板只负责预填，配置可以修改。
- 方案草稿与不可变版本；用例默认参数、方案覆盖和单次运行覆盖；同一用例可绑定多次。
- SDK 多 Git 仓库状态、关注仓库；独立保存已提交内容、本地未提交修改和未跟踪源码；在副本中构建。
- 指定仓库地址和 ref 在副本中获取，保存解析后的提交；不更新原 SDK 工作目录。
- 手工创建观察类用例；ZIP 导入 Python 自动化用例，静态校验后确认来源并发布。
- 本地 / SSH 执行节点，节点互斥、设备身份检查、人工等待、取消、恢复状态与报告附件。
- 固定输入复测和按方案重新采集运行分别提供；模拟演示始终明确标注。
- RMSL321/RV1126B 版本读取示例和由平台维护的 DFU 部署适配器。

当前执行时机为手动。自定义定时 / Gerrit 触发预留独立扩展入口，尚未启用；不会恢复旧定时规则。构建节点首批仅支持控制端 Linux 本机的 SDK，SSH 用于设备测试节点。复杂设备恢复通过代码用例契约表达，首批表单人工用例仅支持观察和记录。

## 从 GitHub 重建与同步部署

推荐使用仓库内的固定版本构建流程，详见 [重建、快速同步与稳定发布](docs/deployment-rebuild.md)。开发更新可以一条命令构建、验证并切换；稳定发布需要单独的明确批准和维护窗口。

## 手工本地启动

需要 Linux、Python 3.10+、Node 22.13+、Git。SSH 节点另需 Python 3、SSH 身份及已登记设备适配器。以下命令都从仓库根目录执行，配置使用全新数据库：

```bash
cp .env.example .env
set -a
. ./.env
set +a
python3 -m venv .venv
.venv/bin/pip install -e './server[dev]'
mkdir -p var
PYTHONPATH=server/src .venv/bin/python -m alembic -c server/alembic.ini upgrade testing@head
PYTHONPATH=server/src .venv/bin/python -m uvicorn develop_hub.main:app --host 127.0.0.1 --port 8175
```

另开终端，加载同一 `.env` 后：

```bash
set -a
. ./.env
set +a
cd web
npm ci
npm run dev -- --host 127.0.0.1 --port 3000
```

打开 http://127.0.0.1:3000。空白首页可创建模拟演示数据，体验「编辑方案 → 检查并运行 → 填写人工记录 → 报告 → 复测」。真实使用时从「设备与环境」登记产品、设备和 SDK，再导入用例。

跨机器访问时配置 API 监听地址、浏览器可访问的 `NEXT_PUBLIC_DEVELOP_HUB_API_URL` 和精确的 CORS 来源。修改前端 API 地址后重新构建前端。此版本用于受信任内网工作台，尚未提供账户权限和不可信代码沙箱；执行用例采用节点账户权限。

一个产物目录只允许一个控制实例。不要使用多个 Uvicorn worker。systemd 示例和 `scripts/install-native.sh` 以新测试数据库为入口，不自动登记旧设备。安装脚本会安装并启动用户服务，开发预览无需执行它。使用 nvm 时在 `.env` 设置 `DEVELOP_HUB_NPM_BIN` 的绝对路径。

## 扩展与数据

- [新用例与节点协议](docs/testing-platform-contract.md)：参数、输入、结果、证据与设备适配器。
- [RK 版本检查示例](samples/case-packages/rk-version/README.md)：创建一个可导入 ZIP 包。
- `server/src/develop_hub/testing/`：配置、版本、输入资产、调度与执行。
- `bench/testing_node.py`：独立步骤执行协议，不携带旧流程。
- `web/app/testing/`：方案、用例、环境、执行与报告界面。
- `var/artifacts/testing/`：源码快照、固件、用例包、运行和证据。属于私有运行数据，不进入 Git。

构建快照会保留各仓库的 Git 对象和历史以支持版本相关构建，不能直接当作公开源码包分享。SDK 默认排除已识别的缓存、输出和凭据路径；实际 SDK 构建前应在环境输入规则中登记构建必需的忽略文件，并检查完整输入。采集前后会检测修改，不能替代文件系统原子快照。

真实烧录要求部署适配器确认设备序列号、物理端口、芯片兼容性、固件指纹、目标槽位及预期版本；BoardConfig 仅作为元数据，不作为烧录判定门槛。服务重启或异常后不会自动重发硬件步骤；设备进入需检查状态时，操作人应先核对残留任务与设备，再在界面提交恢复说明并重新读取身份。

## 验证

```bash
PYTHONPATH=server/src .venv/bin/python -m pytest server/tests
cd web
npm run typecheck
npm run lint
npm run build
```

测试覆盖不可变输入、脏工作区构建、ZIP 导入、真实子进程运行、人工步骤重启恢复、幂等和互斥，以及模拟硬件响应下的部署校验。软件测试不代表已完成真实 SDK 构建或真机烧录验收。

设备适配器由平台部署维护。RMSL321 接入使用 `scripts/provision-rmsl321.py`，配置 `DEVELOP_HUB_TEST_STATIONS` 指向私有注册表；测试人员只选择已部署测试台和设备序列号。详见 [平台扩展契约](docs/testing-platform-contract.md)。
