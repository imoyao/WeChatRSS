# CLAUDE.md — WeChatRSS

给 AI 助手的项目速览。**详细设计与子项目说明以 `README.md` 为准。**

## 这是什么
微信公众号 → RSS 摄取底座，叠加**可插拔的内容分析插件**。第一个插件是**二鸟说·基金手抄报**
（系数/情绪/组合/实证操作的结构化抽取）。与 fundmate（记账主项目）解耦，二鸟说数据独立存于此。

## 关键事实（改代码前必读）
- **摄取层通用、分析层可插拔**：`src/gen_rss.py` 抓任意号 → `feeds/{号}.xml`（含真实 mp 原文链接）。
  `src/analyzers/` 是按号注册的分析插件；`src/analyzers/__init__.py` 的 `ANALYZERS` 是注册表。
- **不要改 `gen_rss.py` / `analyze.py` 主流程**来加新功能；新号分析请在 `analyzers/` 加文件并注册。
- **结构化产出**一律落在 `data/{插件键}/index.json`（如 `data/er-niao/index.json`），
  幂等按 `issue_no` 覆盖更新。**仓库只存链接归档 + 轻量信号，不存正文全文。**
- **双轨**：CI（`rss.yml`）主链路跑 `gen_rss` → `analyze`；WorkBuddy 自动化作兜底/手动重放
  （WebFetch 取全文 → `analyze.py --account erniao --article`）。
- **敏感凭证** `MP_COOKIE` / `ARK_API_KEY` 走 `.env`（本地）或 GitHub Secrets（CI），**绝不提交**。
  `.env` 已被 gitignore。

## 常用命令
```bash
pip install -r requirements.txt
python src/gen_rss.py                       # 摄取 → feeds/*.xml（需 MP_COOKIE）
python src/analyze.py                        # 分析全部注册插件（需 MP_COOKIE+ARK_API_KEY）
python src/analyze.py --account erniao --article 全文.txt --source-url <url>  # 手动喂文
python src/analyze.py --account erniao --from-json structured.json            # 合并预结构化 JSON
```

## 环境变量（`.env` / Secrets）
`MP_COOKIE`（摄取必填）、`MP_TOKEN`（选填）、`WX_ACCOUNTS`（选填）、
`ARK_API_KEY`（分析必填）、`ARK_MODEL`（选填，默认 `doubao-seed-2-1-pro-260628`）、
`ARK_BASE_URL`（选填）。

## 二鸟说插件要点（`src/analyzers/erniao.py`）
- `feed_name = "二鸟说"`，对应 `feeds/二鸟说.xml`。
- 取最新含"手抄报"的 item → 用 `MP_COOKIE` 拉 `js_content` 全文 → Ark 结构化。
- **未配 `__biz` 时会优雅跳过**（目前 `accounts.txt` 的二鸟说行还没 biz）。
- 字段：issue_no / title / publish_date / source_url / coefficient(0-12) / sentiment(枚举) /
  portfolios / market_view / empirical_actions[{name,action,note}]。

## 扩展一个新号的分析
`src/analyzers/` 新建模块继承 `Analyzer`（实现 `run`，或复用 `analyze_text`+`save_rec`）
→ 在 `__init__.py` 的 `ANALYZERS` 注册 → `accounts.txt` 加该号（带 `__biz` 最佳）。
