# -*- coding: utf-8 -*-
"""
策略胜率回测

检验的问题：模拟器给每种策略算出的“盈利概率（胜率）”，历史上准不准？

做法：对过去每一个交易日，按和模拟器相同的规则建一遍每种策略，记下当时预测的胜率，
再看持有到期时实际赚没赚，最后比较预测胜率和实际胜率。

用法（先登录 IB Gateway；当天运行过 backtest_range.py 的话会直接使用已保存的数据）：
    python backtest_strategies.py            回测 QQQ，最近 5 年
    python backtest_strategies.py NVDA       回测别的代码
    python backtest_strategies.py QQQ 10     回测最近 10 年
    python backtest_strategies.py --demo     不连接 IBKR，用模拟数据看报告的样子
"""
import os, sys, math, webbrowser
from pathlib import Path
from datetime import datetime

import backtest_range as br      # 复用它的数据读取（价格和隐含波动率历史）

SYMBOL, YEARS = br.SYMBOL, br.YEARS
HORIZONS = [5, 21]               # 持有的交易日数：约 1 周、1 个月
NAME = {5: "持有 1 周（5 个交易日）到期", 21: "持有 1 个月（21 个交易日）到期"}


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black(F, K, T, v, call):
    """期权理论价。T 以年为单位。"""
    if T <= 1e-9 or v <= 0:
        return max(F - K, 0) if call else max(K - F, 0)
    sq = math.sqrt(T)
    d1 = (math.log(F / K) + v * v * T / 2) / (v * sq)
    d2 = d1 - v * sq
    return F * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - F * ncdf(-d1)


# 每条腿：(方向×张数, 是否 Call, 行权价相对参考价的位置：参考价 + x × 一倍预期波动)
# 和模拟器的模板一致：价外的腿放在一倍预期波动的位置，保护腿放在一倍半的位置
STRATEGIES = [
    ("买入 Call", [(1, True, 0)]),
    ("买入 Put", [(1, False, 0)]),
    ("牛市看涨价差（付钱买）", [(1, True, 0), (-1, True, 1)]),
    ("熊市看跌价差（付钱买）", [(1, False, 0), (-1, False, -1)]),
    ("卖出看跌价差（收钱，看不跌）", [(-1, False, -1), (1, False, -1.5)]),
    ("卖出看涨价差（收钱，看不涨）", [(-1, True, 1), (1, True, 1.5)]),
    ("买入跨式", [(1, True, 0), (1, False, 0)]),
    ("买入宽跨式", [(1, True, 1), (1, False, -1)]),
    ("铁鹰", [(-1, False, -1), (1, False, -1.5), (-1, True, 1), (1, True, 1.5)]),
    ("铁蝶", [(-1, True, 0), (-1, False, 0), (1, True, 1.5), (1, False, -1.5)]),
    ("蝶式", [(1, True, -1), (-2, True, 0), (1, True, 1)]),
    # 第二项写 None 表示这条腿是 100 股股票
    ("备兑看涨（持股 + 卖出 Call）", [(1, None, 0), (-1, True, 1)]),
    ("保护性看跌（持股 + 买入 Put）", [(1, None, 0), (1, False, -1)]),
]


def build(S, iv, n, spec):
    """按当天的价格和隐含波动率建仓。返回 (每条腿, 开仓净成本)。"""
    T, m = n / 252, S * iv * math.sqrt(n / 252)
    legs, cost = [], 0.0
    for q, call, x in spec:
        if call is None:                 # 股票：按当天收盘价买入，盈亏就是之后的涨跌
            legs.append((q, None, S))
            continue
        K = S + x * m
        p = black(S, K, T, iv, call)
        legs.append((q, call, K))
        cost += q * p
    return legs, cost


def payoff(legs, cost, s):
    return sum(q * ((s - K) if call is None else max(s - K, 0) if call else max(K - s, 0)) for q, call, K in legs) - cost


def predicted_pop(legs, cost, S, sig):
    """和模拟器相同的算法：以参考价为中心的对数正态分布，统计到期时赚钱的概率。"""
    ks = sorted({K for _, call, K in legs if call is not None})
    pts = [1e-9] + ks + [ks[-1] * 4]          # 盈亏在这些点之间是直线
    vals = [payoff(legs, cost, x) for x in pts]
    cdf = lambda x: ncdf((math.log(x / S) + sig * sig / 2) / sig)
    pop = 0.0
    for i in range(len(pts) - 1):
        a, b, pa, pb = pts[i], pts[i + 1], vals[i], vals[i + 1]
        if pa > 0 and pb > 0:
            pop += cdf(b) - cdf(a)
        elif (pa > 0) != (pb > 0):
            z = a + (b - a) * (0 - pa) / (pb - pa)   # 盈亏平衡点
            pop += (cdf(z) - cdf(a)) if pa > 0 else (cdf(b) - cdf(z))
    if vals[-1] > 0:
        pop += 1 - cdf(pts[-1])
    return pop


def run(rows, n):
    """rows: [日期, 开, 高, 低, 收, 隐含波动率]。返回每种策略的统计。"""
    out = []
    last = rows[-1][4]
    for name, spec in STRATEGIES:
        pops, pnls = [], []
        for t in range(len(rows) - n):
            S, iv = rows[t][4], rows[t][5]
            legs, cost = build(S, iv, n, spec)
            pops.append(predicted_pop(legs, cost, S, iv * math.sqrt(n / 252)))
            pnls.append(payoff(legs, cost, rows[t + n][4]) / S)     # 换算成股价的比例，便于不同年份相加
        k = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        act = len(wins) / k
        indep = pnls[::n]                                         # 互不重叠的样本，用来估计误差
        p0 = sum(1 for p in indep if p > 0) / len(indep)
        scale = last * 100                                        # 按现在的股价折算成一组的美元数
        out.append({
            "name": name, "n": k, "pred": sum(pops) / k, "act": act,
            "ci": 1.96 * math.sqrt(max(p0 * (1 - p0), 1e-9) / len(indep)),
            "avg": sum(pnls) / k * scale,
            "avg_win": (sum(wins) / len(wins) * scale) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses) * scale) if losses else 0.0,
            "worst": min(pnls) * scale, "best": max(pnls) * scale,
            "years": {y: (lambda s: sum(1 for p in s if p > 0) / len(s) if len(s) >= 20 else None)(
                [p for p, r in zip(pnls, rows) if r[0][:4] == y]) for y in sorted({r[0][:4] for r in rows[:k]})},
        })
    return out


def calibration(rows, n):
    """概率校准：对“到期收盘高于某个价位”的概率，比较预测值和实际发生的比例。
    价位取参考价上下若干倍预期波动，覆盖从很低到很高的各种概率。"""
    out = []
    for x in (-2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2):
        pred, hit, k = 0.0, 0, 0
        for t in range(len(rows) - n):
            S, sig = rows[t][4], rows[t][5] * math.sqrt(n / 252)
            level = S * (1 + x * sig)
            if level <= 0:
                continue
            pred += 1 - ncdf((math.log(level / S) + sig * sig / 2) / sig)
            hit += rows[t + n][4] > level
            k += 1
        indep = max(k // n, 1)
        act = hit / k
        out.append({"x": x, "pred": pred / k, "act": act, "n": k,
                    "ci": 1.96 * math.sqrt(max(act * (1 - act), 1e-9) / indep)})
    return out


def money(x):
    return ("+" if x >= 0 else "-") + "$" + f"{abs(x):,.0f}"


def verdict(s):
    d = s["act"] - s["pred"]
    if abs(d) <= max(s["ci"], 0.03):
        return "和预测接近", ""
    return ("实际比预测高", "up") if d > 0 else ("实际比预测低", "down")


def build_report(rows, source):
    res = {n: run(rows, n) for n in HORIZONS}
    sections, summary = "", []
    for n in HORIZONS:
        body = ""
        for s in res[n]:
            v, cls = verdict(s)
            body += (f"<tr><td>{s['name']}</td><td>{s['pred'] * 100:.0f}%</td><td class='{cls}'>{s['act'] * 100:.0f}%</td>"
                     f"<td>{(s['act'] - s['pred']) * 100:+.0f}</td><td class='{'up' if s['avg'] >= 0 else 'down'}'>{money(s['avg'])}</td>"
                     f"<td>{money(s['avg_win'])}</td><td>{money(s['avg_loss'])}</td><td>{money(s['worst'])}</td><td class='l'>{v}</td></tr>")
        years = sorted(res[n][0]["years"])
        ybody = ""
        for s in res[n]:
            ybody += f"<tr><td>{s['name']}</td>" + "".join(
                f"<td>{'—' if s['years'][y] is None else format(s['years'][y] * 100, '.0f') + '%'}</td>" for y in years) + "</tr>"
        cal = ""
        for c in calibration(rows, n):
            d = c["act"] - c["pred"]
            ok = abs(d) <= max(c["ci"], 0.03)
            where = "参考价" if c["x"] == 0 else f"参考价{'上方' if c['x'] > 0 else '下方'} {abs(c['x']):g} 倍预期波动"
            cal += (f"<tr><td>{where}</td><td>{c['pred'] * 100:.0f}%</td><td class='{'' if ok else ('up' if d > 0 else 'down')}'>{c['act'] * 100:.0f}%</td>"
                    f"<td>{d * 100:+.0f}（误差 ±{c['ci'] * 100:.0f}）</td><td class='l'>{'准' if ok else ('实际比预测更常发生' if d > 0 else '实际比预测更少发生')}</td></tr>")
        gap = sum(abs(s["act"] - s["pred"]) for s in res[n]) / len(res[n])
        summary.append(f"<li><b>{NAME[n]}</b>：{len(res[n])} 种策略里，预测胜率和实际胜率平均相差 <b>{gap * 100:.0f}</b> 个百分点；"
                       f"相差超过误差范围的有 <b>{sum(1 for s in res[n] if verdict(s)[1])}</b> 种。</li>")
        sections += f"""
<section class="panel"><h2>{NAME[n]}</h2>
<div class="tablewrap"><table><thead><tr><th>策略</th><th>预测胜率</th><th>实际胜率</th><th>相差（百分点）</th><th>平均每笔盈亏</th><th>赚的时候平均赚</th><th>亏的时候平均亏</th><th>最差的一笔</th><th class="l">结论</th></tr></thead><tbody>{body}</tbody></table></div>
<p class="sub">共 {res[n][0]['n']} 笔，每个交易日建一笔。金额是一组（每条腿一张）的美元数，已按现在的股价 ${rows[-1][4]:.2f} 折算，方便和模拟器里的数字对照。</p>
<h2>概率本身准不准：高概率是不是真的高，低概率是不是真的低</h2>
<div class="tablewrap"><table><thead><tr><th>到期时收盘高于这个价位</th><th>预测的概率</th><th>实际发生的比例</th><th>相差（百分点）</th><th class="l">结论</th></tr></thead><tbody>{cal}</tbody></table></div>
<p class="sub">这张表检验的是模拟器算概率的方法本身。从上到下，预测的概率由高到低。如果预测 85% 的事实际发生了 85% 左右、预测 15% 的事实际发生了 15% 左右，说明高概率确实高、低概率确实低。所有策略的胜率都是由这些概率组合出来的。</p>
<h2>各年的实际胜率</h2>
<div class="tablewrap"><table><thead><tr><th>策略</th>{''.join(f'<th>{y}</th>' for y in years)}</tr></thead><tbody>{ybody}</tbody></table></div>
</section>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SYMBOL} 策略胜率回测</title>
<style>
:root{{--bg:#eef0f3;--panel:#fff;--ink:#16202e;--muted:#5d6878;--line:#d5dae2;--accent:#1f5fbf;--up:#0d7a4f;--down:#c0392b;
  --font:"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;--num:Consolas,"SFMono-Regular",monospace}}
@media (prefers-color-scheme: dark){{:root{{--bg:#10151d;--panel:#19212c;--ink:#e6ebf2;--muted:#93a0b3;--line:#2c3746;--accent:#6fa4f5;--up:#3fc48d;--down:#f0776a;color-scheme:dark}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.55}}
main{{max-width:1180px;margin:0 auto;padding:20px 16px 48px;display:flex;flex-direction:column;gap:16px}}
h1{{font-size:22px;margin:0;font-weight:600}} h2{{font-size:16px;margin:0;font-weight:600}} p{{margin:0;max-width:95ch}}
.sub{{color:var(--muted);font-size:13px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:12px}}
.tablewrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px;min-width:760px}}
th,td{{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}} th{{color:var(--muted);font-weight:500}}
td{{font-family:var(--num)}} th:first-child,td:first-child,.l{{text-align:left;font-family:var(--font)}}
td.up{{color:var(--up);font-weight:600}} td.down{{color:var(--down);font-weight:600}}
ul{{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:5px;max-width:100ch}}
</style></head><body><main>
<div><h1>{SYMBOL} 策略胜率回测</h1>
<p class="sub">数据：{rows[0][0]} 到 {rows[-1][0]}，共 {len(rows)} 个交易日 · 隐含波动率来源：{source} · 生成于 {datetime.now():%Y-%m-%d %H:%M}</p></div>

<section class="panel"><h2>结论</h2>
<ul>{''.join(summary)}</ul>
<p>看这张报告时注意两件事。第一，“实际胜率”和“预测胜率”接近，说明模拟器的胜率可信；差得多的策略，以后用模拟器时要把它的胜率往实际值的方向调整。第二，胜率高不代表赚钱，要同时看“平均每笔盈亏”：它是正数才说明这个策略在这段历史里长期是赚的。</p></section>
{sections}
<section class="panel"><h2>怎么读这些表</h2><ul class="sub">
<li>预测胜率：建仓那天，按和模拟器相同的算法算出的盈利概率，这里是所有交易日的平均值。</li>
<li>实际胜率：持有到期后真的赚钱的比例。“相差”是实际减预测，正数表示实际比预测的更容易赚钱。</li>
<li>平均每笔盈亏：所有交易的平均结果。“赚的时候平均赚”和“亏的时候平均亏”分开看，可以看出一个策略是“常赚小钱、偶尔亏大钱”还是相反。</li>
<li>最差的一笔：这段历史里单笔亏得最多的一次，用来估计坏情况下要承受多少。</li>
<li>各年的实际胜率：同一个策略在不同年份差别可能很大，说明结果和市场环境有关。</li>
</ul></section>

<section class="panel"><h2>这个回测和真实交易的差别</h2><ul class="sub">
<li>期权价格是用当天的隐含波动率按公式算出来的理论价，不是当时的真实成交价。IBKR 不提供已到期期权的历史报价，所以只能这样近似。</li>
<li>所有行权价用同一个隐含波动率。真实市场里下跌保护更贵，所以真实情况下卖出 Put 一侧收到的钱会比这里多，买入 Put 一侧付的钱也更多。</li>
<li>没有计入买卖价差和手续费。真实交易每笔都要付这些成本，四条腿的策略付得最多，实际的平均盈亏会比表里的差。</li>
<li>行权价取的是理想位置，真实交易只能选交易所挂出的行权价。隐含波动率用的是 30 天期的，1 周的结果只是近似。</li>
<li>每个交易日都建一笔，相邻的交易互相重叠，不是独立的。判断“相差是否显著”时用的是互不重叠的样本，1 个月持有期每年只有 12 个左右。</li>
<li>假设每一笔都持有到期，中途不止损、不提前平仓。过去的结果不保证以后成立，这不是买卖建议。</li>
</ul></section>
</main></body></html>"""


def main():
    rows, source = br.load()
    if len(rows) < 300:
        raise SystemExit("Not enough data: %d days" % len(rows))
    out = os.path.join(br.BASE, f"backtest_strategies_{SYMBOL}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_report(rows, source))
    for n in HORIZONS:
        print(f"--- hold {n} trading days ---")
        for i, s in enumerate(run(rows, n)):
            print(f"{i + 1:>2}. predicted {s['pred'] * 100:3.0f}%  actual {s['act'] * 100:3.0f}%  avg {s['avg']:+8.0f}  worst {s['worst']:+9.0f}")
    print("report:", out)
    if "--no-browser" not in sys.argv:
        webbrowser.open(Path(out).as_uri())


if __name__ == "__main__":
    main()
