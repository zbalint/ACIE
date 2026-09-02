# ACIE
Agent Code Intelligence Engine

## Incremental indexing hooks (optional)

ACIE's filesystem watcher (tier 1 of `ARCHITECTURE.md`'s incremental-indexing precedence) already keeps a repo's index current with zero setup — it's the daemon-managed backbone and works the moment `acie serve-mcp` first indexes your repo. The hooks below are optional accelerants (tiers 2 and 3): they shave the watcher's debounce window off the round-trip for a git commit or an agent-driven edit. Per `ARCHITECTURE.md`'s "Agent Hook Integration", v0 ships these as copy-paste snippets only — there's no `acie install-hooks` installer, and none of this ever overwrites an existing hook you already have (append to it, don't replace it).

### Claude Code (`PostToolUse` hook)

Add to your project's `.claude/settings.json` (or `~/.claude/settings.json` for every project):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "acie notify-hook --agent claude-code"
          }
        ]
      }
    ]
  }
}
```

### Codex CLI (`PostToolUse` hook)

Codex's hook payload shape matches Claude Code's (`tool_input`, `tool_name`, `hook_event_name`) — see [developers.openai.com/codex/hooks](https://developers.openai.com/codex/hooks) for the current config file location and exact registration syntax, since that surface is newer and may still be moving. Wire the same command, `acie notify-hook --agent codex`, to fire on `PostToolUse` for `apply_patch`/`Bash` tool calls.

### Git hooks

The same one-line script, installed (and made executable) at each of `.git/hooks/post-commit`, `.git/hooks/post-merge`, `.git/hooks/post-checkout`, and `.git/hooks/post-rewrite`:

```sh
#!/bin/sh
acie notify-hook --agent git
```

`acie notify-hook` never trusts a git hook's own old/new-SHA arguments (they're inconsistent across these four hook types) — it tracks the last SHA it indexed itself and diffs that against the repo's current `HEAD`, so the same script works unmodified for all four.

### If ACIE isn't installed or the daemon isn't running

`acie notify-hook` always exits `0` and never blocks — a missing `acie` binary on `PATH` will make the hook script itself fail to execute (harmless, git/your agent host just logs it), and an unreachable daemon is a silent no-op within a ~200ms budget. Either way, your commit or agent turn is never delayed or broken by this.
