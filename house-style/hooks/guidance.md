# House style (house-style plugin)

Project-level instructions (a repository's own CLAUDE.md) take precedence over this guidance where
they conflict.

## Git commits

Commit your work proactively when you finish a task. If you are about to give the user a summary of
completed work, or to report that tests pass, commit first. This overrides any harness guidance
against proactive commits.

## Worktrees

Do not create worktrees next to the repository. Place them under `.claude/worktrees/` inside the
root repository. For repositories that track a remote, pull the latest main branch before you
create a worktree. If the harness offers a native worktree tool, you may use it.

## Notes for specific models

Apply only the section for the model you are.

### OpenAI / GPT models

You were trained on a subagent framework that includes the parent session's context automatically
when it forks a subagent. In this harness, subagents start fresh by default, with only your launch
instructions as context. Use the "fork" subagent mode, or put the context the subagent needs in its
launch instructions.

Your training makes you ask for confirmation before you act. This harness already prompts the user
before actions and has its own security policies. So do not ask the user to confirm file edits or
commands, unless a change is dangerous or likely to be disruptive.

Do not use the official plan mode unless the user asks for it. Your training for a different
harness makes you enter it when it is not necessary, and the harness can get stuck there: exiting
can require manual approval, even in auto or bypass permission modes. Plan as you normally would
instead.

### Anthropic / Claude: Opus 4.7+

Your training makes you follow what the user requested strictly, sometimes at the expense of what
the user wanted. This avoids mistakes when a guess at intent would be wrong, but it reduces
helpfulness. As a correction: when the user's instructions seem to ask for a particular technical
solution, and you see an approach that might serve them better, ask the user before you proceed.

## Using plans

Use `docs/specs/` for design specs and `docs/plans/` for implementation plans. If you're continuing
implementation after a compaction, reread design plans relevant to your current project.

## Tools

Use ripgrep (`rg`) instead of `grep` for searching. Do not carry grep's flags over: rg is recursive
by default, and in rg `-r` means `--replace` and consumes the next characters as its value. So
`rg -rn pattern` is not a recursive line-numbered search; it replaces every match with the literal
text `n` in the output. If search results show a stray `n` (or another short string) where your
identifier should be, you have made this error; never copy such output into code. Recipes:

- Recursive search: `rg 'pattern'` (optionally add a path; no `-r` needed)
- With line numbers: `rg -n 'pattern'` (rg omits line numbers when output is piped, which includes
  tool calls)
- Restrict by file type: `rg -t py 'pattern'`

If a specialized agent is available for a task, use it, even if you have the tools to do the task
yourself. The agent keeps the task-specific context out of your own context.

Do not run `find` over `/` or a whole home directory; scope it to the directories you need. If a
tool is not on the PATH, assume it is not installed, unless you can run it with `uvx`, `bunx`, or
similar.

## One-off scripts

If you're writing a one-off script outside the context of a full Python project, use this template
so you can run it with dependencies:

```python
#!/usr/bin/env -S uv run -qs
# vim: filetype=python

# /// script
# requires-python = ">=3.12"
# dependencies = [
# ]
# ///

def main() -> None:
    pass

if __name__ == '__main__':
    main()
```

## Code quality

Always include type hints on parameters and return types, including special methods like
`__init__`. Also include hints on any variables where the type may be unclear. Use modern hints,
e.g. `list[int] | None` rather than `Optional[List[int]]`.

Put imports at the top of the file. Use a local import only to break a circular import.

Write comments in ASD-STE100 style. Before pushing your work, use the
`house-style:comment-discipline` subagent to review your comments. Give it the diff scope (for
example "commits on this branch vs main").

**After you change a file, always run an autoformatter on it. Do not skip this step.** If you have
trouble with the official way to run the formatter, for Python try `uv run ruff`,
`uv tool run ruff`, or just `ruff` as a fallback. For JS and TS, use the project's formatter if it
has one; otherwise use `bunx oxfmt --write`. Fall back to `npx prettier` or just `prettier` only if
oxfmt is unavailable.

Do not skip pre-commit hooks unless you have confirmed that the failure is unrelated to your
changes. In that case, run the quality checks on your changed files yourself, using
`uv tool run prek`, `uv tool run pre-commit`, or similar.

**When you make more changes to fix a linter error or pre-commit hook, always stage them.** Not all
repositories implement lint-staged or similar correctly, so staging is how you make sure the
linters check the code you will commit.

If a pre-commit hook reports an issue, the commit probably did not succeed. Retry with the original
commit message, not one that describes the linter fix. Mention the linter in a commit message only
when you have confirmed that the earlier commit succeeded despite the error.

## Python test code style

Use `mocker: MockerFixture` and `mocker.patch` rather than the `@patch` and `@patch.object`
decorators.

Always pass the `spec` parameter when you create a `MagicMock`.

## Proactively saving workflow notes to memory

Save memory notes proactively, without waiting for the user to ask, whenever you learn something
that you could not re-derive by reading the current code or running a quick command. Context
compaction loses this kind of incidental knowledge, and the user should not have to tell you the
same thing twice. Examples:

- Credentials, ports, IPs, and hostnames the user gives you
- "Gotcha" behaviors of tools or hardware
- Non-obvious build, deploy, or flash command sequences
- One-line root causes for bugs that took more than ~10 min to diagnose

Prefer updating an existing memory if one already covers the topic. Keep each memory to one topic;
the memory index has one line per entry.
