# Joi Config TUI

A terminal-based configuration tool for Joi. Think `raspi-config` — grouped switches and editable fields, not a dashboard.

## Concept

- Runs directly on the Joi VM, sudo only
- Pure Go + tcell (reuse failtop's widget foundation)
- Edits `mesh-policy.json` in place
- No live data / no monitoring — config only

## Layout idea

```
┌─ Joi Config ──────────────────────────────────────────────┐
│                                                            │
│  [Wind]                                                    │
│    Enabled          [ ON]                                  │
│    Shadow mode      [OFF]                                  │
│    Daily cap          3                                    │
│    Quiet hours     23:00 → 07:00                           │
│    Min silence       30 min                                │
│                                                            │
│  [Messaging]                                               │
│    Privacy mode     [OFF]                                  │
│    Kill switch      [OFF]                                  │
│                                                            │
│  [Memory]                                                  │
│    Context window     50                                   │
│    ...                                                     │
│                                                            │
│  [s] save  [q] quit  [r] reload from disk                 │
└────────────────────────────────────────────────────────────┘
```

## Notes

- Booleans → toggle with space/enter
- Numbers/strings → inline edit (vim-style `i` to enter edit mode)
- Groups collapsed/expanded with enter
- Writes atomically (temp file + rename) so a crash mid-save doesn't corrupt config
- Policy reloads on next scheduler tick — no restart needed
