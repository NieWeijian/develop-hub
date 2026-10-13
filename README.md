# Develop Hub

Develop Hub 是部署在 175 上的软件研发控制平面。平台维护流程和审计状态：175 Codex Agent 在隔离 worktree 中完成调研、设计、编码与修复，3588 QA Agent 在 175 的严格控制下执行测试并整理报告。

## 已实现的流程

```text
创建 Coding Run
  → 创建隔离 Git worktree，或选择已有 SDK worktree/分支
  → codex exec --json 生成方案
  → 人工批准方案
  → 使用明确 thread_id resume 原开发线程
  → Coding Agent 声明受影响的固件分区
  → 175 Server 在已配置 SDK 根目录固定执行 ./build.sh
  → 根据退出码、镜像更新时间和 SHA-256 登记固件产物
  → 执行仓库配置的固定验证命令
  → 新建只读 Codex 会话独立审查
  → 验证/审查失败时恢复开发线程修复（默认最多 2 次）
  → 人工最终确认
  → 完成并保留全量审计记录
```

平台不会使用 `resume --last`，不会根据 Agent 自述判定测试成功，也不会自动合并、发布或删除 worktree。

Coding Run 完成后可以从“3588 测试台”创建 QA Run：

```text
选择已完成 Coding Run 和 Test Case
  → 175 生成结构化 Coding → QA 交付说明
  → 通过专用 SSH key 创建 3588 隔离任务目录
  → 用户选择 Server 校验过的固件分区并显式批准烧录
  → 175 复核 SHA-256、传输至 3588 任务目录并再次校验
  → 调用 uvc_host_ota 执行 DFU/OTA，失败时不开始 Test Case
  → 由 175 逐条下发固定 Test Case 命令
  → 命令退出码和保存的输出决定测试结果
  → 3588 Codex Agent 根据既有证据生成结构化 QA 报告
  → 人工填写意见并批准或判定未通过
```

OTA 默认关闭，且不接受用户任意填写的远端镜像路径。平台不会因为 QA Agent 的文字结论覆盖固定命令的失败结果。

Coding Run 到达完成门禁、完成、失败或暂停后，运行详情提供“继续询问原开发 Agent”。Server 使用数据库中绑定的精确 `thread_id` 恢复该 Coding Agent，并以只读 sandbox 附上原任务信息；从 QA 测试台提问时还会自动附带本次 QA 报告、各 Test Case 退出码和输出。每轮问题、回答、依据及建议都会作为独立 `discuss` 阶段保存，不会改变已完成 Run 的状态，也不会在咨询中修改代码。

确认需要修复时，可从 QA 页面点击“转为后续 Coding Run”。表单会带入父 Coding Run、来源 QA、测试证据和最近一次讨论，默认复用原 worktree 并从原开发分支创建新的任务分支。这个后续任务是新的审计对象，必须重新经过方案审批、编码、固定验证、独立审查、Server `./build.sh` 和完成门禁。

QA Run 经人工批准通过后，只有 Coding Run 页面会出现“主分支合并”门禁。操作员二次确认后，Server 只暂存并提交 Agent 在结构化交付中声明的文件；若存在未声明的 tracked/staged 修改或冲突则拒绝执行，其他未跟踪文件不会进入提交。Agent 必须给出英文 `<primary file area>: <concise update>` subject 和英文 bullet body，Server 使用 `git commit --signoff`，仓库 hook 负责补充 Change-Id。随后 Server 仅允许 `--ff-only` 快进到仓库默认分支，因此不会额外生成 `Merge branch ...` commit；若主分支已分叉则拒绝并要求先处理分支同步。门禁保存源 commit、目标原 commit、最终 commit 和 Git 输出，只完成 175 本地合并，不会自动 push、发布或删除分支/worktree。成功后需求状态从 `OPEN` 变为 `CLOSED`。

需要新增或修改 3588 测试程序时，Coding Agent 在 SDK worktree 的 `tools/qa-plugins/<plugin-id>/` 交付带 `manifest.json` 的版本化插件。最终确认 Coding Run 时，175 会校验入口、风险级别、文件边界和内容指纹，并登记为“待更新”；不会自动发送或执行。操作员在测试台点击“安装到 3588”后，Server 才会把不可变产物安装到 `<qa-root>/plugins/<plugin-id>/<version>/<digest>` 并注册其中的 Test Case。

旧版本不会被覆盖，可以与新版本并存；QA Run 会快照精确的插件版本和命令。相同 `plugin_id + version` 只能对应同一份内容，修改后必须提升版本号。完整契约见 [`server/qa-assets/QA_PLUGIN_SPEC.md`](server/qa-assets/QA_PLUGIN_SPEC.md)。

## 目录

- `server/`：FastAPI、SQLAlchemy、Alembic、Coding 状态机与 Codex Runner。
- `web/`：175 Web 控制台。
- `bench/`：3588 Controller、用例运行器、用例定义与固定工具快照。
- `deploy/systemd/user/`：原生 systemd user services，不依赖 Docker。
- `var/`：本地数据库、worktree、JSONL、Prompt、stderr 和最终结构化结果；不进入 Git。
- `var/qa-plugins/`：175 保存的不可变测试插件归档；由数据库登记内容指纹和 3588 安装状态。

## 本地启动

本机需要 Python 3.10+、Node 22.13+、Git 和已登录的 Codex CLI。从仓库根目录复制配置：

```bash
cp .env.example .env
```

先将 `.env` 中的仓库根目录、数据库、产物、worktree、schema、Codex/npm 路径及测试台连接配置调整为当前环境；示例中的 `/home/nwj/develop-hub` 等路径来自 175 部署。然后安装依赖并启动前端：

```bash
set -a
. ./.env
set +a
python3 -m venv .venv
.venv/bin/pip install -e './server[dev]'

cd web
npm ci
npm run dev -- --host 127.0.0.1
```

另一个终端从仓库根目录加载配置并启动 API：

```bash
set -a
. ./.env
set +a
PYTHONPATH=server/src .venv/bin/python -m alembic -c server/alembic.ini upgrade head
PYTHONPATH=server/src .venv/bin/python -m uvicorn develop_hub.main:app --host 127.0.0.1 --port 8175
```

访问 `http://127.0.0.1:3000`，API 文档位于 `http://127.0.0.1:8175/docs`。

## 175 原生部署

1. 将 `.env.example` 复制为 `.env`，核对 Codex/npm 路径、监听地址和跨域来源。
   若 175 直连 Codex 上游不稳定，可设置 `DEVELOP_HUB_CODEX_PROXY`；Runner 只把该代理注入 Codex 子进程，Web/API 内网请求不经过它。`DEVELOP_HUB_CODEX_NO_PROXY` 应列出 175 的内网地址。
2. 初期可使用 SQLite WAL；需要多用户或更高并发时，把 `DEVELOP_HUB_DATABASE_URL` 改成 175 本机 PostgreSQL。
3. 安装 `python3-venv` 后运行 `scripts/install-native.sh`。脚本会构建前端、执行数据库迁移，并安装两个 user-level systemd 服务。
4. 使用 `systemctl --user status develop-hub-api develop-hub-web` 查看状态。

若需要注销 SSH 后继续运行 user service，可执行一次 `loginctl enable-linger nwj`。

## 注册仓库

在前端填写：

- 175 上 Git 仓库的绝对路径；
- 默认源分支；
- SDK 构建根目录，该目录必须包含 `./build.sh`；
- SDK 构建根目录内的固件镜像目录；
- 固定验证命令，每行一条并按顺序执行。

构建命令不可配置：Server 统一使用 `./build.sh`。Agent 只负责根据改动声明需要交付的分区，例如应用改动通常为 `oem`，内核或 bootloader 改动可能需要 `boot`/`uboot`。平台不采信 Agent 文字中的“编译成功”。

“代码仓库管理”可修改已注册仓库的路径、默认分支、验证命令和启用状态。注册或编辑时，Server 会确认默认分支是仓库中真实存在、且能够解析到 commit 的本地或远端分支；创建 Coding Run 时会再次校验本次选择的源分支。校验失败时不会创建 Run、worktree 或 Codex thread。

创建 Coding Run 时支持三种开发工作区模式：

- 新建隔离 worktree：从选定源分支创建平台管理的任务分支和独立目录，适合并行任务。
- 现有 worktree 新建分支：选择已有目录和源分支，在该目录创建并切换到新的任务分支；创建前不能有 tracked/staged 修改或冲突，未跟踪文件会原样保留，且目录不能正被另一个活跃 Run 使用。若未跟踪文件会被目标分支同名文件覆盖，Git 会拒绝切换。
- 继续现有分支：选择已有 worktree，直接在它当前的分支开发，不自动 checkout。

复用已有 worktree 时，平台不会删除该目录；“新建分支”模式完成后目录会保留在任务分支上，由操作员决定后续合并或切换。

Run 展示编号由数据库持久序列分配，删除记录后不会回退或复用。已完成、失败或已取消且没有下游 QA Run/测试插件引用的 Run 经人工确认删除时，Server 会同时清理该 Run 的数据库记录、产物目录和受控 worktree；仅当自动工作分支没有偏离源分支时才会一并删除分支。存在下游 QA 审计记录时禁止删除。

未配置验证命令时流程可以继续，但会记录显式风险，并且仍需最终人工确认。

## 数据边界

- Git：源码、分支和 worktree。
- 数据库：Run、Stage、Approval、Agent Session、Attempt 和标准化 Event。
- `var/artifacts/<run>/<attempt>/`：原始 JSONL、Prompt、stderr、验证日志和最终结构化结果。
- Codex 本地会话存储：供明确 `thread_id` 的 resume 使用；数据库保存线程映射。

## 产品测试平台

产品测试与 Coding Run 独立。日常入口为“测试台 → 开始测试”，按本次目的选择产品和执行方式：

- **测试当前固件**：直接验证在线模组，跳过 SDK 编译和烧录。
- **编译烧录测试**：选择 Gerrit Patch Set，构建并交付镜像，烧录、确认启动身份后执行所选用例。
- **已有镜像烧录测试**：导入 175 上已经编译好的 RV1126B 镜像，校验并保存为候选，再创建验证清单并选择测试项目。
- **仅构建交付**：生成镜像并交付到测试台，交付后结束，测试结果为未测试。

“发布验证”固定候选固件和验证清单，支持分批执行、关联证据及冻结报告。候选可以复用已有版本、从成功构建保存，也可以导入已有镜像；创建候选本身不会重新编译，补测继续使用已固定的镜像。需要重新编译指定提交时，使用“编译烧录测试”。

“特殊场景”提供 SN 断电持久化、日志压力、老化和 OTA 异常的人工门控流程；也可从发布验证清单发起，以保留与候选固件的关联。配置管理的用例库当前接入 **26 项自动用例和 4 项人工门控用例**；RGB/IMU 对齐与多模组同步仍待接入。接入数量表示平台执行能力，各固件的通过结论以具体测试记录和发布验证证据为准。

RMSL321 V2.0 使用 RV1126B SDK，通过正常 UVC 模式识别 CPU 序列号与 USB 端口，DFU 写入另一槽的 oem/system/boot，并在执行用例前核验固件身份。RK3572 使用独立的 update.img 适配器。每轮保存配置、镜像与工具摘要、原始 XU 收发、逐项断言和恢复结果，可下载 JSON 报告；构建、烧录和用例执行分别记录结果。

操作流程、状态与证据位置见 [测试台操作规则](docs/test-workbench.md)，人工门控及发布验证关联见 [特殊场景设计](docs/design/special-scenarios-20261008.md)，Controller 分工见 [QA v2 架构](docs/design/qa-architecture-v2.md)。原架构联调 Demo 创建入口已停用。

## 开发验证

从仓库根目录执行服务端和测试台离线测试。以下测试使用模拟数据，不会对模组执行烧录或测试动作：

```bash
PYTHONPATH=server/src .venv/bin/python -m pytest server/tests bench/test_qa_controller_v2.py bench/test_special_runtime.py bench/test_firmware_identity.py
```

前端检查与构建：

```bash
cd web
npm run lint
npx tsc --noEmit
npm run build
```
