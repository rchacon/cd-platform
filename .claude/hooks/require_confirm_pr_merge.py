#!/usr/bin/env python3
"""PreToolUse hook (Bash matcher): force an explicit permission prompt
before any `gh pr merge` invocation, regardless of the session's
permission mode (including auto-accept/auto mode) -- the user wants
their go-ahead required for every PR merge, not just ones a normal
permission-mode setting happens to prompt for."""
import json
import re
import sys

REASON = "PR merges always require your explicit go-ahead, even in auto mode."

# Matches `gh pr merge` as a whole command anywhere in the string (so it
# still catches `cd foo && gh pr merge 12 --merge`), but not an unrelated
# command that merely contains those words separately.
GH_PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")


def main() -> None:
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")
    if GH_PR_MERGE.search(command):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": REASON,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
