#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：用登录态把二鸟说时间线全部原始帖子 dump 下来，分析缺失的期号。"""
import datetime as _dt
import json
import os
import re
import time
from pathlib import Path

import requests

UID = "3502863673"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REPO = Path(__file__).resolve().parent
RAW = Path("/tmp/raw_posts.jsonl")

_ISSUE_RE = re.compile(r"(?:第\s*)?(\d{1,4})\s*期")
_CN = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,
       "十":10,"十一":11,"十二":12,"十三":13,"十四":14,"十五":15,"十六":16,
       "十七":17,"十八":18,"十九":19,"二十":20,"二十一":21,"二十二":22,
       "二十三":23,"二十四":24,"二十五":25,"二十六":26,"二十七":27,"二十八":28,
       "二十九":29,"三十":30,"三十一":31,"三十二":32,"三十三":33,"三十四":34,
       "三十五":35,"三十六":36,"三十七":37,"三十八":38,"三十九":39,"四十":40}


def parse_cookie(s: str) -> dict:
    out = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def issue_no(title, text):
    hay = f"{title or ''}\n{text or ''}"
    # 阿拉伯数字 + 期
    nums = [int(m) for m in _ISSUE_RE.findall(hay)]
    nums = [n for n in nums if 1 <= n <= 999]
    if nums:
        return max(nums)
    # 中文数字 + 期
    for cn, n in _CN.items():
        if re.search(rf"{cn}\s*期", hay):
            return n
    return None


def main():
    cookie = os.environ.get("XUEQIU_COOKIE", "")
    assert cookie, "need XUEQIU_COOKIE"
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Referer": f"https://xueqiu.com/u/{UID}",
                      "Accept": "application/json, text/plain, */*",
                      "X-Requested-With": "XMLHttpRequest"})
    s.cookies.update(parse_cookie(cookie))
    posts = []
    page = 1
    while page <= 400:
        api = (f"https://xueqiu.com/statuses/user_timeline.json"
               f"?user_id={UID}&page={page}&count=20&type=0")
        r = s.get(api, timeout=30)
        if r.status_code != 200:
            print(f"! page {page} HTTP {r.status_code}")
            break
        try:
            data = r.json()
        except Exception:
            print(f"! page {page} non-json")
            break
        st = data.get("statuses") or data.get("list") or []
        if not st:
            print(f"· page {page} 空，停止（共 {len(posts)} 帖）")
            break
        for x in st:
            posts.append(x)
        if page % 10 == 0:
            print(f"· page {page}: 累计 {len(posts)} 帖")
        page += 1
        time.sleep(0.5)
    # 写原始 dump（标题/正文可能被截断，text 可能含 HTML）
    with RAW.open("w", encoding="utf-8") as f:
        for x in posts:
            f.write(json.dumps({
                "id": x.get("id"),
                "title": x.get("title") or "",
                "text": (x.get("text") or "")[:400],
                "created_at": x.get("created_at"),
                "retweeted": bool(x.get("retweeted_status")),
            }, ensure_ascii=False) + "\n")
    print(f"✓ 原始帖子 {len(posts)} 条 -> {RAW}")

    # 分析：哪些含「手抄报」，期号分布
    with_issue = {}
    with_shou = []
    for x in posts:
        t = x.get("title") or ""
        xtext = x.get("text") or ""
        hay = f"{t}\n{xtext}"
        if "手抄报" in hay:
            with_shou.append(x)
            n = issue_no(t, xtext)
            if n:
                with_issue.setdefault(n, []).append(x.get("id"))
    print(f"含『手抄报』帖: {len(with_shou)}")
    found = sorted(with_issue.keys())
    missing = [n for n in range(1, max(found)+1) if n not in with_issue]
    print(f"能解析期号: {len(found)} 个，范围 {found[0]}~{found[-1]}")
    print(f"缺失期号(1..{found[-1]}): {missing}")
    # 列出含手抄报但解析不到期号的帖子
    print("--- 含手抄报但无可解析期号 ---")
    for x in with_shou:
        if not issue_no(x.get("title"), x.get("text")):
            ca = x.get("created_at")
            d = _dt.datetime.fromtimestamp(int(ca)/1000, _dt.timezone.utc
                ).strftime("%Y-%m-%d") if ca else "?"
            print(f"  id={x.get('id')} date={d} title={ (x.get('title') or '')[:40]!r} text={(x.get('text') or '')[:60]!r}")


if __name__ == "__main__":
    main()
