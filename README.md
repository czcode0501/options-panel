# 期权持仓看板

在浏览器里查看美股和 ETF 的期权持仓分布（"期权筹码峰"），以及期权价格推算出的预期波动范围。数据通过 Interactive Brokers 的 API 从你本机登录的 IB Gateway 读取。

## 功能

- 输入股票代码查询，例如 QQQ、SPY、NVDA
- 自由选择期权到期日的时间范围
- 持仓分布的关键价位：POC、价值区、Call 墙、Put 墙、最大痛点、Put/Call 持仓比
- 期权价格推算的波动范围：约 68% 和约 95% 概率的价格区间、考虑偏斜后的上下幅度、平值跨式价格、平值隐含波动率
- K 线（15 分钟、30 分钟、1 小时、2 小时、1 天）与持仓分布并排显示，起始日期可以自己设定，关键价位都有标注
- 持仓最多的行权价列表，带合计和占比
- 隐含波动率和实际波动率的历史对比，显示现在的隐含波动率在过去一年里的位置
- 可以选择 VIX、VXN、RVX、VXD、VIX9D、VIX3M 等波动率指数和所查代码的隐含波动率对比

## 需要准备

1. Interactive Brokers 账户，并安装 [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)（或 TWS）
2. Python 3.10 或更高版本
3. 想看实时数据需要在 IBKR 订阅美国期权行情（OPRA）；不订阅时使用延迟或收盘冻结数据

## 安装

```
git clone https://github.com/czcode0501/options-panel.git
cd options-panel
pip install -r requirements.txt
```

## 使用

1. 登录 IB Gateway，建议用模拟账户（Paper Trading）。在 配置 → 设置 → API → 设置 里确认端口是 4002，并保持"只读 API"勾选。
2. 启动看板：Windows 上双击 `start_panel.bat`，或在终端里运行

   ```
   python options_panel.py
   ```

3. 浏览器会自动打开 `http://127.0.0.1:8765`。输入股票代码后回车即可。

启动看板的终端窗口要一直开着。按 Ctrl+C 停止。

第一次查询一个代码需要读取几百到几千张合约，耗时从一分钟到几分钟不等，页面上有进度条。同一天内再次查询会直接使用已保存的数据，点"重新读取"可以更新。

### 不连接 IBKR 试用

```
python options_panel.py --demo
```

演示模式使用程序生成的模拟数据，用来看界面和功能，数字没有参考意义。

## 波动范围回测

`backtest_range.py` 用来检验看板里的波动范围准不准：对过去每一个交易日，用当天的隐含波动率算出之后一段时间的 68% 和 95% 范围，再统计实际价格落在范围内的比例。

```
python backtest_range.py            # 回测 QQQ，最近 5 年
python backtest_range.py NVDA       # 回测别的代码
python backtest_range.py QQQ 10     # 回测最近 10 年
python backtest_range.py --demo     # 不连接 IBKR，用模拟数据看报告的样子
```

运行前需要登录 IB Gateway。价格和隐含波动率历史都从 IBKR 读取；QQQ、SPY、IWM、DIA 在 IBKR 取不到隐含波动率历史时，改用 Cboe 公布的波动率指数。结果生成一个网页报告并自动打开。

这个回测只检验范围的宽窄，不涉及涨跌方向，也不检验 Call 墙、Put 墙等持仓价位（后者需要历史持仓量数据）。

## 设置

在 `options_panel.py` 开头可以修改：

| 设置 | 默认值 | 说明 |
|---|---|---|
| `WEB_PORT` | 8765 | 看板网页的端口 |
| `IB_PORT` | 4002 | IB Gateway 模拟账户 4002，实盘 4001；TWS 模拟账户 7497，实盘 7496 |
| `CLIENT_ID` | 11 | API 客户端编号，和其他连接 IBKR 的程序冲突时改成别的数字 |
| `MARKET_DATA_TYPE` | 4 | 4 表示没有实时订阅时使用延迟或冻结数据 |

## 隐私和数据

- 程序不保存也不需要你的 IBKR 用户名、密码或账户号。登录只在 IB Gateway 里进行，程序以只读方式连接本机的 IB Gateway，不能下单。
- 看板只在本机运行（`127.0.0.1`），其他设备访问不到。
- 波动率指数的历史数据从 Cboe 官网（cdn.cboe.com）下载，下载失败时改从 IBKR 读取。这是程序唯一会访问的外部网站，只下载公开数据，不上传任何内容。
- 读取到的行情数据保存在本机的 `options_data` 文件夹里，已在 `.gitignore` 中排除，不会被上传。请不要把这些数据公开发布，IBKR 的行情数据不允许转发。

## 数字的含义和局限

- 预期波动范围来自期权价格，只说明市场认为会走多远，不说明方向。
- 持仓量每天只更新一次，而且不区分持有人是买方还是卖方。
- Call 墙、Put 墙、最大痛点是常用的参考价位，会被突破，不是确定的支撑或压力。

## 免责声明

本项目仅用于学习和研究，不构成任何投资建议。期权交易风险很高，可能损失全部本金。
