# 从源码重建、快速同步与稳定发布

## 支持范围

开发环境：Linux AMD64、Docker Engine、Git、宿主 Python 3.10+；启用网页代理时需要用户 systemd 会话。
没有 systemd 的 CI 可使用 `--no-gateways`，通过私有 Unix socket 完成启动检查。
构建需要访问 Docker Registry、固定 Debian 快照、nodejs.org、PyPI 和 npm；不需要服务器已有 Node、Python 虚拟环境、npm 依赖、SDK、SSH 密钥或业务数据库。
首次构建会下载依赖；后续业务代码更新复用依赖镜像缓存。CPU/内存限制不等于独立主机或磁盘配额。

## 重建开发环境

```sh
git clone --branch codex/isolated-development https://github.com/NieWeijian/develop-hub.git "$HOME/develop-hub-source"
cd "$HOME/develop-hub-source"
python3 scripts/bootstrap-dev.py bootstrap --root "$HOME/develop-hub-runtime" --public-host 127.0.0.1
```

入口默认为 3025 / 8195；跨机器使用时将 `--public-host` 设置为浏览器可访问的主机名或 IP。
已占用端口不能复用，可指定 `--web-port 3035 --api-port 8205`。
开发管理命令拒绝 3000/3015/8175/8185 和受保护目录；应用容器保持无网络、无硬件、无密钥和无稳定目录挂载。
第一次使用全新数据库，只允许模拟设备。数据库/产物保存在部署根目录的 `var`，源码和依赖在运行时只读。

## 开发更新同步到 3025

先提交需要部署的代码，或从 GitHub 快进更新到待验收的提交，再执行：

```sh
git pull --ff-only origin codex/isolated-development
python3 deploy/isolated-development/hub.py deploy --root /home/nwj/develop-hub-dev --ref HEAD --public-host 10.10.10.175
```

无需更新代码时不要机械执行 `git pull`；本地新提交也可以直接部署。
默认 HEAD 存在未提交修改时拒绝运行；显式 `--ref <SHA>` 始终只部署那个提交。
流程为：导出提交 → 复用/重建固定依赖 → 在独立目录构建 → 全量后端测试 → 在数据副本启动候选 → 检查状态 → 备份开发数据库 → 切换开发容器。
构建/测试失败不停止现有部署；候选激活失败重新启动保留的旧容器。
`active.json` 和 API `/api/health` 的 `release` 字段记录实际运行的提交。
运行中的任务或等待人工处理的任务会阻止切换，数据库结构变化会阻止自动升级/回退。

```sh
python3 deploy/isolated-development/hub.py status --root /home/nwj/develop-hub-dev
python3 deploy/isolated-development/hub.py rollback --root /home/nwj/develop-hub-dev
```

## 将相同代码发布到 3015

开发验收通过后固定同一个完整提交 SHA。开发环境和实际平台使用不同的数据库、设备权限和前端 API 地址，因此不直接复制开发数据库或完整开发目录。
生产候选也在无网络、仅模拟设备的容器内验证；只有未来明确批准的实际发布才使用实际平台的私有配置。

```sh
python3 deploy/isolated-development/hub.py prepare --root /home/nwj/develop-hub-production-candidates --ref <SHA> --profile production --production-api-url http://10.10.10.175:8185
python3 deploy/isolated-development/stable_release.py package --root /home/nwj/develop-hub-production-candidates --release <候选ID>
python3 deploy/isolated-development/stable_release.py plan --package /home/nwj/develop-hub-production-candidates/packages/<候选ID>
```

候选包含源码、针对生产 API 地址构建的网页、独立 Node/npm 运行依赖，以及带哈希的 CPython 3.10 离线 wheels。
原生运行支持 Linux AMD64、Python 3.10.x、glibc 2.35+；其他目标需要单独生成和验证相应锁文件。
`plan` 只读检查现有进程、任务和数据库结构，在稳定目录之外保存私有计划，不修改稳定平台。计划一小时后过期。

下面是**未来稳定发布操作**，需要用户明确授权，并安排维护窗口、停止新任务。不能在普通开发或 CI 中执行：

```sh
python3 deploy/isolated-development/stable_release.py apply --plan <计划文件> --approve-stable-update --maintenance-confirmed
```

发布器检查包校验值、进程身份、空闲任务和相同数据库结构；在新包内独立安装离线依赖；在线备份数据库，然后优雅停止旧网页/API并启动新版本。
不覆盖原稳定源码和依赖，不覆盖业务数据库；候选启动或健康检查失败时重新启动旧版。
目前 API 和执行器仍在同一进程，此流程有短暂维护停顿，不承诺不停机发布。数据库结构升级需要另行评审迁移和数据恢复方案。
包与私有控制目录要保留，原稳定目录也不能删除，否则旧版回退入口会丢失。

```sh
python3 deploy/isolated-development/stable_release.py rollback --record <部署记录> --approve-stable-rollback --maintenance-confirmed
```

默认仅回退程序，保留现有业务数据。确需恢复发布前数据库时，另外加 `--restore-database --accept-data-loss`；它会保留当前数据库副本，但会丢失备份后的数据库记录。
代码/数据库回退不能撤销已发生的烧录和设备状态变化，应由操作人另行核对设备，不能自动重发硬件步骤。
备份还需要另存其他主机以覆盖磁盘/主机故障，本仓库不自动上传业务数据或密钥。

## 版本清单和同步边界

- `toolchain.lock.json`：基础镜像 digest、Node 官方下载 SHA-256、固定 Debian 快照和工具版本。
- `requirements/development-linux-amd64.lock`：Python 3.12 开发镜像完整版本和 wheel 校验值。
- `requirements/production-cp310-linux-amd64.lock`：Python 3.10 原生部署离线版本和 wheel 校验值。
- `web/package-lock.json`：前端依赖版本和 integrity；使用 npm ci。
- `production-runtime.lock.json`：原生部署要求和恢复演练镜像 digest。
- Dockerfile、入口、代理、管理器、CI、迁移、源码、测试和配置模板均进入 Git。
- 安装依赖、编译输出、私有计划/部署记录、数据库、固件资产、日志和证据不进入源码 Git；发布包作为受控构建产物单独保存。

升级 Python 依赖时先修改 `requirements/development.in`，再运行 `python3 scripts/lock-dependencies.py` 生成两个带哈希锁文件，检查与 `server/pyproject.toml` 的声明一致并执行完整验证。
工具和 Node 升级需要更新锁清单、Dockerfile 中的基础镜像 digest 和校验值；前端升级同时更新 package.json 和 package-lock.json。
运行 `python3 deploy/isolated-development/audit.py` 检查追踪文件；CI 也执行该检查。
稳定 tag 与实际部署是独立记录：推送源码或 tag 不会自动切换 3015，按批准的版本执行发布。

参考：
- https://pip.pypa.io/en/stable/topics/secure-installs/
- https://docs.docker.com/reference/cli/docker/image/pull/
- https://nodejs.org/dist/v22.22.1/SHASUMS256.txt

启动阶段的健康检查失败会自动回切；运行后发现业务问题或进程崩溃，需要在维护窗口执行手动回退。回退不会终止其他进程占用的端口，也不会自动恢复未知状态的硬件任务。实际平台的 Git/SSH、SDK、测试台与适配器继续由部署维护，私有配置不进入 Git。


## 隔离恢复演练

```sh
python3 deploy/isolated-development/recovery_drill.py --package <生产候选包目录> --backup-directory <私有备份目录> --root <全新演练目录>
```

只读取备份压缩文件，并复制到全新演练目录；不挂载原稳定目录、密钥、设备或 Docker socket 到演练容器。
在 `network=none` 中使用容器自己的 3015/8185，端口不会暴露到宿主机，也不会连接实际平台。
恢复旧源码、网页构建、依赖、数据库和产物；重建虚拟环境解释器别名和 npm 命令链接；检查历史方案/报告，再执行候选启动、手动回退、数据库损坏恢复及启动失败回切。
数据库路径仅在副本中重定位，实际业务数据不变。完整演练结果和日志只保存在私有目录，不进入 GitHub。

发布器自动备份数据库；维护清单还应确认当前设备私有配置和文件产物已有可用备份。文件产物不因程序回退而删除，数据库恢复也不会自动回退固件、报告或设备物理状态。本流程尚未建立异机定时备份。
