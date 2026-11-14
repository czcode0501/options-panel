# -*- coding: utf-8 -*-
"""
波动范围回测

检验的问题：用隐含波动率推算出的“68% 范围”和“95% 范围”，历史上真的有 68% 和 95% 的时候落在里面吗？

用法（先登录 IB Gateway）：
    python backtest_range.py            回测 QQQ
    python backtest_range.py NVDA       回测别的代码
    python backtest_range.py QQQ 10     回测最近 10 年（默认 5 年）
    python backtest_range.py --demo     不连接 IBKR，用模拟数据看报告的样子

结果会生成一个网页报告并自动打开。数据保存在 backtest_data 文件夹，同一天再运行不用重新读取。
"""
import sys, os, csv, json, math, random, webbrowser, urllib.request
from html import escape
from pathlib import Path
from datetime import date, datetime, timedelta

args = [a for a in sys.argv[1:] if not a.startswith("--")]
DEMO = "--demo" in sys.argv
SYMBOL = (args[0] if args else "QQQ").upper()
YEARS = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
HORIZONS = [1, 5, 10, 21]          # 交易日：约 1 天、1 周、2 周、1 个月
IB_PORT = 4002
CLIENT_ID = 12
# IBKR 取不到隐含波动率历史时，这几个 ETF 改用 Cboe 公布的波动率指数
CBOE_INDEX = {"QQQ": "VXN", "SPY": "VIX", "IWM": "RVX", "DIA": "VXD"}

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "backtest_data")
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------- 数据

def fetch_cboe(index):
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{index}_History.csv"
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    out = {}
    for row in csv.DictReader(text.splitlines()):
        try:
            m, d, y = row["DATE"].split("/")
            out[f"{y}-{int(m):02d}-{int(d):02d}"] = float(row["CLOSE"]) / 100
        except (KeyError, ValueError):
            continue
    return out


def fetch_ibkr(symbol, years):
    from ib_async import IB, Stock, util
    if "ipykernel" in sys.modules:      # 在 Spyder 里运行时需要
        util.startLoop()
    ib = IB()
    try:
        ib.connect("127.0.0.1", IB_PORT, clientId=CLIENT_ID, readonly=True, timeout=10)
    except Exception:
        raise SystemExit("Cannot connect to IB Gateway. Log in first (port %d), then run again." % IB_PORT)
    try:
        stk = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(stk)
        if not stk.conId:
            raise SystemExit("Symbol not found: " + symbol)
        print("reading daily prices...")
        px = ib.reqHistoricalData(stk, endDateTime="", durationStr=f"{years} Y", barSizeSetting="1 day",
                                  whatToShow="TRADES", useRTH=True)
        print("reading implied volatility history...")
        ivb = ib.reqHistoricalData(stk, endDateTime="", durationStr=f"{years} Y", barSizeSetting="1 day",
                                   whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True)
    finally:
        ib.disconnect()
    if not px:
        raise SystemExit("No price history returned for " + symbol)
    iv, source = {str(b.date)[:10]: b.close for b in ivb if b.close and b.close > 0}, "IBKR 30 天隐含波动率"
    if len(iv) < 250 and symbol in CBOE_INDEX:
        print("IBKR returned little implied volatility history, using Cboe", CBOE_INDEX[symbol])
        iv, source = fetch_cboe(CBOE_INDEX[symbol]), "Cboe " + CBOE_INDEX[symbol] + " 指数"
    if len(iv) < 250:
        raise SystemExit("Not enough implied volatility history for " + symbol)
    vals = sorted(iv.values())
    if vals[len(vals) // 2] > 3:        # 数值是百分数时换成小数
        iv = {k: v / 100 for k, v in iv.items()}
    rows = [[str(b.date)[:10], b.open, b.high, b.low, b.close, iv[str(b.date)[:10]]]
            for b in px if str(b.date)[:10] in iv]
    return rows, source


def demo_data(years):
    rnd, rows, p, v = random.Random(7), [], 300.0, 0.22
    d = date.today() - timedelta(days=int(years * 365.25))
    while d < date.today():
        if d.weekday() < 5:
            v = max(0.10, v + 0.03 * (0.22 - v) + rnd.gauss(0, 0.008))
            o = p
            p = p * math.exp(rnd.gauss(0.0003, 0.85 * v / math.sqrt(252)))   # 实际波动约为隐含的 85%
            rows.append([d.isoformat(), o, max(o, p) * 1.003, min(o, p) * 0.997, p, v])
        d += timedelta(days=1)
    return rows, "模拟数据"


def load():
    path = os.path.join(DATA_DIR, f"{SYMBOL}_{YEARS}y_{date.today():%Y%m%d}.csv")
    if DEMO:
        return demo_data(YEARS)
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            r = list(csv.reader(f))
        print("loaded saved data from", path)
        return [[x[0]] + [float(v) for v in x[1:]] for x in r[1:]], r[0][0]
    rows, source = fetch_ibkr(SYMBOL, YEARS)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([source])
        w.writerows(rows)
    print("saved data to", path)
    return rows, source


# ---------------------------------------------------------------- 计算
# rows: [日期, 开, 高, 低, 收, 隐含波动率(小数)]

def observe(rows, n):
    """对每一天：用当天的隐含波动率算 n 个交易日后的 1 倍范围，再看实际走了多少倍。"""
    out = []
    for t in range(len(rows) - n):
        s, iv = rows[t][4], rows[t][5]
        sig = iv * math.sqrt(n / 252)
        z = (rows[t + n][4] / s - 1) / sig
        hi = max(r[2] for r in rows[t + 1:t + n + 1])
        lo = min(r[3] for r in rows[t + 1:t + n + 1])
        out.append({"date": rows[t][0], "z": z, "iv": iv, "touch": hi > s * (1 + sig) or lo < s * (1 - sig)})
    return out


def summarize(obs, n):
    k = len(obs)
    if not k:
        return None
    hit1 = sum(abs(o["z"]) <= 1 for o in obs) / k
    hit2 = sum(abs(o["z"]) <= 2 for o in obs) / k
    miss = [o for o in obs if abs(o["z"]) > 1]
    indep = obs[::n]                       # 互不重叠的样本，用来估计误差
    p = sum(abs(o["z"]) <= 1 for o in indep) / len(indep)
    return {"n": k, "hit1": hit1, "hit2": hit2,
            "mean_abs": sum(abs(o["z"]) for o in obs) / k,
            "down": (sum(o["z"] < -1 for o in miss) / len(miss)) if miss else None,
            "touch": sum(o["touch"] for o in obs) / k,
            "n_indep": len(indep), "ci": 1.96 * math.sqrt(max(p * (1 - p), 1e-9) / len(indep))}


def realized_after(rows, n=21):
    out = []
    for t in range(len(rows) - n):
        lr = [math.log(rows[i + 1][4] / rows[i][4]) for i in range(t, t + n)]
        m = sum(lr) / n
        rv = math.sqrt(sum((x - m) ** 2 for x in lr) / (n - 1)) * math.sqrt(252)
        out.append((rows[t][0], rows[t][5], rv))
    return out


# ---------------------------------------------------------------- 报告

def pct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def verdict(hit, target, ci):
    diff = hit - target
    if abs(diff) <= max(ci, 0.02):
        return "基本准确", ""
    return ("范围偏宽", "up") if diff > 0 else ("范围偏窄", "down")


def svg_lines(series, dates, ref=None, ref_label="", height=300, ahead=None):
    """series: [(名称, CSS 颜色变量, [数值或 None])]，各序列和 dates 等长。
    ahead: {序列名: n}，表示这个序列在某一天的数值统计的是之后 n 个交易日。"""
    W, H, L, R, T, B = 1000, height, 52, 16, 14, 28
    vals = [v for _, _, s in series for v in s if v is not None] + ([ref] if ref is not None else [])
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    n = len(dates)
    px = lambda i: L + i / max(n - 1, 1) * (W - L - R)
    py = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" style="width:100%;height:auto;display:block">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        out.append(f'<line x1="{L}" y1="{py(v):.1f}" x2="{W - R}" y2="{py(v):.1f}" stroke="var(--line)"/>'
                   f'<text x="{L - 6}" y="{py(v) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--muted)">{v * 100:.0f}%</text>')
    for i in range(0, n, max(1, n // 7)):
        anchor = "end" if px(i) > W - R - 30 else "middle"
        out.append(f'<text x="{px(i):.1f}" y="{H - 8}" text-anchor="{anchor}" font-size="11" fill="var(--muted)">{dates[i][:7]}</text>')
    if ref is not None:
        out.append(f'<line x1="{L}" y1="{py(ref):.1f}" x2="{W - R}" y2="{py(ref):.1f}" stroke="var(--ink)" stroke-dasharray="5 4"/>'
                   f'<text x="{W - R - 4}" y="{py(ref) - 5:.1f}" text-anchor="end" font-size="11" fill="var(--ink)">{ref_label}</text>')
    for name, color, s in series:
        pts, seg = [], []
        for i, v in enumerate(s):
            if v is None:
                if seg:
                    pts.append(seg)
                seg = []
            else:
                seg.append(f"{px(i):.1f},{py(v):.1f}")
        if seg:
            pts.append(seg)
        for seg in pts:
            out.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="var({color})" stroke-width="1.6"/>')
    out.append(f'<line class="cur" x1="0" x2="0" y1="{T}" y2="{H - B}" stroke="var(--muted)" style="display:none"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:var({c})"></i>{nm}</span>' for nm, c, _ in series)
    payload = {"W": W, "L": L, "R": R, "dates": dates,
               "series": [{"name": nm, "ahead": (ahead or {}).get(nm, 0),
                           "v": [None if v is None else round(v, 4) for v in sv]} for nm, c, sv in series]}
    return ('<div class="chart" data-c="' + escape(json.dumps(payload, ensure_ascii=False), quote=True) + '">'
            + "".join(out) + '<div class="tip"></div></div>'
            + f'<div class="legend">{legend}<span>把鼠标移到图上可以看每一天的具体数值</span></div>')


SCRIPT = """<script>
document.querySelectorAll('.chart').forEach(function(el){
  var c=JSON.parse(el.dataset.c), svg=el.querySelector('svg'), tip=el.querySelector('.tip'), cur=svg.querySelector('.cur'), n=c.dates.length;
  svg.addEventListener('mousemove',function(e){
    var r=svg.getBoundingClientRect(), x=(e.clientX-r.left)/r.width*c.W, span=c.W-c.L-c.R;
    var i=Math.max(0,Math.min(n-1,Math.round((x-c.L)/span*(n-1)))), cx=c.L+i/Math.max(n-1,1)*span;
    cur.setAttribute('x1',cx); cur.setAttribute('x2',cx); cur.style.display='';
    var h='<b>'+c.dates[i]+'</b>';
    c.series.forEach(function(s){
      var v=s.v[i], name=s.name;
      if(s.ahead && v!=null) name+='（'+c.dates[i]+' 之后到 '+c.dates[Math.min(i+s.ahead,n-1)]+'）';
      h+='<br>'+name+'：<b>'+(v==null?(s.ahead?'还没走满 '+s.ahead+' 个交易日':'无数据'):(v*100).toFixed(1)+'%')+'</b>';
    });
    tip.innerHTML=h; tip.style.display='block';
    var px=cx/c.W*r.width, w=tip.offsetWidth;
    tip.style.left=Math.max(0,Math.min(px+14,r.width-w-4))+'px';
    if(px+14+w>r.width) tip.style.left=Math.max(0,px-w-14)+'px';
  });
  svg.addEventListener('mouseleave',function(){ cur.style.display='none'; tip.style.display='none'; });
});
</script>"""


def build_report(rows, source):
    obs = {n: observe(rows, n) for n in HORIZONS}
    stats = {n: summarize(obs[n], n) for n in HORIZONS}
    name = {1: "1 天", 5: "1 周（5 个交易日）", 10: "2 周（10 个交易日）", 21: "1 个月（21 个交易日）"}
    short = {1: "1 天", 5: "1 周", 10: "2 周", 21: "1 个月"}

    # 结论
    lines = []
    for n in HORIZONS:
        s = stats[n]
        if not s:
            continue
        v, _ = verdict(s["hit1"], 0.683, s["ci"])
        lines.append(f"<li><b>{short[n]}</b>：68% 范围的实际命中率是 <b>{pct(s['hit1'])}</b>（误差约 ±{s['ci'] * 100:.0f} 个百分点），"
                     f"95% 范围是 <b>{pct(s['hit2'])}</b>。结论：{v}。</li>")
    main = stats[HORIZONS[-1]] or stats[HORIZONS[0]]
    wide = sum(1 for n in HORIZONS if stats[n] and stats[n]["hit1"] - 0.683 > max(stats[n]["ci"], 0.02))
    narrow = sum(1 for n in HORIZONS if stats[n] and 0.683 - stats[n]["hit1"] > max(stats[n]["ci"], 0.02))
    if wide > narrow:
        overall = "总体上，隐含波动率给出的范围比实际走出来的要宽：市场定价的波动大于之后真实发生的波动。看板上的 68% 范围，实际落在里面的机会比 68% 高。"
    elif narrow > wide:
        overall = "总体上，隐含波动率给出的范围比实际走出来的要窄：这段时间真实波动超过了市场的定价。看板上的 68% 范围，实际落在里面的机会低于 68%。"
    else:
        overall = "总体上，隐含波动率给出的范围和实际结果接近，可以按字面的概率来理解看板上的范围。"

    # 表 1：按时间跨度
    t1 = ""
    for n in HORIZONS:
        s = stats[n]
        if not s:
            continue
        v, cls = verdict(s["hit1"], 0.683, s["ci"])
        t1 += (f"<tr><td>{name[n]}</td><td>{s['n']}</td><td class='{cls}'>{pct(s['hit1'])}</td><td>{pct(s['hit2'])}</td>"
               f"<td>{s['mean_abs']:.2f}</td><td>{pct(s['down'], 0)}</td><td>{pct(s['touch'])}</td><td class='l'>{v}</td></tr>")

    # 表 2：按隐含波动率高低
    ivs = sorted(r[5] for r in rows)
    c1, c2 = ivs[len(ivs) // 3], ivs[2 * len(ivs) // 3]
    groups = [(f"低（低于 {c1 * 100:.0f}%）", lambda v: v < c1), (f"中（{c1 * 100:.0f}% 到 {c2 * 100:.0f}%）", lambda v: c1 <= v < c2),
              (f"高（高于 {c2 * 100:.0f}%）", lambda v: v >= c2)]
    t2 = ""
    for label, f in groups:
        cells = ""
        for n in HORIZONS:
            sub = [o for o in obs[n] if f(o["iv"])]
            cells += f"<td>{pct(sum(abs(o['z']) <= 1 for o in sub) / len(sub)) if sub else '—'}</td>"
        t2 += f"<tr><td>{label}</td>{cells}</tr>"

    # 表 3：按年份
    t3 = ""
    for y in sorted({o["date"][:4] for o in obs[HORIZONS[0]]}):
        cells = ""
        for n in HORIZONS:
            sub = [o for o in obs[n] if o["date"][:4] == y]
            cells += f"<td>{pct(sum(abs(o['z']) <= 1 for o in sub) / len(sub)) if len(sub) >= 20 else '—'}</td>"
        t3 += f"<tr><td>{y}</td>{cells}</tr>"

    # 隐含 vs 实际
    ra = realized_after(rows)
    avg_iv = sum(x[1] for x in ra) / len(ra)
    avg_rv = sum(x[2] for x in ra) / len(ra)
    above = sum(x[1] > x[2] for x in ra) / len(ra)

    # 图
    n5 = 5 if 5 in HORIZONS else HORIZONS[0]
    o5, win = obs[n5], 250
    roll = [None] * len(o5)
    run = 0
    for i, o in enumerate(o5):
        run += abs(o["z"]) <= 1
        if i >= win:
            run -= abs(o5[i - win]["z"]) <= 1
        if i >= win - 1:
            roll[i] = run / win
    chart1 = svg_lines([(f"最近 {win} 个交易日的命中率", "--accent", roll)], [o["date"] for o in o5], 0.683, "理论值 68.3%")
    rv_name = "之后 21 个交易日的实际波动率"
    chart2 = svg_lines([("隐含波动率（当天）", "--accent", [r[5] for r in rows]),
                        (rv_name, "--warn", [x[2] for x in ra] + [None] * (len(rows) - len(ra)))],
                       [r[0] for r in rows], height=320, ahead={rv_name: 21})
    last_rv = ra[-1][0]

    h1 = main["hit1"]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SYMBOL} 波动范围回测</title>
<style>
:root{{--bg:#eef0f3;--panel:#fff;--ink:#16202e;--muted:#5d6878;--line:#d5dae2;--accent:#1f5fbf;--accent-soft:#e3ecfa;--up:#0d7a4f;--down:#c0392b;--warn:#c9730a;
  --font:"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;--num:Consolas,"SFMono-Regular",monospace}}
@media (prefers-color-scheme: dark){{:root{{--bg:#10151d;--panel:#19212c;--ink:#e6ebf2;--muted:#93a0b3;--line:#2c3746;--accent:#6fa4f5;--accent-soft:#1e2c44;--up:#3fc48d;--down:#f0776a;--warn:#f0a94a;color-scheme:dark}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.55}}
main{{max-width:1100px;margin:0 auto;padding:20px 16px 48px;display:flex;flex-direction:column;gap:16px}}
h1{{font-size:22px;margin:0;font-weight:600}} h2{{font-size:16px;margin:0;font-weight:600}} p{{margin:0;max-width:85ch}}
.sub{{color:var(--muted);font-size:13px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:12px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}
.tile{{border:1px solid var(--line);border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}}
.tile.main{{background:var(--accent-soft);border-color:transparent}}
.tile .k{{font-size:12px;color:var(--muted)}} .tile .v{{font-family:var(--num);font-size:20px;font-weight:600}} .tile .d{{font-size:12px;color:var(--muted)}}
.tablewrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px;min-width:560px}}
th,td{{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}} th{{color:var(--muted);font-weight:500}}
td{{font-family:var(--num)}} th:first-child,td:first-child,.l{{text-align:left;font-family:var(--font)}}
td.up{{color:var(--up);font-weight:600}} td.down{{color:var(--down);font-weight:600}}
ul{{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:5px;max-width:95ch}}
.chart{{position:relative}} .chart svg{{cursor:crosshair}}
.tip{{position:absolute;top:8px;pointer-events:none;display:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.15)}}
.tip b{{font-family:var(--num)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted)}} .legend i{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px}}
</style></head><body><main>
<div><h1>{SYMBOL} 波动范围回测</h1>
<p class="sub">数据：{rows[0][0]} 到 {rows[-1][0]}，共 {len(rows)} 个交易日 · 隐含波动率来源：{source} · 生成于 {datetime.now():%Y-%m-%d %H:%M}</p></div>

<section class="panel"><h2>结论</h2><p>{overall}</p><ul>{''.join(lines)}</ul></section>

<section class="panel"><h2>关键数字</h2><div class="tiles">
<div class="tile main"><span class="k">68% 范围的实际命中率（1 个月）</span><span class="v">{pct(h1)}</span><span class="d">理论值 68.3%，高于它说明范围偏宽</span></div>
<div class="tile"><span class="k">95% 范围的实际命中率（1 个月）</span><span class="v">{pct(main['hit2'])}</span><span class="d">理论值 95.4%</span></div>
<div class="tile"><span class="k">隐含波动率的平均值</span><span class="v">{pct(avg_iv)}</span><span class="d">之后一个月实际波动率的平均值是 {pct(avg_rv)}</span></div>
<div class="tile"><span class="k">隐含高于实际的天数占比</span><span class="v">{pct(above, 0)}</span><span class="d">超过一半说明期权通常定价偏贵</span></div>
</div></section>

<section class="panel"><h2>按时间跨度</h2>
<div class="tablewrap"><table><thead><tr><th>时间跨度</th><th>样本数</th><th>68% 范围命中率</th><th>95% 范围命中率</th><th>实际走了预期的几倍</th><th>突破中向下的占比</th><th>期间曾碰到边界</th><th class='l'>结论</th></tr></thead><tbody>{t1}</tbody></table></div>
<ul class="sub">
<li>命中率：到期那天的收盘价落在范围内的比例。理论值分别是 68.3% 和 95.4%。</li>
<li>实际走了预期的几倍：实际涨跌幅除以 1 倍范围的平均值。定价准确时约为 0.80，小于它说明实际波动比定价小。</li>
<li>突破中向下的占比：收盘在范围外的那些次里，有多少是跌破下沿。高于 50% 说明突破更多发生在下跌方向。</li>
<li>期间曾碰到边界：这段时间内盘中最高或最低价碰到过 1 倍范围的比例。它一定高于“收盘在范围外”的比例，做卖方时要看这个数。</li>
</ul></section>

<section class="panel"><h2>隐含波动率高和低的时候，范围准不准</h2>
<div class="tablewrap"><table><thead><tr><th>当天的隐含波动率</th>{''.join(f'<th>{short[n]}</th>' for n in HORIZONS)}</tr></thead><tbody>{t2}</tbody></table></div>
<p class="sub">表中是 68% 范围的命中率。如果“高”那一行明显高于 68%，说明恐慌时期权定价往往过头；如果“低”那一行明显低于 68%，说明市场平静时容易低估之后的波动。</p></section>

<section class="panel"><h2>按年份</h2>
<div class="tablewrap"><table><thead><tr><th>年份</th>{''.join(f'<th>{short[n]}</th>' for n in HORIZONS)}</tr></thead><tbody>{t3}</tbody></table></div>
<p class="sub">表中是 68% 范围的命中率。各年差别大，说明结论和市场环境有关，不能只看平均数。</p></section>

<section class="panel"><h2>命中率随时间的变化（{short[n5]}的 68% 范围）</h2>{chart1}</section>
<section class="panel"><h2>隐含波动率和之后的实际波动率</h2>{chart2}
<p class="sub">蓝线在橙线上方的时候，期权定价的波动大于之后实际发生的波动，卖方占优；橙线冲到蓝线上方的那几段，就是卖方亏大钱的时候。</p>
<p class="sub">蓝线画到最新一天（{rows[-1][0]}）。橙线只能画到 {last_rv}：它在每一天的数值，统计的是那一天之后 21 个交易日的实际波动，{last_rv} 之后的日子还没有走满 21 个交易日，所以暂时没有数值。橙线最右端那个点已经包含了截至 {rows[-1][0]} 的行情。</p></section>

<section class="panel"><h2>方法和局限</h2><ul class="sub">
<li>做法：对过去每一个交易日，用当天收盘时的隐含波动率算出之后 N 个交易日的 1 倍范围（价格 × 隐含波动率 × √(N/252)），再看 N 个交易日后的收盘价落在哪里。只用了当天已知的数据，没有用到未来的信息。</li>
<li>和看板的差别：这里用的是 30 天期的隐含波动率，看板用的是所选到期日的平值跨式价格。两者在 1 个月跨度上最接近；1 天和 1 周的结果只能当近似，因为短期期权有自己的定价。</li>
<li>相邻几天的样本互相重叠，不是独立的。表里的“误差”是按互不重叠的样本算的，1 个月跨度的独立样本每年只有 12 个左右，结论的把握有限。</li>
<li>价格没有做分红调整，对结果影响很小。</li>
<li>这个回测只检验“范围有多宽”准不准，不涉及涨跌方向，也不检验 Call 墙、Put 墙这些持仓价位。过去的统计结果不保证以后成立。</li>
</ul></section>
</main>{SCRIPT}</body></html>"""


def main():
    rows, source = load()
    if len(rows) < 300:
        raise SystemExit("Not enough data: %d days" % len(rows))
    html = build_report(rows, source)
    out = os.path.join(BASE, f"backtest_range_{SYMBOL}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    for n in HORIZONS:
        s = summarize(observe(rows, n), n)
        print(f"{n:>2}d  n={s['n']:<5} hit68={s['hit1'] * 100:5.1f}%  hit95={s['hit2'] * 100:5.1f}%  touch={s['touch'] * 100:5.1f}%")
    print("report:", out)
    if "--no-browser" not in sys.argv:
        webbrowser.open(Path(out).as_uri())


if __name__ == "__main__":
    main()
