# 自动模拟交易（paper-trading-sim）

## 这是什么
本地跑的模拟盘：每天收盘后自动拉数据 → 跑信号 → 模拟成交 → 写日报/周报。
验证策略赚不赚钱，不验证人执不执行（后者是 10 月《个人交易员训练系统 V1.0》在做的事）。

## 设计决策（用户可一句话否决）
1. **独立虚拟盘**：sim_a（CNY 10万）/ sim_hk（HKD 10万）/ sim_crypto（USDT 2万），与实盘隔离。
2. **规则配置化**：所有阈值在 `config/strategy.yaml`，引擎不硬编码。10 月规则定稿后只改配置。
3. **只读冻结系统**：复用母板的数据接口逻辑，不碰、不写任何冻结文件。
4. **港股+加密先行**：A股休市到 10-08，先行验证管线；10-08 起全量。

## 目录
- `MVP方案.md` —— 方案文档（含终局路线图第 8 节）
- `config/strategy.yaml` —— 策略规则（DRAFT，待 10 月定稿）
- `data/quotes.py` —— 数据层（腾讯 A股/港股 + 币安加密 + 汇率）
- `engine/sim.py` —— 模拟撮合引擎（费用/T+1/涨跌停）
- `db/schema.sql` —— SQLite 表结构
- `run_daily.py` —— 每日管线（P0：拉数据→存DB；P1：+信号+模拟成交+日报）

## 运行
```bash
cd ~/workspace/paper-trading-sim
python3 run_daily.py                    # 每日管线：拉数据→信号→模拟成交→日报
（engine/batch_test.py 已于 2026-10-26 模块E清理删除：DRAFT 策略稳健性脚本，策略已废弃、无调用方）
.venv/bin/streamlit run app.py          # 网页版（http://localhost:8501）
```

## 关键测试复现（公开仓可独立执行，2026-10-31 专项整改 P2-02）

公开仓（paper-sim-8502）内含三套关键测试，可独立复现：
`tests/test_safety_supplements.py`（四项安全补充 + 机构影子口径）、
`tests/test_inst_shadow_chain.py`（影子链抓取/收敛/守卫）、
`tests/test_audit_remediation.py`（连续天数日历校验、状态机原子性
含多进程并发、BUY 作废来源状态、运行时账本隔离、哈希规范向量）。

- 环境：Python 3.12.3、pytest 9.1.1（`pip install pytest==9.1.1`）。
  三套测试本身只用 Python 标准库 + pytest；应用运行依赖见
  requirements.txt（streamlit 1.64.0 等）。
- 测试数据库：全部由 pytest 的 tmp_path 在临时目录新建独立
  SQLite，不读取、不写入任何生产数据库，不依赖本地路径、
  私有密钥或网络（机构抓取用注入的假接口）。
- 运行命令（必须在仓库根目录执行，且用 `python -m pytest`
  以保证仓库根目录在 sys.path 上）：
```bash
python -m pytest tests/test_safety_supplements.py \
  tests/test_inst_shadow_chain.py tests/test_audit_remediation.py -q
```
- 表级哈希复算（规范 hash-spec-v1，见 tools/hash_baseline.py
  文件头全文）：`python tools/hash_baseline.py --db <库> \
  --table inst_lhb_daily --order-by "date, symbol"`（只读打开）。

## 进度
- [x] P0：骨架 + 数据层实测打通（2026-10-02）：腾讯港股/ Kraken BTC / 新浪 HKDCNY 三源入库
- [x] P1：信号+撮合+日报管线跑通（2026-10-02）：87 信号/84 成交，现金流独立重算对账 <0.01，首份日报已出
- [x] P3：Streamlit 看板提前交付（2026-10-02）：净值曲线/持仓/信号/成交 + 开始·暂停 + 自动·手动 + 手动确认成交；控制逻辑联调全过（暂停0成交/手动需批准/切回自动重算一致）
- [x] 手机应用（2026-10-02）：私人 fullstack web artifact「模拟盘」（slug=space，非公开链接，手机 Muse App Library 打开）。三市场同页、账户独立；可查看净值/持仓/信号/成交，可操作 开始·暂停 / 自动·手动 / 批准·跳过。接线：sync_snapshot.py 生成快照→pushSnapshot 推送；apply_commands.py 执行取回的指令（pullCommands→执行→ackCommands）。cron：paper-sim-nightly 每晚 17:12 跑批后推送快照；paper-sim-cmd-poll 每 15 分钟同步手机指令。指令回路已端到端实测（set_mode manual↔auto，管线重算一致）
- [x] 展示优化v2（2026-10-02晚）：已实现/未实现盈亏分开、顶部运行状态条（状态/模式/asof/待审批数）、交易日志。
  验收修复P0×2：①前端对60天截断fills求和会少算已实现盈亏→快照加服务端字段 realized_pnl（全量SUM）/pending_approvals，前端改用；
  ②管线全量重算在切手动模式时清空历史fills（84笔+净值曲线被拍平）→已逐笔验证恢复（84笔、87426.12一致），管线改为fills追加制：历史成交为事实不可删，控制状态只决定新信号是否成交；明确跳过的信号任何模式永不成交。
- [ ] 持仓热力图（treemap，按市值分块、盈亏着色）：已列入优化清单，验证优先阶段暂不施工
- [x] 后台测试（2026-10-02）：8 标的×约700天×MA(10/20/30)=24 组回放，引擎无崩溃；结论：DRAFT 规则跨标的不稳定（HK9660 +77% vs HK3690 -27%），参数极差最大 68%，**未达稳健性要求**，须走规则形式化重做，不调参。报告：reports/后台测试-2026-10-02.md
- [ ] P2：A股接入（10-08）
- 每晚约 17:12 cron `paper-sim-nightly` 自动跑管线（后台测试周+日常），异常才汇报

---

## 独立交易终端 (8502) —— 唯一最终系统

2026-10-16 用户拍板：8502 跑过验证终端后将其下线，只留最强。

### 定位
- **绝对独立**：数据独立 / 计算独立 / 决策独立 / 验证独立 / 回执独立
- **唯一外部依赖**：行情源 + 人工券商下单
- **人工执行**：Web不连接券商、不发送订单，信号看完后去券商 App 手动建单

### 本地运行
```bash
.venv/bin/streamlit run app.py --server.port 8502 --server.address 127.0.0.1 --server.headless true
```

### 云端部署 (Streamlit Cloud)
1. 推送代码到 GitHub
2. 在 Streamlit Cloud 新建应用，指向本仓库
3. 设置环境变量 `STREAMLIT_CLOUD=1`（只读模式：回执录入/审批按钮自动禁用）
4. 每次跑批后推送新的 `db/sim.db` 即更新数据

### 板块
- 🏠 工作台：系统状态 + 行情新鲜度 + 今日信号 + 三账户并排 + 待审批 + 事件流
- 📈 行情与策略：分账户策略规则 + 最新行情
- 📝 人工条件单：待建信号提醒 + 券商回执录入
- 💰 成交与本账：持仓（含浮盈亏/K线） + 近期成交
- 🛡 风控与审计：验证KPI + 风控指标 + 审计 + 验证仪表盘
- ⚙ 运维：数据源 + 规则版本 + 指令日志 + 数据完整性

### 分账户策略 (2026-10-16 用户选A)
- sim_a (A股): buyback 接回策略
- sim_hk (港股): ma20 MA20趋势
- sim_crypto (加密): ma20 MA20趋势
