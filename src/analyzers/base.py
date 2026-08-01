#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析器基类与共用工具。

设计定位（可插拔架构）：
  WeChatRSS 的 src/gen_rss.py 负责「摄取」任意公众号的**原始文章**（feeds/{号}.xml）；
  src/analyzers/ 下的每个 Analyzer 则负责把「某个号」的原始内容转成**结构化派生数据**
  （如二鸟说的系数/情绪/组合/实证操作）。

  新增一个号的分析 = 在 analyzers/ 新建一个模块、实现 Analyzer 接口、在 __init__.py
  的 ANALYZERS 注册。主流程（gen_rss / analyze）无需改动 → 真正的可插拔。

共用能力：
  - load_env_file：极简 .env 读取（兼容 dotenv 未装的环境）
  - extract_json：从模型输出稳健提取 JSON
  - call_ark：调用火山方舟（OpenAI 兼容）做结构化解析
  - upsert_issue / read_index / write_index：幂等维护 data/{key}/index.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-2-1-pro-260628"
CST = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_env_file(path: Path) -> dict:
    """极简 .env 读取（不依赖 python-dotenv）。"""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def extract_json(text: str) -> dict:
    """从模型输出中稳健提取 JSON 对象（兼容 ```json 围栏）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("模型输出中未找到 JSON")
    return json.loads(m.group(0))


def call_ark(text: str, system_prompt: str, env: dict,
             model: Optional[str] = None) -> dict:
    """调用火山方舟（OpenAI 兼容 SDK）做结构化解析。

    依赖 openai>=1.x。key / model / base_url 从 env 读取，缺失时回退默认值。
    """
    try:
        import openai
    except ImportError:
        raise RuntimeError("未安装 openai 库（pip install openai）")

    api_key = env.get("ARK_API_KEY") or os.getenv("ARK_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 ARK_API_KEY")
    base_url = env.get("ARK_BASE_URL") or os.getenv("ARK_BASE_URL") or ARK_BASE_URL
    mdl = model or env.get("ARK_MODEL") or os.getenv("ARK_MODEL") or DEFAULT_MODEL

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=mdl,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请解析以下文章：\n\n{text}"},
        ],
        temperature=0,
        max_tokens=2048,
    )
    content = resp.choices[0].message.content or ""
    return extract_json(content)


def read_index(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] {path} 解析失败，重建: {e}", file=sys.stderr)
    return {"source": "", "source_name": "", "updated_at": None,
            "latest_issue": None, "issues": []}


def upsert_issue(index: dict, rec: dict) -> bool:
    """将一期合并进 index，返回是否新增。issue_no 相同则覆盖更新。"""
    issues = index.setdefault("issues", [])
    for i, existing in enumerate(issues):
        if existing.get("issue_no") == rec.get("issue_no"):
            issues[i] = rec
            return False
    issues.append(rec)
    return True


def write_index(path: Path, index: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    index["issues"].sort(key=lambda x: x.get("issue_no") or 0, reverse=True)
    index["latest_issue"] = index["issues"][0]["issue_no"] if index["issues"] else None
    index["updated_at"] = now_iso()
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


class Analyzer:
    """分析器接口。子类必须设置 key / feed_name / source_name，并实现 run()。"""

    key: str = ""            # 注册键，同时作为 data/{key}/ 目录名
    feed_name: str = ""      # 对应 feeds/{feed_name}.xml 的公众号名
    source_name: str = ""

    def run(self, repo_root: Path, env: dict) -> dict:
        """执行分析，返回统计 {"added":int,"updated":int,"skipped":int}。"""
        raise NotImplementedError

    def save_rec(self, repo_root: Path, rec: dict) -> bool:
        """把一条记录幂等写入 data/{key}/index.json，返回是否新增。"""
        index_path = repo_root / "data" / self.key / "index.json"
        index = read_index(index_path)
        index["source"] = self.key
        index["source_name"] = self.source_name
        is_new = upsert_issue(index, rec)
        write_index(index_path, index)
        return is_new
