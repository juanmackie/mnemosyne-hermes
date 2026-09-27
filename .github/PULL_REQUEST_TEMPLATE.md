# Pull Request

## Description

**What does this PR do?**

A clear and concise description of the change, and why it is needed.

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (existing behaviour changes)
- [ ] Documentation update
- [ ] Refactor or cleanup
- [ ] Test or CI improvement
- [ ] Dependency or version bump

## Related Issues

**Closes:** #[issue number]
**Related:** #[issue number]

## Changes Made

**Summary:**

- Change 1: ...
- Change 2: ...

**Files changed:**

- `src/mnemosyne_lite/<file>.py`: [what changed]
- `tests/<file>.py`: [tests added]
- `<doc>.md`: [docs updated]

## Testing

**How has this been verified?**

- [ ] `./test-all.sh` passes (provider contract gates + pytest)
- [ ] `bash scripts/checks.sh` passes (notes, version drift)
- [ ] `pre-commit run --all-files` is clean (ruff, mypy, shellcheck)
- [ ] Manual verification performed

**Manual verification steps:**

1. Command run: ...
2. Expected result: ...
3. Actual result: ...

**Platforms tested:**

- [ ] Linux
- [ ] macOS
- [ ] Windows

## Provider changes

Only fill this in when `integrations/hermes-provider/` changed.

- [ ] The vendored snapshot is byte-identical to upstream (drift gate passes)
- [ ] If it is not, `PATCHES.md` documents the patch and `VENDORED_FROM.json`
      hashes are updated in this same commit
- [ ] `hermes mnemosyne doctor --no-fix` exits 0 after the change
- [ ] The gateway was restarted before verifying in a live session

## Documentation

- [ ] Code comments added where a constraint is not obvious
- [ ] User-facing docs updated (README, QUICK_START, TROUBLESHOOTING, provider
      README)
- [ ] `CHANGELOG.md` entry added for a user-facing change

## Checklist

- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md)
- [ ] The change is scoped to one problem
- [ ] Tests cover the new behaviour, or the PR body says why they cannot
- [ ] No secrets, keys, tokens or private memory data are included
- [ ] No generated or vendored file was edited by hand without a documented reason
- [ ] Commit messages describe the work, not the tool that produced it

## Additional Context

Design decisions, alternatives considered, known limitations, or follow-ups.
