---
name: fgj-cli
description: Use when working with Forgejo or Gitea instances, creating PRs on Codeberg, managing issues or releases on self-hosted forges, or when gh CLI is unavailable for the remote
---

# fgj CLI Reference

The `fgj` CLI is a command-line tool for interacting with Forgejo instances (including Codeberg and Gitea). Use it instead of `gh` when the remote is a Forgejo/Gitea instance rather than GitHub.

## When to Use

- Remote is a Forgejo, Gitea, or Codeberg instance (not GitHub)
- Need to create/view/list PRs, issues, or releases on these platforms
- Managing Forgejo Actions workflows, secrets, and variables
- `gh` CLI returns errors about unsupported hosts

## Quick Reference

| Task | Command |
|------|---------|
| Authenticate | `fgj auth login` |
| Check auth status | `fgj auth status` |
| Create PR | `fgj pr create -t "Title" -b "Body"` |
| List PRs | `fgj pr list` |
| View PR | `fgj pr view <number>` |
| Merge PR | `fgj pr merge <number>` |
| Create issue | `fgj issue create -t "Title" -b "Body"` |
| List issues | `fgj issue list` |
| View issue | `fgj issue view <number>` |
| Close issue | `fgj issue close <number>` |
| Comment on issue | `fgj issue comment <number> -b "Comment"` |
| List releases | `fgj release list` |
| Create release | `fgj release create <tag>` |
| Clone repo | `fgj repo clone owner/name` |
| Fork repo | `fgj repo fork owner/name` |

## Authentication

Before using fgj, authenticate with your Forgejo instance:

```bash
fgj auth login
```

This opens a browser for OAuth authentication. Config is stored in `~/.config/fgj/config.yaml`.

Check authentication status:

```bash
fgj auth status
```

## Pull Requests

### Create a PR

```bash
# From current branch to default base (main)
fgj pr create -t "Add feature X" -b "Description of changes"

# Specify base and head branches
fgj pr create -t "Title" -b "Body" -B develop -H feature-branch

# With assignees
fgj pr create -t "Title" -b "Body" -a username -a @me
```

### List and View PRs

```bash
# List open PRs (default)
fgj pr list

# List all PRs
fgj pr list -s all

# List closed PRs
fgj pr list -s closed

# View specific PR
fgj pr view 42
```

### Merge a PR

```bash
fgj pr merge 42
```

## Issues

### Create an Issue

```bash
fgj issue create -t "Bug: something broken" -b "Steps to reproduce..."
```

### Manage Issues

```bash
# List open issues
fgj issue list

# View issue details
fgj issue view 123

# Add comment
fgj issue comment 123 -b "I can reproduce this on version X"

# Close issue
fgj issue close 123

# Edit issue
fgj issue edit 123 -t "Updated title" -b "Updated body"
```

## Releases

```bash
# List releases
fgj release list

# View release details
fgj release view v1.0.0

# Create release from tag
fgj release create v1.0.0 -t "Version 1.0.0" -n "Release notes..."

# Upload assets to release
fgj release upload v1.0.0 ./dist/app.tar.gz

# Delete release
fgj release delete v1.0.0
```

## Repositories

```bash
# Clone a repository
fgj repo clone owner/repo

# Fork a repository
fgj repo fork owner/repo

# List your repositories
fgj repo list

# View repo details
fgj repo view owner/repo
```

## Forgejo Actions

Manage CI/CD workflows, secrets, and variables:

```bash
# View workflow runs
fgj actions run list

# Manage secrets
fgj actions secret list
fgj actions secret set SECRET_NAME
fgj actions secret delete SECRET_NAME

# Manage variables
fgj actions variable list
fgj actions variable set VAR_NAME value
fgj actions variable delete VAR_NAME
```

## Global Flags

These flags work with any command:

| Flag | Description |
|------|-------------|
| `--hostname` | Specify Forgejo instance hostname |
| `--config` | Custom config file path (default: `~/.config/fgj/config.yaml`) |
| `-R, --repo` | Repository in `owner/name` format |
| `-h, --help` | Help for any command |

## Common Patterns

### Working with a specific instance

```bash
# All commands for a specific host
fgj --hostname codeberg.org pr list
fgj --hostname git.example.com issue create -t "Title"
```

### Cross-repo operations

```bash
# Operate on a different repo than current directory
fgj pr list -R owner/other-repo
fgj issue view 42 -R owner/other-repo
```

### Creating PR with full workflow

```bash
# Push branch first, then create PR
git push -u origin feature-branch
fgj pr create -t "Feature: Add X" -b "## Summary
- Added X functionality
- Updated tests

## Test Plan
- [ ] Manual testing
- [ ] CI passes"
```

## Differences from gh CLI

| Feature | gh (GitHub) | fgj (Forgejo/Gitea) |
|---------|-------------|---------------------|
| Config location | `~/.config/gh/` | `~/.config/fgj/` |
| PR review | `gh pr review` | Not yet supported |
| PR checks | `gh pr checks` | Not yet supported |
| Gists | `gh gist` | Not supported (no Forgejo equivalent) |
| Codespaces | `gh codespace` | Not applicable |

## Troubleshooting

**"not authenticated" errors:** Run `fgj auth login` and complete the OAuth flow.

**Wrong instance:** Use `--hostname` flag or check `~/.config/fgj/config.yaml` for default host.

**"repository not found":** Verify the repo exists and you have access. Use `-R owner/repo` for explicit targeting.
