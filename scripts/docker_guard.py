#!/usr/bin/env python3
"""PreToolUse guard (stdlib-only, fail-open).

Escalates destructive docker commands that could destroy the plugin's Neo4j
container or its data volume to an explicit user confirmation ("ask") —
it never hard-denies, and it makes no decision for anything else (normal
permission flow applies).

Triggers on: docker rm / docker container rm / docker volume rm naming a
graph-neo4j resource, and any docker *prune* (prunes are global — they can
take the stopped container or dangling volumes with them).
"""

import json
import re
import sys


CMD_POS = r"(?:^|[;&|]\s*|\$\(\s*)(?:sudo\s+)?"  # start of a command, not mid-string


def decide(command: str) -> str | None:
    # anchored to a command position so 'echo docker prune' or a commit
    # message mentioning docker never triggers
    prune = re.search(CMD_POS + r"docker\s+(?:\S+\s+)?prune\b", command)
    rm = re.search(CMD_POS + r"docker\s+(?:container\s+|volume\s+)?rm\b", command)
    if prune:
        return (
            "docker prune is global: it can remove the pg-graph-toolkit Neo4j "
            "container (graph-neo4j) or its data volume if stopped/dangling. "
            "Confirm you really want this."
        )
    if rm and re.search(r"graph-neo4j", command):
        return (
            "This removes the pg-graph-toolkit Neo4j container or its data "
            "volume (graph-neo4j*). The graph data would be lost (the volume) "
            "or need re-creation (the container). Confirm you really want this."
        )
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
        command = (data.get("tool_input") or {}).get("command") or ""
    except Exception:
        return  # fail open
    reason = decide(command)
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
    sys.exit(0)
