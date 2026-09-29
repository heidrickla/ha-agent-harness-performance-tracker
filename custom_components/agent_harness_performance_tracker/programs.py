"""The agent programs an entry can name, and where each keeps its harness.

Automatic programs have a file profile in tools/report_run.py, verified end to
end. The others get a suggested manual list taken from their documentation;
the person confirms it. Relative paths resolve against the project root on the
agent's machine. Settings files that commonly hold API keys are left out of
the suggestions.
"""

from __future__ import annotations

from typing import Final

PROGRAM_OTHER: Final = "other"
AUTOMATIC_PROGRAMS: Final = ("claude_code", "codex")

SUGGESTED: Final[dict[str, tuple[str, ...]]] = {
    "claude_code": (),
    "codex": (),
    "copilot_vscode": (
        ".github/copilot-instructions.md",
        ".github/instructions",
        "AGENTS.md",
        ".github/prompts",
        ".github/agents",
        ".github/skills",
        ".github/hooks",
        "~/.copilot/copilot-instructions.md",
        "~/.copilot/instructions",
        "~/.copilot/agents",
        "~/.copilot/skills",
        "~/.copilot/hooks",
    ),
    "cursor": (
        ".cursor/rules",
        "AGENTS.md",
        ".cursor/hooks.json",
        ".cursor/commands",
        ".cursor/skills",
        "~/.cursor/rules",
        "~/.cursor/hooks.json",
        "~/.cursor/commands",
        "~/.cursor/permissions.json",
    ),
    "opencode": (
        "AGENTS.md",
        ".opencode/agents",
        ".opencode/commands",
        ".opencode/skills",
        ".opencode/plugins",
        "~/.config/opencode/AGENTS.md",
        "~/.config/opencode/agents",
        "~/.config/opencode/commands",
        "~/.config/opencode/skills",
        "~/.config/opencode/plugins",
    ),
    "antigravity": (
        "AGENTS.md",
        "GEMINI.md",
        ".agents/rules",
        ".agents/skills",
        ".agents/agents",
        ".agents/hooks.json",
        "~/.gemini/GEMINI.md",
        "~/.gemini/AGENTS.md",
        "~/.gemini/config/rules",
        "~/.gemini/config/hooks.json",
        "~/.gemini/config/skills",
        "~/.gemini/config/agents",
    ),
    "junie": (
        "AGENTS.md",
        ".junie/AGENTS.md",
        ".junie/rules",
        ".junie/playbook.md",
        ".junie/commands",
        ".junie/agents",
        ".junie/skills",
        "~/.junie/AGENTS.md",
        "~/.junie/allowlist.json",
        "~/.junie/commands",
        "~/.junie/agents",
        "~/.junie/skills",
    ),
    "cline": (
        "AGENTS.md",
        ".clinerules",
        ".cline/rules",
        ".cline/workflows",
        ".cline/skills",
        ".cline/agents",
        ".cline/hooks",
        "~/.cline/rules",
        "~/.cline/workflows",
        "~/.cline/skills",
        "~/.cline/hooks",
        "~/Documents/Cline/Rules",
        "~/Documents/Cline/Workflows",
        "~/Documents/Cline/Hooks",
    ),
    "copilot_cli": (
        ".github/copilot-instructions.md",
        ".github/instructions",
        "AGENTS.md",
        ".github/agents",
        ".github/skills",
        ".github/hooks",
        "~/.copilot/copilot-instructions.md",
        "~/.copilot/instructions",
        "~/.copilot/agents",
        "~/.copilot/skills",
        "~/.copilot/hooks",
    ),
    "kilo_code": (
        "AGENTS.md",
        ".kilo/agent",
        ".kilo/agents",
        ".kilo/commands",
        ".kilo/skills",
        ".kilo/plugins",
        "~/.config/kilo/AGENTS.md",
        "~/.config/kilo/agent",
        "~/.config/kilo/commands",
        "~/.agents/skills",
    ),
    PROGRAM_OTHER: (),
}

# By 2026 use at work (JetBrains Developer Ecosystem Survey 2026), then Other.
PROGRAMS: Final = (
    "claude_code",
    "copilot_vscode",
    "codex",
    "cursor",
    "opencode",
    "antigravity",
    "junie",
    "cline",
    "copilot_cli",
    "kilo_code",
    PROGRAM_OTHER,
)


def is_automatic(program: str) -> bool:
    return program in AUTOMATIC_PROGRAMS
