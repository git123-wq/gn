#!/usr/bin/env python3
"""Daily GMGN quality wallets → Nansen smart alerts + GMGN-only Discord ping."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

LONDON = ZoneInfo("Europe/London")
NANSEN = "https://api.nansen.ai/api/v1"
GMGN = "https://gmgn.ai/defi/quotation/v1"
KEY = os.environ.get("NANSEN_API_KEY", "").strip()
GMGN_WEBHOOK = os.environ["GMGN_WEBHOOK"].strip()
BACKUP_WEBHOOK = os.environ.get("BACKUP_WEBHOOK", "").strip()
PATCH_ALERTS = os.environ.get("PATCH_ALERTS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FORCE_RUN = os.environ.get("FORCE_RUN", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RUN_HOUR = int(os.environ.get("RUN_HOUR", "20"))
RUN_MINUTE = int(os.environ.get("RUN_MINUTE", "15"))
MAX_ADD = int(os.environ.get("MAX_ADD", "30"))
RANK_LIMIT = int(os.environ.get("RANK_LIMIT", "200"))
MIN_WIN = float(os.environ.get("MIN_WIN", "0.35"))
MIN_TX = int(os.environ.get("MIN_TX", "4"))
MAX_TX = int(os.environ.get("MAX_TX", "120"))
MIN_PNL = float(os.environ.get("MIN_PNL_USD", "500"))
MAX_PNL = float(os.environ.get("MAX_PNL_USD", "250000"))
TAGS = [
    x.strip()
    for x in os.environ.get("GMGN_TAGS", "smart_degen,launchpad_smart,").split(",")
]
SKIP_TAGS = {
    x.strip().lower()
    for x in os.environ.get("SKIP_TAGS", "snipe_bot,sniper,fresh_wallet").split(",")
    if x.strip()
}
CHAINS = [
    x.strip()
    for x in os.environ.get("GMGN_CHAINS", "sol,robinhood").split(",")
    if x.strip()
]
SOL_ALERTS = [
    x
    for x in (
        os.environ.get("SOL_BUY_ALERT_ID", "").strip(),
        os.environ.get("SOL_SELL_ALERT_ID", "").strip(),
    )
    if x
]
RH_ALERTS = [
    x
    for x in (
        os.environ.get("RH_BUY_ALERT_ID", "").strip(),
        os.environ.get("RH_SELL_ALERT_ID", "").strip(),
    )
    if x
]
STATE = os.environ.get("STATE_FILE", "/app/gmgn_seen.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://gmgn.ai/",
}


def load_seen() -> set[str]:
    try:
        with open(STATE) as f:
            return {str(x).lower() for x in json.load(f)}
    except Exception:
        return set()


def save_seen(seen: set[str]):
    try:
        with open(STATE, "w") as f:
            json.dump(sorted(seen)[-4000:], f)
    except Exception:
        pass


def nansen_get(path: str):
    r = requests.get(f"{NANSEN}{path}", headers={"apikey": KEY}, timeout=45)
    r.raise_for_status()
    return r.json()


def nansen_patch(path: str, body: dict):
    r = requests.patch(
        f"{NANSEN}{path}",
        headers={"Content-Type": "application/json", "apikey": KEY},
        json=body,
        timeout=45,
    )
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}


def list_alerts() -> list[dict]:
    for path in ("/smart-alert/list", "/smart-alerts", "/smart-alert"):
        try:
            raw = nansen_get(path)
        except Exception:
            continue
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for k in ("data", "alerts", "items"):
                if isinstance(raw.get(k), list):
                    return raw[k]
    return []


def merge_patch_alert(alert_id: str, new_addrs: list[str]):
    alerts = list_alerts()
    alert = next((a for a in alerts if a.get("id") == alert_id), None)
    if not alert:
        raise RuntimeError(f"alert {alert_id} not in GET list")
    name = str(alert.get("name") or alert_id)
    data = dict(alert.get("data") or {})
    subjects = list(data.get("subjects") or [])
    have = {
        str(s.get("value") or "").lower()
        for s in subjects
        if isinstance(s, dict)
    }
    added, existed = [], 0
    for addr in new_addrs:
        key = addr.lower()
        if key in have:
            existed += 1
            continue
        subjects.append({"type": "address", "value": addr})
        have.add(key)
        added.append(addr)
    if added:
        data["subjects"] = subjects
        nansen_patch("/smart-alert", {"id": alert_id, "data": data})
    return name, added, len(subjects), existed


def _rows_from(raw) -> tuple[list, str | None]:
    data = raw.get("data") if isinstance(raw, dict) else raw
    cursor = None
    rows = []
    if isinstance(data, dict):
        rows = data.get("rank") or data.get("list") or data.get("wallets") or []
        cursor = (
            data.get("next")
            or data.get("cursor")
            or data.get("next_cursor")
            or data.get("next_page_token")
        )
    elif isinstance(data, list):
        rows = data
    if isinstance(raw, dict) and not cursor:
        cursor = raw.get("next") or raw.get("cursor") or raw.get("next_page_token")
    return rows if isinstance(rows, list) else [], (str(cursor) if cursor else None)


def _fetch_rank(chain: str, tag: str, period: str) -> list[dict]:
    url = f"{GMGN}/rank/{chain}/wallets/{period}"
    order = "pnl_7d" if period == "7d" else "pnl_30d"
    params = {"orderby": order, "direction": "desc", "limit": 100}
    if tag:
        params["tag"] = tag
    last = None
    for i in range(4):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=25)
            if r.status_code in {429, 500, 502, 503}:
                time.sleep(2 * (i + 1))
                last = f"HTTP {r.status_code}"
                continue
            if r.status_code >= 400:
                raise RuntimeError(
                    f"GMGN {chain}/{tag or 'all'}/{period} HTTP {r.status_code}"
                )
            rows, _ = _rows_from(r.json())
            return rows
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GMGN scrape failed {chain}/{tag or 'all'}/{period}: {last}")


def gmgn_rank(chain: str, tag: str) -> list[dict]:
    out: list[dict] = []
    seen_addr: set[str] = set()
    for period in ("7d", "30d"):
        rows = _fetch_rank(chain, tag, period)
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            a = addr_of(row).lower()
            if not a or a in seen_addr:
                continue
            seen_addr.add(a)
            out.append(row)
            added += 1
            if len(out) >= RANK_LIMIT:
                break
        print(
            f"gmgn {chain}/{tag or 'all'}/{period} page={len(rows)} unique+={added} total={len(out)}",
            flush=True,
        )
        if len(out) >= RANK_LIMIT:
            break
        time.sleep(0.3)
    return out


def num(row: dict, *keys) -> float:
    for k in keys:
        if row.get(k) is not None:
            try:
                return float(row[k])
            except Exception:
                pass
    return 0.0


def addr_of(row: dict) -> str:
    w = row.get("wallet")
    if isinstance(w, dict):
        row = {**row, **w}
    return str(
        row.get("address")
        or row.get("wallet_address")
        or row.get("wallet")
        or row.get("account")
        or ""
    ).strip()


def tag_blob(row: dict) -> str:
    tags = row.get("tags") or row.get("tag") or []
    if isinstance(tags, list):
        return " ".join(str(t).lower() for t in tags)
    return str(tags).lower()


def flatten(row: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (row or {}).items():
        key = f"{prefix}{k}".lower()
        if isinstance(v, dict):
            out.update(flatten(v, key + "_"))
        else:
            out[key] = v
            out[str(k).lower()] = v
    return out


def stats_of(row: dict) -> tuple[float, int, float]:
    f = flatten(row)
    wr = num(
        f,
        "winrate_7d",
        "winrate",
        "win_rate",
        "winrate_7day",
        "win_rate_7d",
    )
    if wr > 1:
        wr = wr / 100.0
    tx = int(
        num(
            f,
            "buy_7d",
            "txs_7d",
            "tx_count_7d",
            "buy",
            "txs",
            "txs_buy_7d",
            "buy_count_7d",
        )
    )
    pnl = num(
        f,
        "realized_profit_7d",
        "realized_profit",
        "profit_7d",
        "realized_profit_7day",
        "realized_pnl_7d",
        "usd_profit_7d",
    )
    if pnl <= 0:
        raw = num(f, "pnl_7d", "pnl")
        if raw > 50:
            pnl = raw
    return wr, tx, pnl


def keep(row: dict) -> tuple[bool, str]:
    blob = tag_blob(row)
    if any(t in blob for t in SKIP_TAGS):
        return False, "tag"
    wr, tx, pnl = stats_of(row)
    if wr < MIN_WIN:
        return False, "win"
    if tx < MIN_TX or tx > MAX_TX:
        return False, "tx"
    if pnl < MIN_PNL or pnl > MAX_PNL:
        return False, "pnl"
    return True, "ok"


def post(webhook: str, title: str, desc: str, color: int = 0x00C2A8):
    if not webhook:
        return
    requests.post(
        webhook,
        json={"embeds": [{"title": title, "description": desc[:3900], "color": color}]},
        timeout=20,
    ).raise_for_status()


def sleep_until():
    now = datetime.now(LONDON)
    target = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    secs = (target - now).total_seconds()
    print(f"sleeping {secs:.0f}s until {target.isoformat()}", flush=True)
    time.sleep(secs)


def run_job():
    seen = load_seen()
    picked: dict[str, list[tuple[str, dict]]] = {"sol": [], "robinhood": []}
    errors = []
    stats: list[str] = []
    for chain in CHAINS:
        bucket = "robinhood" if "robin" in chain else "sol"
        for tag in TAGS:
            try:
                rows = gmgn_rank(chain, tag)
                print(f"gmgn {chain}/{tag or 'all'} rows={len(rows)}", flush=True)
            except Exception as e:
                errors.append(f"{chain}/{tag or 'all'}: {e}")
                print(f"ERROR {chain}/{tag or 'all'} {e}", flush=True)
                continue
            reasons = {"tag": 0, "win": 0, "tx": 0, "pnl": 0, "noaddr": 0, "seen": 0, "ok": 0, "thin": 0}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                addr = addr_of(row)
                if not addr:
                    reasons["noaddr"] += 1
                    continue
                if addr.lower() in seen:
                    reasons["seen"] += 1
                    continue
                ok, why = keep(row)
                reasons[why] = reasons.get(why, 0) + 1
                if not ok:
                    continue
                picked[bucket].append((addr, row))
                seen.add(addr.lower())
                if len(picked[bucket]) >= MAX_ADD:
                    break
            stats.append(
                f"{chain}/{tag or 'all'}: fetched {len(rows)} · kept {reasons.get('ok',0)+reasons.get('thin',0)} · "
                f"skip win={reasons['win']} tx={reasons['tx']} pnl={reasons['pnl']} "
                f"tag={reasons['tag']} noaddr={reasons['noaddr']}"
            )
            if len(picked[bucket]) >= MAX_ADD:
                break

    if errors and not any(picked.values()):
        post(
            GMGN_WEBHOOK,
            "GMGN scrape failed",
            "\n".join(errors)[:1800],
            0xED4245,
        )
        return

    if stats or errors:
        post(
            GMGN_WEBHOOK,
            "GMGN filter report",
            "\n".join(stats + errors) or "no fetches",
            0x5865F2,
        )
    save_seen(seen)

    if BACKUP_WEBHOOK and KEY and PATCH_ALERTS:
        try:
            alerts = list_alerts()
            lines = []
            for a in alerts:
                n = len((a.get("data") or {}).get("subjects") or [])
                lines.append(f"{a.get('name') or a.get('id')}: {n} wallets")
            post(BACKUP_WEBHOOK, "Pre-patch backup (GMGN run)", "\n".join(lines) or "no alerts")
        except Exception as e:
            print(f"backup failed {e}", flush=True)

    def fmt_row(addr: str, row: dict) -> str:
        wr, tx, pnl = stats_of(row)
        return f"`{addr}` · wr {wr:.0%} · pnl ${pnl:,.0f} · {tx} buys/7d"

    for chain, rows in picked.items():
        addrs = [a for a, _ in rows][:MAX_ADD]
        if not addrs:
            post(
                GMGN_WEBHOOK,
                f"GMGN {chain} — no new wallets",
                "Filters found nothing new today.",
                0x99AAB5,
            )
            continue
        body = "\n".join(fmt_row(a, r) for a, r in rows[:MAX_ADD])
        post(
            GMGN_WEBHOOK,
            f"GMGN source — {len(addrs)} {chain} wallets",
            body,
            0x00C2A8,
        )
        if not PATCH_ALERTS or not KEY:
            continue
        ids = SOL_ALERTS if chain == "sol" else RH_ALERTS
        for aid in ids:
            try:
                name, added, total, existed = merge_patch_alert(aid, addrs)
                block = "\n".join(added) if added else "none"
                post(
                    GMGN_WEBHOOK,
                    f"GMGN added to {name}" if added else f"GMGN already on {name}",
                    f"added {len(added)} · already {existed} · list now {total}\n```\n{block[:3500]}\n```",
                    0x3BA55D if added else 0x99AAB5,
                )
            except Exception as e:
                post(GMGN_WEBHOOK, f"GMGN patch failed {aid}", str(e), 0xED4245)


def main():
    print("gmgn wallet worker started", flush=True)
    if FORCE_RUN:
        try:
            run_job()
        except Exception as e:
            print(f"force run failed {e}", flush=True)
            post(GMGN_WEBHOOK, "GMGN job failed", str(e), 0xED4245)
    while True:
        sleep_until()
        try:
            run_job()
        except Exception as e:
            print(f"scheduled failed {e}", flush=True)
            try:
                post(GMGN_WEBHOOK, "GMGN job failed", str(e), 0xED4245)
            except Exception:
                pass


if __name__ == "__main__":
    main()
