#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步微信读书「书单」里的公众号到 accounts.txt（仓库可写文件）。

书单页面是服务端渲染的公开页面（无需登录 Cookie），每个条目形如：
    <div class="booklist_book"> ... <img aria-label="NAME"> ...
        <div class="booklist_book_title">NAME</div>
        <div class="booklist_book_author">公众号</div>
只取 author 标记为「公众号」的条目，避免把普通书籍也当成账号。

用法：
    # 同步脚本内置的默认书单
    python sync_booklist.py
    # 指定书单 URL（多个用 | 或换行分隔）
    WEREAD_BOOKLIST_URLS="https://weread.qq.com/misc/booklist/xxx|https://..." python sync_booklist.py
    # 指定输出文件（默认 accounts.txt）
    ACCOUNTS_FILE=accounts.txt python sync_booklist.py
"""
import os
import re
import sys
import time
import urllib.request

# 让 Windows(GBK) 控制台也能正常打印中文与符号
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()  # 本地开发时从 .env 读取 WEREAD_BOOKLIST_URLS 等
except Exception:
    pass

# 默认书单：马原的书单 · 投研自媒体公众号
DEFAULT_BOOKLIST_URLS = [
    "https://weread.qq.com/misc/booklist/19335103_81YvqKoRv",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def fetch_booklist_accounts(url: str) -> list:
    """抓取书单页面，返回其中标记类型为「公众号」的账号名列表（保持顺序、页内去重）。"""
    # 追加时间戳参数 bust 微信读书 CDN 缓存（否则偶发返回被截断的不完整列表）
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}_={int(time.time() * 1000)}",
        headers={"User-Agent": UA, "Cache-Control": "no-cache"},
    )
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    titles = re.findall(r'booklist_book_title">\s*(.*?)\s*</div>', html, re.S)
    authors = re.findall(r'booklist_book_author">\s*(.*?)\s*</div>', html, re.S)
    out = []
    for title, author in zip(titles, authors):
        name = title.strip()
        if name and author.strip() == "公众号" and name not in out:
            out.append(name)
    return out


def parse_urls(raw: str) -> list:
    return [p.strip() for p in re.split(r"[|\n]", raw or "") if p.strip()]


def load_existing(path: str) -> list:
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name and not name.startswith("#"):
                    existing.append(name)
    return existing


def fetch_booklist_total(url: str):
    """从书单页解析「共 N 本」的目标数量。"""
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}_={int(time.time() * 1000)}",
        headers={"User-Agent": UA, "Cache-Control": "no-cache"},
    )
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    m = re.search(r"共\s*(\d+)\s*本", html)
    return int(m.group(1)) if m else None


def fetch_booklist_accounts_retry(url: str, tries: int = 8) -> list:
    """书单接口偶发返回不完整列表，循环抓取直到拿满页面标注的总数（共 N 本）或达到重试上限。

    微信读书书单页服务端渲染不稳定，单次请求可能在 202~209 之间波动，故以页面声明的
    「共 N 本」为目标，多次尝试取并集直到拿满。
    """
    try:
        target = fetch_booklist_total(url)
    except Exception:
        target = None
    best = []
    for i in range(tries):
        if i > 0:
            time.sleep(1.5)
        try:
            found = fetch_booklist_accounts(url)
        except Exception as e:
            print(f"  ⚠ 拉取重试: {e}", file=sys.stderr)
            continue
        # 取并集，避免某次漏掉个别条目
        for name in found:
            if name not in best:
                best.append(name)
        if target and len(best) >= target:
            break
    return best


def main():
    out_file = os.environ.get("ACCOUNTS_FILE", "accounts.txt")
    urls = parse_urls(os.environ.get("WEREAD_BOOKLIST_URLS", "")) or DEFAULT_BOOKLIST_URLS

    existing = load_existing(out_file)
    added = 0
    for url in urls:
        found = fetch_booklist_accounts_retry(url)
        if not found:
            print(f"✗ 拉取书单失败 {url}", file=sys.stderr)
            continue
        before = len(existing)
        for name in found:
            if name not in existing:
                existing.append(name)
                added += 1
        print(f"✓ {url} 识别到 {len(found)} 个公众号，本次新增 {len(existing) - before} 个")

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("# 订阅的公众号账号，每行一个（可由 sync_booklist.py 从微信读书书单同步，append 后自动去重）\n")
        for name in existing:
            f.write(name + "\n")
    print(f"✓ 已写入 {out_file}（共 {len(existing)} 个，本次新增 {added} 个）")


if __name__ == "__main__":
    main()
