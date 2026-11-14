# -*- coding: utf-8 -*-
"""
持仓价位回测

检验的问题：看板上的 Call 墙、Put 墙和最大痛点，历史上到底有没有用？

做法：对每一个到期日，回到它之前第 5 个交易日的开盘前，只用当时已经公布的持仓量算出
Call 墙、Put 墙和最大痛点（规则和看板相同），再看之后到到期日的真实走势，
并和“这些价位完全没有作用”时应该出现的比例作比较。

用法：
    python backtest_walls.py --demo                 用模拟数据看流程和报告（结论没有意义）
    python backtest_walls.py QQQ --oi 文件.csv       用历史持仓量文件回测
    python backtest_walls.py QQQ                    用看板每天保存在 options_data 里的数据回测

持仓量文件需要这几列（列名不分大小写，多余的列会被忽略）：
    date                         数据日期
    exdate / expiry / expiration 到期日
    cp_flag / right / type       C 或 P
    strike_price / strike        行权价（OptionMetrics 的 strike_price 是乘了 1000 的，会自动换算）
    open_interest / oi           持仓量
学校数据库里的 OptionMetrics（WRDS）导出的期权价格文件可以直接用。
"""
import os, sys, csv, json, math, random, webbrowser
from pathlib import Path
from datetime import date, datetime, timedelta

import backtest_range as br      # 复用它的价格和隐含波动率历史

SYMBOL = br.SYMBOL
LEAD = 5                         # 在到期日之前第几个交易日做预测
OI_FILE = None
if "--oi" in sys.argv:
    i = sys.argv.index("--oi")
    OI_FILE = sys.argv[i + 1] if i + 1 < len(sys.argv) else None


def iso(d):
    """把几种常见的日期写法统一成 YYYY-MM-DD。"""
    d = d.strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    if "/" in d:
        m, dd, y = d.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(dd):02d}"
    if len(d) == 9 and d[2:5].isalpha():          # 03JAN2023
        return datetime.strptime(d, "%d%b%Y").strftime("%Y-%m-%d")
    return d[:10]


# ---------------------------------------------------------------- 读取持仓量
# 返回 {数据日期: {到期日: [(行权价, 是否 Call, 持仓量), ...]}}

def load_oi_csv(path, last_price):
    def pick(names, *cands):
        low = {n.lower().strip(): n for n in names}
        for c in cands:
            if c in low:
                return low[c]
        raise SystemExit("Column not found in %s: one of %s" % (path, ", ".join(cands)))

    out, n = {}, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cd = pick(rd.fieldnames, "date")
        ce = pick(rd.fieldnames, "exdate", "expiry", "expiration", "expiration_date")
        cr = pick(rd.fieldnames, "cp_flag", "right", "type", "option_type", "contract_type")
        ck = pick(rd.fieldnames, "strike_price", "strike")
        co = pick(rd.fieldnames, "open_interest", "oi")
        raw = []
        for row in rd:
            try:
                k, o = float(row[ck]), float(row[co] or 0)
            except ValueError:
                continue
            raw.append((iso(row[cd]), iso(row[ce]), row[cr].strip().upper()[:1] == "C", k, o))
    if not raw:
        raise SystemExit("No usable rows in " + path)
    ks = sorted(r[3] for r in raw)
    scale = 1000.0 if ks[len(ks) // 2] > last_price * 20 else 1.0     # OptionMetrics 的行权价乘了 1000
    for d, e, call, k, o in raw:
        if e < d:
            continue
        out.setdefault(d, {}).setdefault(e, []).append((k / scale, call, o))
        n += 1
    print("loaded", n, "rows from", path, "(strike scale 1/%d)" % scale)
    return out


def load_oi_snapshots():
    """读取看板每天保存的数据（options_data/代码_日期.json）。"""
    folder, out = os.path.join(br.BASE, "options_data"), {}
    if not os.path.isdir(folder):
        return out
    for fn in sorted(os.listdir(folder)):
        if not (fn.startswith(SYMBOL.replace(" ", "_") + "_") and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        day = d.get("generated", "")[:10]
        for r in d.get("rows", []):
            if r[3] is not None:
                out.setdefault(day, {}).setdefault(iso(r[0]), []).append((r[1], r[2] == "C", r[3]))
    print("loaded", len(out), "daily snapshots from", folder)
    return out


def demo_oi(rows):
    """模拟的持仓量：整数关口持仓多，Call 集中在上方、Put 集中在下方。持仓和之后的走势没有任何关系。"""
    rnd, out = random.Random(11), {}
    days = [r[0] for r in rows]
    fridays = [d for d in days if date.fromisoformat(d).weekday() == 4]
    for t in range(1, len(rows)):
        s = rows[t - 1][4]
        exps = [e for e in fridays if days[t] <= e][:3]
        step = 1 if s < 800 else 5
        for e in exps:
            k, lst = math.floor(s * 0.88 / step) * step, []
            while k <= s * 1.12:
                lump = 30000 if k % 10 == 0 else 9000 if k % 5 == 0 else 2500
                for call in (True, False):
                    center = s * (1.035 if call else 0.965) * (1 + rnd.gauss(0, 0.004))
                    lst.append((float(k), call, round(lump * math.exp(-((k - center) / (s * 0.035)) ** 2) * rnd.uniform(0.6, 1.4))))
                k += step
            out.setdefault(days[t], {})[e] = lst
    return out


# ---------------------------------------------------------------- 计算

def levels(chain, ref, move):
    """和看板相同的规则。chain: [(行权价, 是否 Call, 持仓量)]。"""
    call, put = {}, {}
    for k, c, o in chain:
        if ref - 2.5 * move <= k <= ref + 2.5 * move:
            (call if c else put)[k] = (call if c else put).get(k, 0) + o
    ks = sorted(set(call) | set(put))
    if not ks or sum(call.values()) + sum(put.values()) <= 0:
        return None
    cw = max((k for k in ks if ref <= k <= ref + 2 * move), key=lambda k: call.get(k, 0), default=None)
    pw = max((k for k in ks if ref - 2 * move <= k <= ref), key=lambda k: put.get(k, 0), default=None)
    pain = lambda s: sum(call.get(k, 0) * max(s - k, 0) + put.get(k, 0) * max(k - s, 0) for k in ks)
    if cw is None or pw is None or call.get(cw, 0) <= 0 or put.get(pw, 0) <= 0:
        return None
    return {"call_wall": cw, "put_wall": pw, "max_pain": min(ks, key=pain)}


def study(rows, oi):
    """rows: [日期, 开, 高, 低, 收, 隐含波动率]。每个到期日产生一条观察。"""
    idx = {r[0]: i for i, r in enumerate(rows)}
    expiries = sorted({e for d in oi for e in oi[d]})
    # 基准用的历史样本：每个交易日起算 LEAD 天，收盘、最高、最低各走了几倍预期波动
    hist = []
    for t in range(1, len(rows) - LEAD + 1):
        r0, s0 = rows[t - 1][4], rows[t - 1][5] * math.sqrt(LEAD / 252)
        w = rows[t:t + LEAD]
        hist.append(((w[-1][4] / r0 - 1) / s0, (max(x[2] for x in w) / r0 - 1) / s0, (min(x[3] for x in w) / r0 - 1) / s0))
    frac = lambda sel, cond: (sum(1 for h in sel if cond(h)) / len(sel)) if sel else None
    obs = []
    for e in expiries:
        if e not in idx:
            continue
        ie, it = idx[e], idx[e] - LEAD + 1          # it：做预测的那个交易日（开盘前）
        if it < 1 or rows[it][0] not in oi:
            continue
        day = rows[it][0]
        ref, iv = rows[it - 1][4], rows[it - 1][5]   # 只用前一天收盘时已知的价格和隐含波动率
        sig = iv * math.sqrt(LEAD / 252)
        move = ref * sig
        chain = [x for ex, lst in oi[day].items() if day <= ex <= e for x in lst]
        lv = levels(chain, ref, move)
        if not lv:
            continue
        cw, pw, mp = lv["call_wall"], lv["put_wall"], lv["max_pain"]
        close = rows[ie][4]
        hi = max(r[2] for r in rows[it:ie + 1])
        lo = min(r[3] for r in rows[it:ie + 1])
        # 基准：把同样距离（以预期波动的倍数计）的价位放到历史上每一个交易日，看同样的事发生的比例
        zc, zp, zm = (cw / ref - 1) / sig, (pw / ref - 1) / sig, ((mp + ref) / 2 / ref - 1) / sig
        obs.append({
            "day": day, "exp": e, "ref": ref, "cw": cw, "pw": pw, "mp": mp, "close": close, "move": move,
            "inside": pw <= close <= cw, "p_inside": frac(hist, lambda h: zp <= h[0] <= zc),
            "touch_c": hi >= cw, "below_c": close < cw, "touch_p": lo <= pw, "above_p": close > pw,
            "p_below_c": frac([h for h in hist if h[1] >= zc], lambda h: h[0] < zc),
            "p_above_p": frac([h for h in hist if h[2] <= zp], lambda h: h[0] > zp),
            "near_mp": abs(close - mp) < abs(close - ref),
            "p_near_mp": frac(hist, (lambda h: h[0] > zm) if mp > ref else (lambda h: h[0] < zm)) if mp != ref else 0.5,
        })
    return obs


# ---------------------------------------------------------------- 报告

def test_row(name, flags, expected, note):
    n = len(flags)
    if n < 5:
        return f"<tr><td>{name}</td><td>{n}</td><td colspan='4' class='l'>样本太少，不作判断</td><td class='l'>{note}</td></tr>", None
    act = sum(flags) / n
    ci = 1.96 * math.sqrt(max(act * (1 - act), 1e-9) / n)
    d = act - expected
    if abs(d) <= max(ci, 0.03):
        v, cls = "没有发现作用", ""
    else:
        v, cls = ("比没有作用时高，可能有用", "up") if d > 0 else ("比没有作用时低，可能起反作用", "down")
    row = (f"<tr><td>{name}</td><td>{n}</td><td class='{cls}'>{act * 100:.0f}%</td><td>{expected * 100:.0f}%</td>"
           f"<td>{d * 100:+.0f}（误差 ±{ci * 100:.0f}）</td><td class='l'>{v}</td><td class='l'>{note}</td></tr>")
    return row, (name, act, expected, ci, v)


def build_report(rows, obs, source, demo):
    mean = lambda xs: (lambda v: sum(v) / len(v) if v else 0.5)([x for x in xs if x is not None])
    tc, tp = [o for o in obs if o["touch_c"]], [o for o in obs if o["touch_p"]]
    tests = [
        test_row("到期日收盘落在 Put 墙和 Call 墙之间", [o["inside"] for o in obs], mean([o["p_inside"] for o in obs]),
                 "基准：历史上任意一天，价格落在同样宽度区间里的比例"),
        test_row("碰到 Call 墙之后，到期日收在墙的下方", [o["below_c"] for o in tc], mean([o["p_below_c"] for o in tc]),
                 "基准：历史上任意一天，碰到同样距离的普通价位后收在它下方的比例"),
        test_row("碰到 Put 墙之后，到期日收在墙的上方", [o["above_p"] for o in tp], mean([o["p_above_p"] for o in tp]),
                 "基准：历史上任意一天，碰到同样距离的普通价位后收在它上方的比例"),
        test_row("到期日收盘离最大痛点比离起始价更近", [o["near_mp"] for o in obs], mean([o["p_near_mp"] for o in obs]),
                 "基准：历史上任意一天，价格朝同一方向走过同样距离的比例"),
    ]
    body = "".join(t[0] for t in tests)
    found = [t[1] for t in tests if t[1]]
    useful = [f[0] for f in found if "可能有用" in f[4]]
    if not found:
        overall = "样本太少，还得不出结论。"
    elif useful:
        overall = "有迹象显示以下项目可能有用：" + "；".join(useful) + "。其余项目没有发现作用。"
    else:
        overall = "这批数据里，没有发现 Call 墙、Put 墙或最大痛点对之后的走势有作用：实际结果和“它们完全没有作用”时应该出现的比例没有明显差别。"

    detail = ""
    for o in obs[-40:][::-1]:
        detail += (f"<tr><td>{o['day']}</td><td>{o['exp']}</td><td>{o['ref']:.2f}</td><td>{o['pw']:g}</td><td>{o['cw']:g}</td>"
                   f"<td>{o['mp']:g}</td><td>{o['close']:.2f}</td><td class='l'>{'在两墙之间' if o['inside'] else ('高于 Call 墙' if o['close'] > o['cw'] else '低于 Put 墙')}</td>"
                   f"<td class='l'>{'碰到过 Call 墙' if o['touch_c'] else ''}{'　' if o['touch_c'] and o['touch_p'] else ''}{'碰到过 Put 墙' if o['touch_p'] else ''}</td></tr>")
    cw_d = mean([(o["cw"] - o["ref"]) / o["move"] for o in obs])
    pw_d = mean([(o["ref"] - o["pw"]) / o["move"] for o in obs])
    banner = ('<section class="panel warnbox"><b>这是模拟数据。</b>这里的持仓量是程序随机生成的，和价格走势没有任何关系，'
              '所以下面的结论没有意义，只用来演示回测的流程和报告的样子。真实数据到了之后用 --oi 重新运行。</section>') if demo else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SYMBOL} 持仓价位回测</title>
<style>
:root{{--bg:#eef0f3;--panel:#fff;--ink:#16202e;--muted:#5d6878;--line:#d5dae2;--up:#0d7a4f;--down:#c0392b;--warnbg:#fff4e0;
  --font:"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;--num:Consolas,"SFMono-Regular",monospace}}
@media (prefers-color-scheme: dark){{:root{{--bg:#10151d;--panel:#19212c;--ink:#e6ebf2;--muted:#93a0b3;--line:#2c3746;--up:#3fc48d;--down:#f0776a;--warnbg:#3a2c12;color-scheme:dark}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.55}}
main{{max-width:1180px;margin:0 auto;padding:20px 16px 48px;display:flex;flex-direction:column;gap:16px}}
h1{{font-size:22px;margin:0;font-weight:600}} h2{{font-size:16px;margin:0;font-weight:600}} p{{margin:0;max-width:95ch}}
.sub{{color:var(--muted);font-size:13px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:12px}}
.warnbox{{background:var(--warnbg);display:block}}
.tablewrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px;min-width:820px}}
th,td{{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line)}} th{{color:var(--muted);font-weight:500;white-space:nowrap}}
td{{font-family:var(--num);white-space:nowrap}} th:first-child,td:first-child,.l{{text-align:left;font-family:var(--font);white-space:normal}}
td.up{{color:var(--up);font-weight:600}} td.down{{color:var(--down);font-weight:600}}
ul{{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:5px;max-width:100ch}}
</style></head><body><main>
<div><h1>{SYMBOL} 持仓价位回测</h1>
<p class="sub">持仓量来源：{source} · 共 {len(obs)} 个到期日 · {obs[0]['exp']} 到 {obs[-1]['exp']} · 在每个到期日之前第 {LEAD} 个交易日的开盘前做预测 · 生成于 {datetime.now():%Y-%m-%d %H:%M}</p></div>
{banner}
<section class="panel"><h2>结论</h2><p>{overall}</p>
<p class="sub">Call 墙平均在起始价上方 {cw_d:.2f} 倍预期波动的位置，Put 墙平均在下方 {pw_d:.2f} 倍的位置。</p></section>

<section class="panel"><h2>四项检验</h2>
<div class="tablewrap"><table><thead><tr><th>检验的说法</th><th>样本数</th><th>实际比例</th><th>没有作用时应有的比例</th><th>相差（百分点）</th><th class="l">结论</th><th class="l">基准是怎么来的</th></tr></thead><tbody>{body}</tbody></table></div>
<p class="sub">只有“实际比例”明显高于“没有作用时应有的比例”，而且差距超过误差范围，才算这个价位可能有用。差距在误差范围内，就不能说它有用。</p></section>

<section class="panel"><h2>逐个到期日的记录（最近 40 个）</h2>
<div class="tablewrap"><table><thead><tr><th>预测日</th><th>到期日</th><th>起始价</th><th>Put 墙</th><th>Call 墙</th><th>最大痛点</th><th>到期日收盘</th><th class="l">结果</th><th class="l">期间</th></tr></thead><tbody>{detail}</tbody></table></div>
<p class="sub">起始价是预测日前一个交易日的收盘价。可以随便挑几行，自己核对当时的价位和后来的真实价格。</p></section>

<section class="panel"><h2>方法和局限</h2><ul class="sub">
<li>没有用到未来的数据：预测日当天的持仓量是开盘前就公布的（它反映的是前一个交易日结束时的持仓），起始价和隐含波动率用的是前一个交易日的收盘值，之后的价格只用来对答案。</li>
<li>Call 墙、Put 墙、最大痛点的算法和看板相同：只统计起始价上下 2.5 倍预期波动以内的行权价，墙只在 2 倍以内找，持仓量把预测日到到期日之间到期的期权加在一起。</li>
<li>基准的算法：把“距离起始价同样多倍预期波动”的价位放到这段历史的每一个交易日上，统计同样的事情发生的比例。这样比较的就是“期权墙”和“同样距离的普通价位”，隐含波动率偏高或偏低造成的影响已经被扣除。基准用了整段历史，它只是用来比较的参照，不参与预测。</li>
<li>每个到期日只算一条观察，互相不重叠。每周到期一年大约 50 条，所以需要几年的数据结论才稳。</li>
<li>“碰到墙”是按每天的最高价和最低价判断的，看不到盘中的先后顺序。</li>
<li>即使某一项显示“可能有用”，也只说明在这段历史里有这个统计规律，不保证以后成立，也不是买卖建议。</li>
</ul></section>
</main></body></html>"""


def main():
    demo = br.DEMO
    rows, src = br.load()
    if demo:
        oi, source = demo_oi(rows), "模拟数据"
    elif OI_FILE:
        oi, source = load_oi_csv(OI_FILE, rows[-1][4]), os.path.basename(OI_FILE)
    else:
        oi, source = load_oi_snapshots(), "看板每天保存的数据"
    obs = study(rows, oi)
    print(len(obs), "expiries with a usable snapshot", LEAD, "trading days earlier")
    if len(obs) < 5:
        raise SystemExit("Not enough history yet. Use --oi with a historical open-interest file, "
                         "or keep querying the symbol in the panel on each trading day.")
    out = os.path.join(br.BASE, f"backtest_walls_{SYMBOL}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_report(rows, obs, source, demo))
    n = len(obs)
    print(f"inside walls: {sum(o['inside'] for o in obs) / n * 100:.0f}%  (no-effect baseline {sum(o['p_inside'] or 0 for o in obs) / n * 100:.0f}%)")
    print("report:", out)
    if "--no-browser" not in sys.argv:
        webbrowser.open(Path(out).as_uri())


if __name__ == "__main__":
    main()
