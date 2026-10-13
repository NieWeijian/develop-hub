# 3588 QA 测试插件契约（schema version 1）

## 目的

测试插件让 Coding Agent 随 SDK 功能变更一起交付新的 3588 Test Case，而不覆盖 QA 节点已有程序。175 Server 是登记、审批、安装和命令绑定的权威来源。

## SDK 目录

每个插件位于 Coding Run worktree 内：

```text
tools/qa-plugins/<plugin-id>/
├── manifest.json
├── run-case
└── ...插件私有资源
```

插件不得包含符号链接，最多 1000 个文件、总计 200 MiB。入口必须位于插件目录内并具有可执行权限；应自带运行所需脚本/二进制/资源，不得依赖 Coding worktree 在 3588 上存在。

## manifest.json

```json
{
  "schema_version": 1,
  "plugin_id": "exposure-readback",
  "version": "1.0.0",
  "name": "曝光信息回读",
  "summary": "验证新固件能够在帧头返回曝光时间和增益。",
  "cases": [
    {
      "code": "QA-EXPOSURE-READBACK",
      "name": "曝光信息回读",
      "description": "采集一帧并确认曝光字段存在且范围合法。",
      "entrypoint": "run-case",
      "args": ["--frames", "1"],
      "expected_exit_code": 0,
      "timeout_seconds": 60,
      "risk_level": "read_only",
      "sudo": false
    }
  ]
}
```

- `plugin_id` 是稳定的小写标识；`version` 标识不可变发布版本。
- `code` 可以跨插件版本保持稳定，便于比较同一测试的演进；测试台用 `plugin_id@version` 区分。
- `entrypoint` 是插件目录内相对路径；`args` 是由 Server 固定绑定的参数，用户创建 QA Run 时不能插入任意命令。
- `risk_level` 只能是 `read_only`、`state_change` 或 `firmware_write`。`firmware_write` 仍需在每次 QA Run 创建时显式确认 NAND 写入。
- `sudo: true` 使用 3588 已配置的非交互式 `sudo -n`，不保存或传递密码。

## 运行接口

入口使用命令行参数，成功时退出 `expected_exit_code`（通常为 0），失败时返回其他退出码。诊断和证据写到标准输出/标准错误；Develop Hub 会保存输出尾部、退出码、开始/完成时间以及精确插件版本。不要仅输出“PASS”却以成功码掩盖内部失败。

如需产生大文件证据，写入当前工作目录下的 `evidence/`；每次 QA Run 的当前目录是 3588 上独立的 `jobs/<qa-run-id>`。

## 登记和安装

1. Coding Agent 实现/更新 SDK `tools` 测试程序并添加或提升插件版本。
2. Coding Run 最终人工确认时，175 扫描并严格校验插件，打包和计算 SHA-256，状态变为“待更新”。
3. 操作员在测试台查看来源、版本、说明、用例数和内容指纹，点击“安装到 3588”。
4. Server 通过 SSH 安装到 `plugins/<plugin-id>/<version>/<digest-prefix>`，验证入口后才注册 Test Case。
5. 新旧版本并存。相同 `plugin_id + version` 若内容不同，登记失败并要求提升版本；相同内容重复登记是幂等操作。
