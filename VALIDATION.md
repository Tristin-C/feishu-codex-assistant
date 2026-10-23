# Local acceptance record

Date: 2026-10-22

## Automated tests

```bash
python3 -m unittest discover -s tests -v
```

Result: 26 tests passed. Coverage includes:

- rich post title/body composition and task/session metadata
- dedupe and failed-send claim release
- atomic task records and Feishu message id lookup
- start mode parsing and missing-mode error
- terminal session discovery by cwd + start time
- queue for a live writer and resume for a closed writer
- two-session reply isolation
- rejection of unknown messages without guessing a target
- reading `task_complete.last_agent_message` from rollouts
- creation of a task mapping from an existing Codex session
- reuse of a session's original H-id when Codex omits `HANDOFF_TASK_ID`
- Feishu `/history` list and `/history H-...` detail commands
- rejection of malformed and unknown history queries without Codex routing
- replying directly to a history detail message routes to its original session
- headless `client` mode using the official Python SDK
- Codex thread listing and `/resume <thread-id> <task>` routing
- Markdown-normalized dedupe between manual handoff sends and rollout watchers

## Codex SDK client-mode test

Using the official `openai-codex` 0.163.0 Python SDK on Python 3.12:

- `Bridge` created a Codex thread without opening Terminal.
- The first turn returned `CLIENT_START_OK`.
- A reply resumed the same thread and returned `CLIENT_RESUME_OK`.
- `thread_list` returned the same thread id, title, and working directory.

The raw app-server protocol was also exercised directly: one thread was
started and returned the final message `APP_SERVER_BRIDGE_OK`.

## Real background Codex test

Started with:

```bash
./handoff start background --cwd /private/tmp \
  'Reply with exactly RETRY_A_BASE. Do not do anything else.'
```

Observed:

- a new Codex session id was parsed from that process's own `run.log`
- the rollout belonged to the same session id
- `last_output` became exactly `RETRY_A_BASE`

Reply routing was then tested against the closed background writer. The bridge
selected `codex exec resume`, and `last_output` became exactly
`RETRY_A_REPLY`.

## Concurrent real sessions

Two background tasks were started concurrently. Their session ids matched their
own run logs:

- A: `01a16335-1a3b-7e63-9077-d801fe0315b7`
- B: `01a16335-1cb2-7603-ad46-052da6eede9f`

After replying only to A:

- A changed to `RETRY_A_REPLY`
- B remained `RETRY_B_BASE`

This verifies that task/session ownership is not inferred from the latest
message.

## Real Terminal TUI test

Started a terminal TUI task in `/private/tmp`. The system:

1. opened macOS Terminal;
2. launched `codex` with the task as its initial prompt;
3. found the TUI's session id;
4. captured the automatic `task_complete` result;
5. accepted a reply while the TUI was still alive and queued it with `codex queue`;
6. captured the second final answer from the same rollout.

Observed final values:

- `HANDOFF_TERMINAL_LIVE`
- `HANDOFF_TERMINAL_REPLY`

## Not verified in this workspace

Live delivery through a newly created Feishu bot was not performed because this
workspace does not contain a new app's credentials and event subscription.
`lark-cli` is installed and its event contract was inspected; `setup.sh` writes
a separate bot profile and starts the launchd service. Real Feishu delivery
requires the operator to finish app creation/publishing and run `setup.sh`.

## Feishu terminal end-to-end test

After the Feishu app was configured, the bot successfully received
`/start terminal`, opened macOS Terminal, started Codex, and returned the final
answer to Feishu. The first run exposed that Codex tool subprocesses did not
reliably inherit `HANDOFF_TASK_ID`, which caused a manual send to create a
second H-id. The Bridge now falls back to the oldest record owned by the same
Codex session. This gives manual sends and the rollout watcher one shared H-id
and dedupe key while preserving terminal/background mode metadata.

