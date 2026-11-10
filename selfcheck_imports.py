# -*- coding: utf-8 -*-
"""自检：源码里有没有"用了却没定义"的全局名字（1.65）。

为什么需要这一条：2026-11-10 把 `cli.py` 的转发函数拆到 `cli_forward.py` 时，
代码搬走了、`import` 没搬：`os` / `json` / `sys` / `subprocess` / `_safe_name` / `BASE` /
`session_mod` —— **同一个坑连踩七次**，每次都靠运行时 NameError 才发现
（`hold:true` 直接崩，用户看到的是"鲸歌内部错误"）。

做法：AST 走一遍 `serial_bridge/*.py`，收集：
  · 模块级定义（import、def、class、赋值、for/with 目标）
  · 函数内的局部名、参数、内建名
剩下的"读"就是运行时才可能炸的全局名。命中即报错。

它不认识 `globals()` 动态注入这类花活 —— 这个项目里没有，真有了再放白名单。
"""

import ast
import builtins
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "serial_bridge")

# 已知的、确实在运行时才注入的名字（保持为空：出现就说明应该显式 import）
ALLOW = {"__file__", "__name__", "__doc__", "__package__"}


def module_names(tree):
    """模块级可见的名字：import 别名、def/class、顶层赋值、__dunder__。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def function_locals(func):
    """这个函数里所有局部绑定（参数、赋值、for/with 目标、except 别名）。"""
    local = set()
    for node in ast.walk(func):
        if isinstance(node, ast.arg):
            local.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    local.add(sub.id)
        elif isinstance(node, ast.Global):
            local.update(node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local.add(alias.asname or alias.name)
    return local


def check_file(path):
    """返回该文件里"用到但模块级没有"的名字列表。"""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=path)
    top = module_names(tree) | set(dir(builtins)) | ALLOW
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = function_locals(node) | top
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in local:
                    problems.append((node.name, sub.id, sub.lineno))
    return problems


def main():
    print("=" * 70)
    print("自检：未定义全局名（搬代码忘搬 import）")
    print("=" * 70)
    total = 0
    bad_files = 0
    scanned = 0
    for root, _dirs, files in os.walk(TARGET):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            scanned += 1
            try:
                problems = check_file(path)
            except SyntaxError as exc:
                print("  FAIL  %s 语法错误：%s" % (name, exc))
                bad_files += 1
                total += 1
                continue
            if problems:
                bad_files += 1
                seen = set()
                for func, ident, line in problems:
                    key = (func, ident)
                    if key in seen:
                        continue
                    seen.add(key)
                    rel = os.path.relpath(path, HERE)
                    print("  FAIL  %s:%d  %s() 里用了 %r，但模块级没有它（少了 import？）"
                          % (rel, line, func, ident))
                    total += 1

    if not total:
        print("\n结论：%d 个文件，未定义全局名 0 处 ✅" % scanned)
        print("=" * 70)
        print("PASS 1 / FAIL 0")
        return 0
    print("\n结论：%d 个文件里共 %d 处用了未定义的名字，运行时必 NameError ❌"
          % (bad_files, total))
    print("=" * 70)
    print("PASS 0 / FAIL %d" % total)
    return 1


if __name__ == "__main__":
    sys.exit(main())
