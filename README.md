# @-/com.leonsilicon.hermes.plugins.slack-own-thread-reply

Hermes plugin that lets Hermes reply in Slack threads it started itself, including from cron jobs. Its main use is following up with another agent, such as ChatGPT's Silicon Dot, on a handoff Hermes posted in #agents.

## Why

Hermes hands a task to Dot with a top-level post in #agents that @mentions Dot, then sets a cron job to check back. When Dot doesn't answer, the cron job has to nudge Dot in that thread, and stock Hermes can't do that:

- Cron agents never get the `messaging` toolset, so they fall back to `hermes send` in a terminal.
- That CLI process has no gateway runner. On Hermes Cloud the relay connector also fronts Slack, so the relay egress guard treats the send as relay-routed. It then requires a session for the exact thread, even though the CLI sends with the native bot token anyway.
- Hermes never holds a session in its own handoff thread. Dot's replies there don't @mention Hermes, and `allow_bots: mentions` drops unmentioned bot messages.

The result is `Refusing to send to relay target 'slack:C…:…': the parent chat is attested but this gateway has no record of the thread`. Hermes's `coding-task-routing` skill then stops and reports the blocker. A task Dot missed (LEON-336, 337 and 338 on 2026-11-10) stays stuck until Leon steps in.

## What it does

It registers one tool, `slack_reply_own_thread(channel, thread_ts, text)`, in the `slack_own_threads` toolset. The tool:

- runs only inside the gateway process, where the live native Slack adapter is (gateway turns and cron jobs both run there). Its `check_fn` hides it anywhere else;
- reads the thread's top message from Slack before every send. It refuses unless that message is the thread root and was posted by Hermes's own bot user, so Composio posts in Leon's name and other people's threads are out;
- sends through the stock `send_message` path, which keeps mirroring and sanitising. In-process the guard sees a native send and lets it through, the same as a gateway turn's `send_message`;
- caps replies at 4 per thread per hour (`SLACK_OWN_THREAD_MAX_PER_HOUR`), so a looping monitor can't keep waking Dot. A failed send doesn't count;
- optionally limits itself to `SLACK_OWN_THREAD_CHANNELS` (comma-separated channel IDs). With no list, any channel is allowed.

This doesn't let Hermes post anywhere it couldn't before. A gateway turn's `send_message` already reaches any Slack thread. The plugin only gives cron jobs a narrow version of that.

## Install

```sh
hermes plugins install leonsiliconAI/hermes-plugin-slack-own-thread-reply --enable
```

See [migration steps](https://github.com/leonsilicon/leonsilicon.ai/blob/main/DEVELOPMENT.md#hermes-plugin-mirrors) for an existing install. Restart the gateway. The gateway log then shows `slack-own-thread-reply: slack_reply_own_thread registered`. Plugin toolsets are on for every platform unless `known_plugin_toolsets` names them, so cron jobs with no `enabled_toolsets` of their own get the tool too.

Then tell Hermes when to use it. The `coding-task-routing` skill on Captain Claude says to use it for follow-ups in its own handoff threads, instead of `hermes send`.

## Test

The tests use Hermes's real relay egress guard and `send_message` tool, so they need a Hermes checkout and its environment:

```sh
git -C ~/.hermes/hermes-agent worktree add --detach /tmp/hermes-v0.23.0 v0.23.0
cd /tmp/hermes-v0.23.0 && UV_PROJECT_ENVIRONMENT=/tmp/hermes-venv uv sync --frozen --extra slack
VIRTUAL_ENV=/tmp/hermes-venv uv pip install pytest pytest-asyncio
cd - && HERMES_HOME=$(mktemp -d) PYTHONPATH=/tmp/hermes-v0.23.0 /tmp/hermes-venv/bin/python -m pytest -q -p no:cacheprovider __tests__
```

Two of the tests pin the upstream behaviour the plugin works around: the guard refuses the thread with no gateway runner and allows it with a live one.
