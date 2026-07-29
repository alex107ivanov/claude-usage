#!/bin/bash
# <xbar.title>Claude Usage Pace</xbar.title>
# <xbar.desc>Claude subscription usage vs linear weekly budget</xbar.desc>
DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
exec /usr/bin/python3 "$DIR/../claude_usage.py" --swiftbar
