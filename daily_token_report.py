#!/usr/bin/env python3
"""daily-token-report: re-check a watchlist of tokens once a day and say what changed since yesterday.

You hold or follow a few coins. Once a day this pulls a fresh rug check for each one from the CyberWareX safety
oracle, appends it to a CSV history, and writes a short report that puts the changes on top: a grade that
dropped, a new flag (owner can now mint, sells started failing), a pool that is gone. Optionally it sends that
report to a Telegram chat, only when something changed.

Python 3.9+ standard library only. No keys needed to start:
  - Oracle: 3 free checks per IP per day, so a watchlist of 3 runs free. For more set CWX_KEY to a prepaid key
    (see README). A token that was not checked is reported as NOT CHECKED, never as unchanged or safe.
  - Telegram (optional): TG_BOT_TOKEN and TG_CHAT.

Usage:
  python3 daily_token_report.py watchlist.txt              # writes history.csv and report.md
  python3 daily_token_report.py watchlist.txt --telegram   # also sends the report when something changed
Watchlist lines: "[chain] 0xaddress [label]", chain one of base bsc ethereum polygon arbitrum (default base),
'#' starts a comment.
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"
UA = "daily-token-report/%s (+https://github.com/cyberwarex/daily-token-report)" % __version__
DEFAULT_ORACLE = "https://oracle.cyberwarex.com"
CHAINS = ("base", "bsc", "ethereum", "polygon", "arbitrum")
CHAIN_NAME = {"base": "Base", "bsc": "BSC", "ethereum": "Ethereum", "polygon": "Polygon", "arbitrum": "Arbitrum"}
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
FIELDS = ["date", "chain", "address", "label", "checked", "verdict", "grade", "risk", "flags", "why"]
VERDICT_RANK = {"ALLOW": 0, "LIMIT": 1, "RECHECK": 2, "BLOCK": 3}
RISK_JUMP = 10                 # a risk score move this big counts as a change
MAX_TOKENS = 25                # hard cap per run, so a pasted list cannot drain a key
GAP_SECONDS = 3                # pause between checks; the oracle rate-limits bursts (429)
DISCLAIMER = "Automated heuristics, not financial advice. https://github.com/cyberwarex/daily-token-report"


# ---------------------------------------------------------------- http

def http_json(url, body=None, headers=None, timeout=20, tries=3):
    """POST (body) or GET (no body) JSON with retries on 429 and 5xx. Returns (status, parsed or None)."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(min(max(wait, 5 * (attempt + 1)), 30))
                continue
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return 0, None
    return 0, None


# ---------------------------------------------------------------- watchlist

def read_watchlist(path):
    """[(chain, address, label)] from a watchlist file; bad lines are reported on stderr and skipped."""
    out, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            parts = raw.split("#", 1)[0].split()
            if not parts:
                continue
            chain = "base"
            if parts[0].lower() in CHAINS:
                chain = parts.pop(0).lower()
            if not parts or not ADDR.match(parts[0]):
                print("watchlist line %d skipped: no 0x address with 40 hex digits" % n, file=sys.stderr)
                continue
            addr = parts[0].lower()
            if (chain, addr) in seen:
                continue
            seen.add((chain, addr))
            out.append((chain, addr, " ".join(parts[1:]).replace("|", "/")[:40]))  # "|" would break the table
    return out


# ---------------------------------------------------------------- oracle

def check_token(oracle, token, key=None, chain="base"):
    """Verdict dict from the oracle. Always returns a dict; 'checked': False when no verdict was obtained."""
    url = "%s/risk/explain?%s" % (oracle.rstrip("/"), urllib.parse.urlencode({"address": token, "chain": chain}))
    headers = {"X-No-Advert": "1"}
    if key:
        headers["X-Subscription-Key"] = key
    else:
        headers["X-Free-Trial"] = "1"
    status, d = http_json(url, headers=headers, timeout=90, tries=3)
    if status == 200 and isinstance(d, dict) and d.get("verdict"):
        rec = d.get("recommendation") or {}
        return {"checked": True, "verdict": d["verdict"], "grade": d.get("grade") or "?",
                "risk": (d.get("risk") or {}).get("overall"), "flags": list(rec.get("reason_codes") or [])[:6],
                "why": rec.get("why") or ""}
    if status == 402:
        return {"checked": False, "why": "prepaid key is empty or invalid" if key else
                "free checks for today are used up; set CWX_KEY to check more"}
    if status == 400:
        return {"checked": False, "why": "the oracle did not accept this address or chain"}
    return {"checked": False, "why": "oracle unavailable (http %s)" % status}


# ---------------------------------------------------------------- history + changes

def load_last(history_path):
    """{(chain, address): last row that was actually checked} from the CSV history."""
    last = {}
    if not os.path.exists(history_path):
        return last
    with open(history_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("checked") == "1":
                last[(row["chain"], row["address"])] = row
    return last


def to_row(date, chain, addr, label, v):
    return {"date": date, "chain": chain, "address": addr, "label": label, "checked": "1" if v["checked"] else "0",
            "verdict": v.get("verdict", ""), "grade": v.get("grade", ""),
            "risk": "" if v.get("risk") is None else str(v["risk"]), "flags": " ".join(v.get("flags") or []),
            "why": (v.get("why") or "")[:200]}


def changes(prev, row):
    """Plain-language list of what moved between the last checked row and today's row. [] = no change."""
    if row["checked"] != "1":
        return ["NOT CHECKED today (%s)" % row["why"]]
    if prev is None:
        return ["first check"]
    out = []
    if prev["verdict"] != row["verdict"]:
        worse = VERDICT_RANK.get(row["verdict"], 9) > VERDICT_RANK.get(prev["verdict"], 9)
        out.append("verdict %s -> %s%s" % (prev["verdict"], row["verdict"], " (worse)" if worse else ""))
    if prev["grade"] != row["grade"]:
        out.append("grade %s -> %s" % (prev["grade"], row["grade"]))
    try:
        if abs(int(row["risk"]) - int(prev["risk"])) >= RISK_JUMP:
            out.append("risk %s -> %s" % (prev["risk"], row["risk"]))
    except ValueError:
        pass
    new_flags = sorted(set(row["flags"].split()) - set(prev["flags"].split()))
    gone_flags = sorted(set(prev["flags"].split()) - set(row["flags"].split()))
    if new_flags:
        out.append("new flags: " + ", ".join(new_flags))
    if gone_flags:
        out.append("flags cleared: " + ", ".join(gone_flags))
    return out


def append_history(history_path, rows):
    new = not os.path.exists(history_path)
    with open(history_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def ran_today(history_path, date):
    if not os.path.exists(history_path):
        return False
    with open(history_path, newline="", encoding="utf-8") as f:
        return any(r.get("date") == date for r in csv.DictReader(f))


# ---------------------------------------------------------------- report

def name_of(row):
    short = "%s...%s" % (row["address"][:6], row["address"][-4:])
    return "%s %s%s" % (CHAIN_NAME.get(row["chain"], row["chain"]), row["label"] + " " if row["label"] else "", short)


def render(date, results):
    """Markdown report: changes first, then the full table. results = [(row, [changes])]."""
    changed = [(r, c) for r, c in results if c and c != ["first check"]]
    lines = ["# Token report %s" % date, ""]
    if changed:
        lines.append("## Changed since last check (%d)" % len(changed))
        lines += ["- **%s**: %s" % (name_of(r), "; ".join(c)) for r, c in changed]
    else:
        lines.append("No changes since the last check.")
    first = sum(1 for _, c in results if c == ["first check"])
    if first:
        lines += ["", "First check for %d token%s; changes show from the next run." % (first, "" if first == 1 else "s")]
    lines += ["", "## All tokens", "", "| token | verdict | grade | risk | flags |", "|---|---|---|---|---|"]
    for r, _ in results:
        if r["checked"] == "1":
            lines.append("| %s | %s | %s | %s | %s |" % (name_of(r), r["verdict"], r["grade"], r["risk"] or "?",
                                                      r["flags"].replace(" ", ", ") or "-"))
        else:
            lines.append("| %s | NOT CHECKED | | | %s |" % (name_of(r), r["why"]))
    lines += ["", DISCLAIMER, ""]
    return "\n".join(lines), len(changed)


def telegram_text(date, results):
    changed = [(r, c) for r, c in results if c and c != ["first check"]]
    lines = ["Token report %s: %d of %d changed" % (date, len(changed), len(results))]
    for r, c in changed[:15]:
        lines.append("- %s: %s" % (name_of(r), "; ".join(c)))
    lines.append(DISCLAIMER)
    return "\n".join(lines)[:3900]


def send_telegram(token, chat, text):
    status, d = http_json("https://api.telegram.org/bot%s/sendMessage" % token,
                          {"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=30, tries=1)
    return status == 200 and isinstance(d, dict) and d.get("ok")


# ---------------------------------------------------------------- main

def run(watchlist, history_path, report_path, oracle, key=None, date=None, check=None, gap=None):
    """One daily pass. Returns (results, changed_count)."""
    check = check or check_token
    gap = GAP_SECONDS if gap is None else gap
    date = date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    last = load_last(history_path)
    results = []
    for i, (chain, addr, label) in enumerate(watchlist):
        if i and gap:
            time.sleep(gap)
        row = to_row(date, chain, addr, label, check(oracle, addr, key=key, chain=chain))
        results.append((row, changes(last.get((chain, addr)), row)))
    append_history(history_path, [r for r, _ in results])
    text, n_changed = render(date, results)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(text)
    return results, n_changed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-check a token watchlist once a day and report what changed.")
    ap.add_argument("watchlist", help="file with one token per line: [chain] 0xaddress [label]")
    ap.add_argument("--history", default="history.csv", help="CSV history, appended each run")
    ap.add_argument("--report", default="report.md", help="markdown report, overwritten each run")
    ap.add_argument("--oracle", default=os.environ.get("CWX_ORACLE", DEFAULT_ORACLE))
    ap.add_argument("--telegram", action="store_true", help="send the report to TG_CHAT when something changed")
    ap.add_argument("--always-send", action="store_true", help="with --telegram, send even with no changes")
    ap.add_argument("--force", action="store_true", help="run again although today is already in the history")
    a = ap.parse_args(argv)

    watch = read_watchlist(a.watchlist)
    if not watch:
        print("watchlist has no valid tokens", file=sys.stderr)
        return 1
    if len(watch) > MAX_TOKENS:
        print("watchlist has %d tokens, checking the first %d" % (len(watch), MAX_TOKENS), file=sys.stderr)
        watch = watch[:MAX_TOKENS]
    tg_token, tg_chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT")
    if a.telegram and not (tg_token and tg_chat):
        print("--telegram needs TG_BOT_TOKEN and TG_CHAT", file=sys.stderr)
        return 1
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if not a.force and ran_today(a.history, date):
        print("already ran today (%s); use --force to run again" % date, file=sys.stderr)
        return 0

    results, n_changed = run(watch, a.history, a.report, a.oracle, key=os.environ.get("CWX_KEY"), date=date)
    with open(a.report, encoding="utf-8") as f:
        print(f.read())
    if a.telegram and (n_changed or a.always_send):
        if not send_telegram(tg_token, tg_chat, telegram_text(date, results)):
            print("telegram send failed", file=sys.stderr)
            return 3
    return 2 if any(r["checked"] != "1" for r, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
