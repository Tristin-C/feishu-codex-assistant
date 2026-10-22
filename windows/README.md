# Windows 一键版

双击 `Install-Feishu-Codex.cmd`。安装程序会：

1. 安装 Python 3.12、Node.js LTS 和飞书 CLI。
2. 安装官方 `openai-codex` Python SDK 及固定版本的 Codex runtime。
3. 打开浏览器创建并发布飞书自建应用。
4. 检查或发起 Codex 登录。
5. 等待用户私聊机器人发送一句话，自动取得 `open_id` 和 `chat_id`。
6. 隐藏启动后台 Bridge，并注册 Windows 登录后自动启动。

安装完成后不需要使用终端，也不需要打开 Codex CLI。飞书指令：

```text
/start client 新建一个 Codex 客户端会话
/threads
/threads 搜索词
/resume <线程ID> 继续执行
/history
/history H-编号
```

回复任务卡片或历史详情卡片会继续原 thread。默认使用
`workspace-write` 沙箱和自动审批审查，不开启完全访问。

配置文件、任务记录和日志位于：

```text
%LOCALAPPDATA%\FeishuCodexBridge
```

飞书应用创建、扫码授权和 Codex 登录仍需要用户亲自确认，这是飞书与
OpenAI 账号的安全边界，不能由安装包代为点击。
