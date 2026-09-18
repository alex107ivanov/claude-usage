#!/usr/bin/env python3
"""Claude subscription usage pace tracker.

Compares current usage against a linear "burn budget" for the reset period, so
you can tell whether you're ahead of or behind pace before the limit resets.

Data sources, in order of preference:

1. Anthropic OAuth usage API (optional): GET https://api.anthropic.com/api/oauth/usage
   Gives exact reset times and per-model scoped windows (e.g. the separate
   Fable weekly limit). Requires an OAuth token — see README. Token lookup:
       - $CLAUDE_USAGE_TOKEN / $CLAUDE_CODE_OAUTH_TOKEN
       - ~/.config/claude-usage/token (single line, chmod 600)
       - macOS Keychain "Claude Code-credentials" (populated by claude CLI login)

2. Claude desktop app cache (no auth): the app samples usage every ~5 minutes
   into ~/Library/Application Support/Claude/plan-usage-history.json
   (fh = 5-hour window %, sd = 7-day window %). Weekly reset moments are
   detected from drops in the history and extrapolated on a 7-day grid.

Usage:
    claude_usage.py             # console report
    claude_usage.py --swiftbar  # SwiftBar/xbar menu bar plugin output
    claude_usage.py --json      # machine-readable
    claude_usage.py --no-api    # skip the OAuth API even if a token exists
    claude_usage.py --config-dir ~/.claude-secondary
                                # track another `claude` login (CLAUDE_CONFIG_DIR);
                                # uses that login's own credentials, API only

Optional config ~/.config/claude-usage/config.json:
    {"weekly_reset": "2026-07-31T20:00:00"}   # fallback next reset (local time)
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HISTORY = os.path.expanduser(
    "~/Library/Application Support/Claude/plan-usage-history.json"
)
CONFIG_DIR = os.path.expanduser("~/.config/claude-usage")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WEEK = timedelta(days=7)
FIVE_H = timedelta(hours=5)
STALE_AFTER = timedelta(minutes=20)


# ---------------------------------------------------------------- OAuth source

def _token_from_creds(text):
    try:
        return json.loads(text).get("claudeAiOauth", {}).get("accessToken", "") or None
    except (ValueError, AttributeError):
        return None


def find_token(config_dir=None):
    if config_dir:
        # Claude Code keeps a custom CLAUDE_CONFIG_DIR login separate: the
        # keychain service is suffixed with a hash of the dir, or the creds
        # live in <dir>/.credentials.json.
        config_dir = os.path.abspath(os.path.expanduser(config_dir))
        suffix = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s",
                 f"Claude Code-credentials-{suffix}", "-w"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            tok = _token_from_creds(out)
            if tok:
                return tok
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            with open(os.path.join(config_dir, ".credentials.json")) as f:
                return _token_from_creds(f.read())
        except OSError:
            return None
    for var in ("CLAUDE_USAGE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        tok = os.environ.get(var, "").strip()
        if tok:
            return tok
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return _token_from_creds(out)
    except (OSError, subprocess.SubprocessError):
        return None


def parse_reset(value):
    """resets_at may be an ISO string or an epoch number; return local naive dt."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return None


def fetch_api_usage(token):
    """Return normalized windows from the OAuth usage endpoint, or None."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-usage-pace-tracker",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except Exception:
        return None

    out = {"five_hour": None, "seven_day": None, "scoped": []}

    def pct_of(obj):
        for key in ("utilization", "percent", "used_percentage"):
            if isinstance(obj.get(key), (int, float)):
                return obj[key]
        return None

    for name in ("five_hour", "seven_day"):
        obj = data.get(name)
        if isinstance(obj, dict) and pct_of(obj) is not None:
            out[name] = {"pct": pct_of(obj), "resets_at": parse_reset(obj.get("resets_at"))}

    # Model-scoped weekly windows (e.g. the separate Fable limit).
    for win in data.get("limits") or []:
        if not isinstance(win, dict) or win.get("kind") != "weekly_scoped":
            continue
        model = (win.get("scope") or {}).get("model") or {}
        name = model.get("display_name")
        pct = pct_of(win)
        if name and pct is not None:
            out["scoped"].append(
                {"name": name, "pct": pct, "resets_at": parse_reset(win.get("resets_at"))}
            )
    return out


# -------------------------------------------------------- desktop cache source

def load_samples():
    try:
        with open(HISTORY) as f:
            data = json.load(f)
    except OSError:
        return []
    samples = []
    for s in data.get("samples", []):
        u = s.get("u", {})
        samples.append(
            (datetime.fromtimestamp(s["t"] / 1000), u.get("fh", 0), u.get("sd", 0))
        )
    samples.sort(key=lambda x: x[0])
    return samples


def find_last_reset(samples, idx, min_prev=5, floor=3):
    """Most recent moment the given metric dropped to ~0 (a window reset)."""
    prev = None
    reset = None
    for s in samples:
        val = s[idx]
        if prev is not None and prev >= min_prev and val <= floor and val < prev:
            reset = s[0]
        prev = val
    return reset


def next_on_grid(anchor, period, now):
    """First anchor + k*period that is strictly in the future."""
    if anchor is None:
        return None
    t = anchor
    while t <= now:
        t += period
    return t


# --------------------------------------------------------------------- pacing

def pace(pct, resets_at, now, period=WEEK):
    """Linear-budget stats for a window ending at resets_at."""
    start = resets_at - period
    elapsed = (now - start) / period
    if not 0 < elapsed <= 1:
        return None
    target_now = elapsed * 100
    eod = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_eod = min((min(eod, resets_at) - start) / period * 100, 100)
    return {
        "elapsed_pct": round(elapsed * 100, 1),
        "target_now_pct": round(target_now, 1),
        "target_end_of_day_pct": round(target_eod, 1),
        "delta_pct": round(pct - target_now, 1),
        "projected_at_reset_pct": round(pct / elapsed),
    }


def analyze(use_api=True, now=None, config_dir=None):
    now = now or datetime.now()
    result = {"source": None, "windows": []}

    api = None
    if use_api:
        token = find_token(config_dir)
        if token:
            api = fetch_api_usage(token)

    if api and (api["seven_day"] or api["scoped"]):
        result["source"] = "api"
        if api["five_hour"]:
            result["windows"].append(
                {"id": "five_hour", "label": "5-hour", "pct": api["five_hour"]["pct"],
                 "resets_at": api["five_hour"]["resets_at"], "pace": None}
            )
        if api["seven_day"]:
            w = api["seven_day"]
            result["windows"].append(
                {"id": "seven_day", "label": "weekly", "pct": w["pct"],
                 "resets_at": w["resets_at"],
                 "pace": pace(w["pct"], w["resets_at"], now) if w["resets_at"] else None}
            )
        for s in api["scoped"]:
            result["windows"].append(
                {"id": "scoped:" + s["name"], "label": s["name"] + " weekly",
                 "pct": s["pct"], "resets_at": s["resets_at"],
                 "pace": pace(s["pct"], s["resets_at"], now) if s["resets_at"] else None}
            )
        result["sampled_at"] = now
        result["stale"] = False
        return result

    if config_dir:
        # The desktop cache belongs to the primary login, not this one.
        raise SystemExit("No data: OAuth API unavailable for " + config_dir)

    # Fallback: desktop app cache.
    samples = load_samples()
    if not samples:
        raise SystemExit(
            "No data: OAuth API unavailable and no samples in " + HISTORY
        )
    ts, fh, sd = samples[-1]
    result["source"] = "desktop-cache"
    result["sampled_at"] = ts
    result["stale"] = (now - ts) > STALE_AFTER

    override = None
    try:
        with open(CONFIG) as f:
            override = datetime.fromisoformat(json.load(f)["weekly_reset"])
    except (OSError, KeyError, ValueError):
        pass

    last_week_reset = find_last_reset(samples, idx=2)
    if override:
        next_week_reset = next_on_grid(override - WEEK, WEEK, now)
    elif last_week_reset:
        next_week_reset = next_on_grid(last_week_reset, WEEK, now)
    else:
        next_week_reset = None

    last_fh_reset = find_last_reset(samples, idx=1)
    next_fh_reset = None
    if last_fh_reset and now - last_fh_reset < FIVE_H:
        next_fh_reset = last_fh_reset + FIVE_H

    result["windows"].append(
        {"id": "five_hour", "label": "5-hour", "pct": fh,
         "resets_at": next_fh_reset, "pace": None}
    )
    result["windows"].append(
        {"id": "seven_day", "label": "weekly", "pct": sd, "resets_at": next_week_reset,
         "pace": pace(sd, next_week_reset, now) if next_week_reset else None}
    )
    return result


# -------------------------------------------------------------------- output

def fmt_dt(dt):
    return dt.strftime("%a %b %-d %H:%M") if dt else "?"


def console_report(r):
    lines = ["Claude subscription usage"]
    src = "live API" if r["source"] == "api" else "desktop app cache"
    lines.append(f"  sampled {fmt_dt(r['sampled_at'])} via {src}"
                 + ("  (STALE — is the Claude app running?)" if r["stale"] else ""))
    for w in r["windows"]:
        lines.append("")
        extra = f"  (resets {fmt_dt(w['resets_at'])})" if w["resets_at"] else ""
        lines.append(f"  {w['label']:<14}: {w['pct']:g}%{extra}")
        p = w["pace"]
        if not p:
            continue
        delta = p["delta_pct"]
        status = ("ON PACE" if abs(delta) <= 3
                  else ("OVER pace" if delta > 0 else "under pace"))
        sign = "+" if delta >= 0 else ""
        lines.append(f"    pace target : {p['target_now_pct']}% now, "
                     f"{p['target_end_of_day_pct']}% by end of today")
        lines.append(f"    status      : {status}  ({sign}{delta} pts vs linear budget)")
        lines.append(f"    projection  : ~{p['projected_at_reset_pct']}% at reset")
    if r["source"] == "desktop-cache":
        lines.append("")
        lines.append("  (add an OAuth token for exact resets + per-model limits — see README)")
    return "\n".join(lines)


def _arrow(delta):
    if delta is None:
        return "◐", None
    if delta > 3:
        return "▲", "red"
    if delta < -3:
        return "▼", "#28a745"
    return "●", None


def swiftbar_report(r, name=None):
    by_id = {w["id"]: w for w in r["windows"]}
    weekly = by_id.get("seven_day")

    parts, color = [], None
    if weekly:
        delta = weekly["pace"]["delta_pct"] if weekly["pace"] else None
        icon, color = _arrow(delta)
        parts.append(f"{icon} {weekly['pct']:g}%")
    head = " ".join(parts) or "no data"
    if name:
        head = f"{name} {head}"
    if r["stale"]:
        head += " ⚠︎"
    lines = [head + (f" | color={color}" if color else "")]
    lines.append("---")
    for w in r["windows"]:
        title = f"{w['label']}: {w['pct']:g}% used"
        if w["resets_at"]:
            title += f" — resets {fmt_dt(w['resets_at'])}"
        lines.append(title + " | size=13")
        p = w["pace"]
        if p:
            sign = "+" if p["delta_pct"] >= 0 else ""
            lines.append(f"-- pace target now: {p['target_now_pct']}%"
                         f" ({sign}{p['delta_pct']} pts) | size=13")
            lines.append(f"-- budget by end of today: {p['target_end_of_day_pct']}% | size=13")
            lines.append(f"-- projected at reset: ~{p['projected_at_reset_pct']}% | size=13")
    lines.append("---")
    src = "live API" if r["source"] == "api" else "desktop cache"
    lines.append(f"Sampled: {fmt_dt(r['sampled_at'])} via {src}"
                 + (" — STALE" if r["stale"] else "") + " | size=12 color=gray")
    return "\n".join(lines)


def _arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


if __name__ == "__main__":
    config_dir = _arg("--config-dir")
    name = _arg("--name")
    r = analyze(use_api="--no-api" not in sys.argv, config_dir=config_dir)
    if "--json" in sys.argv:
        out = dict(r)
        out["sampled_at"] = r["sampled_at"].isoformat()
        out["windows"] = [
            {**w, "resets_at": w["resets_at"].isoformat() if w["resets_at"] else None}
            for w in r["windows"]
        ]
        print(json.dumps(out, indent=2))
    elif "--swiftbar" in sys.argv:
        print(swiftbar_report(r, name))
    else:
        print(console_report(r))
