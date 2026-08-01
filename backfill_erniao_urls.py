#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二鸟说 · 历史手抄报 URL 一次性回填工具（backfill）。

目标：从雪球（xueqiu.com）把 二鸟说（UID=3502863673）历史上所有含「手抄报」的帖子
      整理成一份 URL 目录，写入 data/er-niao/archive.json，
      后续前端/后端只需按 issue_no 取 URL，不必每次重新发现（爬）一次。

两种采集模式：
  A) 游客模式（默认，无需任何凭证）：真实浏览器过 WAF 拿游客令牌，用「页内 fetch」
     驱动 timeline API。但雪球对游客只开放最新 ~20 帖，拿不到历史（已实测：
     page / max_id 翻页与页面滚动都只回最新 185~186 期）。
  B) 登录模式（要全量历史，必须）：设置 XUEQIU_COOKIE（浏览器 F12 复制的 Cookie 头），
     直接用 requests 带登录态翻页，可回看全部历史手抄报。

雪球 API 坑（已踩）：statuses 字段（非 list）、created_at 是毫秒、HttpOnly 令牌、
无头浏览器需 stealth 才能过 WAF、翻页不能用 page/max_id 的游客态。

用法：
  # 仅最新（游客）：
  python backfill_erniao_urls.py
  # 全量历史（登录态，推荐）：
  set XUEQIU_COOKIE=xxxx        # Windows
  python backfill_erniao_urls.py --max-page 300
  # 或在命令里带：
  python backfill_erniao_urls.py --cookie "xq_a_token=...; xqat=...; u=...; ..."
输出：data/er-niao/archive.json  { source, uid, generated_at, total, items[] }
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

UID = "3502863673"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 优先用本机已装的 Edge（Chromium 内核），避免下载 150MB 的 playwright chromium。
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE_PATH = next((p for p in _EDGE_CANDIDATES if Path(p).exists()), None)

# 期号：标题形如「手抄报|186期：...」或「手抄报 第186期」，取「数字+期」里最大的数字
_ISSUE_RE = re.compile(r"(\d{1,4})\s*期")

REPO = Path(__file__).resolve().parent
OUT = REPO / "data" / "er-niao" / "archive.json"


def _issue_no_from_text(title: str, text: str) -> int | None:
    hay = f"{title or ''}\n{text or ''}"
    nums = [int(m) for m in _ISSUE_RE.findall(hay)]
    nums = [n for n in nums if n <= 9999]
    return max(nums) if nums else None


def _pub_from_ms(created_at) -> str:
    if not created_at:
        return ""
    try:
        return _dt.datetime.fromtimestamp(
            int(created_at) / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _add_record(items: dict, x: dict) -> int:
    """从一条 API 帖子提取手抄报记录；命中返回 1，否则 0。"""
    raw = f"{x.get('title') or ''}\n{x.get('text') or ''}"
    if "手抄报" not in raw:
        return 0
    pid_raw = x.get("id")
    if pid_raw is None:
        return 0
    pid = str(pid_raw)
    if pid in items:
        return 0
    items[pid] = {
        "xueqiu_id": pid,
        "issue_no": _issue_no_from_text(x.get("title"), x.get("text")),
        "title": (x.get("title") or "").strip(),
        "publish_date": _pub_from_ms(x.get("created_at")),
        "source_url": f"https://xueqiu.com/{UID}/{pid}",
    }
    return 1


# ---------------------------------------------------------------------------
# B) 登录态：requests + XUEQIU_COOKIE（可回看全量历史）
# ---------------------------------------------------------------------------
def _parse_cookie(s: str) -> dict:
    out = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def collect_authed(cookie_str: str, max_page: int, sleep: float) -> dict:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": f"https://xueqiu.com/u/{UID}",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    })
    s.cookies.update(_parse_cookie(cookie_str))
    items: dict[str, dict] = {}
    page = 1
    empty_streak = 0
    no_issue_streak = 0
    while page <= max_page:
        api = (f"https://xueqiu.com/statuses/user_timeline.json"
               f"?user_id={UID}&page={page}&count=20&type=0")
        try:
            r = s.get(api, timeout=30)
        except Exception as e:
            print(f"  ! page {page} 请求异常: {e}")
            time.sleep(3)
            empty_streak += 1
            if empty_streak >= 3:
                break
            page += 1
            continue
        if r.status_code != 200:
            print(f"  ! page {page} HTTP {r.status_code}: {r.text[:120]}")
            time.sleep(3)
            empty_streak += 1
            if empty_streak >= 3:
                break
            page += 1
            continue
        try:
            data = r.json()
        except Exception:
            print(f"  ! page {page} 非 JSON: {r.text[:120]}")
            time.sleep(3)
            empty_streak += 1
            if empty_streak >= 3:
                break
            page += 1
            continue
        empty_streak = 0
        st = data.get("statuses") or data.get("list") or []
        if not st:
            empty_streak += 1
            if empty_streak >= 3:
                print(f"  · 连续 {empty_streak} 页空，停止翻页")
                break
            page += 1
            time.sleep(sleep)
            continue

        hits = sum(_add_record(items, x) for x in st)
        print(f"  · page {page:>3}: {len(st):>3} 帖，本页命中 {hits}，"
              f"累计 {len(items)} 期")
        if hits == 0:
            no_issue_streak += 1
            if no_issue_streak >= 8:
                print("  · 连续 8 页无手抄报，判定已到历史尽头，停止")
                break
        else:
            no_issue_streak = 0
        page += 1
        time.sleep(sleep)
    return items


# ---------------------------------------------------------------------------
# A) 游客态：浏览器过 WAF + 页内 fetch（仅最新 ~20）
# ---------------------------------------------------------------------------
def _open_browser():
    launch_kwargs = dict(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled",
              "--disable-infobars"],
    )
    if EDGE_PATH:
        launch_kwargs["executable_path"] = EDGE_PATH
        print(f"  · [browser] 使用本机 Edge: {EDGE_PATH}")
    else:
        print("  · [browser] 使用 playwright 自带 chromium")
    p = sync_playwright().start()
    browser = p.chromium.launch(**launch_kwargs)
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = ctx.new_page()
    print("  · [browser] 打开 xueqiu 首页，等待 WAF 挑战结算…")
    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(15000)
    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
    if "xq_a_token" not in cookies:
        print("  · [browser] 首页未拿到令牌，再访问用户页…")
        page.goto(f"https://xueqiu.com/u/{UID}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
    if "xq_a_token" in cookies:
        print("  · [browser] 已拿到 xq_a_token，WAF 通过")
    else:
        print("  ! [browser] 仍未拿到 xq_a_token（本机 IP 可能被 WAF 拦）")
    page.goto(f"https://xueqiu.com/u/{UID}",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)
    return browser, page


_FETCH_JS = """async (url) => {
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


def collect_guest(page, max_calls: int, sleep: float) -> dict:
    items: dict[str, dict] = {}
    max_id = None
    empty_streak = 0
    no_issue_streak = 0
    call_num = 0
    print(f"  · [collect] 游客态 start; page.url={page.url}")
    while call_num < max_calls:
        call_num += 1
        api = (f"https://xueqiu.com/statuses/user_timeline.json"
               f"?user_id={UID}&count=20&type=0")
        if max_id is not None:
            api += f"&max_id={max_id}"
        try:
            res = page.evaluate(_FETCH_JS, api)
        except Exception as e:
            print(f"  ! 第{call_num}次 evaluate 异常: {e}")
            time.sleep(2)
            empty_streak += 1
            if empty_streak >= 3:
                break
            continue
        if res.get("err"):
            print(f"  ! 第{call_num}次 API err {res['err']}: "
                  f"{res.get('head','')[:120]}")
            time.sleep(3)
            empty_streak += 1
            if empty_streak >= 3:
                break
            continue
        empty_streak = 0
        st = res.get("statuses") or []
        if not st:
            empty_streak += 1
            if empty_streak >= 3:
                print(f"  · 连续 {empty_streak} 次空响应，停止翻页")
                break
            time.sleep(sleep)
            continue
        hits = 0
        ids = []
        for x in st:
            if x.get("id") is not None:
                ids.append(int(x["id"]))
            hits += _add_record(items, x)
        if ids:
            max_id = min(ids) - 1
        print(f"  · 第{call_num:>3}次: {len(st):>3} 帖，本页命中 {hits}，"
              f"累计 {len(items)} 期")
        if hits == 0:
            no_issue_streak += 1
            if no_issue_streak >= 8:
                print("  · 连续 8 次无手抄报，判定已到历史尽头，停止")
                break
        else:
            no_issue_streak = 0
        time.sleep(sleep)
    return items


# ---------------------------------------------------------------------------
def merge_from_index(items: dict) -> None:
    idx_path = REPO / "data" / "er-niao" / "index.json"
    if not idx_path.exists():
        return
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        for it in idx.get("issues", []):
            url = it.get("source_url", "")
            m = re.search(r"/(\d+)$", url)
            pid = m.group(1) if m else None
            if pid and pid not in items:
                items[pid] = {
                    "xueqiu_id": pid,
                    "issue_no": it.get("issue_no"),
                    "title": it.get("title", ""),
                    "publish_date": it.get("publish_date", ""),
                    "source_url": url,
                    "from": "index.json",
                }
    except Exception as e:
        print(f"  ! 合并 index.json 失败: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-page", type=int, default=300)
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--cookie", type=str, default="",
                    help="雪球登录态 Cookie（F12 复制的整段 Cookie 头）")
    args = ap.parse_args()

    cookie = args.cookie or os.environ.get("XUEQIU_COOKIE", "")
    if cookie:
        print("▶ 登录态采集（可回看全量历史）")
        items = collect_authed(cookie, args.max_page, args.sleep)
    else:
        print("▶ 游客态采集（仅最新 ~20，要历史请加 --cookie / XUEQIU_COOKIE）")
        browser, page = _open_browser()
        try:
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            if "xq_a_token" not in cookies:
                print("✗ 未获得 xq_a_token。请检查网络/是否被 WAF 永久拦。")
                return 2
            items = collect_guest(page, args.max_page, args.sleep)
        finally:
            browser.close()

    merge_from_index(items)

    records = sorted(items.values(),
                     key=lambda r: (r.get("issue_no") or 0), reverse=True)
    unknown = [r for r in records if not r.get("issue_no")]
    if unknown:
        print(f"  ! 有 {len(unknown)} 条无法解析期号（已保留，需人工核对）：")
        for r in unknown[:10]:
            print("     -", r["source_url"], "|", r["title"][:40])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "source": "erniao",
        "uid": UID,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
            .astimezone().isoformat(timespec="seconds"),
        "generation_method": ("xueqiu user_timeline (login cookie)"
                              if cookie else
                              "xueqiu user_timeline (guest browser token)"),
        "total": len(records),
        "items": records,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    lo = min((r["issue_no"] for r in records if r.get("issue_no")), default="?")
    hi = max((r["issue_no"] for r in records if r.get("issue_no")), default="?")
    print(f"\n✓ 写入 {OUT} —— 共 {len(records)} 期手抄报 URL（期号 {lo} ~ {hi}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
