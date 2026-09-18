#!/bin/bash
# <xbar.title>Claude Secondary Usage Pace</xbar.title>
# <xbar.desc>Usage vs linear weekly budget for the claude-secondary login (~/.claude-secondary)</xbar.desc>
DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
exec /usr/bin/python3 "$DIR/../claude_usage.py" --swiftbar --config-dir ~/.claude-secondary --name "2ⁿᵈ"
