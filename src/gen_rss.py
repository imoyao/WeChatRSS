#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChatRSS —— 自己实现的公众号文章抓取，生成 RSS 2.0 落盘到 feeds/。

抓取方式：微信公众平台登录态（mp.weixin.qq.com 的 getmsg 接口）
  - 通过已登录的 mp 会话（Cookie 含 data_ticket）调用 profile_ext?action=getmsg
    拉取指定 __biz 公众号的历史图文消息，解析后写出 feeds/{账号}.xml。
  - 这是「自己抓取」，不依赖任何第三方 RSS 聚合服务。

运行前需要环境变量：
  MP_COOKIE    mp.weixin.qq.com 登录后的完整 Cookie（必须含 data_ticket / slave_sid 等）
  MP_TOKEN     mp 后台 URL 里的 token 参数（数字串，可选但建议带上）
  ACCOUNTS_FILE 仓库内维护的账号文件，每行一个，格式：
                  账号名
                  账号名,biz            （推荐：直接给出 __biz，跳过解析）
               默认 accounts.txt
  WX_ACCOUNTS  额外的公众号，用 | 分隔，格式同样支持 "名称" 或 "名称,biz"（可选）
  OUT_DIR      RSS 输出目录（默认 feeds）
  MAX_ARTICLES 每个号最多抓取的篇数（默认 20）

订阅账号 = accounts.txt ∪ WX_ACCOUNTS，合并后按名称去重。
文章来源 = 自己调用 mp getmsg 接口，按 __biz 逐号抓取。

如何获取 MP_COOKIE / MP_TOKEN：
  1. 浏览器登录 https://mp.weixin.qq.com （需能扫码登录的公众号账号）
  2. 打开任意公众号图文素材页，F12 -> Network，复制请求头里的 Cookie 整段。
  3. 地址栏 URL 中的 token=xxxxxx 即为 MP_TOKEN。
如何获取某号的 __biz：
  打开该公众号任意一篇文章，URL 形如
  https://mp.weixin.qq.com/s?__biz=MzAxxxx...&mid=...&idx=1&sn=...
  其中 __biz= 后面那段 Base64 即 biz，写进 accounts.txt 的「名称,biz」。
"""
import os
import re
import sys
import json
import time
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()  # 本地开发时从 .env 读取 MP_COOKIE / MP_TOKEN
except Exception:
    pass

import requests

OUT_DIR = os.environ.get("OUT_DIR", "feeds")
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "20"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MP_API = "https://mp.weixin.qq.com/mp/profile_ext"


def load_accounts() -> list:
    """合并 accounts.txt 与 WX_ACCOUNTS，每行格式 '名称' 或 '名称,biz'，按名称去重。

    返回 [{"name":..., "biz":... or None}, ...]。
    """
    seen, accounts = set(), []

    def add(name, biz):
        name = (name or "").strip()
        if not name or name.startswith("#"):
            return
        if name in seen:
            return
        seen.add(name)
        accounts.append({"name": name, "biz": (biz or "").strip() or None})

    path = os.environ.get("ACCOUNTS_FILE", "accounts.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "," in line:
                    name, biz = line.split(",", 1)
                    add(name, biz)
                else:
                    add(line, None)

    for a in os.environ.get("WX_ACCOUNTS", "").split("|"):
        a = a.strip()
        if not a:
            continue
        if "," in a:
            name, biz = a.split(",", 1)
            add(name, biz)
        else:
            add(a, None)
    return accounts


def get_biz_via_sogou(name: str):
    """尽力从搜狗微信搜索解析出 __biz（无登录时获取 biz 的备选手段）。

    搜狗反爬严重、且结果多为 JS 动态渲染，requests 直连经常拿不到，
    因此本函数仅作 best-effort；拿不到时请手动在 accounts.txt 填 biz。
    """
    try:
        q = urllib.parse.quote(name)
        url = f"https://weixin.sogou.com/weixin?type=1&query={q}&ie=utf8"
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        )
        page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        # 账号结果里的公众号主页链接带 __biz
        m = re.search(r'__biz=([A-Za-z0-9+=/]+)', page)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"  ⚠ 搜狗解析 biz 失败({name}): {e}", file=sys.stderr)
    return None


def extract_articles(msg: dict) -> list:
    """从一个 getmsg 返回的消息节点里抽取图文（含多图文 sub）。"""
    comm = msg.get("comm_msg_info", {}) or {}
    ts = comm.get("datetime", 0)
    pub = (datetime.fromtimestamp(ts, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
           if ts else "")
    ext = msg.get("app_msg_ext_info") or {}
    if not ext:
        return []  # 非图文消息（文本/语音等）跳过

    out = []

    def add(entry):
        url = entry.get("content_url", "") or ""
        if not url:
            return
        url = html.unescape(url)
        if url.startswith("/"):
            url = "https://mp.weixin.qq.com" + url
        out.append({
            "title": html.unescape(entry.get("title", "") or ""),
            "link": url,
            "pub_date": pub,
            "summary": html.unescape(entry.get("digest", "") or ""),
            "author": html.unescape(entry.get("author", "") or ""),
        })

    add(ext)
    for sub in ext.get("multi_app_msg_item_list", []) or []:
        add(sub)
    return out


def fetch_account_articles(name: str, biz: str, cookie: str, token: str) -> list:
    """调用 mp getmsg 接口，按 __biz 分页拉取该号的历史图文。

    返回按时间倒序的文章列表（最多 MAX_ARTICLES 篇）。
    """
    if not biz:
        raise RuntimeError("缺少 __biz，无法定位公众号（请在 accounts.txt 用 '名称,biz' 提供）")

    headers = {
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}",
    }
    items, offset = [], 0
    while len(items) < MAX_ARTICLES:
        params = {
            "action": "getmsg",
            "__biz": biz,
            "f": "json",
            "offset": offset,
            "count": 10,
            "is_ok": 1,
            "appmsgid": "",
            "appmsg_type": 9,
        }
        if token:
            params["token"] = token
        r = requests.get(MP_API, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"getmsg 返回非 JSON（可能会话失效/被拦截），HTTP {r.status_code}")

        if str(data.get("err_code")) != "0":
            raise RuntimeError(f"getmsg 返回错误: err_code={data.get('err_code')} "
                               f"err_msg={data.get('err_msg')}")

        msg_list = json.loads(data.get("general_msg_list", "{}") or "{}")
        msgs = msg_list.get("list", [])
        if not msgs:
            break
        for m in msgs:
            for art in extract_articles(m):
                if len(items) >= MAX_ARTICLES:
                    break
                items.append(art)
        offset = data.get("next_offset")
        if not offset:
            break
        time.sleep(1.0)  # 礼貌限速，降低被风控概率
    return items


def build_rss(title: str, items: list) -> str:
    esc = lambda s: html.escape(str(s or ""), quote=True)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             f"<title>{esc(title)}</title>",
             f"<link>https://mp.weixin.qq.com/</link>",
             f"<description>{esc(title)} 公众号 RSS（自抓取）</description>",
             f"<lastBuildDate>{datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>"]
    for it in items:
        lines += ["<item>",
                  f"<title>{esc(it.get('title'))}</title>",
                  f"<link>{esc(it.get('link'))}</link>",
                  f"<guid>{esc(it.get('link'))}</guid>",
                  f"<pubDate>{esc(it.get('pub_date'))}</pubDate>",
                  f"<description><![CDATA[{it.get('summary') or ''}]]></description>",
                  "</item>"]
    lines.append("</channel></rss>")
    return "\n".join(lines)


def main():
    cookie = os.environ.get("MP_COOKIE", "")
    token = os.environ.get("MP_TOKEN", "")
    os.makedirs(OUT_DIR, exist_ok=True)

    accounts = load_accounts()
    print(f"✓ 共加载 {len(accounts)} 个公众号")

    if not cookie:
        print("⚠️ 未设置 MP_COOKIE，无法抓取，仅生成空订阅源", file=sys.stderr)

    any_fail = False
    for acc in accounts:
        name, biz = acc["name"], acc["biz"]
        items = []
        if cookie:
            if not biz:
                biz = get_biz_via_sogou(name)
                if biz:
                    print(f"  · 通过搜狗解析到 {name} 的 biz")
            try:
                items = fetch_account_articles(name, biz, cookie, token)
                print(f"✓ {name}：抓到 {len(items)} 篇")
            except Exception as e:
                any_fail = True
                print(f"✗ {name} 抓取失败: {e}", file=sys.stderr)
        xml = build_rss(name, items)
        with open(os.path.join(OUT_DIR, f"{name}.xml"), "w", encoding="utf-8") as f:
            f.write(xml)

    if any_fail:
        open(os.path.join(OUT_DIR, ".fetch_failed"), "w").close()
        print("⚠️ 已标记抓取失败（feeds/.fetch_failed）", file=sys.stderr)


if __name__ == "__main__":
    main()
