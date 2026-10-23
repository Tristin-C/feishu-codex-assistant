# Feishu Codex Assistant

通过飞书远程使用本机 Codex：新建会话、查看历史、继续原 thread，不需要在
运行过程中打开终端。

[下载 Windows 一键包](https://github.com/Tristin-C/feishu-codex-assistant/releases/latest)

> Windows 包使用官方 Codex Python SDK 和 app-server，不进行 ChatGPT 桌面 UI
> 自动化。首次创建飞书应用和登录 Codex 仍需用户在浏览器或飞书客户端确认。

## 功能

- `/start client <任务>`：在本机创建 Codex thread。
- `/threads`：列出本机 Codex 历史 thread。
- `/threads <搜索词>`：搜索 thread。
- `/resume <线程ID> <指令>`：恢复指定 thread 并继续执行。
- `/history`：查看通过飞书创建的任务。
- 回复任务卡片或历史详情卡片：继续对应 thread。
- 任务最终答复自动回传飞书。
- 确定性 H-id 路由，不按“最近任务”猜测目标。
- 长结果自动提取核心内容，并把完整正文收进折叠面板。

## Windows 一键安装

从 [Releases](https://github.com/Tristin-C/feishu-codex-assistant/releases/latest)
下载 `Feishu-Codex-Assistant-Windows.zip`，解压后双击：

```text
Install-Feishu-Codex.cmd
```

安装器会：

1. 安装 Python 3.12、Node.js LTS 和飞书 CLI。
2. 安装官方 `openai-codex` Python SDK 及固定版本的 Codex runtime。
3. 显示链接和二维码，协助创建并发布飞书自建应用。
4. 检查 Codex 登录状态；未登录时打开浏览器授权。
5. 等待用户私聊机器人发送一句话，自动识别 `open_id` 和 `chat_id`。
6. 隐藏启动后台服务，并注册 Windows 登录后自动启动。

安装完成后不需要终端。配置、记录和日志位于：

```text
%LOCALAPPDATA%\FeishuCodexBridge
```

包目前没有代码签名，Windows SmartScreen 可能显示未知发布者提示。

## 飞书命令

| 命令 | 作用 |
| --- | --- |
| `/start client 分析这个项目` | 新建 Codex 客户端 thread |
| `/threads` | 查看最近 20 个 Codex thread |
| `/threads 登录` | 按标题或预览搜索 thread |
| `/resume <线程ID> 继续修复` | 恢复指定 thread |
| `/history` | 查看最近 20 条飞书任务 |
| `/history H-...` | 查看指定任务详情 |
| `/start background 任务` | 使用 `codex exec` 后台执行 |
| `/start terminal 任务` | macOS 专用：在 Terminal 打开 Codex TUI |

直接回复任务卡片会继续原 thread。`/history H-...` 只查询，不执行；新版本
返回的历史详情消息会登记到原任务，因此也可以直接回复该详情卡片。

## 架构

```text
飞书消息
  │  im.message.receive_v1（lark-cli 长连接）
  ▼
Bridge
  ├─ H-id 任务记录 / 消息映射 / 去重
  ├─ client 模式 → openai-codex SDK → app-server → Codex thread
  ├─ background 模式 → codex exec
  └─ terminal 模式 → macOS Terminal（可选）
  ▼
飞书结果卡片
```

Bridge 不使用 LLM 判断消息语义。回复必须通过被引用消息 ID 或显式 H-id
找到任务，否则拒绝路由。

## 安全边界

- 默认 Windows 配置使用 `workspace-write` 沙箱和自动审批审查。
- `allowed_open_ids` 限制可以发送指令的飞书用户。
- 飞书应用凭据只保存在本机配置中，不提交到仓库。
- 一个飞书应用的长连接只能有一个消费者；不要同时为本应用运行其他事件桥。
- 若把机器人加入群聊，应确认群成员都可信，或继续使用私聊。
- 不要把 `full_access=true` 版本分发给不受信任的人。

## macOS 开发运行

要求：

- Python 3.10+（`client` 模式）
- `codex` CLI
- `lark-cli`

安装 client 模式依赖：

```bash
python3 -m pip install openai-codex
```

复制并填写配置：

```bash
cp config.example.toml config.toml
chmod 600 config.toml
```

前台运行和诊断：

```bash
./handoff serve
./handoff list
./handoff show H-...
tail -f state/logs/bridge.log
```

macOS 的 `setup.sh` 可安装 launchd 服务并写入独立 lark-cli profile。

## 本地验收

```bash
python3 -m py_compile handoff_config.py record_store.py feishu_client.py \
  codex_adapter.py sdk_adapter.py bridge.py handoff
python3 -m unittest discover -s tests -v
```

当前共 29 项测试，覆盖消息解析、任务隔离、历史查询、SDK client 模式、
去重、消息映射、thread 恢复和 terminal/background 兼容逻辑。

更详细结果见 [VALIDATION.md](VALIDATION.md) 和
[Windows 验收说明](windows/VALIDATION.md)。

## 构建 Windows ZIP

```bash
./windows/build.sh
```

默认输出 `dist/Feishu-Codex-Assistant-Windows.zip`；可传入自定义输出目录：

```bash
./windows/build.sh /tmp/release
```

## 已知边界

- Windows 安装器仍需在真实 Windows 电脑完成首次端到端验收。
- 飞书和 OpenAI 的账号授权必须由用户本人确认，安装器不能代替点击。
- `client` 模式与 ChatGPT 桌面端共享本机 Codex 数据，但不是 UI 自动化；
  同时从桌面端和飞书修改同一个活动 thread 时需自行避免并发操作。
- app-server WebSocket 仍可能随 Codex 版本演进；本包固定了已验证的 SDK
  版本，并把协议封装在 `sdk_adapter.py`。

## 官方文档

- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Codex SDK](https://developers.openai.com/codex/sdk)
- [ChatGPT Remote connections](https://learn.chatgpt.com/docs/remote-connections)

