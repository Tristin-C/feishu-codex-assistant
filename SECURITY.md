# Security

This project grants a Feishu bot the ability to create and continue Codex
threads on the host machine. Treat a paired chat as a high-trust control
channel.

## Recommended defaults

- Keep `allowed_open_ids` restricted to accounts you control.
- Use a private chat with the bot unless every group member is trusted.
- Keep `full_access=false`; `workspace-write` is the default Windows setup.
- Keep the Windows session locked when it is not in use.
- Rotate the Feishu app secret if it is exposed.
- Review `%LOCALAPPDATA%\FeishuCodexBridge\config.toml` permissions after
  installation.

## Reporting

If you find a security issue, avoid posting credentials, conversation data, or
other sensitive details in a public issue. Contact the repository owner
privately instead.

