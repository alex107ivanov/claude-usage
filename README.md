# claude-usage

Pace tracker for Claude subscription limits: compares current usage against a
linear weekly budget so you know if you're burning too fast before the reset.

```
  weekly        : 57%  (resets Fri Jul 31 20:04)
    pace target : 66.5% now, 73.8% by end of today
    status      : under pace  (-9.5 pts vs linear budget)
    projection  : ~86% at reset
```

macOS only. No dependencies (Python 3 stdlib).

## Data sources

**Desktop app cache (default, zero setup).** The Claude desktop app polls plan
usage every ~5 minutes and caches it in
`~/Library/Application Support/Claude/plan-usage-history.json`
(`fh` = 5-hour window %, `sd` = 7-day window %). The weekly reset moment is
detected from drops in the sample history (usage falling to ~0) and
extrapolated on a 7-day grid. Caveat: samples only accumulate while the
desktop app is running; output warns when data is stale (>20 min).

**OAuth usage API (optional).** With an OAuth token, the script instead calls
`GET https://api.anthropic.com/api/oauth/usage`, which returns exact reset
times plus **model-scoped weekly windows** — e.g. the separate Fable 5 limit —
each tracked with its own pace budget. Token lookup order:

1. `$CLAUDE_USAGE_TOKEN` or `$CLAUDE_CODE_OAUTH_TOKEN`
2. `~/.config/claude-usage/token` (single line, `chmod 600`)
3. macOS Keychain `Claude Code-credentials` (populated by `claude` CLI
   subscription login)

To set up a token once:

```bash
claude setup-token   # browser flow, prints a long-lived sk-ant-oat... token
mkdir -p ~/.config/claude-usage
# paste the token into ~/.config/claude-usage/token, then:
chmod 600 ~/.config/claude-usage/token
```

An Anthropic *API key* will not work — subscription usage is OAuth-only.

## Console

```bash
python3 claude_usage.py           # human report
python3 claude_usage.py --json    # machine-readable
python3 claude_usage.py --no-api  # force desktop-cache source
```

## Menu bar widget (SwiftBar)

```bash
brew install --cask swiftbar
defaults write com.ameba.SwiftBar PluginDirectory -string "$PWD/swiftbar"
open -a SwiftBar
```

[`swiftbar/claude-usage.5m.sh`](swiftbar/claude-usage.5m.sh) refreshes every
5 minutes and shows e.g. `▼ 57% F▲12%` — weekly usage (and Fable, when the
API source is active) with delta vs the linear budget:

- green `▼` — under pace (more than 3 pts of headroom)
- plain `●` — on pace (±3 pts)
- red `▲` — over pace, slow down
- `⚠︎` — stale data (desktop app not running)

The dropdown shows each window's pace target, end-of-day budget, and
projection at reset.

## Config

If reset detection ever drifts in cache mode, override the next weekly reset
in `~/.config/claude-usage/config.json`:

```json
{ "weekly_reset": "2026-07-31T20:00:00" }
```

For a second login (e.g. an alias `CLAUDE_CONFIG_DIR=~/.claude-secondary claude`),
`swiftbar/claude-secondary-usage.5m.sh` runs
`claude_usage.py --swiftbar --config-dir ~/.claude-secondary --name 2ⁿᵈ`, which
reads that login's own credentials (API only; no desktop-cache fallback).
