## What this PR does

<!-- One-paragraph summary of the change and its motivation. -->

## Why

<!--
The "why" matters more than the "what". What problem does this solve?
What was the prior behavior and why was it wrong / insufficient?
-->

## How to verify

<!--
Commands a reviewer can run to convince themselves this works.
Include any setup, the test commands, and the expected output.
-->

```bash
# example
uv run --project engine pytest engine/tests/test_recon.py
cd packages/cli && pnpm build
```

## Checklist

- [ ] Tests added / updated where appropriate
- [ ] `uv run --project engine pytest -m "not slow and not mainnet"` passes
- [ ] `cd packages/cli && pnpm build` succeeds
- [ ] No new outbound network destinations (or if there are, `PRIVACY.md` is updated)
- [ ] No telemetry, analytics, or phone-home added
- [ ] Documentation updated (`README.md`, `docs/`, `CHANGELOG.md`) for user-visible changes
- [ ] `NOTICE` updated if new dependencies were added
- [ ] No `TODO` / `FIXME` markers left in the new code
- [ ] If touching `builder_fee.py`, re-ran `python scripts/seal_release.py` (or leaving seal empty per dev convention)

## Breaking changes

<!--
If this PR breaks existing user behavior (CLI flag changes, config format
changes, file path migrations, etc.), describe what users must do to
adapt. If none, write "None".
-->

## Related issues

<!-- "Fixes #123", "Refs #456", or "None" -->
