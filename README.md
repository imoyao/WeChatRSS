# WeChatRSS

> 微信公众号 → RSS 摄取底座，叠加**可插拔的内容分析插件**。
> 当前已接入的第一个分析插件：**二鸟说·基金手抄报**（系数 / 情绪 / 组合 / 实证操作结构化抽取）。

---

## 1. 项目定位

WeChatRSS 解决两件事：

1. **摄取（Ingest）**：用已登录的 `mp.weixin.qq.com` 会话，把指定公众号的历史图文抓取为
   标准 RSS 2.0（`feeds/{账号名}.xml`），里面是**真实的微信原文链接**
   `https://mp.weixin.qq.com/s?__biz=...&mid=...&idx=1&sn=...`。
2. **分析（Analyze）**：对其中某些号做**增值结构化分析**（目前是二鸟说手抄报），把原文
   转成机器可读的信号，落盘到 `data/{插件键}/index.json`。

设计原则：**摄取通用、分析可插拔**。加一个新号的分析 = 在 `src/analyzers/` 加一个文件 +
注册一行，主流程（`gen_rss.py` / `analyze.py`）无需改动。

> 与 fundmate 的关系：WeChatRSS 是 fundmate（个人记账工具）外围的**内容扩展**，
> 二鸟说数据已不在 fundmate 仓库，独立维护于此。详见 fundmate 仓库
> `docs/erniao-fetcher-redesign-2026-08-01.md` §12。

---

## 2. 架构

```
                 ┌─────────────────────────────────────────────┐
   公众号列表     │  accounts.txt / WX_ACCOUNTS  (.env)          │
   (名称,biz)     └─────────────────────────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────────────┐
   │  src/gen_rss.py  摄取层（通用，任何号）                      │
   │  · mp getmsg 接口按 __biz 抓取历史图文                      │
   │  · 输出 feeds/{账号名}.xml（含 mp 原文链接 + 摘要）          │
   └──────────────────────────────────────────────────────────┘
                          │ feeds/二鸟说.xml
                          ▼
   ┌──────────────────────────────────────────────────────────┐
   │  src/analyzers/  分析插件层（按号注册，可插拔）              │
   │     base.py        基类 + 共用工具(Ark调用/JSON提取/index读写)│
   │     erniao.py      二鸟说插件：取最新手抄报→全文→Ark结构化     │
   │     __init__.py    ANALYZERS 注册表                        │
   │  src/analyze.py   运行入口（--feed / --article / --from-json）│
   │  → 输出 data/er-niao/index.json（链接归档 + 轻量结构化信号） │
   └──────────────────────────────────────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────────────┐
   │  GitHub Action (rss.yml)  每天 07:00 / 19:00 (北京)         │
   │  sync_booklist → gen_rss → analyze → commit → Pages 部署    │
   └──────────────────────────────────────────────────────────┘
```

**双轨运行**（保证数据不丢）：
- **主链路（CI）**：`rss.yml` 在 `gen_rss` 之后自动跑 `analyze.py`。需 `MP_COOKIE`（抓取）
  与 `ARK_API_KEY`（Ark 结构化）。
- **兜底 / 手动重放（WorkBuddy 自动化）**：用云端 WebFetch 取二鸟说全文（雪球镜像）→ 跑
  `analyze.py --account erniao --article <全文.txt>` → 推送到本仓库。当 CI 的 mp 登录态
  失效时仍能补数据。

---

## 3. 目录结构

```
WeChatRSS/
├── accounts.txt              # 订阅的公众号（每行 名称 或 名称,biz）
├── feeds/                    # gen_rss 产出：每个号一个 RSS XML（含 mp 原文链接）
│   └── 二鸟说.xml
├── data/                     # analyze 产出：结构化信号（进版本库）
│   └── er-niao/
│       └── index.json        # 二鸟说各期结构化归档
├── src/
│   ├── gen_rss.py            # 摄取层：mp getmsg → feeds/*.xml
│   ├── sync_booklist.py      # 从微信读书书单同步账号名到 accounts.txt
│   ├── verify_weread_api.py  # 书单 API 自检
│   ├── analyze.py            # 分析运行入口（调用 analyzers）
│   └── analyzers/            # ★ 可插拔分析插件层
│       ├── __init__.py       #   ANALYZERS 注册表
│       ├── base.py           #   基类 Analyzer + 共用工具
│       └── erniao.py         #   二鸟说插件（第一个实现）
├── .github/workflows/rss.yml # CI：摄取 + 分析 + 部署 Pages
├── requirements.txt
├── .env.example              # 环境变量模板（MP_COOKIE / ARK_*）
└── README.md
```

---

## 4. 二鸟说子项目详解

### 4.1 数据流
`feeds/二鸟说.xml`（最新一期手抄报）→ 用 `MP_COOKIE` 拉该文 `js_content` 全文
→ 火山方舟 Ark 结构化抽取 → 幂等写入 `data/er-niao/index.json`。

### 4.2 产出 schema（`data/er-niao/index.json`）
```json
{
  "source": "erniao",
  "source_name": "二鸟说手抄报",
  "updated_at": "2026-08-01T14:10:00+08:00",
  "latest_issue": 186,
  "issues": [
    {
      "issue_no": 186,
      "title": "手抄报|186期：高切低后，双创半月回调15%",
      "publish_date": "2026-07-17",
      "source_url": "https://xueqiu.com/3502863673/400720074",  // 原文链接（CI 覆盖为 mp 链接）
      "coefficient": 6,                                          // 温度计系数 0-12
      "sentiment": "正常偏热",                                    // 情绪标签（枚举）
      "portfolios": [],                                          // 本期提及组合
      "market_view": "...",                                      // 市场观点摘要
      "empirical_actions": [                                     // 实证操作（独立结构化块）
        {"name": "实证2", "action": "无操作", "note": "..."}
      ],
      "collected_at": "2026-08-01T14:10:00+08:00"
    }
  ]
}
```
- **仓库只存链接归档 + 轻量信号**（约 1KB/期），**不存正文全文**——符合"内容不备份"原则。
- `issue_no` 相同则覆盖更新（幂等），`latest_issue` 取最大期号。
- 情绪枚举：`极热 / 过热 / 较热 / 正常 / 正常偏冷 / 较冷 / 极冷`。
- 操作枚举：`无操作 / 持有 / 加仓 / 减仓 / 定投 / 转换 / 新建仓 / 清仓 / 其他`。

### 4.3 Ark 模型
默认 `doubao-seed-2-1-pro-260628`（火山方舟，OpenAI 兼容）。可在 `.env` 的 `ARK_MODEL`
覆盖。每周只跑几次，成本可忽略。

---

## 5. 本地运行

```bash
pip install -r requirements.txt

# 1) 摄取：生成/更新 feeds/*.xml（需要 MP_COOKIE）
python src/gen_rss.py

# 2) 分析：对注册的分析器跑结构化（默认读 feeds，需要 MP_COOKIE + ARK_API_KEY）
python src/analyze.py

# 手动喂一篇全文（不用 mp Cookie，适合兜底/调试）：
python src/analyze.py --account erniao --article 二鸟说全文.txt \
    --source-url https://mp.weixin.qq.com/s?__biz=...

# 直接合并已结构化好的 JSON：
python src/analyze.py --account erniao --from-json structured.json
```

环境变量（放仓库根 `.env`，已被 gitignore）：
| 变量 | 必填 | 说明 |
|---|---|---|
| `MP_COOKIE` | 摄取必填 | mp.weixin.qq.com 登录 Cookie（含 data_ticket），CI 用 Secrets |
| `MP_TOKEN` | 选填 | mp 后台 URL 的 token（数字串） |
| `WX_ACCOUNTS` | 选填 | 额外账号，`名称` 或 `名称,biz`，管道分隔 |
| `ARK_API_KEY` | 分析必填 | 火山方舟 Key |
| `ARK_MODEL` | 选填 | 模型 ID，默认 `doubao-seed-2-1-pro-260628` |
| `ARK_BASE_URL` | 选填 | 默认 `https://ark.cn-beijing.volces.com/api/v3` |

> ⚠️ `.env` / `MP_COOKIE` 含敏感凭证，**切勿提交**。`.env` 已在 `.gitignore`。

---

## 6. 如何新增一个号的分析（可插拔扩展）

1. 在 `src/analyzers/` 新建 `xxx.py`，继承 `Analyzer`，设置 `key` / `feed_name` /
   `source_name`，实现 `run(repo_root, env)`（或复用 `analyze_text` + `save_rec`）。
2. 在 `src/analyzers/__init__.py` 的 `ANALYZERS` 注册：`"xxx": XxxAnalyzer`。
3. 确保 `accounts.txt` 里有该号（最好带 `__biz`）。
4. 完成。`analyze.py` 会自动跑它，产物落在 `data/xxx/index.json`。

---

## 7. 已知事项 / TODO

- `accounts.txt` 的 `二鸟说` 行**尚未填 `__biz`** → CI 插件目前取不到全文、会优雅跳过；
  补上 `__biz`（从任意二鸟说文章 URL 的 `__biz=` 取得）后，插件会覆盖
  `source_url` 为真实 mp 原文链接。兜底自动化暂用雪球镜像链接。
- 给 WeChatRSS 仓库 Secrets 加 `ARK_API_KEY`（CI 分析用）；本地 `.env` 也放一份供兜底。
- 项目命名：当前沿用 `WeChatRSS`，后续可能改名（功能已超出纯 RSS）。
