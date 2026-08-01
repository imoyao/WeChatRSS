#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二鸟说 · 基金手抄报 分析器（可插拔插件的第一个实现）。

输入：feeds/二鸟说.xml（由 src/gen_rss.py 用 mp 登录态抓取得到，含真实
      mp.weixin.qq.com/s?__biz=... 微信原文链接）。
处理：优先用「雪球 Cookie + JSON API」通道（绕开 WAF/headless 检测，最稳）取最新一期
      手抄报全文；无雪球 Cookie 时降级用 Playwright 无头渲染（尽力，CI 常被 WAF 拦）；
      再不行降级 MP_COOKIE 从微信原文拉取；皆不可用则跳过。最终交给火山方舟(Ark) 结构化抽取。
输出：data/er-niao/index.json（链接归档 + 轻量结构化信号，约 1KB/期，长期不膨胀）。

为什么放这里而不是 fundmate：
  二鸟说只是「某个公众号」的增值分析，WeChatRSS 已具备抓取微信原文的能力，
  且天生自带 mp 原文链接——这正是 fundmate 后端网络拿不到的东西。把分析作为
  WeChatRSS 的插件，fundmate 保持纯记账、不混入内容管道。
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


def fetch_xueqiu_latest_via_api(cookie: str = "") -> dict | None:
    """用 雪球 Cookie（或游客令牌）调 user_timeline JSON API 取最新『手抄报』。

    优先级：① 提供 XUEQIU_COOKIE → 直接用（最稳）；② 未提供 → 先访问首页拿游客
    xq_a_token，再试 API（零凭证尽力，依赖 CI 出口 IP 未被 WAF 拦）。
    相比 headless 渲染，API + 登录态/游客令牌能稳定绕过 WAF 与 headless 检测；
    返回的 text 通常是完整正文（HTML），剥离标签即得全文。全失败返回 None。
    """
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        if cookie:
            s.headers.update({"Cookie": cookie})
        else:
            # 零凭证：访问首页拿游客令牌（被 WAF 拦则后续 API 也会失败 → 优雅降级）
            try:
                s.get("https://xueqiu.com/", timeout=20)
            except Exception:
                pass
        api = (f"https://xueqiu.com/statuses/user_timeline.json"
               f"?user_id={XUEQIU_UID}&page=1&count=20&type=0&sort=alpha")
        r = s.get(api, headers={
            "Referer": f"https://xueqiu.com/u/{XUEQIU_UID}",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        for st in data.get("list", []):
            raw = (st.get("title") or "") + "\n" + (st.get("text") or "")
            if "手抄报" not in raw:
                continue
            pid = st.get("id")
            url = f"https://xueqiu.com/{XUEQIU_UID}/{pid}"
            text = _strip_html(st.get("text") or "")
            if text:
                return {"url": url, "text": text[:20000]}
        print("  · [erniao] 雪球 API 未找到含『手抄报』的帖子（可能本周未发新期）")
        return None
    except Exception as e:
        print(f"  · [erniao] 雪球 API 取数失败: {e}")
        return None


def fetch_xueqiu_latest_via_playwright() -> dict | None:
    """用无头 Chromium 渲染过雪球 WAF，取最新一期手抄报的 {url, text}。

    雪球对普通 requests 返回 WAF 挑战页（非内容），必须用真实浏览器渲染。
    任何失败/被拦都返回 None，由 run() 优雅降级到微信或跳过，绝不抛异常炸掉 Action。
    """
    if not _HAVE_PLAYWRIGHT:
        print("  · [erniao] 未安装 playwright，跳过雪球通道"
              "（pip install playwright && playwright install chromium）")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=UA)
            page = ctx.new_page()
            page.goto(XUEQIU_USER_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)  # 等 JS 渲染 + WAF 挑战结算

            # 抽取含「手抄报」的文章链接（时间倒序，取第一条=最新）
            items = page.evaluate(
                """(uid) => {
                    const out = [];
                    document.querySelectorAll('a').forEach(a => {
                        const href = a.href || '';
                        const text = (a.innerText || '').trim();
                        if (href.includes(uid) && text.includes('手抄报')) {
                            out.push({href, text});
                        }
                    });
                    return out;
                }""",
                XUEQIU_UID,
            )
            if not items:
                print("  · [erniao] 雪球页面未渲染出手抄报条目（可能被 WAF 拦截）")
                browser.close()
                return None

            url = items[0]["href"]
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            text = page.evaluate("() => document.body.innerText") or ""
            browser.close()

            text = text[:20000]
            if "手抄报" not in text:
                print("  · [erniao] 雪球文章正文未取到手抄报内容，疑似被拦")
                return None
            return {"url": url, "text": text}
    except Exception as e:
        print(f"  · [erniao] 雪球 Playwright 取数失败: {e}")
        return None


class ErNiaoAnalyzer(Analyzer):
    key = "erniao"
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

    def analyze_text(self, text: str, env: dict, source_url: str = "") -> dict:
        """给定全文文本 → Ark 结构化 dict（CLI --article 用）。"""
        data = call_ark(text, SYSTEM_PROMPT, env)
        data["source_url"] = source_url
        return data

    def run(self, repo_root: Path, env: dict) -> dict:
        feed_path = repo_root / "feeds" / f"{self.feed_name}.xml"
        meta = parse_feed_latest(feed_path)
        cookie = env.get("MP_COOKIE") or ""

        text = None
        source_url = ""
        xq_cookie = env.get("XUEQIU_COOKIE") or ""
        # 1) 雪球默认通道：有 cookie 走 JSON API（稳，绕开 WAF/headless 检测）；
        #    无 cookie 走 headless（尽力，CI 常被 WAF 拦）
        if xq_cookie:
            xq = fetch_xueqiu_latest_via_api(xq_cookie)
            if xq:
                text = xq["text"]
                source_url = xq["url"]
                print(f"  · [{self.key}] 雪球 API 取到全文 → {source_url}")
        if not text:
            xq = fetch_xueqiu_latest_via_playwright()
            if xq:
                text = xq["text"]
                source_url = xq["url"]
                print(f"  · [{self.key}] 雪球(headless)取到全文 → {source_url}")
        # 2) 微信降级通道（需 MP_COOKIE + feed 链接）
        if not text and cookie and meta and meta.get("link"):
            try:
                text = fetch_mp_article_text(meta["link"], cookie)
                source_url = meta["link"]
                print(f"  · [{self.key}] 微信通道取到全文 → {source_url}")
            except Exception as e:
                print(f"  ✗ [{self.key}] 微信取数失败: {e}", file=sys.stderr)
        if not text:
            print(f"  · [{self.key}] 雪球与微信均不可用，跳过"
                  f"（雪球可能被 WAF 拦截；或配置 MP_COOKIE 走微信降级）")
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
