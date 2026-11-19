# -*- coding: utf-8 -*-
"""
连接诊断：检查 IB Gateway 能不能给出股票和期权的行情，并打印 IBKR 返回的所有提示。

用法（先登录 IB Gateway）：
    python diagnose.py          检查 QQQ
    python diagnose.py NVDA     检查别的代码
把窗口里打印出来的全部内容复制下来发给 Claude。
"""
import sys, math
from datetime import date

PORTS = [int(sys.argv[sys.argv.index("--port") + 1])] if "--port" in sys.argv else [4002, 4001, 7497, 7496]
ARGS = [a for i, a in enumerate(sys.argv[1:], 1) if a != "--port" and sys.argv[i - 1] != "--port"]
SYMBOL = (ARGS[0] if ARGS else "QQQ").upper()
TYPES = {1: "实时", 2: "冻结", 3: "延迟", 4: "延迟冻结"}

from ib_async import IB, Stock, Option, util
if "ipykernel" in sys.modules:
    util.startLoop()

msgs = []


def on_error(reqId, code, text, contract=None):
    msgs.append((code, text))
    print(f"   [IBKR 提示 {code}] {text}")


def show(x):
    try:
        x = float(x)
        return "无" if math.isnan(x) or x < 0 else f"{x:.2f}"
    except (TypeError, ValueError):
        return "无"


ib = IB()
ib.errorEvent += on_error
print("1. 连接 IB Gateway（依次尝试端口 " + "、".join(map(str, PORTS)) + "）")
for PORT in PORTS:
    try:
        ib.connect("127.0.0.1", PORT, clientId=19, readonly=True, timeout=8)
        print(f"   端口 {PORT} 连上了")
        break
    except Exception as e:
        print(f"   端口 {PORT} 连不上：", type(e).__name__)
        try:
            ib.disconnect()
        except Exception:
            pass
else:
    print("   都连不上。请确认 IB Gateway 已经登录，并在 配置 → 设置 → API → 设置 里勾选了“启用 ActiveX 和套接字客户端”。")
    sys.exit()
accts = ib.managedAccounts()
print("   已连接。账户：", ", ".join(accts) or "无",
      "（模拟账户通常以 DU 开头）" if accts else "")
print("   IBKR 服务器时间：", ib.reqCurrentTime())

stk = Stock(SYMBOL, "SMART", "USD")
ib.qualifyContracts(stk)
print(f"\n2. 查找股票 {SYMBOL}：", "找到" if stk.conId else "没找到")
if not stk.conId:
    sys.exit()

for mdt in (1, 3):
    ib.reqMarketDataType(mdt)
    t = ib.reqMktData(stk, "")
    ib.sleep(4)
    print(f"\n3. 股票行情，请求的是“{TYPES[mdt]}”，实际收到的是“{TYPES.get(t.marketDataType, t.marketDataType)}”")
    print(f"   买价 {show(t.bid)}   卖价 {show(t.ask)}   最新价 {show(t.last)}   昨收 {show(t.close)}   时间 {t.time}")
    ib.cancelMktData(stk)

ib.reqMarketDataType(3)
params = [p for p in ib.reqSecDefOptParams(SYMBOL, "", "STK", stk.conId) if p.exchange == "SMART"]
print(f"\n4. 期权链：找到 {len(params)} 组")
if not params:
    sys.exit()
chain = max(params, key=lambda p: len(p.expirations))
today = date.today().strftime("%Y%m%d")
exp = [e for e in sorted(chain.expirations) if e >= today][0]
t = ib.reqTickers(stk)[0]
px = t.marketPrice()
if not px or px != px:
    px = t.close
k = min(chain.strikes, key=lambda s: abs(s - px))
print(f"   最近的到期日 {exp}，参考价 {show(px)}，平值行权价 {k}")

for right in "CP":
    c = Option(SYMBOL, exp, k, right, "SMART", currency="USD", tradingClass=chain.tradingClass)
    ib.qualifyContracts(c)
    if not c.conId:
        print(f"\n5. 期权 {k}{right}：合约没找到")
        continue
    o = ib.reqMktData(c, "101")
    ib.sleep(5)
    print(f"\n5. 期权 {exp} {k} {right}（收到的是“{TYPES.get(o.marketDataType, o.marketDataType)}”）")
    print(f"   买价 {show(o.bid)}   卖价 {show(o.ask)}   最新价 {show(o.last)}   昨收 {show(o.close)}")
    oi = o.callOpenInterest if right == "C" else o.putOpenInterest
    print(f"   持仓量 {show(oi)}   隐含波动率 {show(o.modelGreeks.impliedVol if o.modelGreeks else None)}")
    ib.cancelMktData(c)

ib.disconnect()
codes = {c for c, _ in msgs}
print("\n6. 判断")
if 10197 in codes:
    print("   实盘账户正在别处登录（手机 App、网页或另一台电脑），模拟账户这时收不到行情。退出那边的登录再试。")
if codes & {354, 10090, 10091, 10167, 10168}:
    print("   账户没有订阅相应的实时行情，只能用延迟数据。")
if 2104 in codes or 2106 in codes or 2158 in codes:
    print("   行情服务器连接正常（2104/2106/2158 是正常提示）。")
if not msgs:
    print("   IBKR 没有给出任何提示。")
print("\n把上面全部内容复制发给 Claude。")
