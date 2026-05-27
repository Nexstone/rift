---
name: Bug report
about: Something is broken or behaves unexpectedly
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- Describe the actual behavior. What did RIFT do? -->

## What you expected

<!-- What should have happened instead? -->

## Steps to reproduce

<!-- Exact commands, in order. The shorter the repro, the faster the fix. -->

```bash
# example
rift more recon --no-soak --auto 1 --min 1
```

## Output / logs

<!--
Paste relevant output. For algo / recon sessions, the log path is in
~/.rift/algo/logs/ or ~/.rift/recon/. Redact private keys + wallet
addresses if you'd rather not share them publicly.
-->

```
<paste output here>
```

## Environment

- **RIFT version / commit:** `<git rev-parse --short HEAD>`
- **OS + version:** `<macOS 14.5 / Ubuntu 24.04 / WSL2 / etc.>`
- **Python version:** `<python --version>`
- **Node version:** `<node --version>`
- **`rift doctor` output:**

```
<paste output of `rift doctor` here>
```

## Additional context

<!-- Anything else: screenshots, network conditions, recent config changes, etc. -->
