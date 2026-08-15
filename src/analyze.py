#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py —— 运行可插拔分析器，产出 data/{key}/index.json。

这是「摄取(gen_rss)」与「分析(analyzers)」之间的桥梁。三种用法：

1) 默认（CI 主链路，配合 GitHub Action）：
     python src/analyze.py
   对注册表里的每个分析器，读取对应 feeds/{号}.xml，抓取全文并结构化。
   需要 MP_COOKIE（gen_rss 已经用到）与 ARK_API_KEY（火山方舟）。

2) 手动喂文本（WorkBuddy 自动化兜底 / 手动重放）：
     python src/analyze.py --account erniao --article 二鸟说.txt [--source-url <url>]
   直接把一篇全文交给对应分析器结构化并落盘，不依赖 mp Cookie。

3) 直接合并已结构化好的 JSON（其他 AI / 脚本产出的中间结果）：
     python src/analyze.py --account erniao --from-json structured.json

环境变量：从仓库根 .env 读取（MP_COOKIE / ARK_API_KEY / ARK_MODEL / ARK_BASE_URL）。
"""
import argparse
import json
import os
import sys
from pathlib import Path

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

# 允许以 `python src/analyze.py` 直接运行（把 src 加入 path）
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from analyzers import ANALYZERS, ErNiaoAnalyzer  # noqa: E402
from analyzers.base import load_env_file, now_iso  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict:
    env = load_env_file(REPO_ROOT / ".env")
    # 进程环境变量优先
    for k in ("MP_COOKIE", "ARK_API_KEY", "ARK_MODEL", "ARK_BASE_URL"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description="运行 WeChatRSS 可插拔分析器")
    ap.add_argument("--account", help="只运行指定分析器（默认全部）")
    ap.add_argument("--article", help="手动喂全文文本文件（需配合 --account）")
    ap.add_argument("--source-url", default="", help="手动喂文时的原文链接")
    ap.add_argument("--publish-date", default="",
                    help="手动喂文时的发布日期 YYYY-MM-DD，覆盖模型猜测")
    ap.add_argument("--from-json", help="直接合并已结构化的 JSON 文件（需配合 --account）")
    ap.add_argument("--dry-run", action="store_true", help="只分析不落盘")
    args = ap.parse_args()

    env = load_env()

    if args.account:
        if args.account not in ANALYZERS:
            print(f"[error] 未知分析器: {args.account}", file=sys.stderr)
            return 2
        selected = {args.account: ANALYZERS[args.account]}
    else:
        selected = ANALYZERS

    total = {"added": 0, "updated": 0, "skipped": 0}

    for key, cls in selected.items():
        analyzer = cls()
        print(f"▶ 分析器 {key}（{analyzer.source_name}）")

        if args.article:
            text = Path(args.article).read_text(encoding="utf-8")
            try:
                data = analyzer.analyze_text(
                    text, env, args.source_url, args.publish_date)
            except Exception as e:
                print(f"  ✗ {key} 解析失败: {e}", file=sys.stderr)
                total["skipped"] += 1
                continue
            rec = analyzer._build_rec(
                {"title": data.get("title", "")}, data, args.source_url)
            if args.dry_run:
                print(json.dumps(rec, ensure_ascii=False, indent=2))
            else:
                is_new = analyzer.save_rec(REPO_ROOT, rec)
                print(f"  ✓ {'新增' if is_new else '更新'} {rec['issue_no']} 期")
            total["added"] += int(is_new)
            total["updated"] += int(not is_new)
        elif args.from_json:
            payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
            items = payload.get("issues", [payload]) if isinstance(payload, dict) else [payload]
            for item in items:
                if not item.get("issue_no"):
                    continue
                if args.dry_run:
                    print(json.dumps(item, ensure_ascii=False, indent=2))
                else:
                    is_new = analyzer.save_rec(REPO_ROOT, item)
                    total["added"] += int(is_new)
                    total["updated"] += int(not is_new)
            print(f"  ✓ 合并 {len(items)} 条")
        else:
            stats = analyzer.run(REPO_ROOT, env)
            for k in total:
                total[k] += stats.get(k, 0)

    print(f"\n[ok] 合计 新增 {total['added']} / 更新 {total['updated']} / 跳过 {total['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
