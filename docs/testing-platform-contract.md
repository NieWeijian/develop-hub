# 新测试平台扩展契约（首版）

## 业务对象

产品说明芯片、板型和能力；设备选择所属产品、已部署测试台和序列号；构建环境登记 SDK 位置和配方。方案选择这些资源并绑定不可变用例版本。发布或保存运行时冻结全部配置；之后修改产品、设备、草稿不会修改已有运行。

源码、固件和用例包是独立输入资产。一次运行先冻结配置，再解析来源、保存实际输入，最后记录结果。只有声明的必需用例全部通过，运行才通过；构建完成为「未测试」，模拟结果单独标记。补充观察项仍展示失败，不改变必需项验收结论。

## 自动化用例包

ZIP 根目录包含 `manifest.json` 及入口文件。完整可用样例见 `samples/case-packages/rk-version/`。包协议为 `rk-case-package/1`，运行配置为 `python3-case`。导入只静态检查，确认来源后发布。相同用例 ID + 版本不可替换内容；更新代码时提升版本号。

声明字段包含名称、说明、能力要求、参数 schema、参数标签/单位、入口、超时、设备影响、必需断言和恢复要求。参数支持 object、array、string、number、integer、boolean、enum、长度和数值范围；object 必须声明 `additionalProperties: false`。不支持远端引用或任意 JSON Schema 关键字。平台按默认值 → 方案覆盖 → 本次运行覆盖解析，并记录来源。

执行节点运行：

```text
python3 package/run.py --input /step/input.json --output-dir /step/output
```

入口应读取 `input_schema: rk-case-input/1`，包含 `run_id`、`binding_id`、`case_revision_id`、`parameters`、`device` 和 `firmware`。不要从平台内部数据库或写死的目录读取配置。

输出 `output/result.json`：

```json
{
  "result_schema": "rk-case-result/1",
  "run_id": "从输入复制",
  "binding_id": "从输入复制",
  "case_revision_id": "从输入复制",
  "state": "completed",
  "verdict": "passed",
  "assertions": [
    {"id": "temperature-limit", "name": "温度阈值", "expected": 75, "actual": 42, "unit": "°C", "passed": true}
  ],
  "recovery": {"state": "not_required"},
  "attachments": [{"path": "measurement.txt"}]
}
```

附件路径相对 output。没有有效结果、遗漏必需断言、身份不匹配或非零退出不会通过。`passed` 对应所有断言通过且退出码零；正常断言失败使用 `failed`；执行错误使用 `error` 并非零退出。平台重新核对结论，不能仅返回成功文字。

影响设备状态的用例须声明恢复要求，在 finally 中执行并报告 `restored` 或 `failed`。恢复失败会停止后续用例并隔离设备。超时由节点终止进程组，控制端将未确认状态作为需检查，不能把进程结束当作恢复完成。

## 平台管理的测试台

普通用户只选择已部署测试台、设备名称和 CPU 序列号。适配器命令、SSH 身份、工具路径、校验值和能力由平台维护，不在设备表单配置。`GET /api/testing/test-stations` 返回可选测试台及其功能，不返回安装信息；创建设备提交 `station_id` 和 `serial`。服务端按产品芯片校验兼容性，冻结平台提供的执行配置；用户不能覆盖已管理设备的 node/config/capabilities。设置部署注册表后，真实设备必须通过测试台登记。

平台维护人员在 Linux 控制端运行 `scripts/provision-rmsl321.py --help` 查看安装参数。它使用已有 SSH 信任和可信 UVC/DFU 工具，安装到独立目录，校验内容，读取真实设备身份和版本，最后原子生成权限为 0600 的注册表。设置 `DEVELOP_HUB_TEST_STATIONS` 为注册表绝对路径后启动服务。安装器不会烧录；DFU 工具安装状态与实机烧录验收结果分开展示。无需数据库迁移。

注册表不随开源仓库分发。更换工具使用新的专用安装目录；已存在设备和历史运行保留其配置快照，维护人员需要安排后续设备升级。下面的协议属于平台内部扩展契约。

## 节点与设备适配器

节点配置 `kind` 为 `local`、`ssh` 或明确用于演示的 `simulation`。SSH 另含 `host`、`user`、`port`、专用绝对 `root`、`python`，以及可选的控制端 `key` 路径。首版按节点串行操作设备，防止同一控制工具竞争硬件。

设备 `config.adapter.inspect_argv` 是参数数组，支持 `{input}`、`{output}` 占位符。程序读取输入中的 device 配置，在 stdout 返回 JSON，至少包括与登记值一致的 `serial`；可返回 `firmware_version` 和设备发现信息。平台不会拼接用户填写的 Shell 命令。

`deploy_argv` 同样接收输入和输出目录。输入 firmware 包含已校验的文件清单、临时目录和 digest。适配器应生成 `deployment-result.json`，必须含 `deployment_schema: rk-deployment-result/1`、相同 run_id、目标 serial、firmware_digest、`boot_verified: true`；可返回启动后的 observation。完成写入本身不等于启动验证通过。

`bench/testing_rv1126b_driver.py` 实现 RMSL321/RV1126B inspect 和受约束的 deploy；详情见示例 README。其他 RK 板型注册独立适配器，不修改核心方案状态机。

节点执行器在执行前保存 STARTED，完成后保存 node-result。已有结果可读取，中断步骤不会被自动重复执行。手动恢复可用要求记录操作人及说明并再次读取设备身份；操作人仍需先确认执行节点无残留硬件命令。

## 构建与版本

构建环境 config 包含 sdk_path、input_rules、recipe。recipe 使用 argv 数组、相对 cwd、timeout_seconds、env 和相对 SDK 的 artifacts 匹配规则。原 SDK 永不作为构建工作目录，所有构建均在保存输入的副本上进行。

source.repository_overrides 为 `{path, url, ref}` 数组，用于在副本替换指定仓库。构建前保存解析提交及文件清单；其他仓库保留本地输入。构建失败保留日志，不继续烧录。

「固定输入复测」复用已保存的固件、用例版本和参数；「按方案重新运行」可能读取变化后的源码和远端 ref。设备当前固件每次重新读取身份，只能报告设备自报版本，不声称与某镜像二进制相同。

## API 与部署

### RMSL321 用例包

在控制端运行 `python3 scripts/build-rmsl321-case-package.py --output /path/rmsl321-suite.zip`，生成可导入的 25 项自动用例包及同名 `.manual.json` 人工用例定义。已有版本核对用例单独保留；已停用的 IMU 和四项开关测试不重新发布。用例包复用维护中的执行实现，不依赖旧数据库或调度器。通过用例库导入 ZIP 并确认可信来源，人工定义通过 `POST /api/testing/case-revisions` 发布。

平台维护人员使用 `provision-rmsl321.py --suite` 安装采集工具并登记能力。更新已登记设备须在设备空闲时刷新平台管理的 node/config/capabilities；历史运行和已发布方案的配置快照保持原样，新建或保存运行使用更新后的设备配置。工具路径和摘要继续由平台维护。

四项特殊场景为人工执行与证据记录版：SN 断电、日志压力、老化串口、异常 OTA。操作者使用测试工具完成实际操作并上传证据，平台不自动完成或自动分析这些操作。影响设备状态的人工用例必须声明 `recovery: {required: true, mode: operator_confirmation}`，最后包含 `restore-confirmation` 步骤、必填布尔值 `recovery_confirmed` 和附件。步骤可用 `required_confirmations` 指定必须为真的布尔记录字段；仅点击通过但未确认不会判为通过。失败后跳过后续操作并转到恢复确认，恢复失败、取消或超时未确认时设备需人工处理。

上线代表用例可配置、执行和生成报告，不代表所有项目已通过实机验收。长稳、SN/标定写入、重启及异常场景需按计划单独执行。BoardConfig 是固件来源记录，不再作为烧录门槛；芯片、镜像完整性、目标设备身份及启动版本仍需校验。

接口根为 `/api/testing`。主要集合：products、devices、build-environments、case-revisions、test-plans、test-runs。草稿和人工记录使用修订号防止覆盖；运行请求使用幂等键。`/docs` 提供请求模型。

全新数据库执行 `alembic upgrade testing@head`。testing 是独立迁移分支，不执行旧数据转换。旧定时和发布验证默认停用；定时/Gerrit 自定义触发尚未实现，不应调用旧接口替代。


### 镜像目录选择

已有镜像导入支持浏览平台主机目录并勾选文件，SDK 和镜像不会通过用户电脑上传。`GET /api/testing/firmware-directories` 返回目录入口、子目录、镜像名称和大小；以 `root_id` 和相对 `path` 逐层浏览。默认入口来自已启用的本地 SDK 环境，可用 `DEVELOP_HUB_FIRMWARE_BROWSE_ROOTS` 配置专用镜像目录（Linux 下用冒号分隔绝对路径）。仅列出 .img/.bin/.itb/.dtb，隐藏项和根目录外的软链接不展示；不读取文件内容，不修改 SDK。选择完成后继续使用既有 `/firmware-imports` 保存并校验独立副本，导入不会触发烧录。高级手工路径输入保留。
