# 产品驱动的 QA 测试台

设计原型：[`prototypes/test-workbench.html`](prototypes/test-workbench.html)。仅作为交互参考归档，生产 UI 不加载此 HTML。

## 实施计划与架构

- Server：FastAPI API + SQLAlchemy 状态、快照和事件；受控 subprocess 执行 Git / build / SSH / SCP。
- Web：复用现有 React/Vinext、panel、按钮、表单和配色；重新组织为产品时间线、测试点、基线、轮次交付证据。
- TestProduct / TestPoint / TestRound 不关联 CodingRun。旧 QA 数据、旧烧录和执行流程不用于新测试台。
- 产品只能选 175 管理员注册的 SDK。应用列表由服务端验证真实 Git 根目录、SDK 范围及 Gerrit remote 映射；Kernel、U-Boot、RKBin 不作为测试对象。
- 用户选 Board Config，服务端以参数数组生成 `./build.sh lunch` 并通过 stdin 输入菜单序号，然后执行 `./build.sh`。

## 操作流程

1. 创建产品：选择 SDK，核验 Manifest，选择唯一应用 Repo，选择 Board Config，填写产品及固定组件基线说明。
2. 产品基线页运行受控编译。命令退出码为 0 且非空镜像为本次刷新产物后，基线才能变为 ready。
3. 产品测试点页登记 Change / Patchset / 完整 Commit。Ref 由前端生成且服务端复核，并向产品锁定的 Git remote 查询精确 Ref/Commit。
4. 从具体产品创建 Test Round，选择该产品的测试点、已登记测试台、Test Cases 和 JSON 对象参数。产品和 Repo 不可重新选择。
5. 服务端固定快照，原子领取轮次，取得 SDK 文件锁，检查 Manifest 和固定项目漂移，fetch 并核对 FETCH_HEAD，建立独立测试分支，配置并编译。
6. 校验镜像并复制到轮次独立目录，生成 JSON，传输文件，复核 JSON 和镜像 SHA-256，记录 remote_ready 和校验结果。最后尝试恢复原应用 checkout，恢复结果写入事件。

UI 不根据文字推断执行成功。已启用节点不展示为“在线”。失败后保留历史并创建新轮次，不覆盖原结果。

## 数据库

新增 migration `71ad9b20e451`（接 `4c6f2a9d8e10`）：

- `test_rounds.bench_snapshot`：创建时的节点 ID、名称、host、port、user、remote_root。
- `test_rounds.delivery_result`：远端验证退出码、时间、JSON/镜像摘要和大小。
- 对齐既有 SDK、Gerrit、测试台、轮次唯一索引与模型定义；状态枚举沿用 VARCHAR(32)。
- 一个产品只绑定一个 Repo；不同产品可复用同一应用仓库，移除旧的 SDK/Repo 双列唯一约束。
- 历史轮次的缺失快照保留为空，不用当前节点信息伪造历史；缺快照的旧轮次不能重新启动。

产品快照额外保存 SDK 路径、产物规则、各 Manifest 项目 HEAD、应用 remote 与产品基线构建结果。每轮独立保存 Manifest Commit、Board Config、Project、Change/Patchset/Commit、构建结果、镜像摘要、Test Case 参数、测试台和事件时间线。

## API

- `GET /api/qa/sdk-repositories`：只读注册列表。
- `GET /api/qa/sdk-repositories/{id}/manifest`：路径、Manifest 文件/分支/Commit、同步观察状态、项目数、本地修改。
- `GET /api/qa/sdk-repositories/{id}/applications`：服务端核验的可选应用及 Git 状态；自动映射 Gerrit Project。
- 原 `POST /sdk-repositories` 和 `/discover` 返回 403，操作员不能探测或登记任意路径。
- 产品与测试点、基线验证、测试台、用例和轮次沿用 `/api/qa` 接口。
- `GET /api/qa/test-rounds?product_id=...` 支持产品历史范围。
- `POST /api/qa/test-rounds/{id}/start` 仅接受新轮次，重复启动返回 409，不改变已完成结果。

## 管理员注册 SDK

先完成数据库迁移。注册从 175 shell 执行，不从浏览器执行：

```bash
PYTHONPATH=server/src .venv/bin/python -m develop_hub.register_sdk \
  --name SDK_NAME --root /path/to/registered-sdk \
  --board-directory project/cfg --board-pattern 'BoardConfig*.mk' \
  --artifact-root /path/to/registered-sdk/output/image --artifact-pattern '*.img'
```

Board Config 文件名必须符合 SDK 的 lunch 接口；注册工具不静默覆盖已有 SDK。测试台 SSH key 与 known_hosts 使用现有服务端配置，StrictHostKeyChecking 保持开启。

## 验证与本阶段边界

运行指定的 pytest、lint、build，并用独立 SQLite 库验证 Alembic 升级/回退/再升级与模型一致性。
浏览器使用独立测试数据检查产品列表、Manifest/Repo 选择、产品时间线及轮次详情，不操作真实 SDK 或远端测试设备。

本阶段止于 JSON 生成、JSON/镜像传输及远端文件验证。烧录、Test Case 执行、设备证据采集和 QA 报告未实现。
没有自动同步 Gerrit 队列或解析跨 Change 依赖；本阶段登记精确测试点并核验 Ref/Commit。Manifest 同步字段表示本地观察状态，不声称实时远端已同步。
SDK 文件锁协调此测试台的基线与轮次进程；外部人工 SDK 操作不受锁控制，必须避免同目录并发操作。

实现未升级运行中实例数据库、未重启正式服务、未进行真实 SDK 构建或真实测试台交付验收。

### 本次验证结果（2026-09-11）

- `PYTHONPATH=server/src .venv/bin/python -m pytest server/tests`：30 passed。
- `cd web && npm run lint && npm run build`：全部通过。
- 临时 SQLite：全链升级、新 migration 回退/再升级、`alembic check` 通过，无模型差异。
- 浏览器：产品列表/历史、轮次证据、Manifest/Repo 联动、Board Config 生成命令已检查；控制台无 error。
- SSH 传输以受控替身验证流水线；远端校验命令另用本地临时文件执行并验证 JSON/镜像摘要。未声称通过真实测试台现场验收。
- 非阻塞既有警告：Starlette TestClient 弃用提示、旧 Coding/QA 双向外键排序提示、构建工具 punycode/路由分类提示。

### 创建页面修复（2026-09-11）

- 支持 repo 管理的 `rk` remote，1126B/3572 本地应用仓库均通过只读发现验证。
- 产品表单移除 Kernel/U-Boot/RKBin 输入，保留历史数据兼容性。
- 新增 `GET /api/qa/sdk-repositories/{id}/board-configs`，返回实际菜单序号与配置命令。
- lunch 以 C locale 下的 SDK 菜单排序生成输入；RK3572 从 1 编号、RV1126B 从 0 编号。服务端输入序号后核对 SDK 配置符号链接，选错配置即拒绝继续编译。
- 此次无数据库结构变更。31 个后端测试通过，包括 rk remote、菜单序号和配置选错时拒绝构建。

### 基线干净检查范围调整

仅以产品绑定应用 Repo 的已跟踪文件（工作区和暂存区）作为干净门禁。未跟踪文件单独记录为 `untracked_files`，状态为 `untracked_only`，不阻止构建。不自动删除、暂存或忽略用户文件。
其他 SDK 项目的本地修改继续记录到 Manifest 快照，不阻止基线或轮次构建。Manifest 身份变化、项目缺失和固定项目 Commit 漂移仍保留校验。
未跟踪文件若与目标 Patchset 路径冲突，Git checkout 自身会拒绝覆盖并记录错误。

### 基线实时进度

基线验证 POST 立即返回 202，服务端后台执行并原子阻止重复提交。现有 `baseline_result.progress` 保存阶段、命令、开始/更新时间和最近 24KB 日志，不新增数据库字段。
服务端同时增量读取 stdout/stderr，每 0.5 秒保存进度；前端每 2 秒刷新，显示阶段、耗时、可跟随滚动的普通日志及结果摘要。原始 JSON 保留在次要折叠区。
服务重启会把未完成基线标记为中断，防止页面永久显示运行中。已有完成记录也提取日志为可读文本。


## Gerrit 链接登记 Patchset

登记弹窗粘贴 Change 链接后延迟 500ms 自动查询；展示查询状态、可重试错误和 Patchset 下拉选择。默认选最新版本，链接带 /Patchset 时选指定版本；Commit、分支、提交者自动填充，测试点说明可编辑。修改链接立即清除旧结果并禁用保存，过期请求取消。

新增 POST /api/qa/products/{product_id}/patchset-lookup，请求为 {"url": "Gerrit Change 链接"}。服务端从已登记应用 remote 确定 Gerrit SSH 主机，通过只读 gerrit query 查询；校验链接主机、Project 和返回 Project，不访问用户输入的任意 URL。保存沿用 Ref/Commit 的服务端再次核验。本次无数据库结构变化。

验证：链接解析、跨主机/仓库拒绝、最新与指定 Patchset、缺失版本；真实 316200 返回 PS1–PS6，默认 PS6。阶段边界仍为 JSON/镜像远端校验，不涉及烧录或 Test Case 执行。


## Test Round 配置与时间线修正（2026-09-12）

测试点新增 submitted_at，显示所选 Patchset 在 Gerrit 的上传时间及 Owner；登记时保存、创建轮次时固定到产品快照，交付 JSON source 同步携带。Test Case 新增 basic/special category，对应基本功能 set/特殊场景 set，已有用例默认 basic。新增 PATCH /api/qa/test-cases/{id}/category，只调整分组，允许插件用例分类而不改插件命令；分类写入 test_case_snapshot 和 JSON，历史快照保持原值。

新建轮次固定 sdk_configuration.artifact_patterns=["update.img"]，产物缺失、为空或未刷新均失败；镜像摘要、冻结文件、传输、远端验证均使用该规则。历史轮次继续保留原产物记录。

后端事件推进时结束上一阶段：通过检查才标记 succeeded，失败或中断则标记 failed/blocked，details 记录 finished_at。前端展示阶段名、中文状态和结束时间。迁移 82bc10a4d721 新增字段并依据后续阶段事件修正历史 running 状态；同时对齐历史状态列长度。迁移在生产数据库副本上完成升级、降级、再次升级及 alembic check。

交付边界仍为远端 JSON 和镜像校验，不烧录、不执行 Test Case。


## 3588 Controller 与固件烧录（2026-09-12）

新增 bench/qa-controller.cpp（175 交叉编译 ARM64 静态程序）与 Python 标准库协议/烧录适配器 worker.py，3588 systemd 常驻。任务文件仍位于 jobs/TEST-NNNNNN；只有带校验摘要的 READY 原子发布后才接收，历史交付不会自动触发烧录。Controller 维护 result.json、events.jsonl、logs/。当前执行边界 firmware_flash，所选 Test Cases 明确 not_run。

TestRoundCreate 新增 flash_firmware 和 device_serial，默认 API 仍仅交付；界面允许勾选烧录并通过 GET /api/qa/benches/{id}/devices 检测、选择目标设备。新状态 bench_running、flash_complete、bench_failed；93ad11b5e832 迁移新增 controller_result。Hub 通过 SSH 轮询回传状态并核对 round_id 和 config_sha256；服务重启重新连接，不重复烧录。

适配器当前仅支持 RK3572（351A）。固件 SHA-256、大小、单设备、序列号、RCI 芯片信息、SFI 固件标签全部核验后才能 UF。原生 /usr/bin/upgrade_tool 缺失 RCI/SFI；保留原工具，使用已注册 SDK 完整静态 x86_64 工具配合 Debian qemu-user-static 运行。工具退出 0 且明确确认烧录成功才报告 flash_complete，boot_verified=false。

Controller 重启后的未结束任务标记 interrupted，不自动重新烧录。当前不实现取消烧录、设备启动版本验证、Test Case 执行或多设备并行。已有 9 个 test-bins 的路径和摘要在 bench/test-bins.inventory.json，适配器仍待逐项梳理。
