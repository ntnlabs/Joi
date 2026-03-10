# Claude Code Instructions

Project-specific guidance for Claude Code when working on Joi.

## Project Context

Joi is a security-focused offline AI assistant running on isolated VMs. The codebase prioritizes:
- Security (fail-closed, defense-in-depth)
- Simplicity (avoid over-engineering)
- Reliability (proper error handling, thread safety)

## Code Style Preferences

- **Keep it simple**: Don't create abstractions for one-time operations
- **Minimal modules**: Only extract when there's clear benefit (the user prefers 2-5 modules, not many small files)
- **Structured logging**: Always use `extra={}` for key-value data
  ```python
  logger.info("Event description", extra={"key": value, "action": "event_name"})
  ```
- **No emojis** in code or commit messages unless explicitly requested

## Mandatory Steps

- **New environment variables**: Always update the default file with the commented-out variable and default value
  - Joi: `execution/joi/systemd/joi-api.default`
  - Mesh: `execution/mesh/systemd/mesh-signal-worker.default`
- **New features/functions**: Always update the install scripts in `sysprep/` (stage1-4)

## Things to Avoid

- IPv6 - not used in this project
- Over-engineering or premature abstraction
- Creating documentation files unless explicitly requested
- Adding features beyond what was asked

## Commit Style

- Imperative mood: "Add feature" not "Added feature"
- Short first line, details in body if needed
- Always include co-author tag:
  ```
  Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
  ```

## Architecture Notes

- **Joi VM**: Isolated, no WAN access, runs the AI
- **Mesh VM**: Internet-facing, Signal proxy, stateless
- **Communication**: Nebula VPN + HMAC authentication
- **Config flow**: Joi → Mesh (one-way push, mesh is stateless)

## Testing

No test framework yet. Verify changes with:
```bash
python3 -m py_compile <file.py>
```

User tests manually via Signal after deployment.
