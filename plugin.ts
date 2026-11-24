import { fileURLToPath } from "node:url";

/** Hermes plugin name, as listed in `plugins.enabled`. */
export const pluginName = "slack-own-thread-reply";

/** Directory to copy (without `__tests__/`) to `$HERMES_HOME/plugins/slack-own-thread-reply/`. */
export const pluginDirectory = fileURLToPath(new URL(".", import.meta.url));

/** Files that make up the deployed plugin. */
export const pluginFiles = [
  "plugin.yaml",
  "__init__.py",
  "pyproject.toml",
  "_pythonmonkey.py",
  "policy.cjs",
] as const;

/** Tool the plugin registers, and the toolset it belongs to. */
export const toolName = "slack_reply_own_thread";
export const toolsetName = "slack_own_threads";

/** Optional `.env` settings: hourly reply cap per thread (default 4), and a channel allowlist. */
export const maxPerHourEnv = "SLACK_OWN_THREAD_MAX_PER_HOUR";
export const channelsEnv = "SLACK_OWN_THREAD_CHANNELS";
