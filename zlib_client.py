"""
Z-Library CLI Client
====================
完全通过 heartleo/zlib (Go) CLI 子进程完成所有操作，
避免 Python HTTP 库被 Cloudflare 拦截。

核心流程:
  1. zlib login --email ... --password ...    → 认证
  2. zlib search "query" --count N             → 表格输出，Python 解析提取 book ID
  3. script -qec "zlib download <id> --dir X"  → 通过 PTY 运行下载 (bubbletea 需要 TTY)
  4. zlib profile                               → 查询下载限额
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

ZLIB_CONFIG_DIR = Path.home() / ".config" / "zlib"
SESSION_FILE = ZLIB_CONFIG_DIR / "session.json"


class ZLibraryError(Exception):
    pass


# ── zlib CLI 封装 ──────────────────────────────────


def _run_zlib(*args: str, timeout: int = 120) -> str:
    """运行 zlib CLI 命令，返回 stdout"""
    cmd = ["zlib"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ZLibraryError(
            "zlib CLI not found. Install with:\n"
            "  curl -fsSL https://raw.githubusercontent.com/heartleo/zlib/main/install.sh | sh"
        )
    except subprocess.TimeoutExpired:
        raise ZLibraryError(f"zlib {' '.join(args)} timed out ({timeout}s)")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ZLibraryError(f"zlib {' '.join(args)} failed: {stderr or 'exit code ' + str(result.returncode)}")

    return result.stdout


def _run_zlib_in_pty(*args: str, timeout: int = 300) -> tuple[str, int]:
    """在伪终端中运行 zlib CLI 命令（用于 bubbletea TUI 命令如 download）"""
    cmd = ["zlib"] + list(args)
    cmd_str = " ".join(cmd)

    # 使用 script 建立 PTY，让 bubbletea 正常工作
    script_cmd = ["script", "-qec", cmd_str, "/dev/null"]

    try:
        result = subprocess.run(
            script_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ZLibraryError(f"zlib {' '.join(args)} timed out ({timeout}s)")

    # script 的输出包含 ANSI 转义码，需要清理
    return result.stdout, result.returncode


# ── Session 管理 ──────────────────────────────────


def load_session() -> Optional[dict]:
    """从 ~/.config/zlib/session.json 加载会话"""
    if not SESSION_FILE.exists():
        return None
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, PermissionError):
        return None


def is_logged_in() -> bool:
    session = load_session()
    return session is not None and bool(session.get("cookies")) and bool(session.get("domain"))


# ── 登录 ──────────────────────────────────


def login(email: str, password: str) -> str:
    """登录 Z-Library，返回可用域名"""
    _run_zlib("version", timeout=10)
    _run_zlib("login", "--email", email, "--password", password, timeout=30)

    for attempt in range(5):
        session = load_session()
        if session and session.get("cookies"):
            return session.get("domain", "")
        time.sleep(1)

    raise ZLibraryError("登录后未找到 session 文件")


# ── 搜索（解析表格输出）─────────────────────────


def _strip_ansi(text: str) -> str:
    """去除 ANSI 转义码"""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def _parse_search_table(output: str) -> list[dict]:
    """解析 zlib search 的表格输出"""
    # 先去 ANSI
    clean = _strip_ansi(output)
    books = []

    # 表格格式（来自 Go 源码）:
    # ✓ Found 10 results · Page 1 / 3
    #
    # ╭─────┬──────┬──────────────────────────┬─────────────────┬──────┬────────┬──────────┬──────────╮
    # │  #  │ ID   │ Title                    │ Authors         │ Year │ Format │ Size     │ Rating   │
    # ├─────┼──────┼──────────────────────────┼─────────────────┼──────┼────────┼──────────┼──────────┤
    # │  1  │ abcd │ The Go Programming Lang. │ Donovan & K.    │ 2015 │ EPUB   │ 2.5 MB   │ 4.5      │
    # ╰─────┴──────┴──────────────────────────┴─────────────────┴──────┴────────┴──────────┴──────────╯

    lines = clean.split("\n")
    in_table = False
    in_header = False

    for line in lines:
        stripped = line.strip()

        # 跳过空行和"Found N results"行
        if not stripped or stripped.startswith("✓"):
            continue

        # 检测表格开始
        if stripped.startswith("╭"):
            in_table = True
            in_header = True
            continue

        if not in_table:
            continue

        # 检测表头/数据分隔线
        if stripped.startswith("├") or stripped.startswith("╰"):
            in_header = False
            continue

        # 数据行
        if stripped.startswith("│") and stripped.endswith("│"):
            if in_header:
                in_header = False
                continue

            cells = _split_table_row(stripped)
            if len(cells) >= 3:
                idx = cells[0].strip()
                book_id = cells[1].strip()
                title = cells[2].strip()
                authors = cells[3].strip() if len(cells) > 3 else ""
                year = cells[4].strip() if len(cells) > 4 else ""
                ext = cells[5].strip() if len(cells) > 5 else ""
                size = cells[6].strip() if len(cells) > 6 else ""

                if idx in ("", "#"):
                    continue
                if not book_id or book_id == "-":
                    continue

                books.append({
                    "id": book_id,
                    "title": title,
                    "author": authors,
                    "year": year,
                    "extension": ext.lower(),
                    "size": size,
                })

        # 表格结束
        elif stripped.startswith("╰"):
            break

    return books


def _split_table_row(line: str) -> list[str]:
    """按 │ 分割表格行"""
    inner = line.strip("│\n")
    cells = inner.split("│")
    return [c.strip() for c in cells]


def search(query: str, page: int = 1, count: int = 10) -> list[dict]:
    """搜索图书，返回列表"""
    output = _run_zlib("search", query, "--page", str(page), "--count", str(count), timeout=30)
    return _parse_search_table(output)


# ── 下载（通过 PTY）─────────────────────────


def _clean_pty_output(raw: str) -> str:
    """清理 script + bubbletea 的输出，提取有用信息"""
    text = _strip_ansi(raw)
    lines = text.split("\n")

    # 查找 "Saved to:" 行
    for line in lines:
        if "Saved to:" in line:
            return line.strip()

    # 查找文件路径
    for line in lines:
        line = line.strip()
        if line and not line.startswith("│") and not line.startswith("╭") and not line.startswith("╰") and not line.startswith("├"):
            if "/" in line and os.path.exists(line):
                return f"Saved to: {line}"

    return text.strip()[:200]


def _extract_error(raw: str) -> str:
    """从 PTY 输出中提取真正的错误信息"""
    text = _strip_ansi(raw)
    lines = text.split("\n")

    # 收集所有可能包含错误信息的行（保留 500 行缓存）
    error_lines = []
    for line in lines:
        sl = line.strip()
        # 跳过 spinner 动画行（只包含 spinner 字符和 "Fetching"）
        if sl and not sl.startswith("│") and not sl.startswith("╭") \
           and not sl.startswith("╰") and not sl.startswith("├"):
            # 跳过纯 spinner 行
            if not any(c in sl for c in "⣽⣻⢿⡿⣟⣯⣷⣾"):
                error_lines.append(sl)
            elif "Error:" in sl or "error" in sl.lower() or "fail" in sl.lower():
                error_lines.append(sl)

    # 优先查找 "Error:" 行
    for line in error_lines:
        if "Error:" in line:
            return line

    # 查找最后几条非 spinner 行
    meaningful = [l for l in error_lines if l and len(l) > 5]
    if meaningful:
        return meaningful[-1]

    # 退回到截断输出（但扩大到 500 字符）
    return text.strip()[:500]


def download(book_id: str, dest_dir: str, timeout: int = 600) -> Optional[dict]:
    """下载图书到本地目录（通过 PTY 运行 zlib download）"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"  运行 zlib download {book_id}... (通过伪终端)")
    raw_output, exit_code = _run_zlib_in_pty(
        "download", book_id, "--dir", str(dest_dir),
        timeout=timeout
    )

    summary = _clean_pty_output(raw_output)
    error_detail = _extract_error(raw_output)

    if exit_code != 0:
        print(f"  [!] 下载命令返回退出码 {exit_code}")
        print(f"  错误: {error_detail}")

        # 检测 "no download URL available" — 这是 Z-Library 侧的问题，fallback 也没用
        if "no download URL available" in error_detail.lower() or "no download" in error_detail.lower():
            print(f"  [x] Z-Library 无此书的下载链接，跳过 fallback")
            return None

        # 尝试 fallback：不用 PTY，直接用 subprocess 跑（bubbletea 可能不支持 script）
        print(f"  [!] 尝试备用下载方式 (直接子进程)...")
        try:
            result_direct = subprocess.run(
                ["zlib", "download", book_id, "--dir", str(dest_dir)],
                capture_output=True, text=True, timeout=timeout,
            )
            if result_direct.returncode == 0:
                print(f"  [✓] 备用下载成功")
                # 查找下载的文件
                files = sorted(dest_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True) if dest_dir.exists() else []
                if files:
                    newest = files[0]
                    now = time.time()
                    if now - newest.stat().st_mtime < 60:
                        return {
                            "filepath": str(newest),
                            "filename": newest.name,
                            "size": newest.stat().st_size,
                            "book_id": book_id,
                        }
                print(f"  [!] 备用下载完成但找不到文件")
                return None
            else:
                print(f"  [!] 备用方式也失败: {result_direct.stderr.strip()[:300]}")
        except Exception as e2:
            print(f"  [!] 备用方式异常: {e2}")
        return None

    print(f"  输出: {summary}")

    # 找到刚刚下载的文件
    files = sorted(dest_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True) if dest_dir.exists() else []
    if not files:
        print(f"  [!] 下载完成但未找到文件")
        return None

    newest = files[0]
    now = time.time()
    if now - newest.stat().st_mtime > 15:
        print(f"  [!] 未找到刚刚下载的文件，最近: {newest.name}")
        return None

    return {
        "filepath": str(newest),
        "filename": newest.name,
        "size": newest.stat().st_size,
        "book_id": book_id,
    }


# ── 限额查询 ──────────────────────────────────


def get_daily_limit() -> dict:
    """查询当日下载限额"""
    output = _run_zlib("profile", timeout=15)
    clean = _strip_ansi(output)
    m = re.search(r"(\d+)\s*/\s*(\d+)", clean)
    if m:
        used = int(m.group(1))
        total = int(m.group(2))
        return {
            "used": used,
            "total": total,
            "remaining": total - used,
        }
    return {"used": 0, "total": 10, "remaining": 10}