#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可插拔分析器注册表。

每个分析器对应一个公众号（或一个数据源），把该号的原始文章转成结构化派生数据。
新增一个号的分析：在 analyzers/ 下新建模块、继承 Analyzer 并实现 run()，
然后在此处注册到 ANALYZERS 即可。主流程（gen_rss / analyze）无需改动。
"""
from .base import Analyzer
from .erniao import ErNiaoAnalyzer

ANALYZERS = {
    "erniao": ErNiaoAnalyzer,
}

__all__ = ["Analyzer", "ANALYZERS", "ErNiaoAnalyzer"]
