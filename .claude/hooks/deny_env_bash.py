#!/usr/bin/env python3
"""PreToolUse hook (Bash matcher): block any Bash command whose text
references the repo's real .env (cat/grep/head/python -c/etc. -- whatever
form it takes) -- it holds live secrets. Matches ".env" as a whole token
(word-boundary), so .env.sample/.env.example are left alone, and commands
that don't literally name .env (docker compose reading it implicitly,
`make start-server`) are unaffected."""
import json
import re
import sys

REASON = (
    "This command references .env (real secrets) -- reading/printing it "
    "is blocked. Check whether a var is set without printing its value "
    "(e.g. grep -q/-c), or ask the user for it."
)

# ".env" not glued to another identifier char on either side, so
# ".env.sample"/"foo.envrc" don't match but "cat .env", "cat ./.env",
# "grep X .env" do.
ENV_TOKEN = re.compile(r"(^|[^A-Za-z0-9_.-])\.env($|[^A-Za-z0-9_.-])")


def main() -> None:
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")
    if ENV_TOKEN.search(command):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": REASON,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
