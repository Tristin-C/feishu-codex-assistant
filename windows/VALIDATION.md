# Windows package validation

## Verified in the build environment

- Python sources compile under Python 3.9 and Python 3.12.
- All 32 Bridge tests pass.
- The official `openai-codex` 0.163.0 SDK created a Codex thread without a
  terminal UI.
- The Bridge ran a client-mode turn, resumed the same thread, and listed it in
  Codex history.
- The release ZIP contains source and installer files only, with no local
  app credentials, task records, logs, or cached test data.

## Requires acceptance on a real Windows PC

The build environment is macOS and has no Windows VM. These Windows-specific
steps still need one end-to-end run on the target machine:

- `winget` installation of Python 3.12 and Node.js LTS.
- Global installation of `@larksuite/cli@1.0.97`.
- Windows Scheduled Task creation and hidden `pythonw.exe` startup.
- Loopback TCP IPC between the bridge and local helper processes.
- Browser/QR creation of a new Feishu self-built app.
- First Feishu message pairing and outbound test message.

Windows SmartScreen may warn because the package is not code-signed. Download
only from the owner who supplied it, and verify the SHA-256 checksum if the
package is forwarded through chat.
