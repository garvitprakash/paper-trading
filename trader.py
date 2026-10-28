#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paper Trading Robot - Indian stocks (swing trading)
---------------------------------------------------
Ye program asli paisa nahi lagata. Sirf nakli (paper) kharid-bikri karta hai.

Kaam:
  scan  : subah stock chunna (aapke niyam se) aur list banana, Telegram par bhejna
  poll  : Telegram par aapka jawab (kaun sa stock kharidna hai) padhna
  buy   : aapke chune stock ko khule market me opening bhaav par kharidna
  check : har 15 minute me profit / loss target dekhna aur bechna
  eod   : din ka final hisaab
Sab niyam config.json me hain. Is file ko badalne ki zaroorat nahi.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE, "data")
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
UNIVERSE_PATH = os.path.join(DATA_DIR, "universe.json")

NSE_LISTS = {
    "Nifty 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "Nifty Next 50": "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "Nifty Midcap 150": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "Nifty Smallcap 100": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
}

OUTBOX = []  # Telegram messages; state save hone ke baad hi bheje jaate hain


# ----------------------------------------------------------------------------
# chhote helper
# ----------------------------------------------------------------------------
def now_ist():
    return datetime.now(IST)


def inr(x, d=2):
    """Indian style paisa: 1234567.5 -> ₹12,34,567.50"""
    neg = x < 0
    s = f"{abs(x):.{d}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    out = whole + ("." + frac if d else "")
    return ("-" if neg else "") + "₹" + out


def num(x, d=0):
    return inr(x, d).replace("₹", "")


def notify(text):
    print("[TELEGRAM]", text.replace("\n", " | "))
    OUTBOX.append(text)


def flush_outbox():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        if OUTBOX:
            print("(Telegram ka token/chat id nahi mila, isliye message sirf yahan dikhaya gaya)")
        OUTBOX.clear()
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for text in OUTBOX:
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        if cur.strip():
            chunks.append(cur)
        for ch in chunks:
            try:
                r = requests.post(url, data={"chat_id": chat, "text": ch.rstrip(),
                                             "disable_web_page_preview": True}, timeout=25)
                if r.status_code != 200:
                    print("Telegram error:", r.status_code, r.text[:200])
            except Exception as e:  # noqa
                print("Telegram bhejne me dikkat:", e)
            time.sleep(0.4)
    OUTBOX.clear()


def warn_once(st, now, text):
    """Ek din me ek hi baar warning bhejo (baar-baar spam na ho)"""
    print(text)
    if st["daily"].get("scan_warn") != str(now.date()):
        st["daily"]["scan_warn"] = str(now.date())
        notify(text)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def default_state(cfg):
    return {
        "version": 1,
        "start_capital": cfg["capital"],
        "cash": float(cfg["capital"]),
        "next_amount": float(cfg["start_amount"]),
        "positions": [],
        "trades": [],
        "today_list": None,
        "list_history": [],
        "snapshots": [],
        "daily": {},
        "summary": {},
        "updated_at": None,
    }


def load_state(cfg):
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        base = default_state(cfg)
        for k, v in base.items():
            st.setdefault(k, v)
        return st
    return default_state(cfg)


def compute_summary(cfg, st):
    realized = sum(t["profit_loss"] for t in st["trades"])
    invested_open = sum(p["invested"] for p in st["positions"])
    unreal = sum((p.get("last_price", p["buy_price"]) - p["buy_price"]) * p["qty"] for p in st["positions"])
    wins = sum(1 for t in st["trades"] if t["profit_loss"] > 0)
    n = len(st["trades"])
    mv = sum(p.get("last_price", p["buy_price"]) * p["qty"] for p in st["positions"])
    return {
        "total_actual_pl": round(realized, 2),
        "unrealized_pl_approx": round(unreal, 2),
        "closed_trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(100.0 * wins / n, 1) if n else None,
        "open_positions": len(st["positions"]),
        "max_positions": cfg["max_positions"],
        "invested_open": round(invested_open, 2),
        "cash": round(st["cash"], 2),
        "portfolio_value": round(st["cash"] + mv, 2),
        "start_capital": st["start_capital"],
        "next_amount": round(st["next_amount"], 2),
        "total_tax_paid": round(sum(t["tax"] for t in st["trades"]), 2),
        "total_charges_paid": round(sum(t["buy_charges"] + t["sell_charges"] for t in st["trades"]), 2),
        "take_profit_pct": cfg["take_profit_pct"],
        "stop_loss_pct": cfg["stop_loss_pct"],
    }


def save_state(cfg, st):
    os.makedirs(DATA_DIR, exist_ok=True)
    st["summary"] = compute_summary(cfg, st)
    st["updated_at"] = now_ist().isoformat(timespec="seconds")
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


# ----------------------------------------------------------------------------
# kharcha aur tax
# ----------------------------------------------------------------------------
def calc_charges(cfg, value, side):
    c = cfg["charges"]
    brokerage = value * c["brokerage_pct"] / 100
    stt = value * c["stt_pct"] / 100
    exch = value * c["exchange_pct"] / 100
    sebi = value * c["sebi_pct"] / 100
    stamp = value * c["stamp_buy_pct"] / 100 if side == "buy" else 0.0
    dp = c["dp_sell_flat"] if side == "sell" else 0.0
    gst = (brokerage + exch + sebi) * c["gst_pct"] / 100
    return round(brokerage + stt + exch + sebi + stamp + dp + gst, 2)


def calc_tax(cfg, profit_after_charges, days_held):
    if profit_after_charges <= 0:
        return 0.0
    t = cfg["tax"]
    base = t["ltcg_rate_pct"] if days_held > 365 else t["stcg_rate_pct"]
    rate = base * (1 + t["cess_pct"] / 100) / 100
    return round(profit_after_charges * rate, 2)


# ----------------------------------------------------------------------------
# data lena (Yahoo Finance, free, lagbhag 15 minute purana)
# ----------------------------------------------------------------------------
def _yf():
    import yfinance as yf
    return yf


def _split(df, tickers):
    import pandas as pd
    out = {}
    if df is None or len(df) == 0:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = set(df.columns.get_level_values(0))
        for t in tickers:
            if t in lvl0:
                d = df[t].dropna(how="all")
                if len(d):
                    out[t] = d
    elif len(tickers) == 1:
        d = df.dropna(how="all")
        if len(d):
            out[tickers[0]] = d
    return out


def download(tickers, **kw):
    yf = _yf()
    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 80):
        chunk = tickers[i:i + 80]
        for attempt in range(3):
            try:
                df = yf.download(chunk, group_by="ticker", progress=False, threads=True,
                                 auto_adjust=False, **kw)
                out.update(_split(df, chunk))
                break
            except Exception as e:  # noqa
                print("download dikkat (koshish %d): %s" % (attempt + 1, e))
                time.sleep(4)
    return out


def get_daily(tickers):
    import pandas as pd
    res = download(tickers, period="2y", interval="1d")
    for t, df in res.items():
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        df = df.copy()
        df.index = idx.normalize()
        res[t] = df
    return res


def get_intraday(tickers):
    import pandas as pd
    res = download(tickers, period="5d", interval="5m")
    for t, df in res.items():
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC").tz_convert(IST) if idx.tz is None else idx.tz_convert(IST)
        df = df.copy()
        df.index = idx
        res[t] = df
    return res


def get_mcap_cr(ticker):
    """Market cap crore rupaye me (ya None)"""
    yf = _yf()
    for attempt in range(2):
        try:
            tk = yf.Ticker(ticker)
            mc = None
            try:
                mc = tk.fast_info["marketCap"]
            except Exception:  # noqa
                mc = None
            if not mc:
                mc = (tk.info or {}).get("marketCap")
            if mc:
                return float(mc) / 1e7
        except Exception as e:  # noqa
            print("mcap dikkat", ticker, e)
            time.sleep(2)
    return None


def rsi_series(close, n):
    d = close.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = au / ad
    return 100 - 100 / (1 + rs)


# ----------------------------------------------------------------------------
# stock universe (Nifty 50 / Next 50 / Midcap 150 / Smallcap 100)
# ----------------------------------------------------------------------------
def _download_group(group, url):
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept": "text/csv,*/*"}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=hdr, timeout=30)
            if r.status_code == 200 and "Symbol" in r.text[:400]:
                rows = []
                for row in csv.DictReader(io.StringIO(r.text)):
                    sym = (row.get("Symbol") or "").strip()
                    if not sym or sym.upper().startswith("DUMMY"):
                        continue
                    rows.append({"symbol": sym, "name": (row.get("Company Name") or sym).strip(),
                                 "group": group, "ticker": sym + ".NS"})
                if rows:
                    return rows
        except Exception as e:  # noqa
            print("universe dikkat", group, e)
        time.sleep(3)
    return None


def load_universe(cfg, now):
    cache = {}
    if os.path.exists(UNIVERSE_PATH):
        try:
            with open(UNIVERSE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa
            cache = {}
    groups = cache.get("groups", {})
    fetched = cache.get("fetched", {})
    changed = False
    for g, url in NSE_LISTS.items():
        age_days = 999
        if g in fetched:
            try:
                age_days = (now.date() - datetime.fromisoformat(fetched[g]).date()).days
            except Exception:  # noqa
                pass
        if g not in groups or age_days >= 7:
            rows = _download_group(g, url)
            if rows:
                groups[g] = rows
                fetched[g] = now.isoformat(timespec="seconds")
                changed = True
            else:
                print("NSE se %s ki list nahi mili, purani saved list use hogi" % g)
    if changed:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UNIVERSE_PATH, "w", encoding="utf-8") as f:
            json.dump({"groups": groups, "fetched": fetched}, f, ensure_ascii=False)
    seen, out = set(), []
    for g in cfg["universe_groups"]:
        for r in groups.get(g, []):
            if r["ticker"] not in seen:
                seen.add(r["ticker"])
                out.append(r)
    return out


# ----------------------------------------------------------------------------
# kharid ki rakam: kam se kam target rakam lagegi
# ----------------------------------------------------------------------------
def plan_qty(cfg, amount, px):
    """Kitne share lene hain. Rakam kabhi target se kam nahi hogi (zaroorat ho to 1 share aur).
    Agar total rakam target ke (1 + max_overshoot_pct%) se upar jaye to None (bahut mehnga)."""
    if not px or px <= 0:
        return None
    qty = max(1, int(math.ceil(amount / px - 1e-9)))
    cap = amount * (1 + cfg["max_overshoot_pct"] / 100.0)
    if qty * px > cap + 1e-6:
        return None
    return qty


def one_year_return(df):
    """Pichhle 1 saal ka return % (ya None agar 1 saal ka data nahi)"""
    import pandas as pd
    if len(df) < 2:
        return None
    last_date = df.index[-1]
    ref_date = last_date - pd.Timedelta(days=365)
    older = df[df.index <= ref_date]
    if len(older) == 0:
        return None
    if (ref_date - older.index[-1]).days > 12:  # data me bada gap
        return None
    ref = float(older["Close"].iloc[-1])
    if ref <= 0:
        return None
    return (float(df["Close"].iloc[-1]) / ref - 1) * 100.0


def short_group(g):
    return g or ""


# ----------------------------------------------------------------------------
# list ka Telegram message
# ----------------------------------------------------------------------------
STATUS_TAG = {
    "selected": "  ✅ CHUNA (kharid hogi)", "bought": "  🟢 KHARIDA", "too_expensive": "",
    "no_funds": "  ⚠️ fund kam", "already": "  (pehle se hamare paas)", "locked": "  🔒 circuit me",
}


def pm(hhmm):
    h, m = [int(x) for x in hhmm.split(":")]
    return "%d:%02d %s" % (h - 12 if h > 12 else h, m, "pm" if h >= 12 else "am")


def how_to_reply_text(cfg):
    last = pm(cfg["last_buy_time"])
    return ("👉 KAUN SA STOCK KHARIDNA HAI? Is chat me list ke NUMBER likh kar bhej dijiye:\n"
            "• Ek stock: 3\n"
            "• Kai stock: 1 4 7   (beech me space ya comma)\n"
            "• Upar ke utne stock jitni jagah khaali hai: sab\n"
            "• Aaj kuch nahi kharidna: 0\n"
            "• List dobara dekhni hai: list\n\n"
            "⏰ 9:15 am se pehle bhejenge to khule bhaav par kharid hogi (message 9:30 ke baad aayega). "
            "Uske baad bhejenge to us samay ke bhaav par, kuch hi minute me. "
            "Aaj ke liye %s tak bhej sakte hain." % last)


def format_list_messages(cfg, st, items, date_str, mcap_unknown=None):
    free = cfg["max_positions"] - len(st["positions"])
    lines = ["📋 AAJ KI LIST - %s\n%d stock niyam par khare utre (sabse kam RSI sabse upar).\n"
             "Khaali jagah: %d | Ek stock par kam se kam: %s\n" %
             (date_str, len(items), free, inr(st["next_amount"], 0))]
    for i, it in enumerate(items, 1):
        r1 = it.get("ret_1y_pct")
        r1s = ("%+.1f%%" % r1) if r1 is not None else "-"
        if it.get("plan_qty"):
            plan = "lagega %s (%d share)" % (inr(it["plan_cost"], 0), it["plan_qty"])
        else:
            plan = "⚠️ bahut mehnga"
        lines.append("%d. %s | RSI %.1f | %s | 1 saal %s\n    %s | MCap %s cr | %s%s" %
                     (i, it["symbol"], it["rsi"], inr(it["close"]), r1s, short_group(it["group"]),
                      num(it["mcap_cr"]), plan, STATUS_TAG.get(it["status"], "")))
    if mcap_unknown:
        lines.append("\nNote: in stock ka market cap nahi mil paaya, isliye list me nahi hain: " + ", ".join(mcap_unknown))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 1) SCAN - subah stock chunna
# ----------------------------------------------------------------------------
def run_scan(cfg, st, now):
    today = now.date()
    uni = load_universe(cfg, now)
    if not uni:
        warn_once(st, now, "⚠️ Aaj ki list nahi ban saki: NSE se stock ki list nahi mil rahi. Program 15 minute baad phir koshish karega.")
        return
    use_today = now.hour * 60 + now.minute >= 15 * 60 + 45
    daily = get_daily([u["ticker"] for u in uni])
    if len(daily) < 0.5 * len(uni):
        warn_once(st, now, "⚠️ Aaj ki list nahi ban saki: Yahoo Finance se price data nahi mil raha (%d/%d stock). "
                           "Program 15 minute baad phir koshish karega." % (len(daily), len(uni)))
        return
    survivors = []
    for u in uni:
        df = daily.get(u["ticker"])
        if df is None:
            continue
        df = df[df["Close"].notna()]
        if not use_today:
            df = df[[d < today for d in df.index.date]]
        if len(df) < cfg["rsi_period"] + 5:
            continue
        r = rsi_series(df["Close"], cfg["rsi_period"]).iloc[-1]
        if r != r:  # NaN
            continue
        last = df.iloc[-1]
        prev_close = float(df["Close"].iloc[-2])
        vol = float(last["Volume"]) if last["Volume"] == last["Volume"] else 0.0
        if r > cfg["max_rsi"] or vol < cfg["min_volume_shares"]:
            continue
        ret1y = one_year_return(df)
        if ret1y is None or ret1y <= cfg["min_return_1y_pct"]:
            continue
        close, high, low = float(last["Close"]), float(last["High"]), float(last["Low"])
        chg = (close / prev_close - 1) * 100 if prev_close else 0
        locked = False
        if abs(chg) >= cfg["circuit_move_pct"]:
            if chg > 0 and close >= high * 0.9999:
                locked = True  # upper circuit jaisa
            if chg < 0 and close <= low * 1.0001:
                locked = True  # lower circuit jaisa
        if locked:
            continue
        survivors.append((u, round(float(r), 1), close, vol, round(ret1y, 1)))
    items = []
    mcap_unknown = []
    for u, r, close, vol, ret1y in survivors:
        mc = get_mcap_cr(u["ticker"])
        if mc is None:
            mcap_unknown.append(u["symbol"])
            continue
        if mc < cfg["min_market_cap_cr"]:
            continue
        q = plan_qty(cfg, st["next_amount"], close)
        items.append({"symbol": u["symbol"], "ticker": u["ticker"], "name": u["name"], "group": u["group"],
                      "rsi": r, "ret_1y_pct": ret1y, "mcap_cr": round(mc), "close": round(close, 2),
                      "volume": int(vol), "plan_qty": q, "plan_cost": round(q * close, 2) if q else None,
                      "status": "pending" if q else "too_expensive"})
    items.sort(key=lambda x: x["rsi"])
    items = items[:cfg["list_max"]]

    old = st.get("today_list")
    if old and old.get("date") != str(today):
        st["list_history"].insert(0, old)
        st["list_history"] = st["list_history"][:30]
    st["today_list"] = {"date": str(today), "created_at": now.isoformat(timespec="seconds"),
                        "scanned": len(uni), "mcap_unknown": mcap_unknown, "items": items}
    st["daily"]["scan_done"] = str(today)
    st["daily"]["sel_counter"] = 0

    head = "📋 AAJ KI LIST - %s\n" % now.strftime("%d %b %Y")
    if not items:
        notify(head + "Aaj koi stock niyam par khara nahi utra. Aaj kuch nahi kharidenge.")
        return
    notify(format_list_messages(cfg, st, items, now.strftime("%d %b %Y"), mcap_unknown))
    notify(how_to_reply_text(cfg))


# ----------------------------------------------------------------------------
# aapka jawab (Telegram) padhna
# ----------------------------------------------------------------------------
def parse_reply(text):
    """(kind, numbers). kind = list / all / none / nums / help"""
    t = (text or "").strip().lower()
    if not t:
        return "help", []
    if t in ("list", "/list", "lists"):
        return "list", []
    if t in ("sab", "sabhi", "saare", "sare", "all", "/all"):
        return "all", []
    if t in ("0", "nahi", "nahin", "none", "no", "skip", "koi nahi", "kuch nahi", "kuchh nahi"):
        return "none", []
    t2 = re.sub(r"(\d+)\s*-\s*(\d+)",
                lambda m: " ".join(str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1))
                if 0 < int(m.group(1)) <= int(m.group(2)) <= 200 else m.group(0), t)
    rest = re.sub(r"\b(aur|and|or|number|no|nos|stock|stocks|ko)\b", " ", t2)
    rest = re.sub(r"[\d\s,.;:+&/#-]", "", rest)
    nums = [int(x) for x in re.findall(r"\d+", t2)]
    if rest or not nums:
        return "help", []
    nums = [n for n in dict.fromkeys(nums) if n > 0]
    if not nums:
        return "none", []
    return "nums", nums


def selected_count(tl):
    return sum(1 for it in tl["items"] if it["status"] == "selected")


def handle_reply(cfg, st, now, text):
    """Ek jawab sambhalna. True lauta agar kuch naya chuna gaya (kharid try karni hai)."""
    tl = st["today_list"]
    items = tl["items"]
    d = st["daily"]
    mins = now.hour * 60 + now.minute
    hh, mm = [int(x) for x in cfg["last_buy_time"].split(":")]
    kind, nums = parse_reply(text)
    if kind == "help":
        notify("Maaf kijiye, ye samajh nahi aaya. 😊\n\n" + how_to_reply_text(cfg))
        return False
    if kind == "list":
        notify(format_list_messages(cfg, st, items, now.strftime("%d %b %Y"), tl.get("mcap_unknown")))
        return False
    if d.get("holiday") == str(now.date()):
        notify("ℹ️ Aaj market band hai, isliye kuch nahi kharida ja sakta.")
        return False
    if mins >= hh * 60 + mm:
        notify("⏰ Aaj ke kharid ka samay (%s) nikal gaya. Kal ki list ka intezaar kijiye." % pm(cfg["last_buy_time"]))
        return False
    if kind == "none":
        notify("👍 Theek hai, aaj ke liye kuch naya nahi kharidenge (jo chune ja chuke hain wo kharide jayenge).")
        return False

    free = cfg["max_positions"] - len(st["positions"])
    if kind == "all":
        room = max(0, free - selected_count(tl))
        nums = []
        for i, it in enumerate(items, 1):
            if len(nums) >= room:
                break
            if it["status"] == "pending":
                nums.append(i)
        if not nums:
            notify("ℹ️ Abhi koi jagah khaali nahi hai (ya list me kharidne layak stock nahi bacha).")
            return False

    pre_open = mins < 9 * 60 + 15
    ok, problems = [], []
    for n in nums:
        if n < 1 or n > len(items):
            problems.append("%d: list me is number ka stock nahi hai (1 se %d tak)" % (n, len(items)))
            continue
        it = items[n - 1]
        s = it["status"]
        if s == "pending":
            d["sel_counter"] = d.get("sel_counter", 0) + 1
            it["status"] = "selected"
            it["sel_order"] = d["sel_counter"]
            it["selected_at"] = now.isoformat(timespec="seconds")
            it["pre_open"] = pre_open
            ok.append((n, it))
        elif s == "selected":
            problems.append("%d. %s: pehle se chuna hua hai" % (n, it["symbol"]))
        elif s == "bought":
            problems.append("%d. %s: pehle hi kharida ja chuka hai" % (n, it["symbol"]))
        elif s == "too_expensive":
            problems.append("%d. %s: bahut mehnga hai (1 share bhi target rakam se kaafi upar), isliye nahi kharid sakte" % (n, it["symbol"]))
        elif s == "already":
            problems.append("%d. %s: ye pehle se hamare paas hai" % (n, it["symbol"]))
        else:
            problems.append("%d. %s: abhi kharida nahi ja sakta (%s)" % (n, it["symbol"], s))

    msg = []
    if ok:
        msg.append("✅ Aapne %d stock chune:" % len(ok))
        for n, it in ok:
            msg.append("• %d. %s (%s)" % (n, it["symbol"], inr(it["close"])))
        waiting = max(0, selected_count(tl) - free)
        if pre_open:
            msg.append("\nKharid khule bhaav (9:15 ka bhaav) par hogi. Message 9:30 ke baad aayega.")
        else:
            msg.append("\nKharid abhi ke bhaav par, kuch hi minute me hogi.")
        msg.append("Abhi %d/%d stock hain, khaali jagah %d." % (len(st["positions"]), cfg["max_positions"], free))
        if waiting:
            msg.append("%d stock jagah khaali hone par (koi stock bikne par) kharide jayenge." % waiting)
        msg.append("Kharid ki rakam kam se kam %s." % inr(st["next_amount"], 0))
    if problems:
        msg.append(("\n" if ok else "") + "⚠️ Ye nahi ho paya:\n" + "\n".join("• " + p for p in problems))
    notify("\n".join(msg))
    return bool(ok)


def tg_updates(offset, wait):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    params = {"timeout": wait, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get("https://api.telegram.org/bot%s/getUpdates" % token, params=params, timeout=wait + 20)
        j = r.json()
        if j.get("ok"):
            return j.get("result", [])
        print("Telegram getUpdates error:", str(j)[:200])
    except Exception as e:  # noqa
        print("Telegram padhne me dikkat:", e)
    return None


def execute_buys_now(cfg, st, now):
    """Jawab milne ke turant baad, agar market khula hai to kharid karna."""
    d = st["daily"]
    today = str(now.date())
    mins = now.hour * 60 + now.minute
    hh, mm = [int(x) for x in cfg["last_buy_time"].split(":")]
    if d.get("holiday") == today or mins < 9 * 60 + 30 or mins >= hh * 60 + mm:
        return  # 9:30 se pehle ka kharid run_buy sambhalega
    if d.get("buy_done") != today:
        run_buy(cfg, st, now)
        return
    tl = st["today_list"]
    tickers = [it["ticker"] for it in tl["items"] if it["status"] == "selected"]
    if not tickers:
        return
    intra = get_intraday(tickers)
    bought, skipped = fill_slots(cfg, st, now, intra, open_mode=False)
    notify_buys(cfg, st, bought, skipped)


def run_poll(cfg, st, now, wait=40):
    """Telegram par naye jawab dekhna. True lauta agar state badli (save karna hai)."""
    today = str(now.date())
    mins = now.hour * 60 + now.minute
    tl = st.get("today_list")
    if (now.weekday() >= 5 or not tl or tl.get("date") != today or mins >= 15 * 60 + 45
            or not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()):
        time.sleep(wait)
        return False
    updates = tg_updates(st.get("tg_offset"), wait)
    if not updates:
        if updates is None:
            time.sleep(10)
        return False
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    try:
        created_ts = datetime.fromisoformat(tl["created_at"]).timestamp()
    except Exception:  # noqa
        created_ts = 0
    want_buy = False
    for u in updates:
        st["tg_offset"] = u["update_id"] + 1
        m = u.get("message")
        if not m or str(m.get("chat", {}).get("id")) != chat:
            continue
        if m.get("date", 0) < created_ts:
            continue  # list banne se pehle ka message
        txt = m.get("text")
        if txt is None:
            continue
        now2 = now_ist()
        try:
            if handle_reply(cfg, st, now2, txt):
                want_buy = True
        except Exception:  # noqa
            print(traceback.format_exc())
    if want_buy:
        try:
            execute_buys_now(cfg, st, now_ist())
        except Exception:  # noqa
            print(traceback.format_exc())
            notify("⚠️ Kharid me abhi dikkat aayi (data ya internet). Agle chakkar me phir koshish hogi.")
    return True


# ----------------------------------------------------------------------------
# kharidna
# ----------------------------------------------------------------------------
def _today_rows(df, today):
    return df[[d == today for d in df.index.date]]


def _price_for(df, today, use_open):
    """(price, bar_time, locked) - aaj ke 5-minute data se"""
    if df is None:
        return None, None, False
    rows = _today_rows(df, today)
    if len(rows) == 0:
        return None, None, False
    if use_open:
        r = rows.iloc[0]
        return float(r["Open"]), rows.index[0], False
    r = rows.iloc[-1]
    return float(r["Close"]), rows.index[-1], False


def _prev_close(df, today):
    older = df[[d < today for d in df.index.date]]
    if len(older) == 0:
        return None
    return float(older["Close"].iloc[-1])


def fill_slots(cfg, st, now, intra, open_mode):
    """Aapke chune stock kharidna (jitni jagah khaali ho). (kharide, chhode) lautata hai.
    open_mode=True: 9:15 se pehle chune gaye stock khule bhaav par; baaki abhi ke bhaav par."""
    tl = st.get("today_list")
    if not tl or tl.get("date") != str(now.date()):
        return [], []
    today = now.date()
    free = cfg["max_positions"] - len(st["positions"])
    held = {p["symbol"] for p in st["positions"]}
    bought, skipped = [], []
    sel = sorted([it for it in tl["items"] if it["status"] == "selected"], key=lambda x: x.get("sel_order", 0))
    for it in sel:
        if free <= 0:
            break
        if it["symbol"] in held:
            it["status"] = "already"
            continue
        df = intra.get(it["ticker"])
        use_open = bool(open_mode and it.get("pre_open"))
        px, ts, _ = _price_for(df, today, use_open)
        if px is None or px <= 0:
            continue  # data abhi nahi aaya, agli run me phir koshish
        if use_open and df is not None:
            rows = _today_rows(df, today)
            pc = _prev_close(df, today)
            r0 = rows.iloc[0]
            if pc and abs(px / pc - 1) * 100 >= cfg["circuit_move_pct"] and float(r0["High"]) == float(r0["Low"]):
                it["status"] = "locked"
                skipped.append((it, "circuit me hai"))
                continue
        amount = st["next_amount"]
        qty = plan_qty(cfg, amount, px)
        if qty is None:
            it["status"] = "too_expensive"
            skipped.append((it, "bahut mehnga (1 share %s, chahiye lagbhag %s)" % (inr(px), inr(amount, 0))))
            continue
        cost = round(qty * px, 2)
        ch = calc_charges(cfg, cost, "buy")
        if cost + ch > st["cash"]:
            it["status"] = "no_funds"
            skipped.append((it, "cash kam hai"))
            continue
        pos = {
            "symbol": it["symbol"], "ticker": it["ticker"], "name": it["name"], "group": it["group"],
            "mcap_cr": it.get("mcap_cr"), "rsi": it.get("rsi"), "ret_1y_pct": it.get("ret_1y_pct"),
            "buy_date": str(today), "buy_time": ts.isoformat(), "buy_price": round(px, 2), "qty": qty,
            "invested": cost, "buy_charges": ch,
            "target": round(px * (1 + cfg["take_profit_pct"] / 100), 2),
            "stop": round(px * (1 - cfg["stop_loss_pct"] / 100), 2),
            "last_price": round(px, 2), "last_checked": ts.isoformat(),
        }
        st["cash"] = round(st["cash"] - cost - ch, 2)
        st["positions"].append(pos)
        it["status"] = "bought"
        held.add(it["symbol"])
        free -= 1
        bought.append(pos)
    return bought, skipped


def notify_buys(cfg, st, bought, skipped=None):
    if not bought and not skipped:
        return
    lines = []
    if bought:
        lines.append("🟢 KHARIDA (%d stock)\n" % len(bought))
        for p in bought:
            lines.append("• %s (%s | MCap %s cr)\n  %d share x %s = %s\n  Target %s | Stop-loss %s" %
                         (p["symbol"], short_group(p["group"]), num(p.get("mcap_cr") or 0), p["qty"],
                          inr(p["buy_price"]), inr(p["invested"]), inr(p["target"]), inr(p["stop"])))
        lines.append("\nAbhi portfolio: %d/%d stock | Cash %s" %
                     (len(st["positions"]), cfg["max_positions"], inr(st["cash"])))
    for it, why in (skipped or []):
        lines.append(("\n" if not lines else "") + "⚠️ %s nahi kharida: %s" % (it["symbol"], why))
    notify("\n".join(lines))


def run_buy(cfg, st, now):
    today = now.date()
    d = st["daily"]
    mins = now.hour * 60 + now.minute
    idx = get_intraday(["^NSEI"]).get("^NSEI")
    market_open_today = idx is not None and len(_today_rows(idx, today)) > 0
    if not market_open_today:
        if mins >= 10 * 60 + 15:
            d["holiday"] = str(today)
            d["buy_done"] = str(today)
            notify("ℹ️ Aaj market band lagta hai (holiday). Aaj koi kharid-bikri nahi hogi.")
        else:
            print("Market data abhi nahi aaya, agli run me phir dekhenge.")
        return
    tl = st.get("today_list")
    if tl and tl.get("date") == str(today):
        tickers = [it["ticker"] for it in tl["items"] if it["status"] == "selected"]
        if tickers:
            intra = get_intraday(tickers)
            bought, skipped = fill_slots(cfg, st, now, intra, open_mode=True)
            notify_buys(cfg, st, bought, skipped)
    d["buy_done"] = str(today)


def slot_hint(cfg, st, now):
    tl = st.get("today_list")
    if not tl or tl.get("date") != str(now.date()):
        return
    hh, mm = [int(x) for x in cfg["last_buy_time"].split(":")]
    if now.hour * 60 + now.minute > hh * 60 + mm:
        return
    free = cfg["max_positions"] - len(st["positions"])
    waiting = selected_count(tl)
    pending = sum(1 for it in tl["items"] if it["status"] == "pending")
    if free - waiting > 0 and pending > 0:
        notify("🔔 %d jagah khaali hai. Aaj ki list me se aur stock kharidna ho to number bhejiye "
               "(jaise: 4 7), ya 'list' likh kar list dekhiye. Kharid rakam kam se kam %s." %
               (free - waiting, inr(st["next_amount"], 0)))


# ----------------------------------------------------------------------------
# bechna
# ----------------------------------------------------------------------------
def do_sell(cfg, st, pos, price, ts, reason):
    today = ts.date()
    qty = pos["qty"]
    sell_amt = round(qty * price, 2)
    sch = calc_charges(cfg, sell_amt, "sell")
    total_cost = pos["invested"] + pos["buy_charges"]
    days = (today - datetime.fromisoformat(pos["buy_date"]).date()).days
    profit_after_charges = sell_amt - sch - total_cost
    tax = calc_tax(cfg, profit_after_charges, days)
    net_cash = round(sell_amt - sch - tax, 2)
    pl = round(net_cash - total_cost, 2)
    trade = {
        "symbol": pos["symbol"], "name": pos["name"], "group": pos["group"], "mcap_cr": pos.get("mcap_cr"),
        "buy_date": pos["buy_date"], "qty": qty, "buy_price": pos["buy_price"],
        "buy_amount": pos["invested"], "buy_charges": pos["buy_charges"], "total_cost": round(total_cost, 2),
        "sell_date": str(today), "sell_time": ts.isoformat(), "sell_price": round(price, 2),
        "sell_amount": sell_amt, "sell_charges": sch, "days_held": days, "tax": tax,
        "net_cash": net_cash, "profit_loss": pl,
        "profit_pct": round(100.0 * pl / total_cost, 2) if total_cost else 0.0,
        "reason": reason,
    }
    st["trades"].append(trade)
    st["positions"] = [p for p in st["positions"] if p["symbol"] != pos["symbol"]]
    st["cash"] = round(st["cash"] + net_cash, 2)
    old_amt = st["next_amount"]
    if reason == "target":
        st["next_amount"] = round(st["next_amount"] * (1 + cfg["increase_pct_after_profit_sale"] / 100), 2)
    return trade, old_amt


def notify_sell(cfg, st, tr, old_amt):
    why = ("🎯 %g%% profit pura hua" % cfg["take_profit_pct"]) if tr["reason"] == "target" else \
          ("🛑 %g%% loss (stop-loss) laga" % cfg["stop_loss_pct"])
    icon = "✅" if tr["profit_loss"] > 0 else "🔴"
    tot = sum(t["profit_loss"] for t in st["trades"])
    msg = ("%s BECHA: %s (%s | MCap %s cr)\n%s\n"
           "Kharid %s -> Bikri %s | %d share\n"
           "Kitne din rakha: %d\n"
           "Charges %s | Tax %s\n"
           "Final cash hath me: %s\n"
           "Is trade ka Profit/Loss: %s (%.2f%%)\n"
           "Total actual Profit/Loss: %s\n"
           "Abhi portfolio: %d/%d stock | Cash %s") % (
        icon, tr["symbol"], short_group(tr["group"]), num(tr.get("mcap_cr") or 0), why, inr(tr["buy_price"]), inr(tr["sell_price"]), tr["qty"],
        tr["days_held"], inr(tr["buy_charges"] + tr["sell_charges"]), inr(tr["tax"]), inr(tr["net_cash"]),
        inr(tr["profit_loss"]), tr["profit_pct"], inr(tot),
        len(st["positions"]), cfg["max_positions"], inr(st["cash"]))
    if tr["reason"] == "target":
        msg += "\nAgli kharid ki rakam: %s -> %s (%g%% badhi)" % (inr(old_amt), inr(st["next_amount"]),
                                                          cfg["increase_pct_after_profit_sale"])
    notify(msg)


def run_check(cfg, st, now):
    today = now.date()
    d = st["daily"]
    if d.get("holiday") == str(today):
        return
    # niyam badle ho to purane stock par bhi naya target/stop lagao; market cap ki kami poori karo
    for p in st["positions"]:
        p["target"] = round(p["buy_price"] * (1 + cfg["take_profit_pct"] / 100), 2)
        p["stop"] = round(p["buy_price"] * (1 - cfg["stop_loss_pct"] / 100), 2)
        if not p.get("mcap_cr"):
            try:
                mc = get_mcap_cr(p["ticker"])
                if mc:
                    p["mcap_cr"] = round(mc)
            except Exception:  # noqa
                pass
    tl = st.get("today_list")
    cand = []
    if tl and tl.get("date") == str(today):
        cand = [it["ticker"] for it in tl["items"] if it["status"] == "selected"]
    held_t = [p["ticker"] for p in st["positions"]]
    tickers = list(dict.fromkeys(held_t + cand))
    if not tickers:
        return
    intra = get_intraday(tickers)

    sells = []
    for pos in list(st["positions"]):
        df = intra.get(pos["ticker"])
        if df is None:
            continue
        last_dt = datetime.fromisoformat(pos["last_checked"])
        newbars = df[df.index > last_dt]
        if len(newbars) == 0:
            continue
        hit = None
        for ts, r in newbars.iterrows():
            o, h, l = float(r["Open"]), float(r["High"]), float(r["Low"])
            if l <= pos["stop"]:
                hit = ("stoploss", min(o, pos["stop"]), ts)  # gap-down me open bhaav
                break
            if h >= pos["target"]:
                hit = ("target", max(o, pos["target"]), ts)  # gap-up me open bhaav
                break
        pos["last_price"] = round(float(newbars["Close"].iloc[-1]), 2)
        pos["last_checked"] = newbars.index[-1].isoformat()
        if hit:
            sells.append((pos, hit))
    for pos, (reason, price, ts) in sells:
        tr, old_amt = do_sell(cfg, st, pos, price, ts, reason)
        notify_sell(cfg, st, tr, old_amt)

    # aapke chune hue stock (jo jagah na hone se ruke the) kharidna, market band hone se pehle tak
    hh, mm = [int(x) for x in cfg["last_buy_time"].split(":")]
    in_time = now.hour * 60 + now.minute <= hh * 60 + mm
    if in_time and d.get("buy_done") == str(today):
        if cfg["max_positions"] - len(st["positions"]) > 0 and cand:
            bought, skipped = fill_slots(cfg, st, now, intra, open_mode=False)
            notify_buys(cfg, st, bought, skipped)
    if sells and in_time:
        slot_hint(cfg, st, now)


# ----------------------------------------------------------------------------
# din ka final hisaab
# ----------------------------------------------------------------------------
def run_eod(cfg, st, now):
    today = str(now.date())
    s = compute_summary(cfg, st)
    snap = {"date": today, "total_actual_pl": s["total_actual_pl"], "unrealized_pl_approx": s["unrealized_pl_approx"],
            "cash": s["cash"], "portfolio_value": s["portfolio_value"], "open_positions": s["open_positions"]}
    st["snapshots"] = [x for x in st["snapshots"] if x["date"] != today] + [snap]
    st["snapshots"] = st["snapshots"][-400:]
    st["daily"]["eod_done"] = today


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def auto(cfg, st, now):
    today = str(now.date())
    d = st["daily"]
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        print("Aaj weekend hai, kuch nahi karna.")
        return
    if d.get("holiday") == today and d.get("eod_done") != today:
        if mins >= 15 * 60 + 45:
            run_eod(cfg, st, now)
        return
    if mins < 9 * 60 + 30:
        if d.get("scan_done") != today and mins >= 7 * 60:
            run_scan(cfg, st, now)
        return
    if d.get("scan_done") != today and mins < 15 * 60:
        run_scan(cfg, st, now)  # der se chali to bhi list banao
    if d.get("scan_done") == today and d.get("buy_done") != today and mins < 15 * 60:
        run_buy(cfg, st, now)
    if d.get("holiday") == today:
        return
    run_check(cfg, st, now)
    if mins >= 15 * 60 + 45 and d.get("eod_done") != today:
        run_eod(cfg, st, now)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "scan", "buy", "check", "eod", "poll", "test"])
    args = ap.parse_args()
    cfg = load_config()
    now = now_ist()
    print("Chal raha hai: mode=%s, samay=%s" % (args.mode, now.strftime("%Y-%m-%d %H:%M IST")))
    if args.mode == "test":
        notify("✅ Test message: Telegram connection sahi kaam kar raha hai. Paper trading robot taiyar hai.")
        flush_outbox()
        return
    st = load_state(cfg)
    if args.mode == "poll":
        # Telegram par aapka jawab dekho. State tabhi save hoti hai jab kuch naya aaya ho.
        try:
            changed = run_poll(cfg, st, now)
        except Exception:  # noqa
            print(traceback.format_exc())
            OUTBOX.clear()
            time.sleep(20)
            return
        if changed:
            save_state(cfg, st)
            flush_outbox()
        return
    try:
        if args.mode == "auto":
            auto(cfg, st, now)
        elif args.mode == "scan":
            run_scan(cfg, st, now)
        elif args.mode == "buy":
            run_buy(cfg, st, now)
        elif args.mode == "check":
            run_check(cfg, st, now)
        elif args.mode == "eod":
            run_eod(cfg, st, now)
    except Exception:  # noqa
        err = traceback.format_exc()
        print(err)
        OUTBOX.clear()
        st2 = load_state(cfg)
        if st2["daily"].get("error_notified") != str(now.date()):
            st2["daily"]["error_notified"] = str(now.date())
            save_state(cfg, st2)
            notify("⚠️ Program me ek dikkat aayi (data ya internet ki). Agli run me khud phir koshish hogi. "
                   "Agar roz aisa aaye to mujhe bataiye.\n\n" + err.strip().splitlines()[-1][:300])
            flush_outbox()
        return
    save_state(cfg, st)
    flush_outbox()


if __name__ == "__main__":
    main()
