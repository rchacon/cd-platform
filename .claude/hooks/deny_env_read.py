#!/usr/bin/env python3
"""PreToolUse hook (Read matcher): block the Read tool from opening the
repo's real .env -- it holds live secrets (Cognito IDs, CONGRESS_API_KEY).
.env.sample/.env.example stay readable, they carry no real values."""
import json
import os
import sys

REASON = (
    "Reading .env directly is blocked -- it holds real secrets. Check "
    "whether a var is set without printing its value (e.g. grep -q), or "
    "ask the user for it."
)


def main() -> None:
    data = json.load(sys.stdin)
    file_path = data.get("tool_input", {}).get("file_path", "")
    if os.path.basename(file_path) == ".env":
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
