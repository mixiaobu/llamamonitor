#!/usr/bin/env python3
"""横屏 + regression 视口：fresh navigate + 全页面横滚/嵌套滚动审计（不截图，快）。
用法: cdp_m_landscape.py <ws>   # 内置视口表
"""
import sys, os, json, glob
sys.path.insert(0, os.path.dirname(__file__))
from cdp_m_audit import *  # 复用底层函数（send/read_msg 等不导出，改为内联复用）
