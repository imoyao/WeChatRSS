#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二鸟说 · 基金手抄报 分析器（可插拔插件的第一个实现）。

输入：feeds/二鸟说.xml（由 src/gen_rss.py 用 mp 登录态抓取得到，含真实
      mp.weixin.qq.com/s?__biz=... 微信原文链接）。
处理：默认走「雪球游客零凭证通道」——真实浏览器过阿里云 WAF 拿游客令牌，
      用页内 fetch 驱动 timeline API 取最新一期手抄报全文。零 Secret、全自动，
      适合 GitHub Actions 长期无人值守运行。
      若配置了 XUEQIU_COOKIE（登录态），则作为更快更全的兜底（但 Cookie 会过期，
      需定期重贴，非必须）；再不行降级 MP_COOKIE 从微信原文拉取；皆不可用则跳过。
      最终交给火山方舟(Ark) 结构化抽取。
输出：data/er-niao/index.json（链接归档 + 轻量结构化信号，约 1KB/期，长期不膨胀）。

为什么放这里而不是 fundmate：
  二鸟说只是「某个公众号」的增值分析，WeChatRSS 已具备抓取微信原文的能力，
  且天生自带 mp 原文链接——这正是 fundmate 后端网络拿不到的东西。把分析作为
  WeChatRSS 的插件，fundmate 保持纯记账、不混入内容管道。

雪球游客通道踩坑记录（已解决）：
  - 阿里云 WAF 会检测无头浏览器自动化特征，默认 headless 拿不到 xq_a_token；
    必须 --disable-blink-features=AutomationControlled + 抹 navigator.webdriver。
  - xq_a_token 是 HttpOnly，document.cookie 看不到，必须用 ctx.cookies() 读。
  - 游客态 timeline API 必须用「页内 fetch（credentials:'include'）」调用，
    普通 requests 即便带上游客令牌也会被 10022 拒；页内 fetch 自动带齐 cookie 会话。
  - 游客态只开放最新 ~20 帖，足够「每日抓最新一期」，不够回看历史（历史见 archive.json）。
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    from playwright.sync_api import sync_playwright
    _HAVE_PLAYWRIGHT = True
except Exception:
    _HAVE_PLAYWRIGHT = False

from .base import (
    Analyzer, UA, call_ark, now_iso,
)

# 二鸟说 雪球 UID（雪球镜像通道用它定位最新手抄报）
XUEQIU_UID = "3502863673"
XUEQIU_USER_URL = f"https://xueqiu.com/u/{XUEQIU_UID}"

# 优先用本机已装的 Edge（Chromium 内核），避免下载 150MB 的 playwright chromium；
# CI(ubuntu) 上无 Edge，则回退到 rss.yml 已安装的 playwright chromium。
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE_PATH = next((p for p in _EDGE_CANDIDATES if Path(p).exists()), None)

SENTIMENT_ENUM = "极热 / 过热 / 较热 / 正常 / 正常偏冷 / 较冷 / 极冷"
PORTFOLIO_ENUM = "价值五剑、成长五剑、平衡五剑、天颐五剑、稳益五剑"
ACTION_ENUM = "无操作 / 持有 / 加仓 / 减仓 / 定投 / 转换 / 新建仓 / 清仓 / 其他"

SYSTEM_PROMPT = f"""你是一个专业的基金公众号「二鸟说·基金手抄报」文章结构化解析器。
输入是一篇手抄报全文，请抽取以下字段，只输出一个 JSON 对象，不要任何额外解释。

字段定义：
  - issue_no: 期号（整数，从标题"手抄报|第N期"或"手抄报|N期"提取，仅数字）
  - title: 文章完整标题
  - publish_date: 发布日期（YYYY-MM-DD；若文中写"发布于 07-17"则年份取 2026）
  - coefficient: 本周温度计系数（0-12 的整数；若文中是"温度为 X 度"则取 X）
  - sentiment: 情绪标签，必须从以下选择：{SENTIMENT_ENUM}
  - portfolios: 本期提及的基金组合名称数组，只能从以下选择：{PORTFOLIO_ENUM}；没有则 []
  - market_view: 市场观点（1-3 句原文要点概括，不要改写太多）
  - empirical_actions: 【独立结构化数组】逐条解析文中的"实证"或各组合当周操作。
        每条是一个对象：
          {{ "name": 条目名（如 "实证2" 或组合名 "成长五剑"）,
             "action": 操作，从以下选择：{ACTION_ENUM},
             "note": 该条操作的原文摘录或简短说明（保留关键信息） }}
        注意：有几条操作就输出几个对象，不要把多条合并成一段字符串；
              若文中明确"无操作"也请如实输出 action="无操作"。
  - content: 不需要，不要输出此字段

输出示例：
{{
  "issue_no": 186,
  "title": "手抄报|186期：高切低后，双创半月回调15%",
  "publish_date": "2026-07-17",
  "coefficient": 6,
  "sentiment": "正常偏热",
  "portfolios": ["价值五剑", "成长五剑"],
  "market_view": "市场高切低，双创半月回调15%，成交缩量。",
  "empirical_actions": [
    {{ "name": "实证2", "action": "无操作", "note": "面对K型分化，建议高切低…" }}
  ]
}}

只输出 JSON。
"""


def fetch_mp_article_text(link: str, cookie: str) -> str:
    """用 mp 登录态 Cookie 拉取微信文章全文（#js_content 区），转纯文本。"""
    headers = {
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": "https://mp.weixin.qq.com/",
    }
    r = requests.get(link, headers=headers, timeout=30)
    r.raise_for_status()
    html = r.text
    start = html.find('id="js_content"')
    if start == -1:
        raise RuntimeError("未找到 js_content（文章正文），可能登录态失效")
    chunk = html[start:start + 300000]
    chunk = re.sub(r"<script[\s\S]*?</script>", "", chunk)
    chunk = re.sub(r"<style[\s\S]*?</style>", "", chunk)
    text = re.sub(r"<[^>]+>", "\n", chunk)
    import html as _html
    text = _html.unescape(text)
    text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
    return text


def parse_feed_latest(feed_path: Path) -> dict | None:
    """从 RSS 里取最新一期「手抄报」的标题/链接/日期。"""
    if not feed_path.exists():
        return None
    tree = ET.parse(feed_path)
    root = tree.getroot()
    items = root.findall(".//item")
    for it in items:
        title = (it.findtext("title") or "").strip()
        if "手抄报" not in title:
            continue
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        return {"title": title, "link": link, "pub_date": pub}
    return None


def _strip_html(s: str) -> str:
    """去掉 HTML 标签并反转义，得到纯文本。"""
    if not s:
        return ""
    s = re.sub(r"<script[\s\S]*?</script>", " ", s)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", "\n", s)
    import html as _html
    s = _html.unescape(s)
    return "\n".join(l.strip() for l in s.splitlines() if l.strip())


# ---------------------------------------------------------------------------
# 雪球游客通道：浏览器过 WAF + 页内 fetch（零凭证，默认主通道）
# ---------------------------------------------------------------------------
def _open_guest_browser():
    """真实浏览器过 WAF，返回 (browser, page)，page 已停在用户页且拿到游客令牌。

    关键：阿里云 WAF 会检测无头浏览器自动化特征（navigator.webdriver /
    AutomationControlled），默认 headless 拿不到 xq_a_token。必须 stealth 参数 +
    抹掉 webdriver 标记，模拟真人浏览器才能通过挑战拿到游客令牌。
    xq_a_token 是 HttpOnly，必须用 ctx.cookies() 读取（document.cookie 看不到）。
    """
    launch_kwargs = dict(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled",
              "--disable-infobars"],
    )
    if EDGE_PATH:
        launch_kwargs["executable_path"] = EDGE_PATH
        print(f"  · [erniao] 游客通道使用本机 Edge: {EDGE_PATH}")
    else:
        print("  · [erniao] 游客通道使用 playwright 自带 chromium")
    p = sync_playwright().start()
    browser = p.chromium.launch(**launch_kwargs)
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    # 抹掉自动化标记，避免被 WAF 识别为 bot
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = ctx.new_page()
    print("  · [erniao] 打开 xueqiu 首页，等待 WAF 挑战结算…")
    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(15000)
    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
    if "xq_a_token" not in cookies:
        print("  · [erniao] 首页未拿到令牌，再访问用户页…")
        page.goto(XUEQIU_USER_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
    if "xq_a_token" in cookies:
        print("  · [erniao] 已拿到 xq_a_token，WAF 通过（游客零凭证）")
    else:
        print("  ! [erniao] 未拿到 xq_a_token（本环境 IP 可能被 WAF 拦）")
    page.goto(XUEQIU_USER_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)
    return browser, page


# 页内 fetch：credentials:'include' 自动带齐 cookie 会话，绕过 10022 拒访
_TIMELINE_FETCH_JS = """async (url) => {
    const r = await fetch(url, {credentials:'include',
        headers:{'X-Requested-With':'XMLHttpRequest','Accept':'application/json'}});
    const t = await r.text();
    let j=null; try{j=JSON.parse(t);}catch(e){}
    if(!j) return {err:r.status, head:t.slice(0,200)};
    const st = j.statuses || j.list || [];
    return {statuses: st.map(x=>({
        id:x.id, title:x.title, text:x.text, created_at:x.created_at
    }))};
}"""


def fetch_xueqiu_latest_via_guest() -> dict | None:
    """雪球游客零凭证通道：过 WAF 拿游客令牌，页内 fetch 最新一期手抄报。

    零 Secret、全自动。游客态只能取最新 ~20 帖，足够「每日抓最新一期」。
    任何失败/被拦都返回 None，由 run() 优雅降级，绝不抛异常炸掉 Action。
    """
    if not _HAVE_PLAYWRIGHT:
        print("  · [erniao] 未安装 playwright，跳过雪球游客通道"
              "（pip install playwright && playwright install chromium）")
        return None
    try:
        browser, page = _open_guest_browser()
        try:
            api = (f"https://xueqiu.com/statuses/user_timeline.json"
                   f"?user_id={XUEQIU_UID}&count=20&type=0")
            res = page.evaluate(_TIMELINE_FETCH_JS, api)
            if res.get("err"):
                print(f"  · [erniao] 游客 timeline API err {res['err']}: "
                      f"{res.get('head','')[:120]}")
                return None
            statuses = res.get("statuses") or []
            cands = [s for s in statuses
                     if "手抄报" in (s.get("title") or "") + (s.get("text") or "")]
            if not cands:
                print("  · [erniao] 游客 timeline 未找到手抄报"
                      "（本周可能未发新期）")
                return None
            # 取 id 最大（最新）的一期
            cands.sort(key=lambda s: int(s.get("id") or 0), reverse=True)
            s = cands[0]
            pid = str(s.get("id"))
            url = f"https://xueqiu.com/{XUEQIU_UID}/{pid}"
            text = _strip_html(s.get("text") or "")
            # 游客 timeline 的 text 偶偏短，补抓正文页拿全文
            if len(text) < 200:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                t2 = page.evaluate("() => document.body.innerText") or ""
                if "手抄报" in t2:
                    text = t2
            if not text.strip():
                return None
            return {"url": url, "text": text[:20000]}
        finally:
            browser.close()
    except Exception as e:
        print(f"  · [erniao] 雪球游客通道取数失败: {e}")
        return None


def fetch_xueqiu_latest_via_api(cookie: str = "") -> dict | None:
    """雪球登录态 JSON API 通道（XUEQIU_COOKIE 配置时作为游客通道的兜底）。

    登录态可绕过 WAF/headless 检测，比游客更稳更全，但 Cookie 会过期需定期重贴。
    无 cookie 时先访问首页拿游客令牌（可能被 WAF 拦 → 优雅降级）。全失败返回 None。
    """
    if not cookie:
        return None
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        s.headers.update({"Cookie": cookie})
        api = (f"https://xueqiu.com/statuses/user_timeline.json"
               f"?user_id={XUEQIU_UID}&page=1&count=20&type=0&sort=alpha")
        r = s.get(api, headers={
            "Referer": XUEQIU_USER_URL,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        for st in data.get("statuses") or data.get("list") or []:
            raw = (st.get("title") or "") + "\n" + (st.get("text") or "")
            if "手抄报" not in raw:
                continue
            pid = st.get("id")
            url = f"https://xueqiu.com/{XUEQIU_UID}/{pid}"
            text = _strip_html(st.get("text") or "")
            if text:
                return {"url": url, "text": text[:20000]}
        print("  · [erniao] 雪球登录 API 未找到含『手抄报』的帖子")
        return None
    except Exception as e:
        print(f"  · [erniao] 雪球登录 API 取数失败: {e}")
        return None


class ErNiaoAnalyzer(Analyzer):
    key = "er-niao"          # data/er-niao/ 目录（与历史路径保持一致）
    feed_name = "二鸟说"
    source_name = "二鸟说手抄报"

    def _build_rec(self, meta: dict, data: dict, source_url: str) -> dict:
        return {
            "issue_no": data.get("issue_no"),
            "title": data.get("title") or meta.get("title", ""),
            "publish_date": data.get("publish_date", ""),
            "source_url": source_url or data.get("source_url", ""),  # 微信原文链接
            "coefficient": data.get("coefficient"),
            "sentiment": data.get("sentiment", ""),
            "portfolios": data.get("portfolios", data.get("portfolio", [])),
            "market_view": data.get("market_view", ""),
            "empirical_actions": data.get("empirical_actions", []),
            "collected_at": now_iso(),
        }

    def analyze_text(self, text: str, env: dict, source_url: str = "",
                     publish_date: str = "") -> dict:
        """给定全文文本 → Ark 结构化 dict（CLI --article 用）。

        publish_date: 调用方已知真实发布日期时（如从雪球页头读到的 "发布于 2026-08-14"）
        优先采用，覆盖模型对日期的猜测——模型在 CLI 路径下只看到正文，
        对「最新一期」的发布日期常猜错（曾误判 188 期为 2026-08-07）。
        """
        data = call_ark(text, SYSTEM_PROMPT, env)
        data["source_url"] = source_url
        if publish_date:
            data["publish_date"] = publish_date
        return data

    def run(self, repo_root: Path, env: dict) -> dict:
        feed_path = repo_root / "feeds" / f"{self.feed_name}.xml"
        meta = parse_feed_latest(feed_path)
        cookie = env.get("MP_COOKIE") or ""
        xq_cookie = env.get("XUEQIU_COOKIE") or ""

        text = None
        source_url = ""

        # 1) 游客零凭证通道（默认主通道，不依赖任何 Secret）
        xq = fetch_xueqiu_latest_via_guest()
        if xq:
            text = xq["text"]
            source_url = xq["url"]
            print(f"  · [{self.key}] 游客通道取到全文 → {source_url}")

        # 2) 可选：配置了 XUEQIU_COOKIE 则登录 API 兜底（更快更全，但需定期重贴）
        if not text and xq_cookie:
            xq2 = fetch_xueqiu_latest_via_api(xq_cookie)
            if xq2:
                text = xq2["text"]
                source_url = xq2["url"]
                print(f"  · [{self.key}] 雪球登录 API 取到全文 → {source_url}")

        # 3) 微信降级通道（需 MP_COOKIE + feed 链接）
        if not text and cookie and meta and meta.get("link"):
            try:
                text = fetch_mp_article_text(meta["link"], cookie)
                source_url = meta["link"]
                print(f"  · [{self.key}] 微信通道取到全文 → {source_url}")
            except Exception as e:
                print(f"  ✗ [{self.key}] 微信取数失败: {e}", file=sys.stderr)

        if not text:
            print(f"  · [{self.key}] 雪球游客/登录与微信均不可用，跳过"
                  f"（雪球可能被 WAF 拦截；或配置 XUEQIU_COOKIE / MP_COOKIE 兜底）")
            return {"added": 0, "updated": 0, "skipped": 1}

        try:
            data = call_ark(text, SYSTEM_PROMPT, env)
        except Exception as e:
            print(f"  ✗ [{self.key}] Ark 解析失败: {e}", file=sys.stderr)
            return {"added": 0, "updated": 0, "skipped": 1}

        rec = self._build_rec(meta or {}, data, source_url)
        is_new = self.save_rec(repo_root, rec)
        print(f"  ✓ [{self.key}] {'新增' if is_new else '更新'} "
              f"{rec['issue_no']} 期 → {repo_root / 'data' / self.key / 'index.json'}")
        return {"added": int(is_new), "updated": int(not is_new), "skipped": 0}
