#!/usr/bin/env python3
"""1.1.2 PASS E：清理 static/ 里的 Phase 15 / 16B-16E / spec §xx 流水账注释。
只处理注释体（CSS /* *//、JS /* *// 与 //、HTML <!-- -->），保留 WHY 说明文本。
幂等：跑两遍结果相同。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# 替换顺序敏感
SUBS = [
    (re.compile(r"Phase 16[B-E]\s*§[\d./ -§]+[：:]?"), ""),
    (re.compile(r"Phase 16[B-E]\b"), ""),
    (re.compile(r"Phase 15(?:\.\d+)?\b"), ""),
    (re.compile(r"Phase 1[0-3]\b"), ""),          # Phase 10/11/12/13 同类流水账
    (re.compile(r"Phase \d+\b"), ""),             # Phase 9 等其余流水账
    (re.compile(r"Phase 语义"), "原有语义"),
    (re.compile(r"spec\s*§[\d./ -§]+[：:，,（）()]*"), ""),
    (re.compile(r"16[B-E]\s*[：:]"), ""),
    (re.compile(r"16[B-E]\s+"), ""),
    (re.compile(r"§[\d./ -§]+[：:]?"), ""),
]


def _is_empty(new: str) -> bool:
    return not re.sub(r"[\s=—–*-·]+", "", new)


def transform(body: str) -> str:
    s = body
    for pat, rep in SUBS:
        s = pat.sub(rep, s)
    s = re.sub(r"(\S)\+", r"\1 +", s)  # 括号移除后 "Stale+ 全局" -> "Stale + 全局"
    # 只含空白/标点（可跨行）的括号整组删除（"（Phase 15, spec §x）" 删中间后剩 "（,）"）
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"[（(][，,。.：:;；、\s]*[）)]", "", s, flags=re.S)
    # 行尾孤立的开括号（闭合符在下一行且已被上面的规则处理掉）
    s = re.sub(r"[（(]\s*$", "", s, flags=re.M)
    s = re.sub(r" +([，。；：、！？,;:!?])", r"\1", s)
    s = re.sub(r"  +", " ", s)
    s = re.sub(r"^\s*[：:]\s*", "", s, flags=re.M)
    s = re.sub(r"（ +", "（", s)
    s = re.sub(r" +）", "）", s)
    return s


def process_css(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    n = 0

    def rep(m):
        nonlocal n
        body = m.group(1)
        new = transform(body)
        if new != body:
            n += 1
        return "" if _is_empty(new) else "/*" + new + "*/"

    out = re.sub(r"/\*(.*?)\*/", rep, text, flags=re.S)
    if out != text:
        path.write_text(out, encoding="utf-8")
    return n


def process_js(path: Path) -> int:
    # 单遍状态机：字符串（" ' `）原样保留，只改块注释 / 行注释。
    # 行注释要求前一字符是语句边界符，避免把除法 / URL 里的 // 当注释。
    text = path.read_text(encoding="utf-8")
    n = 0
    out = []
    i, length = 0, len(text)
    # 行注释合法的前一字符集（语句边界）
    stmt_boundary = set(' \t\r\n;{}()[]:=+-*/&|!,?')
    while i < length:
        ch = text[i]
        if ch in "\"'`":
            j = i + 1
            while j < length:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    break
                if ch != "`" and text[j] == "\n":
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
        elif ch == "/" and i + 1 < length and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = length if end == -1 else end + 2
            body = text[i + 2:end - 2]
            new = transform(body)
            if new != body:
                n += 1
            out.append("" if _is_empty(new) else "/*" + new + "*/")
            i = end
        elif ch == "/" and i + 1 < length and text[i + 1] == "/":
            prev = text[i - 1] if i > 0 else "\n"
            if prev in stmt_boundary:
                end = text.find("\n", i)
                end = length if end == -1 else end
                body = text[i + 2:end]
                new = transform(body)
                if new != body:
                    n += 1
                out.append("" if _is_empty(new) else "//" + new)
                i = end
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    result = "".join(out)
    if result != text:
        path.write_text(result, encoding="utf-8")
    return n


def process_html(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    n = 0

    def rep(m):
        nonlocal n
        body = m.group(1)
        new = transform(body)
        if new != body:
            n += 1
        return "" if _is_empty(new) else "<!--" + new + "-->"

    out = re.sub(r"<!--(.*?)-->", rep, text, flags=re.S)
    if out != text:
        path.write_text(out, encoding="utf-8")
    return n


def main() -> int:
    total = 0
    for css in sorted(STATIC.glob("css/*.css")):
        total += process_css(css)
    for js in sorted(STATIC.glob("js/*.js")):
        total += process_js(js)
    html = STATIC / "index.html"
    if html.exists():
        total += process_html(html)
    print("comments transformed:", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
