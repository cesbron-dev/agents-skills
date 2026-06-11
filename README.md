# agents-skills

A collection of reusable skills for AI coding agents (Claude Code, GitHub Copilot, Antigravity, …).

## Skills

| Skill | Description |
|---|---|
| [`mkdocs-docs`](skills/mkdocs-docs/) | Token-efficient search over a MkDocs documentation site via local git cache |

## Structure

```
skills/
└── <skill-name>/
    ├── SKILL.md              # Agent instructions (Claude Code slash-command format)
    ├── bin/                  # Helper scripts called by the skill
    └── config.example.json  # Example project configuration
```

## Installation (Claude Code)

Copy or symlink a skill directory into `~/.claude/skills/`:

```bash
ln -s "$(pwd)/skills/mkdocs-docs" ~/.claude/skills/mkdocs-docs
```

Then invoke it in Claude Code with `/mkdocs-docs`.
