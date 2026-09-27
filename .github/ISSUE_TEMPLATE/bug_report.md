---
name: Bug Report
about: Report a bug or unexpected behavior
title: '[BUG] '
labels: bug
assignees: ''
---

## Description

A clear and concise description of the bug.

## Environment

- **Component**: [Hermes provider / lite CLI / lite MCP server]
- **Version**: output of `hermes mnemosyne version` or `mnemosyne-lite --version`
- **OS**: [e.g. macOS 14.0, Ubuntu 22.04, Windows 11]
- **Python Version**: [e.g. 3.11.9]
- **Hermes Version**: [e.g. 0.19.0, output of `hermes --version`]
- **Installed with**: [`./install.sh` / `pip install -e .` / other]

## Steps to Reproduce

1. Run command `...`
2. Expected behavior: ...
3. Actual behavior: ...

## Expected Behavior

What you expected to happen.

## Actual Behavior

What actually happened. Include error messages, stop the traceback at the line
that names the problem, and redact any paths containing your username.

```text
[Paste error messages or output here]
```

## Diagnostics

```bash
# Provider
hermes mnemosyne doctor --no-fix
hermes memory status
hermes --version
ls -l "$HERMES_HOME/plugins/mnemosyne"

# Lite surface
mnemosyne-lite diagnostics
echo "$MNEMOSYNE_DB_PATH"
ls -l ~/.mnemosyne-lite/
```

`mnemosyne-lite diagnostics` prints the resolved database path and counts even
when the store is missing, which is usually enough to identify the problem.
Both surfaces print single-line errors to stderr; there is no log level to raise
and no API key involved in memory storage or search.

## MCP Integration (if applicable)

- **Client**: [Hermes / Claude Code / Cursor / other, with version]
- **Server entry**: the `command`, `args` and `env` you configured

## Workaround

If you found a workaround, describe it here so others can use it.

## Possible Solution

If you have an idea about the cause or the fix, share it.

---

**Before submitting:**
- [ ] I have checked [TROUBLESHOOTING.md](../../TROUBLESHOOTING.md)
- [ ] I have searched the existing issues
- [ ] I have included the diagnostics output for the surface that failed
- [ ] I have removed paths with usernames and any private memory content
