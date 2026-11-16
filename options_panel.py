# -*- coding: utf-8 -*-
"""
期权持仓看板（常驻版）

用法：
  1. 登录 IB Gateway（模拟账户，端口 4002）
  2. 双击 start_panel.bat（或在 PowerShell 里运行  python options_panel.py）
  3. 浏览器会自动打开 http://127.0.0.1:8765 ，在页面里输入股票代码查询

程序窗口要一直开着，关掉窗口看板就停了。数据按“代码 + 日期”保存在 options_data 文件夹里。
"""
import sys, os, re, csv, json, math, time, queue, random, threading, asyncio, webbrowser, urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

WEB_PORT = 8765          # 看板网页的端口
IB_PORT = 4002           # IB Gateway 模拟账户 4002；实盘 4001；TWS 模拟 7497
CLIENT_ID = 11
MARKET_DATA_TYPE = 4     # 4 = 没有实时订阅时用延迟/收盘冻结数据
BATCH = 90               # 账户同时只能订阅约 100 张合约，所以分批读取
# K 线周期：(看板里的名字, IBKR 的周期, 读取多长的历史)
# 最后一项是备用的较短历史：较长的那次读取失败时改用它
BAR_SETS = [("15m", "15 mins", "60 D", "30 D"), ("30m", "30 mins", "120 D", "60 D"),
            ("1h", "1 hour", "1 Y", "120 D"), ("2h", "2 hours", "2 Y", "1 Y"), ("1d", "1 day", "5 Y", "2 Y")]
# 可以在看板里选来对比的波动率指数
INDEXES = ("VIX", "VXN", "RVX", "VXD", "VIX9D", "VIX3M")
DEMO = "--demo" in sys.argv   # 演示模式：不连接 IBKR，用模拟数据

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "options_data")
os.makedirs(DATA_DIR, exist_ok=True)

lock = threading.Lock()
jobs = queue.Queue()
state = {"symbol": None, "status": "idle", "msg": "", "done": 0, "total": 0, "error": ""}
cache = {}   # 代码 -> 当天的数据
index_cache = {}   # 指数名 -> (日期, 数据)


def set_state(**kw):
    with lock:
        state.update(kw)


def clean(x):
    """把空值、nan、-1（表示没有报价）统一成 None。"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x) or x < 0:
        return None
    return x


def cache_path(symbol):
    return os.path.join(DATA_DIR, f"{symbol.replace(' ', '_')}_{date.today():%Y%m%d}.json")


def get_cached(symbol, days=0):
    """返回今天已读取的数据；没有或范围不够时返回 None。"""
    with lock:
        d = cache.get(symbol)
    if d is None and os.path.exists(cache_path(symbol)):
        try:
            with open(cache_path(symbol), encoding="utf-8") as f:
                d = json.load(f)
            with lock:
                cache[symbol] = d
        except (OSError, ValueError):
            d = None
    if d is not None and d.get("v") != 4:
        return None
    if d is not None and d.get("generated", "")[:10] != date.today().isoformat():
        return None
    if d is not None and d.get("max_days", 0) < days:
        return None
    return d


def index_path(name):
    return os.path.join(DATA_DIR, f"index_{name}_{date.today():%Y%m%d}.json")


def cached_index(name):
    with lock:
        d = index_cache.get(name)
    if d and d[0] == date.today():
        return d[1]
    if os.path.exists(index_path(name)):
        try:
            with open(index_path(name), encoding="utf-8") as f:
                rows = json.load(f)
            with lock:
                index_cache[name] = (date.today(), rows)
            return rows
        except (OSError, ValueError):
            pass
    return None


def save_index(name, rows):
    with lock:
        index_cache[name] = (date.today(), rows)
    with open(index_path(name), "w", encoding="utf-8") as f:
        json.dump(rows, f)


def fetch_index_cboe(name):
    """从 Cboe 官网下载波动率指数的每日历史。返回 [[日期, 数值(小数)], ...]。"""
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode("utf-8", "replace")
    rows = []
    for row in csv.DictReader(text.splitlines()):
        row = {k.strip().upper(): v for k, v in row.items() if k}
        try:
            d = row["DATE"].strip()
            if "/" in d:
                m, dd, y = d.split("/")
                d = f"{y}-{int(m):02d}-{int(dd):02d}"
            rows.append([d, float(row["CLOSE"]) / 100])
        except (KeyError, ValueError):
            continue
    if len(rows) < 100:
        raise RuntimeError("unexpected file format")
    return rows[-1400:]


def fetch_index_ib(ib, name):
    """备用：从 IBKR 读取波动率指数的每日历史（可能需要指数行情订阅）。"""
    from ib_async import Index
    c = Index(name, "CBOE", "USD")
    ib.qualifyContracts(c)
    if not c.conId:
        raise RuntimeError("index not found")
    bars = ib.reqHistoricalData(c, endDateTime="", durationStr="5 Y", barSizeSetting="1 day",
                                whatToShow="TRADES", useRTH=True)
    if len(bars) < 100:
        raise RuntimeError("no data")
    return [[str(b.date)[:10], b.close / 100] for b in bars]


def demo_index(name):
    rnd, rows, d, i = random.Random(name), [], date.today() - timedelta(days=1900), 0
    base = {"VIX": 0.18, "VXN": 0.23, "RVX": 0.24, "VXD": 0.16, "VIX9D": 0.17, "VIX3M": 0.20}[name]
    while d <= date.today():
        if d.weekday() < 5:
            rows.append([d.isoformat(), round(base + 0.05 * math.sin(i / 45) + rnd.gauss(0, 0.008), 4)])
            i += 1
        d += timedelta(days=1)
    return rows


def hist_vol(closes):
    r = [math.log(b / a) for a, b in zip(closes[-31:-1], closes[-30:]) if a > 0 and b > 0]
    if len(r) < 5:
        return 0.3
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


# ---------------------------------------------------------------- 从 IBKR 读取

def fetch(ib, symbol, days):
    from ib_async import Stock, Option

    set_state(msg="正在查询 " + symbol + " 的行情和历史价格", done=0, total=0)
    stk = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(stk)
    if not stk.conId:
        raise RuntimeError("在 IBKR 里找不到代码 " + symbol + "，请检查拼写（目前只支持美股和 ETF）")

    t = ib.reqTickers(stk)[0]
    bar = lambda b: [str(b.date)[:16], b.open, b.high, b.low, b.close, clean(b.volume) or 0]
    bars = {}
    for n, (key, size, dur, shorter) in enumerate(BAR_SETS):
        set_state(msg=f"正在读取 K 线：{n + 1}/{len(BAR_SETS)}")
        got = []
        for d in (dur, shorter):
            try:
                got = ib.reqHistoricalData(stk, endDateTime="", durationStr=d, barSizeSetting=size,
                                           whatToShow="TRADES", useRTH=True)
            except ConnectionError:
                raise
            except Exception:
                got = []
            if got:
                break
        bars[key] = [bar(b) for b in got]
    daily = bars["1d"]
    set_state(msg="正在读取隐含波动率历史")
    try:
        ivb = ib.reqHistoricalData(stk, endDateTime="", durationStr="5 Y", barSizeSetting="1 day",
                                   whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True)
    except ConnectionError:
        raise
    except Exception:
        ivb = []
    ivmap = {str(b.date)[:10]: b.close for b in ivb if b.close and b.close > 0}
    if ivmap and sorted(ivmap.values())[len(ivmap) // 2] > 3:     # 数值是百分数时换成小数
        ivmap = {k: v / 100 for k, v in ivmap.items()}
    vol_hist = [[b[0][:10], b[4], ivmap.get(b[0][:10])] for b in daily]
    recent = next((bars[k][-1][4] for k in ("15m", "30m", "1h", "1d") if bars.get(k)), None)
    price = clean(t.marketPrice()) or clean(t.last) or recent or clean(t.close)
    if not price:
        raise RuntimeError("读不到 " + symbol + " 的价格，可能是行情暂时不可用，请稍后重试")
    vol = max(hist_vol([b[4] for b in daily]) * 1.2, 0.12)

    params = [p for p in ib.reqSecDefOptParams(symbol, "", "STK", stk.conId) if p.exchange == "SMART"]
    if not params:
        raise RuntimeError(symbol + " 没有可交易的期权")
    chain = max(params, key=lambda p: len(p.expirations))
    today = date.today()
    include_today = datetime.now().hour < 16
    expiries = []
    for e in sorted(chain.expirations):
        d = (date(int(e[:4]), int(e[4:6]), int(e[6:8])) - today).days
        if (d >= 0 if include_today else d > 0) and d <= days:
            expiries.append((e, d))
    if not expiries:
        raise RuntimeError(symbol + " 在未来 " + str(days) + " 天内没有到期的期权，请把读取范围调大")

    # 第一步：列出每个到期日的全部合约
    listed = {}
    for n, (e, d) in enumerate(expiries):
        set_state(msg=f"正在查找合约：到期日 {n + 1}/{len(expiries)}")
        cds = ib.reqContractDetails(Option(symbol, e, exchange="SMART", currency="USD",
                                           tradingClass=chain.tradingClass))
        listed[e] = [cd.contract for cd in cds]

    # 第二步：先读每个到期日平值附近的一对 Call 和 Put，得到市场对这个到期日的预期波动。
    # 财报前这个幅度会远大于历史波动，只按历史波动定范围会漏掉需要的行权价。
    implied = {}
    try:
        set_state(msg="正在读取各到期日的预期波动")
        probe = []
        for e, _ in expiries:
            ks = sorted({c.strike for c in listed[e]})
            if ks:
                k0 = min(ks, key=lambda k: abs(k - price))
                probe += [c for c in listed[e] if c.strike == k0]
        for i in range(0, len(probe), BATCH):
            tks = [ib.reqMktData(c, "") for c in probe[i:i + BATCH]]
            ib.sleep(2.5)
            for tk in tks:
                b, a = clean(tk.bid), clean(tk.ask)
                px = (a + b) / 2 if (a and b is not None and a >= b) else (clean(tk.last) or clean(tk.close))
                if px:
                    e = tk.contract.lastTradeDateOrContractMonth
                    implied[e] = implied.get(e, 0) + px
                ib.cancelMktData(tk.contract)
    except ConnectionError:
        raise
    except Exception:
        implied = {}

    # 第三步：按“历史波动”和“市场预期波动”里较大的那个确定每个到期日要读的行权价范围
    contracts = []
    for e, d in expiries:
        rng = 2.5 * vol * math.sqrt(max(d, 1) / 365)
        if implied.get(e):
            rng = max(rng, 2.7 * implied[e] * 1.25 / price)     # 跨式价格 × 1.25 约为一倍预期波动
        rng = min(max(rng, 0.04), 0.6)
        cs = [c for c in listed[e] if abs(c.strike - price) <= price * rng]
        if d > 45:   # 远期合约只保留 5 的整数倍行权价，减少读取量
            thin = [c for c in cs if c.strike % 5 == 0]
            if len(thin) >= 20:
                cs = thin
        contracts += cs
    if not contracts:
        raise RuntimeError("没有找到 " + symbol + " 的期权合约")

    def oi_of(tk):
        return clean(tk.callOpenInterest if tk.contract.right == "C" else tk.putOpenInterest)

    rows = []
    total = len(contracts)
    for i in range(0, total, BATCH):
        set_state(msg="正在读取报价和持仓量", done=i, total=total)
        tickers = [ib.reqMktData(c, "101") for c in contracts[i:i + BATCH]]
        for step in range(12):
            ib.sleep(0.5)
            got = sum(1 for tk in tickers if oi_of(tk) is not None)
            if step >= 2 and got >= 0.95 * len(tickers):
                break
        for tk in tickers:
            c = tk.contract
            iv = clean(tk.modelGreeks.impliedVol) if tk.modelGreeks else None
            rows.append([c.lastTradeDateOrContractMonth, c.strike, c.right, oi_of(tk),
                         clean(tk.bid), clean(tk.ask), clean(tk.last), iv, clean(tk.volume)])
            ib.cancelMktData(c)

    return {"v": 4, "symbol": symbol, "price": price,
            "generated": datetime.now().isoformat(timespec="minutes"),
            "max_days": days, "bars": bars, "vol": vol_hist, "rows": rows}


# ---------------------------------------------------------------- 演示数据（不连接 IBKR）

def demo_fetch(symbol, days):
    rnd = random.Random(symbol)
    S = {"QQQ": 677.8, "SPY": 690.0, "NVDA": 185.0}.get(symbol, round(40 + rnd.random() * 400, 2))
    vol = 0.22 if symbol in ("QQQ", "SPY") else 0.45
    ncdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def bs(K, T, v, call):
        sq = math.sqrt(T)
        d1 = (math.log(S / K) + v * v / 2 * T) / (v * sq)
        d2 = d1 - v * sq
        return S * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - S * ncdf(-d1)

    def series(n, step, fmt, dt):
        out, p, now = [], S * 1.06, datetime.now()
        for i in range(n):
            o = p
            p = max(p * (1 + rnd.gauss(-0.0002, step)), 1)
            hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, step / 2))), min(o, p) * (1 - abs(rnd.gauss(0, step / 2)))
            out.append([(now - dt * (n - i)).strftime(fmt), o, hi, lo, p])
        k = S / out[-1][4]
        return [[b[0]] + [round(x * k, 2) for x in b[1:]] + [round(abs(rnd.gauss(1, 0.5)) * 2e6 * step / 0.004)]
                for b in out]

    today, rows, exps = date.today(), [], []
    for d in range(1, days + 1):
        dd = today + timedelta(days=d)
        if dd.weekday() < 5 and (d <= 10 or dd.weekday() == 4):
            exps.append((dd.strftime("%Y%m%d"), d))
    step = 1 if S < 800 else 5
    total = sum(1 for _ in exps) * 100
    for n, (e, d) in enumerate(exps):
        set_state(msg="正在读取报价和持仓量（演示数据）", done=n * 100, total=total)
        time.sleep(0.15)
        rng = min(max(2.5 * vol * math.sqrt(d / 365), 0.04), 0.6)
        k = math.ceil(S * (1 - rng) / step) * step
        while k <= S * (1 + rng):
            iv = vol - 0.0012 * (k - S) * (680 / S)
            for r in "CP":
                p = bs(k, d / 365, iv, r == "C")
                lump = 30000 if k % 10 == 0 else 9000 if k % 5 == 0 else 2500
                center = S * (1.03 if r == "C" else 0.97)
                oi = round(lump * math.exp(-((k - center) / (S * 0.035)) ** 2) * (3 if d % 7 == 6 else 1))
                rows.append([e, k, r, oi, round(max(p - 0.03, 0.01), 2), round(p + 0.03, 2), round(p, 2), iv, 100])
            k += step
    f = "%Y-%m-%d %H:%M"
    bars = {"15m": series(780, 0.002, f, timedelta(minutes=15)), "30m": series(780, 0.003, f, timedelta(minutes=30)),
            "1h": series(840, 0.004, f, timedelta(hours=1)), "2h": series(900, 0.006, f, timedelta(hours=2)),
            "1d": series(1260, 0.012, "%Y-%m-%d", timedelta(days=1))}
    vol_hist = [[b[0], b[4], round(vol + 0.06 * math.sin(i / 45) + rnd.gauss(0, 0.006), 4)]
                for i, b in enumerate(bars["1d"])]
    return {"v": 4, "symbol": symbol, "price": round(S * 0.995, 2),
            "generated": datetime.now().isoformat(timespec="minutes"), "max_days": days,
            "bars": bars, "vol": vol_hist, "rows": rows}


# ---------------------------------------------------------------- 后台线程：负责和 IBKR 通信

def worker():
    ib = None
    if not DEMO:
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_async import IB
        ib = IB()
    while True:
        try:
            job = jobs.get(timeout=0.2)
        except queue.Empty:
            if ib is not None and ib.isConnected():
                try:
                    ib.sleep(0.05)   # 空闲时保持和 IB Gateway 的连接
                except Exception:
                    pass
            continue
        if job[0] == "index":          # 从 IBKR 读取一个波动率指数的历史
            _, name, holder = job
            try:
                if not ib.isConnected():
                    ib.connect("127.0.0.1", IB_PORT, clientId=CLIENT_ID, readonly=True, timeout=10)
                    ib.reqMarketDataType(MARKET_DATA_TYPE)
                holder["rows"] = fetch_index_ib(ib, name)
                save_index(name, holder["rows"])
            except Exception as e:
                holder["error"] = str(e) or type(e).__name__
            holder["ev"].set()
            continue
        _, symbol, days = job
        try:
            if DEMO:
                data = demo_fetch(symbol, days)
            else:
                if not ib.isConnected():
                    set_state(msg="正在连接 IB Gateway")
                    try:
                        ib.connect("127.0.0.1", IB_PORT, clientId=CLIENT_ID, readonly=True, timeout=10)
                    except Exception:
                        raise RuntimeError("连不上 IB Gateway。请确认它已经登录、端口是 "
                                           + str(IB_PORT) + "，然后点“重新读取”")
                    ib.reqMarketDataType(MARKET_DATA_TYPE)
                data = fetch(ib, symbol, days)
            with lock:
                cache[symbol] = data
            with open(cache_path(symbol), "w", encoding="utf-8") as f:
                json.dump(data, f)
            set_state(status="ready", msg="", error="")
            print(time.strftime("%H:%M:%S"), symbol, "ok,", len(data["rows"]), "contracts")
        except Exception as e:
            msg = str(e) or type(e).__name__
            if isinstance(e, (ConnectionError, OSError, asyncio.CancelledError)) or "disconnect" in msg.lower():
                msg = "和 IB Gateway 的连接中断了（它可能在自动重启或维护）。请确认它已登录后点“重新读取”"
                try:
                    ib.disconnect()
                except Exception:
                    pass
            set_state(status="error", error=msg, msg="")
            print(time.strftime("%H:%M:%S"), symbol, "failed:", msg)


# ---------------------------------------------------------------- 网页服务

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json; charset=utf-8", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        symbol = re.sub(r"\s+", " ", q.get("symbol", "").upper().strip())
        if u.path == "/":
            return self.send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/sim":
            return self.send(SIM_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/state":
            with lock:
                return self.send(dict(state))
        if u.path == "/api/symbols":
            files = sorted((f for f in os.listdir(DATA_DIR) if f.endswith(".json") and not f.startswith("index_")),
                           key=lambda f: os.path.getmtime(os.path.join(DATA_DIR, f)), reverse=True)
            seen = []
            for f in files:
                s = f.rsplit("_", 1)[0].replace("_", " ")
                if s not in seen:
                    seen.append(s)
            return self.send(seen[:12])
        if u.path == "/api/index":
            name = q.get("name", "").upper()
            if name not in INDEXES:
                return self.send({"error": "不支持这个指数"})
            rows = cached_index(name)
            if rows is None:
                try:
                    rows = demo_index(name) if DEMO else fetch_index_cboe(name)
                except Exception:
                    rows = None
                if rows is None:            # Cboe 下载失败时改从 IBKR 读取
                    with lock:
                        busy = state["status"] == "loading"
                    if busy:
                        return self.send({"retry": True})
                    holder = {"ev": threading.Event()}
                    jobs.put(("index", name, holder))
                    if not holder["ev"].wait(60):
                        return self.send({"retry": True})
                    rows = holder.get("rows")
                    if not rows:
                        return self.send({"error": "读不到 " + name + " 的历史数据：Cboe 网站下载失败，IBKR 也没有返回"
                                                   "（IBKR 的指数数据可能需要行情订阅）。"})
                else:
                    save_index(name, rows)
            return self.send({"name": name, "rows": rows})
        if u.path == "/api/data":
            d = get_cached(symbol)
            return self.send(d if d else {"error": "还没有 " + symbol + " 的数据"})
        if u.path == "/api/load":
            if not re.fullmatch(r"[A-Z][A-Z0-9. ]{0,9}", symbol):
                return self.send({"status": "invalid", "error": "请输入正确的股票代码，例如 QQQ、SPY、NVDA"})
            try:
                days = min(max(int(q.get("days", 30)), 5), 400)
            except ValueError:
                days = 30
            if q.get("force") != "1" and get_cached(symbol, days):
                return self.send({"status": "ready"})
            with lock:
                if state["status"] == "loading":
                    return self.send({"status": "busy", "symbol": state["symbol"]})
                state.update(symbol=symbol, status="loading", msg="准备读取", done=0, total=0, error="")
            jobs.put(("chain", symbol, days))
            return self.send({"status": "loading"})
        self.send({"error": "not found"}, code=404)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>期权持仓看板</title>
<style>
:root{
  --bg:#eef0f3; --panel:#ffffff; --ink:#16202e; --muted:#5d6878; --line:#d5dae2;
  --accent:#1f5fbf; --accent-soft:#e3ecfa; --up:#0d7a4f; --down:#c0392b; --pain:#7a3fb0; --poc:#c98a00; --warn:#c9730a;
  --font:"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
  --num:Consolas,"SFMono-Regular",monospace;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#10151d; --panel:#19212c; --ink:#e6ebf2; --muted:#93a0b3; --line:#2c3746;
    --accent:#6fa4f5; --accent-soft:#1e2c44; --up:#3fc48d; --down:#f0776a; --pain:#b98be6; --poc:#e6b23c; --warn:#f0a94a; color-scheme:dark; }
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.5}
main{max-width:1360px;margin:0 auto;padding:20px 16px 48px;display:flex;flex-direction:column;gap:16px}
h1{font-size:22px;margin:0;font-weight:600}
h2{font-size:15px;margin:0;font-weight:600}
.sub{color:var(--muted);font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.grow{flex:1;min-width:240px}
input[type=text],input[type=date],select{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
input[type=text]{width:130px;font-family:var(--num);text-transform:uppercase}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.chip{padding:4px 10px;font-size:13px;font-family:var(--num)}
a.btn{text-decoration:none;display:inline-block}
.btn[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#status{font-size:13px;color:var(--muted)}
#status.error{color:var(--down)}
#bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;display:none}
#bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
.slider{display:flex;flex-direction:column;gap:2px}
.slider label{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);gap:12px}
.slider label b{color:var(--ink);font-family:var(--num);font-weight:600}
input[type=range]{width:100%;accent-color:var(--accent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.tile{border:1px solid var(--line);border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.tile.main{background:var(--accent-soft);border-color:transparent}
.tile .k{font-size:12px;color:var(--muted)}
.tile .v{font-family:var(--num);font-size:18px;font-weight:600}
.tile .v.sm{font-size:14px;font-family:var(--font)}
.tile .d{font-size:12px;color:var(--muted)}
.up{color:var(--up)} .down{color:var(--down)}
.chartwrap{position:relative}
canvas{width:100%;height:740px;display:block}
canvas.volcv{height:340px;cursor:crosshair}
.tipbox{position:absolute;top:8px;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:12px;display:none;box-shadow:0 2px 8px rgba(0,0,0,.15);white-space:nowrap}
.tipbox b{font-family:var(--num)}
p.sub{margin:0;max-width:100ch}
#tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:12px;display:none;box-shadow:0 2px 8px rgba(0,0,0,.15);white-space:nowrap;font-family:var(--num)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-1px;margin-right:5px}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:520px}
th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}
td{font-family:var(--num)}
th:first-child,td:first-child{text-align:left}
tr.sum td{font-weight:600;border-top:2px solid var(--line);font-family:var(--font)}
ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted);max-width:100ch}
#body{display:flex;flex-direction:column;gap:16px}
#body.empty{display:none}
</style>
</head>
<body>
<main>
  <section class="panel">
    <div class="row">
      <h1 id="title">期权持仓看板</h1>
      <a class="btn chip" href="/sim">打开期权模拟器</a>
      <span class="grow"></span>
      <label class="sub" for="sym">股票代码</label>
      <input id="sym" type="text" value="QQQ" maxlength="10" autocomplete="off" spellcheck="false">
      <label class="sub" for="days">读取范围</label>
      <select id="days">
        <option value="30">未来 30 天内到期</option>
        <option value="60">未来 60 天内到期</option>
        <option value="90">未来 90 天内到期</option>
        <option value="180">未来 180 天内到期</option>
      </select>
      <button class="btn primary" id="go" type="button">查询</button>
      <button class="btn" id="reload" type="button">重新读取</button>
    </div>
    <div class="row" id="chips"></div>
    <div id="status">正在启动…</div>
    <div id="bar"><i></i></div>
  </section>

  <div id="body" class="empty">
  <section class="panel">
    <h2>期权到期日的时间范围</h2>
    <div class="row">
      <button class="btn chip" type="button" data-d="7">一周内</button>
      <button class="btn chip" type="button" data-d="14">两周内</button>
      <button class="btn chip" type="button" data-d="30">一个月内</button>
      <button class="btn chip" type="button" data-d="90">三个月内</button>
      <button class="btn chip" type="button" data-d="9999">全部</button>
      <button class="btn chip" type="button" id="only">只看结束日这一天</button>
    </div>
    <div class="slider"><label for="e0">开始：<b id="e0-out"></b></label><input id="e0" type="range" min="0" max="0" step="1" value="0"></div>
    <div class="slider"><label for="e1">结束：<b id="e1-out"></b></label><input id="e1" type="range" min="0" max="0" step="1" value="0"></div>
    <div class="sub" id="range-note"></div>
  </section>

  <section class="panel">
    <h2>期权价格推算的波动范围（从现在到结束日）</h2>
    <div class="tiles" id="t-move"></div>
  </section>

  <section class="panel">
    <h2>持仓分布的关键价位（所选时间范围内到期的期权）</h2>
    <div class="tiles" id="t-oi"></div>
  </section>

  <section class="panel">
    <div class="row">
      <h2>K 线与期权持仓分布</h2>
      <span class="grow"></span>
    </div>
    <div class="row">
      <span class="sub">K 线周期</span>
      <button class="btn chip" type="button" data-tf="15m">15 分钟</button>
      <button class="btn chip" type="button" data-tf="30m">30 分钟</button>
      <button class="btn chip" type="button" data-tf="1h" aria-pressed="true">1 小时</button>
      <button class="btn chip" type="button" data-tf="2h">2 小时</button>
      <button class="btn chip" type="button" data-tf="1d">1 天</button>
      <span class="grow"></span>
      <label class="sub" for="kstart">K 线起始日期</label>
      <input id="kstart" type="date">
      <button class="btn chip" type="button" data-q="5">近 5 天</button>
      <button class="btn chip" type="button" data-q="30">近 1 个月</button>
      <button class="btn chip" type="button" data-q="90">近 3 个月</button>
      <button class="btn chip" type="button" data-q="365">近 1 年</button>
      <button class="btn chip" type="button" id="tg-vp" aria-pressed="true">成交量分布</button>
    </div>
    <div class="sub" id="k-note"></div>
    <div class="chartwrap"><canvas id="cv"></canvas><div id="tip"></div></div>
    <div class="legend">
      <span><i style="background:var(--down)"></i>Put 持仓</span>
      <span><i style="background:var(--up)"></i>Call 持仓</span>
      <span><i style="background:var(--accent);opacity:.25"></i>预期波动范围（约 68% 概率）</span>
      <span><i style="background:var(--muted);opacity:.25"></i>价值区（70% 的持仓）</span>
      <span>在 K 线上滚动鼠标滚轮可以缩放；鼠标移到 K 线或横条上看明细</span>
    </div>
    <div class="sub">K 线下方的柱子是每根 K 线的成交量。K 线右侧的灰色横条是所显示这段 K 线的成交量按价位的分布，它会随 K 线的周期和起始日期变化；最长的那一条是这段时间成交最密集的价位，价格回到那里时容易遇到支撑或阻力。</div>
    <div class="sub">左边的 K 线是过去的价格，用上方的 K 线周期和起始日期调整。右边的持仓分布是今天还没到期的期权，用页面上方“期权到期日的时间范围”的两个滑块调整，改 K 线的时间不会改变它。
    </div>
  </section>

  <section class="panel">
    <h2>持仓最多的行权价</h2>
    <div class="tablewrap"><table><thead><tr><th>行权价</th><th>距参考价</th><th>Call 持仓</th><th>Put 持仓</th><th>合计</th><th>占比</th></tr></thead><tbody id="top"></tbody></table></div>
  </section>

  <section class="panel">
    <div class="row">
      <h2>隐含波动率和实际波动率</h2>
      <span class="grow"></span>
      <span class="sub">显示范围</span>
      <button class="btn chip" type="button" data-vr="126">近 6 个月</button>
      <button class="btn chip" type="button" data-vr="252" aria-pressed="true">近 1 年</button>
      <button class="btn chip" type="button" data-vr="504">近 2 年</button>
      <button class="btn chip" type="button" data-vr="99999">全部</button>
    </div>
    <div class="row">
      <span class="sub">显示哪些线</span>
      <button class="btn chip" type="button" id="tg-trail" aria-pressed="true">过去 21 个交易日的实际波动率</button>
      <button class="btn chip" type="button" id="tg-ahead" aria-pressed="true">之后 21 个交易日的实际波动率</button>
    </div>
    <div class="tiles" id="t-vol"></div>
    <div class="chartwrap"><canvas id="vc1" class="volcv"></canvas><div class="tipbox" id="vt1"></div></div>
    <div class="legend" id="vl1"></div>
    <p class="sub">蓝线是市场每天对未来一个月波动的预期。紫线是到当天为止最近 21 个交易日实际走出来的波动，每天当时就能算出来，可以直接和蓝线比较：紫线高于蓝线，说明最近的实际波动超过了市场的预期。橙线是每一天之后 21 个交易日的实际波动，是事后才知道的答案，所以最近 21 个交易日没有数值，只能用来回头检验当时的预期准不准。</p>
  </section>

  <section class="panel">
    <div class="row">
      <h2>和波动率指数对比</h2>
      <span class="grow"></span>
      <span class="sub">显示范围和上一张图相同</span>
    </div>
    <div class="row" id="idx-chips"></div>
    <div class="sub" id="idx-note"></div>
    <div class="tiles" id="t-idx"></div>
    <div class="chartwrap"><canvas id="vc2" class="volcv"></canvas><div class="tipbox" id="vt2"></div></div>
    <div class="legend" id="vl2"></div>
    <p class="sub">蓝线是所查代码自己的 30 天隐含波动率（IBKR 的算法）。VXN 对应纳指 100，VIX 对应标普 500，RVX 对应罗素 2000，VXD 对应道指；VIX9D 和 VIX3M 是标普 500 的 9 天期和 3 个月期。这些指数由 Cboe 计算，把下跌保护的溢价也算了进去，所以即使标的相同（比如 QQQ 和 VXN），指数的数值通常也比蓝线高，两者适合比较走势和差距的变化，不适合直接比较绝对数字。查个股时，蓝线比 VIX 高出多少，反映的是这只股票相对大盘的额外风险。</p>
  </section>

  <section class="panel">
    <h2>这些数字是怎么算的</h2>
    <ul>
      <li>参考价：用结束日那个到期日的平值 Call 和 Put 的价差反推出的标的价格。它和期权报价是同一时刻的，休市时比盘后的最新成交价更适合做基准。</li>
      <li>预期波动范围：平值跨式价格（平值 Call 加 Put）乘以 1.25，约等于一个标准差的波动幅度，到结束日价格落在这个范围内的概率约 68%；两倍是约 95%。</li>
      <li>偏斜后的范围：用相邻行权价的期权价差反推出市场给各个价位的概率，再取第 16 和第 84 百分位，能反映下跌和上涨不对称的情况。</li>
      <li>持仓分布只统计 95% 范围附近的行权价，离现价太远的持仓多是对冲或博极端行情的仓位，不计入。</li>
      <li>POC：合计持仓最多的行权价。价值区：从 POC 向两边扩展，直到包含 70% 的持仓。Call 墙和 Put 墙：95% 范围内，参考价上方 Call 持仓最多、下方 Put 持仓最多的行权价。最大痛点：让期权买方合计亏损最多的到期价格。</li>
      <li>实际波动率：每天收盘价涨跌幅（对数收益率）的标准差，乘以 √252 换算成年化。隐含波动率在过去一年的位置：过去 252 个交易日里，有多少天的隐含波动率低于今天。</li>
      <li>这些都只说明价位和幅度，不说明方向。持仓量每天只更新一次；没有实时行情订阅时，报价是延迟的，休市时是收盘时的冻结数据。</li>
    </ul>
  </section>
  </div>
</main>

<script>
// 每行: [到期日, 行权价, C/P, 持仓量, 买价, 卖价, 最新价, IV, 成交量]

/*CALC-START*/
function mid(r, strict){
  const b=r[4], a=r[5], l=r[6];
  if(b!=null && a!=null && a>=b && a>0) return (a+b)/2;
  return (!strict && l!=null) ? l : null;   // 没有买卖报价时，宽松模式才用最新成交价
}
function profile(rows, lo, hi){
  const c={}, p={};
  for(const r of rows){
    if(r[3]==null || r[1]<lo || r[1]>hi) continue;
    const t=r[2]==='C'?c:p; t[r[1]]=(t[r[1]]||0)+r[3];
  }
  const ks=[...new Set([...Object.keys(c),...Object.keys(p)].map(Number))].sort((a,b)=>a-b);
  return {ks:ks, call:ks.map(k=>c[k]||0), put:ks.map(k=>p[k]||0)};
}
function levels(pf, ref, wlo, whi){
  const ks=pf.ks, call=pf.call, put=pf.put, n=ks.length;
  if(!n) return null;
  const tot=ks.map((k,i)=>call[i]+put[i]);
  const sum=tot.reduce((a,b)=>a+b,0);
  if(!sum) return null;
  let pi=0; for(let i=1;i<n;i++) if(tot[i]>tot[pi]) pi=i;
  let lo=pi, hi=pi, acc=tot[pi];
  while(acc<0.7*sum && (lo>0 || hi<n-1)){
    const up=hi<n-1?tot[hi+1]:-1, dn=lo>0?tot[lo-1]:-1;
    if(up>=dn){ hi++; acc+=up; } else { lo--; acc+=dn; }
  }
  let cw=-1, pw=-1;
  for(let i=0;i<n;i++){
    if(ks[i]>=ref && ks[i]<=whi && (cw<0 || call[i]>call[cw])) cw=i;
    if(ks[i]<=ref && ks[i]>=wlo && (pw<0 || put[i]>put[pw])) pw=i;
  }
  let mp=0, best=Infinity;
  for(let j=0;j<n;j++){
    let s=0;
    for(let i=0;i<n;i++) s+=call[i]*Math.max(ks[j]-ks[i],0)+put[i]*Math.max(ks[i]-ks[j],0);
    if(s<best){ best=s; mp=j; }
  }
  return {poc:ks[pi], val:ks[lo], vah:ks[hi], callWall:cw>=0?ks[cw]:null, putWall:pw>=0?ks[pw]:null,
          maxPain:ks[mp], callTot:call.reduce((a,b)=>a+b,0), putTot:put.reduce((a,b)=>a+b,0), tot:tot, sum:sum};
}
function expected(rows){
  let C={}, P={}, both=[];
  for(const strict of [true,false]){     // 先只用有买卖报价的合约；不够再放宽
    C={}; P={};
    for(const r of rows){ const m=mid(r,strict); if(m==null) continue; (r[2]==='C'?C:P)[r[1]]=m; }
    both=Object.keys(C).map(Number).filter(k=>P[k]!=null).sort((a,b)=>a-b);
    if(both.length>=3) break;
  }
  if(!both.length) return null;
  let kA=both[0];
  for(const k of both) if(Math.abs(C[k]-P[k])<Math.abs(C[kA]-P[kA])) kA=k;
  const F=kA+C[kA]-P[kA], straddle=C[kA]+P[kA], move=straddle*1.2533;
  const all=[...new Set([...Object.keys(C),...Object.keys(P)].map(Number))].sort((a,b)=>a-b);
  const pts=[];
  for(let i=1;i<all.length-1;i++){
    const k=all[i], a=all[i-1], b=all[i+1]; let v=null;
    if(k<=F){ if(P[a]!=null && P[b]!=null) v=(P[b]-P[a])/(b-a); }
    else if(C[a]!=null && C[b]!=null) v=1+(C[b]-C[a])/(b-a);
    if(v!=null) pts.push([k, Math.min(1,Math.max(0,v))]);
  }
  const raw=pts.map(p=>p[1]);            // 先取相邻三点的中位数，去掉个别异常报价
  pts.forEach(function(p,i){ const w=[raw[Math.max(i-1,0)],raw[i],raw[Math.min(i+1,raw.length-1)]].sort((a,b)=>a-b); p[1]=w[1]; });
  let run=0; for(const p of pts){ run=Math.max(run,p[1]); p[1]=run; }
  const q=function(x){
    for(let i=1;i<pts.length;i++){
      if(pts[i-1][1]<x && pts[i][1]>=x){
        const k0=pts[i-1][0], c0=pts[i-1][1], k1=pts[i][0], c1=pts[i][1];
        return k0+(k1-k0)*(x-c0)/(c1-c0);
      }
    }
    return null;
  };
  const ivs=rows.filter(r=>r[1]===kA && r[7]!=null).map(r=>r[7]);
  const iv=ivs.length?ivs.reduce((a,b)=>a+b,0)/ivs.length:null;
  let lo16=q(0.16), hi84=q(0.84);
  if(lo16==null || hi84==null || !(lo16<F && F<hi84)){ lo16=null; hi84=null; }   // 结果不合理时不显示
  return {F:F, kA:kA, straddle:straddle, move:move, lo16:lo16, hi84:hi84, iv:iv};
}
function daysTo(exp, generated){
  const t=new Date(+exp.slice(0,4), +exp.slice(4,6)-1, +exp.slice(6,8), 16, 0);
  return Math.max((t-new Date(generated))/86400000, 0.02);
}
/*CALC-END*/

const $=id=>document.getElementById(id);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let D=null, expiries=[], byExp={}, state=null, geo=null, hover=null;
const TFN={'15m':'15 分钟','30m':'30 分钟','1h':'1 小时','2h':'2 小时','1d':'日'};
let tf='1h', viewN=140, curSymbol='', pendingForce=false, pollTimer=null;

const f2=x=>x.toFixed(2), f0=x=>Math.round(x).toLocaleString('en-US');
const fvol=v=>v>=1e9?(v/1e9).toFixed(2)+'B':v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(Math.round(v));
let showVP=true;
const pct=(v,ref)=>((v/ref-1)*100>=0?'+':'')+((v/ref-1)*100).toFixed(1)+'%';
const dstr=e=>e.slice(0,4)+'-'+e.slice(4,6)+'-'+e.slice(6,8);
const wk=e=>'日一二三四五六'[new Date(+e.slice(0,4),+e.slice(4,6)-1,+e.slice(6,8)).getDay()];
const dtxt=d=>d<1?(d*24).toFixed(1)+' 小时后':d.toFixed(1)+' 天后';
function tile(k,v,d,cls,main){ return '<div class="tile'+(main?' main':'')+'"><span class="k">'+k+'</span><span class="v '+(cls||'')+'">'+v+'</span><span class="d">'+(d||'')+'</span></div>'; }

/* ---------- 和看板程序通信 ---------- */
async function api(path){ const r=await fetch(path); return r.json(); }
function setStatus(kind,msg,done,total){
  const s=$('status'); s.className=kind==='error'?'error':''; s.textContent=msg;
  const show=kind==='loading';
  $('bar').style.display=show?'block':'none';
  $('bar').firstChild.style.width=(show && total?Math.round(done/total*100):0)+'%';
  $('go').disabled=$('reload').disabled=show;
}
async function load(force){
  const sym=$('sym').value.trim().toUpperCase().replace(/\s+/g,' ');
  if(!sym) return;
  curSymbol=sym; pendingForce=!!force;
  setStatus('loading','正在准备读取 '+sym+' …',0,0);
  let r;
  try{ r=await api('/api/load?symbol='+encodeURIComponent(sym)+'&days='+$('days').value+'&force='+(force?1:0)); }
  catch(e){ return setStatus('error','看板程序没有响应。请确认启动它的那个黑色窗口还开着，然后刷新这个页面。'); }
  if(r.status==='ready') return getData();
  if(r.status==='invalid') return setStatus('error',r.error);
  if(r.status==='loading') pendingForce=false;
  poll();
}
function poll(){
  clearTimeout(pollTimer);
  pollTimer=setTimeout(async function(){
    let s;
    try{ s=await api('/api/state'); }
    catch(e){ return setStatus('error','看板程序没有响应。请确认启动它的那个黑色窗口还开着，然后刷新这个页面。'); }
    if(s.status==='loading'){
      const p=s.total?'（'+s.done+' / '+s.total+' 张合约）':'';
      setStatus('loading',(s.symbol===curSymbol?'':'正在读取 '+s.symbol+'，完成后接着读取 '+curSymbol+'。')+s.symbol+'：'+s.msg+p,s.done,s.total);
      poll();
    }
    else if(s.symbol===curSymbol && s.status==='ready') getData();
    else if(s.symbol===curSymbol && s.status==='error') setStatus('error',s.error);
    else load(pendingForce);
  },1000);
}
async function getData(){
  const r=await api('/api/data?symbol='+encodeURIComponent(curSymbol));
  if(r.error) return setStatus('error',r.error);
  D=r; init();
}
async function chips(){
  let list=[]; try{ list=await api('/api/symbols'); }catch(e){}
  $('chips').innerHTML=list.length?'<span class="sub">最近查询</span>'+list.map(s=>'<button class="btn chip" type="button" data-s="'+s+'">'+s+'</button>').join(''):'';
}
function init(){
  expiries=[...new Set(D.rows.map(r=>r[0]))].sort();
  byExp={}; for(const r of D.rows){ (byExp[r[0]]=byExp[r[0]]||[]).push(r); }
  $('title').textContent=D.symbol+' 期权持仓看板'; document.title=D.symbol+' 期权持仓看板';
  const age=Math.round((Date.now()-new Date(D.generated))/60000);
  setStatus('ready','数据读取于 '+D.generated.replace('T',' ')+(age>=1?'（'+(age>=60?Math.floor(age/60)+' 小时 ':'')+age%60+' 分钟前）':'')+' · 最新价 $'+f2(D.price)+' · '+expiries.length+' 个到期日，'+D.rows.length+' 张合约 · 已读取未来 '+D.max_days+' 天内到期的期权。想更新数据点“重新读取”。');
  $('body').classList.remove('empty');
  if([...$('days').options].some(o=>+o.value===D.max_days)) $('days').value=D.max_days;
  $('e0').max=$('e1').max=Math.max(expiries.length-1,0);
  $('e0').value=0;
  let def=expiries.findIndex(e=>daysTo(e,D.generated)>=5); if(def<0) def=expiries.length-1;
  $('e1').value=def;
  setStart(daysAgo(30),true);
  try{ localStorage.setItem('opt_symbol',D.symbol); }catch(e){}
  chips(); render(); renderVol(); loadIndexes();
}

/* ---------- 计算和显示 ---------- */
function compute(){
  const a=+$('e0').value, b=+$('e1').value, i0=Math.min(a,b), i1=Math.max(a,b);
  const exp=expiries[i1], sel=expiries.slice(i0,i1+1);
  let rows=[]; for(const e of sel) rows=rows.concat(byExp[e]);
  const ex=expected(byExp[exp]), ref=ex?ex.F:D.price, days=daysTo(exp,D.generated);
  const lo=ex?ref-2.5*ex.move:-Infinity, hi=ex?ref+2.5*ex.move:Infinity;
  const pf=profile(rows,lo,hi), lv=levels(pf,ref,ex?ref-2*ex.move:-Infinity,ex?ref+2*ex.move:Infinity);
  state={i0:i0,i1:i1,exp:exp,sel:sel,ex:ex,ref:ref,pf:pf,lv:lv,days:days};
}
function render(){
  if(!D || !expiries.length) return;
  compute();
  const s=state, ex=s.ex, lv=s.lv, ref=s.ref;
  $('e0-out').textContent=dstr(expiries[s.i0])+'（周'+wk(expiries[s.i0])+'）';
  $('e1-out').textContent=dstr(s.exp)+'（周'+wk(s.exp)+'，'+dtxt(s.days)+'）';
  $('range-note').textContent='持仓分布统计 '+s.sel.length+' 个到期日；波动范围按结束日 '+dstr(s.exp)+' 计算。';
  let h='';
  if(ex){
    const m=ex.move;
    h+=tile('预期波动幅度（约 68% 概率）','±$'+f2(m)+'　±'+(m/ref*100).toFixed(1)+'%','到 '+dstr(s.exp)+' 收盘，价格大概率在 $'+f2(ref-m)+' 到 $'+f2(ref+m)+' 之间','',true);
    h+=tile('上方 / 下方（68%）','<span class="up">$'+f2(ref+m)+'</span> / <span class="down">$'+f2(ref-m)+'</span>','涨到这里是 '+pct(ref+m,ref)+'，跌到这里是 '+pct(ref-m,ref));
    h+=tile('更宽的范围（约 95% 概率）','$'+f2(ref-2*m)+' – $'+f2(ref+2*m),'两倍幅度，±'+(2*m/ref*100).toFixed(1)+'%，超出这个范围属于少见的大行情');
    if(ex.lo16!=null && ex.hi84!=null)
      h+=tile('考虑偏斜后的范围（68%）','<span class="down">'+pct(ex.lo16,ref)+'</span> / <span class="up">'+pct(ex.hi84,ref)+'</span>','$'+f2(ex.lo16)+' 到 $'+f2(ex.hi84)+'。下跌一侧通常更宽，因为下跌保护更贵');
    else h+=tile('考虑偏斜后的范围（68%）','数据不足','这个到期日的买卖报价不够完整，算出来的结果不可靠，所以不显示');
    h+=tile('平值跨式价格','$'+f2(ex.straddle),'行权价 '+ex.kA+' 的 Call 加 Put。买入跨式要波动超过这个数才赚钱');
    if(ex.iv!=null) h+=tile('平值隐含波动率',(ex.iv*100).toFixed(1)+'%','按它折算的幅度：±$'+f2(ref*ex.iv*Math.sqrt(s.days/365))+'，可以和上面的结果对照');
    else h+=tile('平值隐含波动率（按跨式价格反推）',(m/ref/Math.sqrt(s.days/365)*100).toFixed(1)+'%','IBKR 没有给出这个到期日的隐含波动率，这里用跨式价格换算成年化');
    h+=tile('参考价（期权反推的标的价格）','$'+f2(ex.F),'最新价 $'+f2(D.price)+(Math.abs(ex.F-D.price)/D.price>0.003?'，两者不同是因为取数时间不同，以参考价为准':''));
    h+=tile('平均每天的波动','±'+(m/ref*100/Math.sqrt(Math.max(s.days,1))).toFixed(2)+'%','把这段时间的幅度折算到单日');
  } else h=tile('预期波动幅度','无数据','结束日这个到期日没有可用的买卖报价');
  $('t-move').innerHTML=h;

  h='';
  if(lv){
    const inside=ref>=lv.val && ref<=lv.vah;
    h+=tile('POC（持仓最多的行权价）','$'+lv.poc,'距参考价 '+pct(lv.poc,ref)+'，这里聚集了 '+(lv.tot[s.pf.ks.indexOf(lv.poc)]/lv.sum*100).toFixed(1)+'% 的持仓','',true);
    h+=tile('价值区（70% 的持仓）','$'+lv.val+' – $'+lv.vah,'参考价'+(inside?'在价值区内':(ref<lv.val?'在价值区下方':'在价值区上方')));
    h+=tile('Call 墙（上方压力参考）',lv.callWall!=null?'<span class="up">$'+lv.callWall+'</span>':'无',lv.callWall!=null?'距参考价 '+pct(lv.callWall,ref):'');
    h+=tile('Put 墙（下方支撑参考）',lv.putWall!=null?'<span class="down">$'+lv.putWall+'</span>':'无',lv.putWall!=null?'距参考价 '+pct(lv.putWall,ref):'');
    h+=tile('最大痛点','$'+lv.maxPain,'距参考价 '+pct(lv.maxPain,ref));
    h+=tile('Put / Call 持仓比',lv.callTot?(lv.putTot/lv.callTot).toFixed(2):'无','Put '+f0(lv.putTot)+' 张，Call '+f0(lv.callTot)+' 张');
    if(ex && lv.callWall!=null && lv.putWall!=null){
      const up=ref+ex.move, dn=ref-ex.move;
      h+=tile('两堵墙和 68% 范围的关系',(lv.callWall<=up?'Call 墙在范围内':'Call 墙在范围外')+'，'+(lv.putWall>=dn?'Put 墙在范围内':'Put 墙在范围外'),'在范围内表示按市场预期的波动有机会碰到这个价位','sm');
    }
    h+=tile('统计的到期日',s.sel.length+' 个',dstr(s.sel[0])+(s.sel.length>1?' 到 '+dstr(s.sel[s.sel.length-1]):''));
  } else h=tile('持仓分布','无数据','所选范围内没有读到持仓量');
  $('t-oi').innerHTML=h;

  let t='';
  if(lv){
    const ks=s.pf.ks;
    const top=ks.map((k,i)=>i).sort((a,b)=>lv.tot[b]-lv.tot[a]).slice(0,8);
    let cs=0, ps=0, ts=0;
    top.forEach(i=>{
      cs+=s.pf.call[i]; ps+=s.pf.put[i]; ts+=lv.tot[i];
      t+='<tr><td>$'+ks[i]+'</td><td>'+pct(ks[i],ref)+'</td><td>'+f0(s.pf.call[i])+'</td><td>'+f0(s.pf.put[i])+'</td><td>'+f0(lv.tot[i])+'</td><td>'+(lv.tot[i]/lv.sum*100).toFixed(1)+'%</td></tr>';
    });
    t+='<tr class="sum"><td>以上 '+top.length+' 个行权价合计</td><td></td><td>'+f0(cs)+'</td><td>'+f0(ps)+'</td><td>'+f0(ts)+'</td><td>'+(ts/lv.sum*100).toFixed(1)+'%</td></tr>';
    t+='<tr class="sum"><td>统计范围内全部 '+ks.length+' 个行权价</td><td></td><td>'+f0(lv.callTot)+'</td><td>'+f0(lv.putTot)+'</td><td>'+f0(lv.sum)+'</td><td>100%</td></tr>';
  }
  $('top').innerHTML=t;
  draw();
}

function draw(){
  if(!state) return;
  const s=state, c=$('cv'), dpr=window.devicePixelRatio||1, W=c.clientWidth, H=c.clientHeight;
  c.width=Math.round(W*dpr); c.height=Math.round(H*dpr);
  const g=c.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const ks=s.pf.ks, lv=s.lv, ex=s.ex, ref=s.ref;
  const all=(D.bars||{})[tf]||[], bars=all.slice(-Math.min(viewN,all.length));
  const muted=css('--muted'), line=css('--line'), ink=css('--ink');
  let y0=Infinity, y1=-Infinity;
  bars.forEach(b=>{ y0=Math.min(y0,b[3]); y1=Math.max(y1,b[2]); });
  if(ex){ y0=Math.min(y0,ref-2.2*ex.move); y1=Math.max(y1,ref+2.2*ex.move); }
  else if(ks.length){ y0=Math.min(y0,ks[0]); y1=Math.max(y1,ks[ks.length-1]); }
  if(!isFinite(y0)){ y0=D.price*0.9; y1=D.price*1.1; }
  const padY=(y1-y0)*0.03; y0-=padY; y1+=padY;
  const hasVol=bars.some(b=>b[5]>0), volH=hasVol?78:0;
  const L=58, R=Math.min(215,W*0.24), T=24, B=30+(hasVol?volH+12:0), PW=W-L-R, PH=H-T-B;
  let volPoc=null;
  const hw=bars.length?PW*0.55:0, gap=bars.length?14:0, px0=L+hw+gap, pw=PW-hw-gap;
  const py=v=>T+PH-(v-y0)/(y1-y0)*PH;
  geo={py:py,y0:y0,y1:y1,T:T,PH:PH,px0:px0,pw:pw,L:L,hw:hw,bars:bars};

  g.font='11px '+css('--num'); g.textAlign='right'; g.textBaseline='middle'; g.lineWidth=1;
  const raw=(y1-y0)/8, p10=Math.pow(10,Math.floor(Math.log10(raw))), mm=raw/p10, st=(mm<1.5?1:mm<3.5?2:mm<7.5?5:10)*p10;
  for(let v=Math.ceil(y0/st)*st; v<=y1; v+=st){
    g.strokeStyle=line; g.beginPath(); g.moveTo(L,py(v)); g.lineTo(L+PW,py(v)); g.stroke();
    g.fillStyle=muted; g.fillText(v.toFixed(st<1?1:0),L-6,py(v));
  }
  const clip=(a,b)=>[Math.max(py(a),T),Math.min(py(b),T+PH)];
  if(lv){ const r=clip(lv.vah,lv.val); g.fillStyle=muted; g.globalAlpha=0.10; g.fillRect(px0,r[0],pw,r[1]-r[0]); g.globalAlpha=1; }
  if(ex){ const r=clip(ref+ex.move,ref-ex.move); g.fillStyle=css('--accent'); g.globalAlpha=0.13; g.fillRect(L,r[0],PW,r[1]-r[0]); g.globalAlpha=1; }

  // K 线
  if(bars.length){
    const n=bars.length, cw=hw/n, bw=Math.max(1,Math.min(cw*0.7,14));
    if(hasVol){
      // 所显示这段 K 线的成交量按价位的分布（每根 K 线的成交量平均摊到它的最低价和最高价之间）
      if(showVP){
        const nb=80, bin=(y1-y0)/nb, vp=new Array(nb).fill(0);
        bars.forEach(function(b){
          const a=Math.max(0,Math.min(nb-1,Math.floor((b[3]-y0)/bin))), z=Math.max(0,Math.min(nb-1,Math.floor((b[2]-y0)/bin)));
          for(let k=a;k<=z;k++) vp[k]+=(b[5]||0)/(z-a+1);
        });
        const mv=Math.max(...vp); let pk=0;
        if(mv>0){
          vp.forEach(function(v,k){ if(v>vp[pk]) pk=k; });
          vp.forEach(function(v,k){
            const w=v/mv*hw*0.3, yb=py(y0+(k+1)*bin);
            g.fillStyle=muted; g.globalAlpha=k===pk?0.42:0.2; g.fillRect(L+hw-w,yb,w,Math.max(1,py(y0+k*bin)-yb-0.5));
          });
          g.globalAlpha=1; volPoc=y0+(pk+0.5)*bin;
        }
      }
      // 每根 K 线的成交量
      const vmax=Math.max(...bars.map(b=>b[5]||0)), vy=T+PH+12;
      bars.forEach(function(b,i){
        const hgt=(b[5]||0)/vmax*volH;
        g.fillStyle=css(b[4]>=b[1]?'--up':'--down'); g.globalAlpha=0.6;
        g.fillRect(L+cw*(i+0.5)-bw/2,vy+volH-hgt,bw,hgt);
      });
      g.globalAlpha=1; g.strokeStyle=line; g.lineWidth=1;
      g.beginPath(); g.moveTo(L,vy+volH); g.lineTo(L+hw,vy+volH); g.stroke();
      g.fillStyle=muted; g.font='11px '+css('--num'); g.textAlign='right'; g.textBaseline='top';
      g.fillText(fvol(vmax),L-6,vy); g.textBaseline='bottom'; g.fillText('成交量',L-6,vy+volH);
    }
    bars.forEach((b,i)=>{
      const x=L+cw*(i+0.5), up=b[4]>=b[1], col=css(up?'--up':'--down');
      g.strokeStyle=col; g.fillStyle=col; g.lineWidth=1;
      g.beginPath(); g.moveTo(x,py(b[2])); g.lineTo(x,py(b[3])); g.stroke();
      const top=py(Math.max(b[1],b[4])), bot=py(Math.min(b[1],b[4]));
      g.fillRect(x-bw/2,top,bw,Math.max(1,bot-top));
    });
    g.fillStyle=muted; g.textAlign='center'; g.textBaseline='top'; g.font='11px '+css('--num');
    const step=Math.max(1,Math.floor(n/6));
    for(let i=0;i<n;i+=step) g.fillText(n>300 && tf==='1d'?bars[i][0].slice(0,7):bars[i][0].slice(5,10),L+cw*(i+0.5),H-22);
    g.textAlign='left'; g.font='12px '+css('--font');
    g.fillText(TFN[tf]+' K 线，'+bars[0][0].slice(0,10)+' 起，共 '+n+' 根',L+4,T-19);
    g.strokeStyle=line; g.beginPath(); g.moveTo(px0-gap/2,T); g.lineTo(px0-gap/2,T+PH); g.stroke();
  }

  // 持仓分布
  if(lv){
    let gapK=Infinity; for(let i=1;i<ks.length;i++) gapK=Math.min(gapK,ks[i]-ks[i-1]);
    if(!isFinite(gapK)) gapK=(y1-y0)/40;
    const bh=Math.max(1,Math.abs(py(0)-py(gapK))*0.8);
    const maxT=Math.max(...lv.tot), sx=v=>v/maxT*(pw-8);
    ks.forEach((k,i)=>{
      if(k<y0 || k>y1) return;
      const y=py(k)-bh/2;
      g.globalAlpha=0.8;
      g.fillStyle=css('--down'); g.fillRect(px0,y,sx(s.pf.put[i]),bh);
      g.fillStyle=css('--up'); g.fillRect(px0+sx(s.pf.put[i]),y,sx(s.pf.call[i]),bh);
      g.globalAlpha=1;
    });
    const pi=ks.indexOf(lv.poc);
    g.strokeStyle=css('--poc'); g.lineWidth=2; g.strokeRect(px0,py(lv.poc)-bh/2-1,sx(lv.tot[pi]),bh+2);
    g.strokeStyle=ink; g.lineWidth=1.1; g.globalAlpha=0.7; g.beginPath();
    ks.forEach((k,i)=>{ let a=0,n=0; for(let j=Math.max(0,i-2);j<=Math.min(ks.length-1,i+2);j++){a+=lv.tot[j];n++;} const x=px0+sx(a/n); if(i) g.lineTo(x,py(k)); else g.moveTo(x,py(k)); });
    g.stroke(); g.globalAlpha=1;
    g.fillStyle=muted; g.textAlign='left'; g.textBaseline='top'; g.font='12px '+css('--font');
    g.fillText('持仓分布：'+(s.sel.length>1?dstr(s.sel[0]).slice(5)+' 到 '+dstr(s.exp).slice(5):dstr(s.exp).slice(5))+' 到期的期权，最长的横条 = '+f0(maxT)+' 张',px0+4,T-19);
  } else {
    g.fillStyle=muted; g.textAlign='left'; g.font='13px '+css('--font'); g.fillText('所选范围内没有持仓数据',px0+8,T+20);
  }

  // 标注
  const labels=[];
  const mark=function(v,text,color,dash,full){
    if(v==null || v<y0 || v>y1) return;
    g.strokeStyle=color; g.lineWidth=1.3; g.setLineDash(dash); g.beginPath();
    g.moveTo(full?L:px0,py(v)); g.lineTo(L+PW,py(v)); g.stroke(); g.setLineDash([]);
    labels.push({y:py(v),text:text,color:color});
  };
  mark(D.price,'最新价 '+f2(D.price),ink,[4,3],true);
  if(volPoc!=null) mark(volPoc,'成交最密集的价位 '+f2(volPoc)+' ('+pct(volPoc,ref)+')',muted,[3,3],true);
  if(ex){
    if(Math.abs(ex.F-D.price)/D.price>0.003) mark(ex.F,'参考价 '+f2(ex.F),css('--accent'),[],true);
    mark(ref+ex.move,'上沿 68%  '+f2(ref+ex.move)+' ('+pct(ref+ex.move,ref)+')',css('--accent'),[6,3],true);
    mark(ref-ex.move,'下沿 68%  '+f2(ref-ex.move)+' ('+pct(ref-ex.move,ref)+')',css('--accent'),[6,3],true);
    mark(ref+2*ex.move,'上沿 95%  '+f2(ref+2*ex.move)+' ('+pct(ref+2*ex.move,ref)+')',css('--accent'),[2,4],true);
    mark(ref-2*ex.move,'下沿 95%  '+f2(ref-2*ex.move)+' ('+pct(ref-2*ex.move,ref)+')',css('--accent'),[2,4],true);
  }
  if(lv){
    mark(lv.callWall,'Call 墙 '+lv.callWall+' ('+pct(lv.callWall,ref)+')',css('--up'),[2,2]);
    mark(lv.putWall,'Put 墙 '+lv.putWall+' ('+pct(lv.putWall,ref)+')',css('--down'),[2,2]);
    mark(lv.maxPain,'最大痛点 '+lv.maxPain+' ('+pct(lv.maxPain,ref)+')',css('--pain'),[8,3,2,3]);
    mark(lv.poc,'POC '+lv.poc+' ('+pct(lv.poc,ref)+')',css('--poc'),[]);
    mark(lv.vah,'价值区上沿 '+lv.vah,muted,[1,3]);
    mark(lv.val,'价值区下沿 '+lv.val,muted,[1,3]);
  }
  labels.sort((a,b)=>a.y-b.y);
  for(let i=0;i<labels.length;i++){ labels[i].ly=labels[i].y; if(i && labels[i].ly<labels[i-1].ly+15) labels[i].ly=labels[i-1].ly+15; }
  for(let i=labels.length-1;i>=0;i--){ const maxY=T+PH+10-(labels.length-1-i)*15; if(labels[i].ly>maxY) labels[i].ly=maxY; }
  g.font='12px '+css('--font'); g.textAlign='left'; g.textBaseline='middle';
  labels.forEach(l=>{
    g.strokeStyle=l.color; g.lineWidth=1; g.beginPath(); g.moveTo(L+PW,l.y); g.lineTo(L+PW+8,l.ly); g.stroke();
    g.fillStyle=l.color; g.fillText(l.text,L+PW+11,l.ly);
  });
  if(hover){
    g.strokeStyle=muted; g.lineWidth=1; g.beginPath();
    if(hover.y!=null){ g.moveTo(L,hover.y); g.lineTo(L+PW,hover.y); }
    if(hover.x!=null){ g.moveTo(hover.x,T); g.lineTo(hover.x,T+PH); }
    g.stroke();
  }
}

$('cv').addEventListener('mousemove',function(e){
  if(!geo) return;
  const r=this.getBoundingClientRect(), y=e.clientY-r.top, x=e.clientX-r.left, tip=$('tip');
  if(y<geo.T || y>geo.T+geo.PH || x<geo.L){ hover=null; tip.style.display='none'; draw(); return; }
  let html='';
  if(x<geo.L+geo.hw && geo.bars.length){
    const n=geo.bars.length, i=Math.min(n-1,Math.max(0,Math.floor((x-geo.L)/(geo.hw/n)))), b=geo.bars[i];
    hover={x:geo.L+geo.hw/n*(i+0.5), y:y};
    const v=geo.y1-(y-geo.T)/geo.PH*(geo.y1-geo.y0);
    html=b[0]+'<br>开 '+f2(b[1])+'　高 '+f2(b[2])+'<br>低 '+f2(b[3])+'　收 '+f2(b[4])+(b[5]?'<br>成交量 '+fvol(b[5]):'')+'<br>光标价位 $'+f2(v)+'（'+pct(v,state.ref)+'）';
  } else if(state.lv){
    const v=geo.y1-(y-geo.T)/geo.PH*(geo.y1-geo.y0), ks=state.pf.ks, lv=state.lv;
    let bi=0; for(let i=1;i<ks.length;i++) if(Math.abs(ks[i]-v)<Math.abs(ks[bi]-v)) bi=i;
    hover={y:geo.py(ks[bi])};
    html='行权价 $'+ks[bi]+'（'+pct(ks[bi],state.ref)+'）<br>Call 持仓 '+f0(state.pf.call[bi])+'<br>Put 持仓 '+f0(state.pf.put[bi])+'<br>合计 '+f0(lv.tot[bi])+'，占 '+(lv.tot[bi]/lv.sum*100).toFixed(1)+'%';
  } else { hover=null; }
  draw();
  if(!html){ tip.style.display='none'; return; }
  tip.innerHTML=html; tip.style.display='block';
  tip.style.left=Math.min(Math.max(x+14,0),r.width-tip.offsetWidth-4)+'px';
  tip.style.top=Math.max(y-80,0)+'px';
});
$('cv').addEventListener('mouseleave',function(){ hover=null; $('tip').style.display='none'; draw(); });
$('cv').addEventListener('wheel',function(e){
  if(!geo || !D) return;
  const x=e.clientX-this.getBoundingClientRect().left;
  if(x>geo.L+geo.hw) return;
  e.preventDefault();
  const total=((D.bars||{})[tf]||[]).length;
  viewN=Math.round(Math.min(total,Math.max(10,Math.min(viewN,total)*(e.deltaY>0?1.15:0.87))));
  syncStart(); draw();
},{passive:false});

/* ---------- K 线周期和起始日期 ---------- */
function daysAgo(n){ const d=new Date(Date.now()-n*86400000); return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
function syncStart(){
  const a=(D.bars||{})[tf]||[];
  if(!a.length){ $('k-note').textContent='这个周期没有读到 K 线数据。'; return; }
  const n=Math.min(viewN,a.length), first=a[0][0].slice(0,10), last=a[a.length-1][0].slice(0,10);
  const d1=(D.bars['1d']||[]); $('kstart').min=d1.length?d1[0][0].slice(0,10):first; $('kstart').max=last; $('kstart').value=a[a.length-n][0].slice(0,10);
  $('k-note').textContent=TFN[tf]+' K 线已读取 '+first+' 到 '+last+' 的数据，起始日期可以在这个范围内任意选择。周期越短，能看的历史越短；选的时间超出范围时会自动换成更长的周期。';
}
function setStart(dateStr,nodraw){
  const a=(D.bars||{})[tf]||[];
  const n=a.filter(b=>b[0].slice(0,10)>=dateStr).length;
  viewN=Math.max(n,Math.min(10,a.length));
  syncStart(); if(!nodraw) draw();
}
document.querySelectorAll('[data-tf]').forEach(b=>b.addEventListener('click',function(){
  if(!D) return;
  const cur=$('kstart').value||daysAgo(30);
  tf=this.dataset.tf;
  document.querySelectorAll('[data-tf]').forEach(x=>x.setAttribute('aria-pressed',x===this));
  setStart(cur);
}));
function useTf(k){ tf=k; document.querySelectorAll('[data-tf]').forEach(x=>x.setAttribute('aria-pressed',x.dataset.tf===k)); }
// 当前周期的历史不够长时，换成能覆盖所选起始日期的最短周期
function fitTf(dateStr){
  const order=['15m','30m','1h','2h','1d'], bars=D.bars||{};
  const covers=k=>(bars[k]||[]).length && bars[k][0][0].slice(0,10)<=dateStr;
  if(covers(tf)) return;
  for(let i=order.indexOf(tf)+1;i<order.length;i++){ if(covers(order[i])){ useTf(order[i]); return; } }
  if((bars['1d']||[]).length) useTf('1d');
}
document.querySelectorAll('[data-q]').forEach(b=>b.addEventListener('click',function(){ if(!D) return; const d=daysAgo(+this.dataset.q); fitTf(d); setStart(d); }));
$('tg-vp').addEventListener('click',function(){ showVP=!showVP; this.setAttribute('aria-pressed',showVP); draw(); });
$('kstart').addEventListener('change',function(){ if(D && this.value){ fitTf(this.value); setStart(this.value); } });
document.querySelectorAll('[data-d]').forEach(b=>b.addEventListener('click',function(){
  if(!expiries.length) return;
  const d=+this.dataset.d; let last=0;
  expiries.forEach((e,i)=>{ if(daysTo(e,D.generated)<=d) last=i; });
  $('e0').value=0; $('e1').value=last; render();
}));
$('only').addEventListener('click',function(){ const i=Math.max(+$('e0').value,+$('e1').value); $('e0').value=i; $('e1').value=i; render(); });
$('e0').addEventListener('input',render);
$('e1').addEventListener('input',render);
$('go').addEventListener('click',()=>load(false));
$('reload').addEventListener('click',()=>load(true));
$('days').addEventListener('change',()=>load(false));
$('sym').addEventListener('keydown',e=>{ if(e.key==='Enter') load(false); });
$('chips').addEventListener('click',e=>{ const s=e.target.dataset && e.target.dataset.s; if(s){ $('sym').value=s; load(false); } });
window.addEventListener('resize',function(){ draw(); drawLines('vc1'); drawLines('vc2'); });
if(window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',function(){ draw(); drawLines('vc1'); drawLines('vc2'); });

/* ---------- 波动率图 ---------- */
const IDX={VIX:['VIX（标普 500）','--pain'],VXN:['VXN（纳指 100）','--warn'],RVX:['RVX（罗素 2000）','--up'],
           VXD:['VXD（道指）','--down'],VIX9D:['VIX9D（标普 9 天期）','--poc'],VIX3M:['VIX3M（标普 3 个月期）','--muted']};
let volN=252, showTrail=true, showAhead=true, idxOn=['VIX','VXN'], idxData={}, charts={};
try{ const s=JSON.parse(localStorage.getItem('opt_idx')||'null'); if(Array.isArray(s)) idxOn=s.filter(k=>IDX[k]); }catch(e){}

function rvSeries(close,n){
  const lr=[null]; for(let i=1;i<close.length;i++) lr.push(Math.log(close[i]/close[i-1]));
  const sd=function(a,b){ let m=0; for(let i=a;i<=b;i++) m+=lr[i]; m/=(b-a+1); let q=0; for(let i=a;i<=b;i++) q+=(lr[i]-m)*(lr[i]-m); return Math.sqrt(q/(b-a))*Math.sqrt(252); };
  return {ahead:close.map((_,t)=>t+n<close.length?sd(t+1,t+n):null), trail:close.map((_,t)=>t-n>=0?sd(t-n+1,t):null)};
}
function lastVal(a){ for(let i=a.length-1;i>=0;i--) if(a[i]!=null) return [a[i],i]; return [null,-1]; }
function drawLines(id){
  const ch=charts[id], c=$(id); if(!c) return;
  const dpr=window.devicePixelRatio||1, W=c.clientWidth, H=c.clientHeight;
  c.width=Math.round(W*dpr); c.height=Math.round(H*dpr);
  const g=c.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const muted=css('--muted');
  if(!ch){ g.fillStyle=muted; g.font='13px '+css('--font'); g.fillText('没有可显示的数据',16,30); return; }
  const N=ch.dates.length, a=Math.max(0,N-ch.n), n=N-a;
  let lo=Infinity, hi=-Infinity;
  ch.series.forEach(s=>{ for(let i=a;i<N;i++){ const v=s.v[i]; if(v!=null){ lo=Math.min(lo,v); hi=Math.max(hi,v); } } });
  if(!isFinite(lo)){ g.fillStyle=muted; g.font='13px '+css('--font'); g.fillText('这个范围内没有数据',16,30); ch.geo=null; return; }
  const pad=(hi-lo)*0.08||0.01; lo=Math.max(0,lo-pad); hi+=pad;
  const L=50, R=14, T=12, B=26;
  const px=i=>L+i/Math.max(n-1,1)*(W-L-R), py=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo));
  ch.geo={L:L,R:R,W:W,n:n,a:a};
  g.font='11px '+css('--num'); g.lineWidth=1; g.textAlign='right'; g.textBaseline='middle';
  for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4;
    g.strokeStyle=css('--line'); g.beginPath(); g.moveTo(L,py(v)); g.lineTo(W-R,py(v)); g.stroke();
    g.fillStyle=muted; g.fillText((v*100).toFixed(0)+'%',L-6,py(v)); }
  g.textBaseline='top';
  const step=Math.max(1,Math.floor(n/6));
  for(let i=0;i<n;i+=step){ g.textAlign=px(i)>W-R-30?'right':'center'; g.fillText(n>140?ch.dates[a+i].slice(0,7):ch.dates[a+i].slice(5,10),px(i),H-B+8); }
  ch.series.forEach(s=>{
    g.strokeStyle=css(s.color); g.lineWidth=s.color==='--accent'?2:1.5; g.beginPath(); let pen=false;
    for(let i=0;i<n;i++){ const v=s.v[a+i]; if(v==null){ pen=false; continue; } if(pen) g.lineTo(px(i),py(v)); else { g.moveTo(px(i),py(v)); pen=true; } }
    g.stroke();
  });
  if(ch.hover!=null){ g.strokeStyle=muted; g.lineWidth=1; g.beginPath(); g.moveTo(px(ch.hover),T); g.lineTo(px(ch.hover),H-B); g.stroke(); }
}
function bindHover(id,tipId){
  const c=$(id), tip=$(tipId);
  c.addEventListener('mousemove',function(e){
    const ch=charts[id]; if(!ch || !ch.geo) return;
    const r=c.getBoundingClientRect(), g=ch.geo, span=g.W-g.L-g.R;
    const i=Math.max(0,Math.min(g.n-1,Math.round((e.clientX-r.left-g.L)/span*(g.n-1)))), k=g.a+i;
    ch.hover=i; drawLines(id);
    let h='<b>'+ch.dates[k]+'</b>';
    ch.series.forEach(function(s){
      const v=s.v[k]; let nm=s.name;
      if(s.ahead && v!=null) nm+='（到 '+ch.dates[Math.min(k+s.ahead,ch.dates.length-1)]+'）';
      h+='<br>'+nm+'：<b>'+(v==null?(s.ahead?'还没走满 '+s.ahead+' 个交易日':'无数据'):(v*100).toFixed(1)+'%')+'</b>';
    });
    tip.innerHTML=h; tip.style.display='block';
    const x=g.L+i/Math.max(g.n-1,1)*span, w=tip.offsetWidth;
    tip.style.left=(x+14+w>r.width?Math.max(0,x-w-14):x+14)+'px';
  });
  c.addEventListener('mouseleave',function(){ const ch=charts[id]; if(ch){ ch.hover=null; drawLines(id); } tip.style.display='none'; });
}
function legendOf(series){ return series.map(s=>'<span><i style="background:var('+s.color+')"></i>'+s.name+'</span>').join('')+'<span>把鼠标移到图上看每一天的具体数值</span>'; }
function renderVol(){
  const v=(D && D.vol)||[], has=v.filter(r=>r[2]!=null).length;
  if(v.length<30 || has<30){
    charts.vc1=null; charts.vc2=null; drawLines('vc1'); drawLines('vc2');
    $('t-vol').innerHTML=tile('隐含波动率历史','无数据','IBKR 没有返回这个代码的隐含波动率历史');
    $('vl1').innerHTML=''; $('vl2').innerHTML=''; $('t-idx').innerHTML=''; return;
  }
  const dates=v.map(r=>r[0]), close=v.map(r=>r[1]), iv=v.map(r=>r[2]), rv=rvSeries(close,21);
  const s1=[{name:'隐含波动率（当天）',color:'--accent',v:iv}];
  if(showTrail) s1.push({name:'过去 21 个交易日的实际波动率',color:'--pain',v:rv.trail});
  if(showAhead) s1.push({name:'之后 21 个交易日的实际波动率',color:'--warn',v:rv.ahead,ahead:21});
  charts.vc1={dates:dates,series:s1,n:volN,hover:null}; drawLines('vc1'); $('vl1').innerHTML=legendOf(s1);

  const li=lastVal(iv), lt=lastVal(rv.trail), la=lastVal(rv.ahead);
  const yr=iv.slice(-252).filter(x=>x!=null), rank=yr.length?yr.filter(x=>x<li[0]).length/yr.length:null;
  let h=tile('现在的隐含波动率',(li[0]*100).toFixed(1)+'%','数据日期 '+dates[li[1]]+'，市场对未来一个月波动的预期','',true);
  if(lt[0]!=null){
    const d=(lt[0]-li[0])*100;
    h+=tile('过去 21 个交易日的实际波动率',(lt[0]*100).toFixed(1)+'%','截至 '+dates[lt[1]]);
    h+=tile('实际减去预期',(d>=0?'+':'')+d.toFixed(1)+' 个百分点',d>2?'最近的实际波动高于市场预期，看板的范围容易偏窄':(d<-2?'最近的实际波动低于市场预期，看板的范围偏宽':'最近的实际波动和市场预期接近'),d>2?'down':(d<-2?'up':''));
  }
  if(rank!=null) h+=tile('隐含波动率在过去一年的位置','高于 '+(rank*100).toFixed(0)+'% 的日子',rank>0.8?'处在一年里偏高的位置，期权偏贵':(rank<0.2?'处在一年里偏低的位置，期权偏便宜':'处在一年里的中间位置'));
  if(la[0]!=null) h+=tile('最近一个已知的“之后 21 日”数值',(la[0]*100).toFixed(1)+'%',dates[la[1]]+' 之后到 '+dates[Math.min(la[1]+21,dates.length-1)]+' 的实际波动，当时的隐含波动率是 '+(iv[la[1]]!=null?(iv[la[1]]*100).toFixed(1)+'%':'无数据'));
  $('t-vol').innerHTML=h;

  const s2=[{name:D.symbol+' 的隐含波动率',color:'--accent',v:iv}];
  let t='';
  idxOn.forEach(function(k){
    const m=idxData[k]; if(!m) return;
    let prev=null, miss=0;   // 指数个别日期缺数据时，沿用前一天的数值，最多 3 天
    const sv=dates.map(function(d){ if(m[d]!=null){ prev=m[d]; miss=0; return prev; } miss++; return miss<=3?prev:null; });
    s2.push({name:IDX[k][0],color:IDX[k][1],v:sv});
    const l=lastVal(sv);
    if(l[0]!=null && iv[l[1]]!=null){ const d=(l[0]-iv[l[1]])*100;
      t+=tile(IDX[k][0],(l[0]*100).toFixed(1)+'%',dates[l[1]]+'，比 '+D.symbol+' 的隐含波动率'+(d>=0?'高 ':'低 ')+Math.abs(d).toFixed(1)+' 个百分点'); }
  });
  charts.vc2={dates:dates,series:s2,n:volN,hover:null}; drawLines('vc2'); $('vl2').innerHTML=legendOf(s2); $('t-idx').innerHTML=t;
}
function idxChips(){
  $('idx-chips').innerHTML='<span class="sub">对比对象（可多选）</span>'+Object.keys(IDX).map(k=>'<button class="btn chip" type="button" data-i="'+k+'" aria-pressed="'+(idxOn.indexOf(k)>=0)+'">'+IDX[k][0]+'</button>').join('');
}
async function loadIndexes(){
  idxChips();
  let waiting=false; const errs=[];
  for(const k of idxOn){
    if(idxData[k]) continue;
    let r; try{ r=await api('/api/index?name='+k); }catch(e){ r={error:'看板程序没有响应'}; }
    if(r.rows){ const m={}; r.rows.forEach(x=>{ m[x[0]]=x[1]; }); idxData[k]=m; }
    else if(r.retry) waiting=true;
    else errs.push(r.error||('读不到 '+k));
  }
  $('idx-note').textContent=errs.join(' ')+(waiting?' 看板正在读取期权数据，指数稍后自动重试。':'');
  $('idx-note').className='sub'+(errs.length?' down':'');
  if(D) renderVol();
  if(waiting) setTimeout(loadIndexes,5000);
}
$('idx-chips').addEventListener('click',function(e){
  const k=e.target.dataset && e.target.dataset.i; if(!k) return;
  const i=idxOn.indexOf(k); if(i>=0) idxOn.splice(i,1); else idxOn.push(k);
  try{ localStorage.setItem('opt_idx',JSON.stringify(idxOn)); }catch(err){}
  loadIndexes();
});
document.querySelectorAll('[data-vr]').forEach(b=>b.addEventListener('click',function(){
  volN=+this.dataset.vr;
  document.querySelectorAll('[data-vr]').forEach(x=>x.setAttribute('aria-pressed',x===this));
  if(charts.vc1) charts.vc1.n=volN; if(charts.vc2) charts.vc2.n=volN; drawLines('vc1'); drawLines('vc2');
}));
$('tg-trail').addEventListener('click',function(){ showTrail=!showTrail; this.setAttribute('aria-pressed',showTrail); if(D) renderVol(); });
$('tg-ahead').addEventListener('click',function(){ showAhead=!showAhead; this.setAttribute('aria-pressed',showAhead); if(D) renderVol(); });
bindHover('vc1','vt1'); bindHover('vc2','vt2'); idxChips();

try{ const s=localStorage.getItem('opt_symbol'); if(s) $('sym').value=s; }catch(e){}
chips(); load(false);
</script>
</body>
</html>
"""


# 第二个页面：期权模拟器。样式和通用的计算函数取自看板页面，保证两边算法一致
SIM_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>期权模拟器</title>
<style>
__CSS__
canvas#pc{height:440px;cursor:crosshair}
.legs{display:flex;flex-direction:column;gap:6px;overflow-x:auto}
.leg{display:grid;grid-template-columns:76px 76px 110px 64px 150px 1fr 34px;gap:6px;align-items:center;min-width:720px}
.leg.head{font-size:12px;color:var(--muted)}
.leg select,.leg input{padding:6px 6px;width:100%;font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:6px}
.leg .px{font-family:var(--num);font-size:13px}
.x{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:6px;height:32px;cursor:pointer;font:inherit}
tr.pick{cursor:pointer}
tr.pick:hover td{background:var(--accent-soft)}
tr.cur td{background:var(--accent-soft);font-weight:600}
.warn{color:var(--down)}
.say{font-size:15px;max-width:95ch}
#combo{scroll-margin-top:12px;transition:box-shadow .6s, border-color .6s}
#combo.flash{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
</style>
</head>
<body>
<main>
  <section class="panel">
    <div class="row">
      <h1 id="title">期权模拟器</h1>
      <a class="btn chip" href="/">返回期权看板</a>
      <span class="grow"></span>
      <label class="sub" for="sym">股票代码</label>
      <select id="sym"></select>
      <label class="sub" for="exp">到期日</label>
      <select id="exp"></select>
      <span class="sub">成交价格按</span>
      <button class="btn chip" type="button" id="pm-ba" aria-pressed="true">买卖报价</button>
      <button class="btn chip" type="button" id="pm-mid" aria-pressed="false">中间价</button>
    </div>
    <div id="status" class="sub">正在读取…</div>
  </section>

  <div id="body" class="empty">
  <section class="panel">
    <h2>常见策略一览（点任意一行，下面的“当前组合”就换成这个策略）</h2>
    <div class="tablewrap"><table><thead><tr><th>策略</th><th>开仓</th><th>最大盈利</th><th>最大亏损</th><th>盈利概率</th><th>盈亏平衡点</th><th>怎样才赚钱</th></tr></thead><tbody id="tpl"></tbody></table></div>
    <p class="sub">每个策略的行权价按这个到期日的预期波动幅度自动选取：平值指最接近参考价的行权价，价外的腿放在大约一倍预期波动的位置。金额是一组（每条腿一张）的美元数。</p>
  </section>

  <section class="panel" id="combo">
    <h2 id="combo-title">当前组合</h2>
    <div class="legs" id="legs"></div>
    <div class="row"><button class="btn" id="add" type="button">加一条腿</button><span class="sub" id="leg-note"></span></div>
    <p class="say" id="say"></p>
    <div class="tiles" id="t-main"></div>
    <div class="tiles" id="t-more"></div>
  </section>

  <section class="panel">
    <h2>历史回测：这个组合的胜率准不准</h2>
    <p class="sub">把和当前组合结构相同的交易放到过去每一个交易日上：每条腿离参考价的距离按“几倍预期波动”保持不变，持有同样长的时间到期。预测只用当天已知的价格和隐含波动率，之后的真实价格只用来对答案。</p>
    <div class="row"><button class="btn primary" id="bt-run" type="button">用历史数据回测当前组合</button><span class="sub" id="bt-note"></span></div>
    <div class="tiles" id="bt-out"></div>
    <div class="tablewrap"><table><thead id="bt-head"></thead><tbody id="bt-years"></tbody></table></div>
    <p class="sub">回测里的期权价格是按当天的隐含波动率用公式算的理论价，所有行权价用同一个隐含波动率，没有计入买卖价差和手续费，行权价取理想位置，并假设每笔都持有到期。它检验的是胜率算法准不准，实际交易的平均盈亏会比这里差一些。</p>
  </section>

  <section class="panel">
    <h2>到期时和现在的盈亏</h2>
    <div class="chartwrap"><canvas id="pc"></canvas><div class="tipbox" id="ptip"></div></div>
    <div class="legend">
      <span><i style="background:var(--muted)"></i>到期时的盈亏（虚线）</span>
      <span><i style="background:var(--accent)"></i>假设情形下的盈亏</span>
      <span><i style="background:var(--up);opacity:.35"></i>赚钱的价位</span>
      <span><i style="background:var(--down);opacity:.35"></i>亏钱的价位</span>
      <span><i style="background:var(--accent);opacity:.2"></i>预期波动范围（约 68% 概率）</span>
      <span>把鼠标移到图上看每个价位的盈亏和到达概率</span>
    </div>
  </section>

  <section class="panel">
    <h2>开仓之后，假设发生了这些变化</h2>
    <div class="slider"><label for="w-s">标的价格变到 <b id="w-s-o"></b></label><input id="w-s" type="range" min="0" max="1" step="0.01" value="0"></div>
    <div class="slider"><label for="w-v">隐含波动率变化 <b id="w-v-o"></b></label><input id="w-v" type="range" min="-20" max="20" step="1" value="0"></div>
    <div class="slider"><label for="w-t">时间过去了 <b id="w-t-o"></b></label><input id="w-t" type="range" min="0" max="1" step="any" value="0"></div>
    <div class="tiles" id="t-what"></div>
  </section>

  <section class="panel">
    <h2>这些数字是怎么算的，哪些不能全信</h2>
    <ul>
      <li>价格用的是看板读取到的真实报价。默认按买卖报价算：买入的腿按卖价成交，卖出的腿按买价成交，这更接近实际能成交的价格。选“中间价”会更乐观。没有买卖报价的合约改用最新成交价，并会标出来。</li>
      <li>最大盈利、最大亏损、盈亏平衡点是按到期那天计算的，这三个数是确定的，不依赖任何假设。组合里有不同到期日的腿时，按最近的到期日计算，还没到期的腿按理论价估值。</li>
      <li>盈利概率（胜率）是按期权市场自己的定价算出来的：以参考价为中心，波动大小取这个到期日的预期波动幅度，统计到期时价格落在赚钱区间的概率。它不是对未来的预测，也不包含方向判断。</li>
      <li>胜率高不等于划算。卖方策略通常胜率高但赚得少、亏的时候亏得多；买方策略相反。“按市场定价的平均结果”把概率和金额合在一起看，它通常是一个小的负数，差额就是买卖价差的成本。</li>
      <li>回测显示这套概率在平时是准的，但隐含波动率偏高的时候实际波动往往更大，胜率会比这里显示的低。</li>
      <li>没有计入手续费、提前被行权和盘中触发止损的情况。休市时报价是收盘时的冻结数据，开盘后会变。这些都不是买卖建议。</li>
    </ul>
  </section>
  </div>
</main>

<script>
// 每行: [到期日, 行权价, C/P, 持仓量, 买价, 卖价, 最新价, IV, 成交量]
__CALC__

/*SIM-START*/
function ncdf(x){
  const t=1/(1+0.2316419*Math.abs(x)), d=0.3989422804014327*Math.exp(-x*x/2);
  const p=d*t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))));
  return x>=0?1-p:p;
}
// 以远期价格 F 计价的期权理论价；T 以年为单位
function black(F,K,T,v,call){
  if(T<=1e-9 || v<=0) return Math.max(call?F-K:K-F,0);
  const sq=Math.sqrt(T), d1=(Math.log(F/K)+v*v*T/2)/(v*sq), d2=d1-v*sq;
  return call?F*ncdf(d1)-K*ncdf(d2):K*ncdf(-d2)-F*ncdf(-d1);
}
function impliedVol(price,F,K,T,call){
  if(price==null || T<=1e-9 || price<=Math.max(call?F-K:K-F,0)+1e-6) return null;
  let lo=0.01, hi=5;
  if(black(F,K,T,hi,call)<price) return null;
  for(let i=0;i<60;i++){ const m=(lo+hi)/2; if(black(F,K,T,m,call)<price) lo=m; else hi=m; }
  return (lo+hi)/2;
}
function legPrice(r,side,useMid){
  const b=r[4], a=r[5], l=r[6], m=(b!=null && a!=null && a>=b && a>0)?(a+b)/2:null;
  if(useMid) return m!=null?m:l;
  if(side>0) return (a!=null && a>0)?a:(m!=null?m:l);
  return (b!=null && b>0)?b:(m!=null?m:l);
}
// legs: [{m: 方向×张数, call, K, price, iv, T(天)}]；F 参考价，sigT 到最近到期日的一倍波动（占参考价的比例），Tn 最近到期日的天数
function analyze(legs,F,sigT,Tn){
  const pnl=function(S){
    let v=0;
    for(const l of legs){
      if(l.stock){ v+=l.m*(S-l.price); continue; }
      const rem=(l.T-Tn)/365;
      v+=l.m*((rem<=1e-6?Math.max(l.call?S-l.K:l.K-S,0):black(S,l.K,rem,l.iv,l.call))-l.price);
    }
    return v*100;
  };
  const z=x=>(Math.log(x/F)+sigT*sigT/2)/sigT;
  const N=3000, lo=F*Math.exp(-6*sigT), hi=F*Math.exp(6*sigT);
  let pop=0, ev=0, mx=-Infinity, mn=Infinity, prevC=ncdf(z(lo));
  const mids=[], mass=[];
  for(let i=0;i<N;i++){
    const b=lo*Math.pow(hi/lo,(i+1)/N), a=lo*Math.pow(hi/lo,i/N), c=ncdf(z(b)), w=c-prevC, p=pnl((a+b)/2);
    prevC=c; mids.push(p); mass.push(w);
    if(p>0) pop+=w; ev+=w*p; if(p>mx) mx=p; if(p<mn) mn=p;
  }
  const ks=[...new Set(legs.filter(l=>!l.stock).map(l=>l.K))].sort((a,b)=>a-b);
  if(!ks.length) ks.push(F);
  [0.01].concat(ks,[ks[ks.length-1]*3]).forEach(function(s){ const p=pnl(s); if(p>mx) mx=p; if(p<mn) mn=p; });
  let netC=0; legs.forEach(l=>{ if(l.call || l.stock) netC+=l.m; });   // 股票和 Call 在大涨时都随价格上涨
  const upInf=netC>1e-9, dnInf=netC<-1e-9;
  let half=null;
  if(!dnInf && mn<0){ half=0; mids.forEach((p,i)=>{ if(p<=mn*0.5) half+=mass[i]; }); }
  // 盈亏平衡点和赚钱的区间：从接近 0 扫到足够高
  const top=Math.max(hi,ks[ks.length-1]*1.6), M=4000, be=[], zones=[];
  let px=0.01, pp=pnl(px), start=pp>0?0:null;
  for(let i=1;i<=M;i++){
    const x=0.01+(top-0.01)*i/M, p=pnl(x);
    if((pp<=0 && p>0) || (pp>0 && p<=0)){
      const r=px+(x-px)*(0-pp)/(p-pp); be.push(r);
      if(p>0) start=r; else { zones.push([start,r]); start=null; }
    }
    px=x; pp=p;
  }
  if(start!==null) zones.push([start,Infinity]);
  const cost=legs.reduce((a,l)=>a+l.m*l.price,0)*100;
  return {pnl:pnl, cost:cost, pop:pop, ev:ev, max:mx, min:mn, upInf:upInf, dnInf:dnInf, be:be, zones:zones, half:half};
}
// 历史检验：把同样结构的组合放到过去每一个交易日上。
// spec: [{m, stock, call, x}]，x 是行权价离参考价多少倍预期波动；N 持有的交易日数；vol: [[日期, 收盘价, 隐含波动率]]
function histTest(spec,N,vol){
  const rows=vol.filter(r=>r[1]>0 && r[2]>0), out={n:0,wins:0,pred:0,sum:0,sumW:0,sumL:0,worst:Infinity,best:-Infinity,years:{},indep:0,indepWins:0};
  for(let t=0;t+N<rows.length;t++){
    const S=rows[t][1], iv=rows[t][2], sig=iv*Math.sqrt(N/252), m=S*sig;
    const legs=spec.map(function(s){
      if(s.stock) return {m:s.m,stock:true,price:S,iv:0,T:N};
      const K=Math.max(S+s.x*m,S*0.02);
      return {m:s.m,call:s.call,K:K,price:black(S,K,N/252,iv,s.call),iv:iv,T:N};
    });
    const a=analyze(legs,S,sig,N), p=a.pnl(rows[t+N][1])/S, win=p>0, y=rows[t][0].slice(0,4);
    out.n++; out.pred+=a.pop; out.sum+=p; if(win){ out.wins++; out.sumW+=p; } else out.sumL+=p;
    if(p<out.worst) out.worst=p; if(p>out.best) out.best=p;
    const yy=out.years[y]=out.years[y]||{n:0,wins:0,sum:0}; yy.n++; yy.sum+=p; if(win) yy.wins++;
    if(t%N===0){ out.indep++; if(win) out.indepWins++; }
  }
  if(out.n){ out.first=rows[0][0]; out.last=rows[out.n-1][0]; }
  return out;
}
/*SIM-END*/

const $=id=>document.getElementById(id);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const f2=x=>x.toFixed(2);
const usd=x=>'$'+Math.abs(Math.round(x)).toLocaleString('en-US');
const sgn=x=>(x>=0?'+':'-')+usd(x);
const dstr=e=>e.slice(0,4)+'-'+e.slice(4,6)+'-'+e.slice(6,8);
const wk=e=>'日一二三四五六'[new Date(+e.slice(0,4),+e.slice(4,6)-1,+e.slice(6,8)).getDay()];
const pctOf=(v,ref)=>((v/ref-1)*100>=0?'+':'')+((v/ref-1)*100).toFixed(1)+'%';
function tile(k,v,d,cls,main){ return '<div class="tile'+(main?' main':'')+'"><span class="k">'+k+'</span><span class="v '+(cls||'')+'">'+v+'</span><span class="d">'+(d||'')+'</span></div>'; }

let D=null, expiries=[], byExp={}, info={}, look={}, legs=[], useMid=false, curTpl=null, R=null, geo=null, hoverX=null;

async function api(path){ const r=await fetch(path); return r.json(); }
async function start(){
  let list=[]; try{ list=await api('/api/symbols'); }catch(e){}
  let want=''; try{ want=localStorage.getItem('opt_symbol')||''; }catch(e){}
  if(!list.length){ $('status').textContent='还没有任何数据。请先回到期权看板查询一个股票代码。'; $('status').className='warn'; return; }
  if(list.indexOf(want)<0) want=list[0];
  $('sym').innerHTML=list.map(s=>'<option'+(s===want?' selected':'')+'>'+s+'</option>').join('');
  loadSymbol(want);
}
async function loadSymbol(sym){
  let r; try{ r=await api('/api/data?symbol='+encodeURIComponent(sym)); }catch(e){ r={error:'看板程序没有响应'}; }
  if(r.error || !r.rows){ $('status').textContent=(r.error||'没有数据')+'。请先回到期权看板查询这个代码（今天的数据才可用）。'; $('status').className='warn'; $('body').classList.add('empty'); return; }
  D=r; byExp={}; look={}; info={};
  for(const x of D.rows){ (byExp[x[0]]=byExp[x[0]]||[]).push(x); look[x[0]+'|'+x[1]+'|'+x[2]]=x; }
  expiries=Object.keys(byExp).sort().filter(function(e){
    const ex=expected(byExp[e]); if(!ex) return false;
    const T=daysTo(e,D.generated);
    info[e]={F:ex.F, move:ex.move, T:T, atm:ex.iv!=null?ex.iv:ex.move/ex.F/Math.sqrt(T/365), kA:ex.kA};
    return true;
  });
  if(!expiries.length){ $('status').textContent='这个代码没有可用的买卖报价，无法模拟。'; $('status').className='warn'; $('body').classList.add('empty'); return; }
  let def=expiries.findIndex(e=>info[e].T>=5); if(def<0) def=expiries.length-1;
  $('exp').innerHTML=expiries.map((e,i)=>'<option value="'+e+'"'+(i===def?' selected':'')+'>'+dstr(e)+'（周'+wk(e)+'，'+info[e].T.toFixed(1)+' 天后）</option>').join('');
  document.title=D.symbol+' 期权模拟器'; $('title').textContent=D.symbol+' 期权模拟器';
  $('body').classList.remove('empty'); $('status').className='sub';
  try{ localStorage.setItem('opt_symbol',D.symbol); }catch(e){}
  setExpiry();
}
function setExpiry(){
  const e=$('exp').value, I=info[e];
  $('status').textContent='数据读取于 '+D.generated.replace('T',' ')+' · 参考价 $'+f2(I.F)+' · 到 '+dstr(e)+' 的预期波动 ±$'+f2(I.move)+'（±'+(I.move/I.F*100).toFixed(1)+'%，约 68% 概率） · 没有实时订阅时为延迟或收盘冻结的报价';
  curTpl='condor'; legs=template('condor',e)||template('call',e)||[];
  renderTemplates(); drawLegs(); update(true);
}

/* ---------- 模板 ---------- */
function strikesOf(e,type){ return byExp[e].filter(x=>x[2]===type && legPrice(x,1,true)!=null).map(x=>x[1]).sort((a,b)=>a-b); }
function near(list,x){ let b=null; for(const k of list) if(b===null || Math.abs(k-x)<Math.abs(b-x)) b=k; return b; }
function further(list,k,dir){ const i=list.indexOf(k)+dir; return i>=0 && i<list.length?list[i]:null; }
const TPL=[['call','买入 Call'],['put','买入 Put'],['bullcall','牛市看涨价差（付钱买）'],['bearput','熊市看跌价差（付钱买）'],
  ['bullput','卖出看跌价差（收钱，看不跌）'],['bearcall','卖出看涨价差（收钱，看不涨）'],['straddle','买入跨式'],['strangle','买入宽跨式'],
  ['condor','铁鹰'],['ironfly','铁蝶'],['butterfly','蝶式'],['calendar','日历价差'],
  ['covered','备兑看涨（持有 100 股 + 卖出 Call）'],['protective','保护性看跌（持有 100 股 + 买入 Put）']];
function template(id,e){
  const I=info[e], C=strikesOf(e,'C'), P=strikesOf(e,'P'); if(!C.length || !P.length) return null;
  const F=I.F, m=I.move, L=(side,type,K,qty,exp)=>({side:side,type:type,K:K,qty:qty||1,exp:exp||e});
  const pick=function(list,x,avoid,dir){ let k=near(list,x); while(k!==null && avoid.indexOf(k)>=0) k=further(list,k,dir); return k; };
  const ac=near(C,F), ap=near(P,F);
  const c1=pick(C,F+m,[ac],1), c2=c1===null?null:pick(C,F+1.5*m,[ac,c1],1);
  const p1=pick(P,F-m,[ap],-1), p2=p1===null?null:pick(P,F-1.5*m,[ap,p1],-1);
  const cl=pick(C,F-m,[ac],-1);
  const ok=(...a)=>a.every(x=>x!==null && x!==undefined);
  switch(id){
    case 'call': return [L(1,'C',ac)];
    case 'put': return [L(1,'P',ap)];
    case 'bullcall': return ok(c1)?[L(1,'C',ac),L(-1,'C',c1)]:null;
    case 'bearput': return ok(p1)?[L(1,'P',ap),L(-1,'P',p1)]:null;
    case 'bullput': return ok(p1,p2)?[L(-1,'P',p1),L(1,'P',p2)]:null;
    case 'bearcall': return ok(c1,c2)?[L(-1,'C',c1),L(1,'C',c2)]:null;
    case 'straddle': return [L(1,'C',ac),L(1,'P',near(P,ac))];
    case 'strangle': return ok(c1,p1)?[L(1,'C',c1),L(1,'P',p1)]:null;
    case 'condor': return ok(c1,c2,p1,p2)?[L(-1,'P',p1),L(1,'P',p2),L(-1,'C',c1),L(1,'C',c2)]:null;
    case 'ironfly': return ok(c2,p2)?[L(-1,'C',ac),L(-1,'P',near(P,ac)),L(1,'C',c2),L(1,'P',p2)]:null;
    case 'butterfly': return ok(cl,c1)?[L(1,'C',cl),L(-1,'C',ac,2),L(1,'C',c1)]:null;
    case 'covered': return ok(c1)?[L(1,'S',null),L(-1,'C',c1)]:null;
    case 'protective': return ok(p1)?[L(1,'S',null),L(1,'P',p1)]:null;
    case 'calendar': { const i=expiries.indexOf(e), e2=expiries[i+1]; if(!e2) return null; const k2=near(strikesOf(e2,'C'),ac); return k2===ac?[L(-1,'C',ac),L(1,'C',ac,1,e2)]:null; }
  }
  return null;
}

/* ---------- 计算 ---------- */
function resolve(list){
  if(!list || !list.length) return null;
  const out=[]; let Tn=Infinity, en=null, noQuote=0;
  for(const l of list){
    if(l.type==='S'){ const J=info[l.exp]; if(!J) return null; out.push({m:l.side*l.qty,stock:true,K:null,price:J.F,iv:0,T:J.T}); continue; }
    const r=look[l.exp+'|'+l.K+'|'+l.type], I=info[l.exp]; if(!r || !I) return null;
    const price=legPrice(r,l.side,useMid); if(price==null) return null;
    if(!(r[4]!=null && r[5]!=null && r[5]>=r[4] && r[5]>0)) noQuote++;
    const call=l.type==='C';
    const iv=r[7]!=null?r[7]:(impliedVol(legPrice(r,1,true),I.F,l.K,I.T/365,call)||I.atm);
    out.push({m:l.side*l.qty, call:call, K:l.K, price:price, iv:iv, T:I.T, bid:r[4], ask:r[5]});
    if(I.T<Tn){ Tn=I.T; en=l.exp; }
  }
  if(en===null){ en=list[0].exp; Tn=info[en].T; }
  out.forEach(l=>{ if(l.stock) l.T=Tn; });
  const I=info[en];
  return {legs:out, F:I.F, sigT:I.move/I.F, move:I.move, Tn:Tn, exp:en, noQuote:noQuote};
}
function zoneText(a){
  if(!a.zones.length) return '到期时在任何价位都不赚钱';
  if(a.zones.length===1 && a.zones[0][0]===0 && a.zones[0][1]===Infinity) return '到期时在任何价位都赚钱';
  return '到期时价格'+a.zones.map(function(z){
    if(z[0]===0) return '低于 $'+f2(z[1]);
    if(z[1]===Infinity) return '高于 $'+f2(z[0]);
    return '在 $'+f2(z[0])+' 到 $'+f2(z[1])+' 之间';
  }).join('，或')+'才赚钱';
}
function renderTemplates(){
  const e=$('exp').value; let h='';
  TPL.forEach(function(t){
    const r=resolve(template(t[0],e));
    if(!r){ h+='<tr><td>'+t[1]+'</td><td colspan="6" style="text-align:left;font-family:var(--font);color:var(--muted)">这个到期日的行权价或报价不够，组不出来</td></tr>'; return; }
    const a=analyze(r.legs,r.F,r.sigT,r.Tn);
    h+='<tr class="pick'+(curTpl===t[0]?' cur':'')+'" data-t="'+t[0]+'"><td>'+t[1]+'</td><td>'+(a.cost>=0?'付出 ':'收入 ')+usd(a.cost)+'</td>'+
      '<td class="up">'+(a.upInf?'无上限':sgn(a.max))+'</td><td class="down">'+(a.dnInf?'无上限':sgn(a.min))+'</td>'+
      '<td>'+(a.pop*100).toFixed(0)+'%</td><td>'+(a.be.length?a.be.map(x=>'$'+f2(x)).join(' / '):'无')+'</td>'+
      '<td style="text-align:left;font-family:var(--font)">'+zoneText(a).replace('到期时','')+'</td></tr>';
  });
  $('tpl').innerHTML=h;
}
function drawLegs(){
  let h='<div class="leg head"><span>方向</span><span>类型</span><span>行权价</span><span>张数</span><span>到期日</span><span>成交价格</span><span></span></div>';
  legs.forEach(function(l,i){
    const tsel='<select id="lg-'+i+'-type" aria-label="类型"><option value="C"'+(l.type==='C'?' selected':'')+'>Call</option><option value="P"'+(l.type==='P'?' selected':'')+'>Put</option><option value="S"'+(l.type==='S'?' selected':'')+'>股票</option></select>';
    const ssel='<select id="lg-'+i+'-side" aria-label="方向"><option value="1"'+(l.side>0?' selected':'')+'>买入</option><option value="-1"'+(l.side<0?' selected':'')+'>卖出</option></select>';
    const rm='<button class="x" id="lg-'+i+'-rm" type="button" aria-label="删除这条腿"'+(legs.length<2?' disabled':'')+'>×</button>';
    if(l.type==='S'){
      h+='<div class="leg">'+ssel+tsel+'<select disabled aria-label="行权价"><option>不适用</option></select>'+
        '<input id="lg-'+i+'-qty" aria-label="张数" type="number" min="1" max="50" step="1" value="'+l.qty+'">'+
        '<select disabled aria-label="到期日"><option>不适用</option></select>'+
        '<span class="px">$'+f2(info[l.exp].F)+'　按参考价，1 张 = 100 股</span>'+rm+'</div>';
      return;
    }
    const ks=strikesOf(l.exp,l.type); if(ks.indexOf(l.K)<0) l.K=near(ks,l.K==null?info[l.exp].F:l.K);
    const r=look[l.exp+'|'+l.K+'|'+l.type], p=r?legPrice(r,l.side,useMid):null;
    const q=r && r[4]!=null && r[5]!=null && r[5]>=r[4] && r[5]>0;
    h+='<div class="leg">'+
      '<select id="lg-'+i+'-side" aria-label="方向"><option value="1"'+(l.side>0?' selected':'')+'>买入</option><option value="-1"'+(l.side<0?' selected':'')+'>卖出</option></select>'+
      '<select id="lg-'+i+'-type" aria-label="类型"><option value="C"'+(l.type==='C'?' selected':'')+'>Call</option><option value="P"'+(l.type==='P'?' selected':'')+'>Put</option></select>'+
      '<select id="lg-'+i+'-K" aria-label="行权价">'+ks.map(k=>'<option'+(k===l.K?' selected':'')+'>'+k+'</option>').join('')+'</select>'+
      '<input id="lg-'+i+'-qty" aria-label="张数" type="number" min="1" max="50" step="1" value="'+l.qty+'">'+
      '<select id="lg-'+i+'-exp" aria-label="到期日">'+expiries.map(e=>'<option value="'+e+'"'+(e===l.exp?' selected':'')+'>'+dstr(e)+'</option>').join('')+'</select>'+
      '<span class="px">'+(p==null?'<span class="warn">没有报价</span>':'$'+f2(p)+(q?'　买 '+f2(r[4])+' / 卖 '+f2(r[5]):'　<span class="warn">无买卖报价，用最新成交价</span>'))+'</span>'+
      '<button class="x" id="lg-'+i+'-rm" type="button" aria-label="删除这条腿"'+(legs.length<2?' disabled':'')+'>×</button></div>';
  });
  $('legs').innerHTML=h; $('add').disabled=legs.length>=6;
}
function update(resetWhat){
  const nm=TPL.find(t=>t[0]===curTpl);
  $('combo-title').textContent='当前组合：'+(nm?nm[1]:'自己修改过的组合');
  R=resolve(legs);
  if(!R){ $('say').textContent='这个组合里有合约没有报价，算不出来。换一个行权价或到期日试试。'; $('say').className='say warn'; $('t-main').innerHTML=''; $('t-more').innerHTML=''; $('t-what').innerHTML=''; geo=null; drawChart(); return; }
  const a=analyze(R.legs,R.F,R.sigT,R.Tn); R.a=a;
  $('say').className='say';
  $('say').textContent=zoneText(a)+'。按期权市场的定价，这种情况发生的概率约 '+(a.pop*100).toFixed(0)+'%。'+
    (a.dnInf?'注意：这个组合有裸卖的 Call，标的大涨时亏损没有上限。':'');
  $('leg-note').textContent=R.noQuote?('有 '+R.noQuote+' 条腿没有买卖报价，用的是最新成交价，结果可能不准。'):'';
  const rr=(!a.upInf && !a.dnInf && a.min<0)?a.max/Math.abs(a.min):null;
  const hasStock=R.legs.some(l=>l.stock);
  let h=tile(a.cost>=0?'开仓付出':'开仓收入',usd(a.cost),hasStock?'包含买卖股票的金额（按参考价，每张 100 股）':(a.cost>=0?'买方组合：先付钱，最多亏掉这笔钱':'卖方组合：先收钱，这笔钱就是最多能赚的'),'',true);
  h+=tile('盈利概率（胜率）',(a.pop*100).toFixed(0)+'%','到 '+dstr(R.exp)+' 收盘时赚钱的概率；亏钱的概率是 '+((1-a.pop)*100).toFixed(0)+'%','',true);
  h+=tile('最大盈利',a.upInf?'无上限':sgn(a.max),a.upInf?'标的涨得越多赚得越多':(a.cost>0?'相当于成本的 '+(a.max/a.cost*100).toFixed(0)+'%':''),'up');
  h+=tile('最大亏损',a.dnInf?'无上限':sgn(a.min),a.dnInf?'标的大涨时亏损没有上限':(a.half!=null?'亏掉一半以上最大亏损的概率约 '+(a.half*100).toFixed(0)+'%':''),'down');
  $('t-main').innerHTML=h;
  h=tile('盈亏平衡点',a.be.length?a.be.map(x=>'$'+f2(x)).join(' / '):'无',a.be.length?'距参考价 '+a.be.map(x=>pctOf(x,R.F)).join(' / ')+'；一倍预期波动是 ±'+(R.sigT*100).toFixed(1)+'%':'','sm');
  if(rr!=null) h+=tile('最多赚的是最多亏的几倍',rr.toFixed(2)+' 倍',rr<0.5?'赚得少亏得多，靠高胜率弥补':(rr>2?'亏得少赚得多，但胜率通常低':'赚和亏的幅度接近'));
  h+=tile('按市场定价的平均结果',sgn(a.ev),'把每种结果的金额乘以概率加总。通常是小的负数，差额主要是买卖价差',a.ev>=0?'up':'down');
  const now=S=>R.legs.reduce((s,l)=>s+l.m*(l.stock?S:black(S,l.K,l.T/365,l.iv,l.call)),0)*100;
  const d=(now(R.F*1.005)-now(R.F*0.995))/(R.F*0.01);
  const opt=R.legs.filter(l=>!l.stock);
  const th=opt.reduce((s,l)=>s+l.m*(black(R.F,l.K,Math.max(l.T-1,0)/365,l.iv,l.call)-black(R.F,l.K,l.T/365,l.iv,l.call)),0)*100;
  const ve=opt.reduce((s,l)=>s+l.m*(black(R.F,l.K,l.T/365,l.iv+0.01,l.call)-black(R.F,l.K,l.T/365,l.iv,l.call)),0)*100;
  h+=tile('标的涨 $1 的盈亏（Delta）',sgn(d),Math.abs(d)<8?'几乎不赌方向':(d>0?'偏看涨':'偏看跌'));
  h+=tile('过一天的盈亏（Theta）',sgn(th),th>1?'时间流逝对它有利':(th<-1?'时间流逝对它不利':'时间影响很小'));
  h+=tile('隐含波动率升 1 点的盈亏（Vega）',sgn(ve),ve>1?'波动率上升对它有利':(ve<-1?'波动率下降对它有利':'波动率影响很小'));
  $('t-more').innerHTML=h;
  $('bt-out').innerHTML=''; $('bt-years').innerHTML=''; $('bt-note').textContent='';
  if(resetWhat){
    const ks=R.legs.filter(l=>!l.stock).map(l=>l.K); if(!ks.length) ks.push(R.F);
    const lo=Math.min(R.F-3*R.move,Math.min(...ks)-0.5*R.move), hi=Math.max(R.F+3*R.move,Math.max(...ks)+0.5*R.move);
    $('w-s').min=Math.max(lo,0.01).toFixed(2); $('w-s').max=hi.toFixed(2); $('w-s').step=((hi-lo)/400).toFixed(3); $('w-s').value=R.F.toFixed(2);
    $('w-v').value=0; $('w-t').max=R.Tn; $('w-t').value=0;
  }
  what();
}
function whatPnl(S){
  const dv=+$('w-v').value/100, el=+$('w-t').value;
  return R.legs.reduce((s,l)=>s+l.m*((l.stock?S:black(S,l.K,Math.max(l.T-el,0)/365,Math.max(l.iv+dv,0.01),l.call))-l.price),0)*100;
}
function what(){
  if(!R) return;
  const S=+$('w-s').value, dv=+$('w-v').value, el=+$('w-t').value, p=whatPnl(S);
  $('w-s-o').textContent='$'+f2(S)+'（'+pctOf(S,R.F)+'）';
  $('w-v-o').textContent=(dv>=0?'+':'')+dv+' 个百分点';
  $('w-t-o').textContent=(el<1?(el*24).toFixed(1)+' 小时':el.toFixed(1)+' 天')+' / '+R.Tn.toFixed(1)+' 天';
  $('t-what').innerHTML=tile('假设情形下的盈亏',sgn(p),'如果现在就按理论价平仓',p>=0?'up':'down',true)+
    tile('和到期时相比',sgn(R.a.pnl(S)),'同样的价位，持有到 '+dstr(R.exp)+' 的盈亏',R.a.pnl(S)>=0?'up':'down');
  drawChart();
}

/* ---------- 图 ---------- */
function drawChart(){
  const c=$('pc'), dpr=window.devicePixelRatio||1, W=c.clientWidth, H=c.clientHeight;
  c.width=Math.round(W*dpr); c.height=Math.round(H*dpr);
  const g=c.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const muted=css('--muted');
  if(!R){ g.fillStyle=muted; g.font='13px '+css('--font'); g.fillText('没有可显示的组合',16,30); return; }
  const x0=+$('w-s').min, x1=+$('w-s').max, N=260, ex=[], nw=[]; let y0=0, y1=0;
  for(let i=0;i<=N;i++){ const x=x0+(x1-x0)*i/N, a=R.a.pnl(x), b=whatPnl(x); ex.push([x,a]); nw.push([x,b]); y0=Math.min(y0,a,b); y1=Math.max(y1,a,b); }
  const sp=Math.max(y1-y0,1); y0-=sp*0.08; y1+=sp*0.08;
  const L=64, Rm=16, T=26, B=30, px=x=>L+(x-x0)/(x1-x0)*(W-L-Rm), py=y=>T+(H-T-B)*(1-(y-y0)/(y1-y0));
  geo={x0:x0,x1:x1,L:L,Rm:Rm,W:W,T:T,B:B,H:H};
  // 预期波动范围
  g.fillStyle=css('--accent'); g.globalAlpha=0.10;
  const a=Math.max(px(R.F-R.move),L), b=Math.min(px(R.F+R.move),W-Rm); g.fillRect(a,T,b-a,H-T-B); g.globalAlpha=1;
  // 网格
  g.font='11px '+css('--num'); g.lineWidth=1;
  const step=function(r,n){ const raw=r/n, p=Math.pow(10,Math.floor(Math.log10(raw))), m=raw/p; return (m<1.5?1:m<3.5?2:m<7.5?5:10)*p; };
  const ys=step(y1-y0,6); g.textAlign='right'; g.textBaseline='middle';
  for(let y=Math.ceil(y0/ys)*ys;y<=y1;y+=ys){ g.strokeStyle=css('--line'); g.beginPath(); g.moveTo(L,py(y)); g.lineTo(W-Rm,py(y)); g.stroke(); g.fillStyle=muted; g.fillText((y<0?'-':'')+'$'+Math.abs(Math.round(y)).toLocaleString('en-US'),L-6,py(y)); }
  const xs=step(x1-x0,8); g.textAlign='center'; g.textBaseline='top';
  for(let x=Math.ceil(x0/xs)*xs;x<=x1;x+=xs) g.fillText(xs<1?x.toFixed(1):x.toFixed(0),px(x),H-B+8);
  // 赚钱和亏钱的区域
  const area=function(pos){
    g.beginPath(); g.moveTo(px(ex[0][0]),py(0));
    ex.forEach(p=>g.lineTo(px(p[0]),py(pos?Math.max(p[1],0):Math.min(p[1],0))));
    g.lineTo(px(ex[N][0]),py(0)); g.closePath(); g.fillStyle=css(pos?'--up':'--down'); g.globalAlpha=0.16; g.fill(); g.globalAlpha=1;
  };
  area(true); area(false);
  g.strokeStyle=css('--ink'); g.lineWidth=1; g.beginPath(); g.moveTo(L,py(0)); g.lineTo(W-Rm,py(0)); g.stroke();
  // 参考价和行权价
  g.setLineDash([4,3]); g.strokeStyle=css('--accent'); g.beginPath(); g.moveTo(px(R.F),T); g.lineTo(px(R.F),H-B); g.stroke();
  g.setLineDash([2,3]); g.strokeStyle=muted;
  [...new Set(R.legs.filter(l=>!l.stock).map(l=>l.K))].forEach(function(k){ if(k<x0||k>x1) return; g.beginPath(); g.moveTo(px(k),T); g.lineTo(px(k),H-B); g.stroke(); });
  g.setLineDash([]);
  g.font='11px '+css('--font'); g.textBaseline='bottom'; g.textAlign='center'; g.fillStyle=css('--accent');
  g.fillText('参考价 '+f2(R.F),px(R.F),T-4);
  g.fillStyle=muted; g.textAlign='left'; g.fillText('浅蓝色区域：约 68% 概率落在这里',L+4,T-4);
  const path=function(pts,col,dash,w){ g.beginPath(); g.setLineDash(dash); g.strokeStyle=col; g.lineWidth=w; pts.forEach((p,i)=>{ if(i) g.lineTo(px(p[0]),py(p[1])); else g.moveTo(px(p[0]),py(p[1])); }); g.stroke(); g.setLineDash([]); };
  path(ex,muted,[6,4],1.8); path(nw,css('--accent'),[],2);
  R.a.be.forEach(function(x){ if(x<x0||x>x1) return; g.beginPath(); g.fillStyle=css('--ink'); g.arc(px(x),py(0),3.5,0,Math.PI*2); g.fill(); });
  const S=+$('w-s').value; g.beginPath(); g.fillStyle=css('--accent'); g.arc(px(S),py(whatPnl(S)),5,0,Math.PI*2); g.fill(); g.lineWidth=2; g.strokeStyle=css('--panel'); g.stroke();
  if(hoverX!=null){ g.strokeStyle=muted; g.lineWidth=1; g.beginPath(); g.moveTo(px(hoverX),T); g.lineTo(px(hoverX),H-B); g.stroke(); }
}
$('pc').addEventListener('mousemove',function(e){
  if(!geo || !R) return;
  const r=this.getBoundingClientRect(), xpix=e.clientX-r.left, g=geo;
  const x=g.x0+(xpix-g.L)/(g.W-g.L-g.Rm)*(g.x1-g.x0);
  if(x<g.x0 || x>g.x1){ hoverX=null; $('ptip').style.display='none'; drawChart(); return; }
  hoverX=x; drawChart();
  const pa=1-ncdf((Math.log(x/R.F)+R.sigT*R.sigT/2)/R.sigT), tip=$('ptip');
  tip.innerHTML='<b>$'+f2(x)+'</b>（'+pctOf(x,R.F)+'）<br>到期时盈亏：<b>'+sgn(R.a.pnl(x))+'</b><br>假设情形下盈亏：<b>'+sgn(whatPnl(x))+'</b><br>到期收盘高于这里的概率：<b>'+(pa*100).toFixed(0)+'%</b><br>到期收盘低于这里的概率：<b>'+((1-pa)*100).toFixed(0)+'%</b>';
  tip.style.display='block';
  tip.style.left=(xpix+14+tip.offsetWidth>r.width?Math.max(0,xpix-tip.offsetWidth-14):xpix+14)+'px';
});
$('pc').addEventListener('mouseleave',function(){ hoverX=null; $('ptip').style.display='none'; drawChart(); });

/* ---------- 交互 ---------- */
$('legs').addEventListener('change',function(e){
  const m=/^lg-(\d+)-(\w+)$/.exec(e.target.id); if(!m) return;
  const l=legs[+m[1]], v=e.target.value;
  if(m[2]==='side') l.side=+v; else if(m[2]==='type') l.type=v; else if(m[2]==='K') l.K=+v;
  else if(m[2]==='qty') l.qty=Math.max(1,Math.round(+v)||1); else if(m[2]==='exp') l.exp=v;
  curTpl=null; renderTemplates(); drawLegs(); update(true);
});
$('legs').addEventListener('click',function(e){
  const m=/^lg-(\d+)-rm$/.exec(e.target.id); if(!m || legs.length<2) return;
  legs.splice(+m[1],1); curTpl=null; renderTemplates(); drawLegs(); update(true);
});
$('add').addEventListener('click',function(){ const l=legs[legs.length-1]; legs.push({side:1,type:l.type,K:l.K,qty:1,exp:l.exp}); curTpl=null; renderTemplates(); drawLegs(); update(true); });
$('tpl').addEventListener('click',function(e){
  const tr=e.target.closest('tr.pick'); if(!tr) return;
  const t=template(tr.dataset.t,$('exp').value); if(!t) return;
  curTpl=tr.dataset.t; legs=t; renderTemplates(); drawLegs(); update(true);
  // 跳到“当前组合”，并闪一下边框，让人看到它已经换成了刚点的策略
  const c=$('combo'); c.scrollIntoView({behavior:'smooth',block:'start'});
  c.classList.add('flash'); setTimeout(function(){ c.classList.remove('flash'); },1200);
});
function runBacktest(){
  if(!R) return;
  const note=$('bt-note'); $('bt-out').innerHTML=''; $('bt-years').innerHTML=''; $('bt-head').innerHTML='';
  if(R.legs.some(l=>!l.stock && Math.abs(l.T-R.Tn)>1e-6)){ note.textContent='组合里有不同到期日的腿，暂时不支持回测。'; return; }
  const vol=D.vol||[];
  if(vol.filter(r=>r[2]!=null).length<300){ note.textContent='没有足够的历史数据。请回到看板点“重新读取”，读取这个代码的价格和隐含波动率历史。'; return; }
  const N=Math.max(1,Math.round(R.Tn*252/365));
  const spec=R.legs.map(l=>({m:l.m,stock:!!l.stock,call:l.call,x:l.stock?0:(l.K-R.F)/R.move}));
  const b=histTest(spec,N,vol);
  if(b.n<60){ note.textContent='历史样本太少，无法回测。'; return; }
  const act=b.wins/b.n, pred=b.pred/b.n, p0=b.indepWins/b.indep, ci=1.96*Math.sqrt(Math.max(p0*(1-p0),1e-9)/b.indep), d=act-pred, k=R.F;
  const near=Math.abs(d)<=Math.max(ci,0.03);
  note.textContent='共 '+b.n+' 笔，'+b.first+' 到 '+b.last+'，每笔持有 '+N+' 个交易日。金额已按现在的参考价折算。';
  let h=tile('模拟器现在给出的胜率',(R.a.pop*100).toFixed(0)+'%','按今天的真实报价算出来的','',true);
  h+=tile('历史上的实际胜率',(act*100).toFixed(0)+'%','同样结构的交易持有到期后真的赚钱的比例',near?'':(d>0?'up':'down'),true);
  h+=tile('当时预测的平均胜率',(pred*100).toFixed(0)+'%','每笔建仓那天按同样的算法算出的胜率，取平均');
  h+=tile('实际减去预测',(d>=0?'+':'')+(d*100).toFixed(0)+' 个百分点（误差 ±'+(ci*100).toFixed(0)+'）',near?'在误差范围内，这个组合的胜率可以相信':(d>0?'历史上实际比预测的更容易赚钱':'历史上实际比预测的更难赚钱，现在的胜率要打折扣'),'sm');
  h+=tile('平均每笔盈亏',sgn(b.sum/b.n*k),'正数说明这个结构在这段历史里长期是赚的',b.sum>=0?'up':'down');
  h+=tile('赚的时候平均赚',b.wins?sgn(b.sumW/b.wins*k):'无','共 '+b.wins+' 笔','up');
  h+=tile('亏的时候平均亏',b.n-b.wins?sgn(b.sumL/(b.n-b.wins)*k):'无','共 '+(b.n-b.wins)+' 笔','down');
  h+=tile('最差的一笔',sgn(b.worst*k),'最好的一笔 '+sgn(b.best*k),'down');
  $('bt-out').innerHTML=h;
  $('bt-head').innerHTML='<tr><th>年份</th><th>笔数</th><th>实际胜率</th><th>平均每笔盈亏</th></tr>';
  $('bt-years').innerHTML=Object.keys(b.years).sort().map(function(y){ const v=b.years[y]; return '<tr><td>'+y+'</td><td>'+v.n+'</td><td>'+(v.wins/v.n*100).toFixed(0)+'%</td><td class="'+(v.sum>=0?'up':'down')+'">'+sgn(v.sum/v.n*k)+'</td></tr>'; }).join('');
}
$('bt-run').addEventListener('click',runBacktest);
function setMode(mid){ useMid=mid; $('pm-ba').setAttribute('aria-pressed',!mid); $('pm-mid').setAttribute('aria-pressed',mid); if(D){ renderTemplates(); drawLegs(); update(false); } }
$('pm-ba').addEventListener('click',()=>setMode(false));
$('pm-mid').addEventListener('click',()=>setMode(true));
$('exp').addEventListener('change',setExpiry);
$('sym').addEventListener('change',function(){ loadSymbol(this.value); });
['w-s','w-v','w-t'].forEach(id=>$(id).addEventListener('input',what));
window.addEventListener('resize',drawChart);
if(window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',drawChart);
start();
</script>
</body>
</html>
"""
SIM_PAGE = (SIM_PAGE.replace("__CSS__", PAGE.split("<style>")[1].split("</style>")[0])
            .replace("__CALC__", PAGE.split("/*CALC-START*/")[1].split("/*CALC-END*/")[0]))


def main():
    url = f"http://127.0.0.1:{WEB_PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Handler)
    except OSError:
        print("The panel is already running. Opening", url)
        webbrowser.open(url)
        return
    threading.Thread(target=worker, daemon=True).start()
    print("Options panel is running at", url, "(demo data)" if DEMO else "")
    print("Keep this window open. Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
