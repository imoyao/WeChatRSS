#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
落地第一步：反向压力测试，确认微信读书接口路径与返回结构。

用法：
  WEREAD_COOKIE="你的cookie" python verify_weread_api.py
或临时把 cookie 写进下方变量（仅本地调试，勿提交）。
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import requests

COOKIE = os.environ.get("WEREAD_COOKIE", "你的cookie")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
API = "https://weread.qq.com/api/subscribe"  # ← 与 gen_rss.py 保持一致，待验证


def main():
    r = requests.get(API,
                     headers={"User-Agent": UA, "Cookie": COOKIE},
                     params={"query": "且慢"}, timeout=20)
    print("status:", r.status_code)
    print("body[:500]:", r.text[:500])

    if r.status_code == 200:
        try:
            print("json keys:", list(r.json().keys()))
        except Exception:
            print("（响应非 JSON）")
    elif r.status_code in (301, 302, 403):
        print("接口路径不对或 cookie 失效，需对照 WeWe RSS 源码确认真实路径")


if __name__ == "__main__":
    main()
