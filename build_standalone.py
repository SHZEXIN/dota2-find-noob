# -*- coding: utf-8 -*-
"""
把 web_server.py + teammate_intersection.py 合并为单文件 standalone_server.py。

动机：部署时只需上传 1 个文件，systemd 也只指一个路径，减少手工出错。

做法（保守、可验证）：
  1. 取 teammate_intersection.py 中实际被 web_server 用到的符号，按名字内联；
  2. 在 web_server.py 中剔除 `import teammate_intersection as ti` 引导块；
  3. 把 `ti.X` 全部替换为 `X`；
  4. 修正 `ti.__file__` 这类自省引用。

内联的符号（由 web_server.py 的实际引用决定，非猜测）：
  API, STEAM64_BASE, api_get, to_account_id, parse_time

关于 HERO_NAMES：
  两边各有一个同名模块级变量。web 侧那份是权威数据源（Web 启动时从
  /heroes 拉取并写入），领域层那份只是被 Web 侧赋值的镜像。合并到同一
  命名空间后只能是同一个变量，因此这里**不内联**领域层的定义（否则会把
  web 侧的定义覆盖回 {}），并把 web 侧的同步语句改成自赋值去掉。

产物仍是纯标准库，可独立运行。
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DOMAIN = os.path.join(HERE, "teammate_intersection.py")
SRC_WEB = os.path.join(HERE, "web_server.py")
OUT = os.path.join(HERE, "standalone_server.py")

# web_server.py 实际引用到的 ti.* 符号（脚本会再自动校验一遍）
# 注意：HERO_NAMES 不在其中，理由见文件头注释。
WANTED = ["API", "STEAM64_BASE", "api_get", "to_account_id", "parse_time"]

# 合并后需要改写/删除的语句（原样 -> 替换为），按顺序执行
REWRITES = [
    # 领域层不再有独立 HERO_NAMES，web 侧无需再"同步给领域层"
    ("ti.HERO_NAMES = HERO_NAMES  # 同步给领域层",
     "# 单文件版：领域层与 web 层共用同一个 HERO_NAMES，无需同步"),
]


def top_level_blocks(lines):
    """
    按顶层定义切分源码，返回 [(name, start_idx, end_idx)] 与模块级游离语句。
    name 为函数/变量名；游离语句（import、赋值等）name 为 None。
    """
    tree = ast.parse("".join(lines))
    blocks = []
    consumed = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            blocks.append((node.name, node.lineno - 1, node.end_lineno))
            for i in range(node.lineno - 1, node.end_lineno):
                consumed.add(i)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    blocks.append((t.id, node.lineno - 1, node.end_lineno))
                    for i in range(node.lineno - 1, node.end_lineno):
                        consumed.add(i)
    return blocks, consumed, tree


def main():
    for p in (SRC_DOMAIN, SRC_WEB):
        if not os.path.exists(p):
            raise SystemExit(f"缺少源文件: {p}")

    dom_lines = open(SRC_DOMAIN, encoding="utf-8").read().splitlines(keepends=True)
    web_lines = open(SRC_WEB, encoding="utf-8").read().splitlines(keepends=True)

    # ---------- 1. 校验 WANTED 覆盖了 web 侧全部 ti.* 引用 ----------
    web_src = "".join(web_lines)
    used = set(re.findall(r"\bti\.([A-Za-z_][A-Za-z_0-9]*)", web_src))
    used.discard("__file__")  # 自省，单独处理
    # REWRITES 中已定点改写的语句，其引用不构成"需要内联的符号"
    rewritten_refs = set(re.findall(r"\bti\.([A-Za-z_][A-Za-z_0-9]*)",
                                    "".join(old for old, _ in REWRITES)))
    used -= rewritten_refs
    missing = used - set(WANTED)
    if missing:
        raise SystemExit(
            "WANTED 未覆盖全部 ti.* 引用，需补上: %s" % sorted(missing)
        )
    unused = set(WANTED) - used
    if unused:
        print(f"[warn] WANTED 中这些符号 web 侧未直接引用: {sorted(unused)}")

    # ---------- 2. 从领域层抽取所需块 ----------
    dom_blocks, _consumed, _tree = top_level_blocks(dom_lines)
    picked = []
    for want in WANTED:
        hit = [b for b in dom_blocks if b[0] == want]
        if not hit:
            raise SystemExit(f"领域层中找不到符号: {want}")
        picked.append(hit[0])

    # 按原文件顺序排列，保证依赖顺序自然
    picked.sort(key=lambda b: b[1])
    domain_chunk = []
    for name, s, e in picked:
        domain_chunk.append("".join(dom_lines[s:e]).rstrip() + "\n\n")

    print("已内联领域层符号:")
    for name, s, e in picked:
        print(f"  {name:16s} 行 {s+1}-{e}")

    # ---------- 2b. 收集领域层的顶层 import ----------
    # 关键陷阱：内联进来的函数体可能用到 web 侧没导入的模块（例如 urllib.request）。
    # 而 web 侧恰好导入了 urllib.parse，会让 `import urllib` 成功，
    # 于是 urllib.request 缺失只在**运行到**该函数时才炸，不易发现。
    #
    # 必须用 AST 取 tree.body 里的 Import/ImportFrom，不能用行扫描——
    # 行扫描会把函数体内部缩进的 import 也抓出来，破坏顶层缩进。
    domain_imports = []
    seen_imports = set()
    for node in _tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            snippet = "".join(dom_lines[node.lineno - 1:node.end_lineno])
            if not snippet.endswith("\n"):
                snippet += "\n"
            if snippet in seen_imports:
                continue
            seen_imports.add(snippet)
            domain_imports.append(snippet)
    print("一并携带领域层 import:")
    for ln in domain_imports:
        print(f"  {ln.rstrip()}")

    # ---------- 3. 剔除 web 侧的 import 引导块 ----------
    out_web = []
    skip = False
    for ln in web_lines:
        if ln.startswith("import teammate_intersection"):
            skip = True
            continue
        if skip:
            # 跳过引导块残留（注释行与紧跟的断言/空行），遇到第一个非注释非空行恢复
            st = ln.strip()
            if st == "" or st.startswith("#"):
                continue
            skip = False
        out_web.append(ln)
    web_body = "".join(out_web)

    # 修正 ti.__file__ 自省（单文件后无独立领域层文件）
    web_body = web_body.replace(
        'print(f"领域层: {os.path.basename(ti.__file__)}")',
        'print(f"领域层: 已内联（单文件部署版）")',
    )

    # ---------- 4. 定点改写 & ti.X -> X ----------
    for old, new in REWRITES:
        if old not in web_body:
            raise SystemExit(f"预期语句未找到，web_server.py 可能已改动: {old!r}")
        web_body = web_body.replace(old, new)

    web_body = re.sub(r"\bti\.([A-Za-z_][A-Za-z_0-9]*)", r"\1", web_body)

    leftover = re.findall(r"\bti\b", web_body)
    if leftover:
        raise SystemExit(f"仍残留 {len(leftover)} 处 ti 引用，请检查")

    # ---------- 4b. 防回归：HERO_NAMES 必须只有一处定义 ----------
    heronames_defs = re.findall(r"^HERO_NAMES = .*$", body_preview := (
        "".join(domain_chunk) + web_body), re.M)
    if len(heronames_defs) != 1:
        raise SystemExit(
            f"HERO_NAMES 模块级定义应恰好 1 处，实际 {len(heronames_defs)} 处: {heronames_defs}"
        )

    # ---------- 5. 组装 ----------
    header = (
        '#!/usr/bin/env python3\n'
        '# -*- coding: utf-8 -*-\n'
        '"""\n'
        'Dota2 同局查询 —— 单文件部署版（自动生成，请勿手工编辑）\n'
        '\n'
        '由 web_server.py 与 teammate_intersection.py 合并而成。\n'
        '生成方式: python build_standalone.py\n'
        '纯标准库，无第三方依赖，可直接 python3 standalone_server.py 运行。\n'
        '"""\n\n'
        '# ============ 领域层（内联自 teammate_intersection.py）============\n\n'
    )
    body = (header
            + "".join(domain_imports)
            + "\n"
            + "".join(domain_chunk)
            + (
        '\n# ============ Web 服务层（内联自 web_server.py）============\n\n' + web_body
    ))

    open(OUT, "w", encoding="utf-8").write(body)

    # ---------- 6. 自检 ----------
    ast.parse(body)
    size = os.path.getsize(OUT)
    print(f"\n[ok] 已生成 {os.path.basename(OUT)}  ({size} 字节)")
    print(f"[ok] 语法校验通过")

    # ---------- 6b. 自检：内联块用到的顶层模块必须已 import ----------
    check_imports(body)
    return 0


def check_imports(body):
    """
    防回归：扫描内联后的代码里所有 `mod.attr` 形式的模块引用，
    确认这些顶层模块都已 import。

    动机：urllib.request 缺失那次事故——web 侧只导入了 urllib.parse，
    使 `import urllib` 生效，`urllib.request` 直到运行到 api_get 才报错。
    """
    tree = ast.parse(body)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

    # 收集所有 <Name>.<attr> 中的首名，且该名字不是本地定义的变量/函数
    local = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    local.add(t.id)
        elif isinstance(node, ast.For):
            pass

    STDLIB_HINTS = {
        "urllib", "os", "sys", "json", "time", "csv", "io", "re", "random",
        "string", "threading", "traceback", "collections", "signal", "webbrowser",
        "http", "ast", "hashlib", "datetime", "math", "itertools",
    }
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            nm = node.value.id
            if nm in STDLIB_HINTS and nm not in local:
                referenced.add(nm)

    missing = referenced - imported
    if missing:
        raise SystemExit(
            "[失败] 以下模块被引用但未 import（内联 import 遗漏）: %s" % sorted(missing)
        )
    print(f"[ok] import 自检通过（引用的顶层模块均已导入: {sorted(referenced)}）")


if __name__ == "__main__":
    sys.exit(main())
