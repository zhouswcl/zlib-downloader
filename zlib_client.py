"""
Z-Library CLI Client
====================
使用 heartleo/zlib (Go) CLI 进行登录认证，
然后复用 Go 客户端的 session cookie 做 Python HTTP 请求。

核心流程:
  1. zlib login --email ... --password ...   → 保存 session 到 ~/.config/zlib/session.json
  2. Python 读取 session.json 的 cookies + domain
  3. Python 复用 cookie 发 HTTP 请求 (Go 客户端已处理好 Cloudflare 握手)
  4. zlib profile 查询限额
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

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
        # zlib CLI 可能返回非零但已登录成功
        raise ZLibraryError(f"zlib {' '.join(args)} failed: {stderr or 'exit code ' + str(result.returncode)}")

    return result.stdout


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


def parse_domain(domain_url: str) -> str:
    """从 URL 提取域名（用于 cookie 设置）"""
    parsed = urlparse(domain_url)
    return parsed.netloc or parsed.path


def _make_requests_session(session_data: dict) -> requests.Session:
    """从 zlib session 数据创建 requests.Session"""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })

    domain = session_data.get("domain", "")
    netloc = parse_domain(domain)

    for key, value in session_data.get("cookies", {}).items():
        sess.cookies.set(key, value, domain=netloc, path="/")

    return sess


# ── 登录 ──────────────────────────────────


def login(email: str, password: str) -> str:
    """登录 Z-Library，返回可用域名"""
    # 先确认 zlib CLI 已就绪
    _run_zlib("version", timeout=10)

    # 登录
    _run_zlib("login", "--email", email, "--password", password, timeout=30)

    # 确认 session 文件已创建
    for attempt in range(5):
        session = load_session()
        if session and session.get("cookies"):
            return session.get("domain", "")
        time.sleep(1)

    raise ZLibraryError("登录后未找到 session 文件")


# ── 搜索 ──────────────────────────────────


def search(query: str, page: int = 1, count: int = 10) -> list[dict]:
    """搜索图书，返回列表（复用 zlib session 的 cookie 做 HTTP 请求）"""
    session_data = load_session()
    if not session_data:
        raise ZLibraryError("未登录，请先运行 login()")

    domain = session_data.get("domain", "")
    if not domain:
        raise ZLibraryError("session 中没有域名信息")

    sess = _make_requests_session(session_data)
    url = f"{domain}/s/{requests.utils.quote(query)}?page={page}"

    resp = sess.get(url, timeout=30)

    if resp.status_code == 503:
        # Z-Library 限流，等 5 秒重试一次
        print(f"  503 服务忙，等 5 秒重试...")
        time.sleep(5)
        resp = sess.get(url, timeout=30)

    if resp.status_code != 200:
        raise ZLibraryError(f"搜索请求返回 HTTP {resp.status_code}")

    return _parse_search_results(resp.text, domain)


def _parse_search_results(html: str, domain: str) -> list[dict]:
    """解析搜索结果的 HTML（4 层 fallback）"""
    soup = BeautifulSoup(html, "html.parser")
    books = []
    seen_ids = set()

    # 策略 1: z-bookcard 自定义元素（新版 Z-Library）
    for card in soup.select("z-bookcard, [is='z-bookcard']"):
        book = _parse_book_card(card, domain)
        bid = book.get("id", "")
        if bid and bid not in seen_ids:
            seen_ids.add(bid)
            books.append(book)

    # 策略 2: .book-item 传统结构（旧版镜像）
    if not books:
        for card in soup.select(".book-item, [class*=resItemCard], [class*=bookCard]"):
            book = _parse_book_card_legacy(card, domain)
            bid = book.get("id", "")
            if bid and bid not in seen_ids and book.get("title"):
                seen_ids.add(bid)
                books.append(book)

    # 策略 3: 通用卡片结构
    if not books:
        for card in soup.select("a[href*='/book/'], div[class*=card]"):
            link = card if card.name == "a" else card.select_one("a[href*='/book/']")
            if link:
                book = _parse_book_card_legacy(card, domain)
                if book.get("title") and book.get("id"):
                    books.append(book)

    # 策略 4: 遍历所有链接
    if not books:
        for a in soup.select("a[href*='/book/']"):
            href = a.get("href", "")
            m = re.search(r"/book/([^/]+)", href)
            if m:
                bid = m.group(1)
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    books.append({
                        "id": bid,
                        "title": a.get_text(strip=True),
                        "url": href if href.startswith("http") else domain + href,
                    })

    return books


def _parse_book_card(card, domain: str) -> dict:
    """解析新版 z-bookcard 元素"""
    book = {
        "id": (card.get("id") or "").strip(),
        "isbn": (card.get("isbn") or "").strip(),
        "title": "",
        "author": "",
        "publisher": (card.get("publisher") or "").strip(),
        "year": (card.get("year") or "").strip(),
        "language": (card.get("language") or "").strip(),
        "extension": (card.get("extension") or "").strip(),
        "size": (card.get("filesize") or "").strip(),
        "rating": (card.get("rating") or "").strip(),
        "quality": (card.get("quality") or "").strip(),
        "url": "",
        "cover": "",
    }

    href = card.get("href", "")
    if href:
        if href.startswith("/"):
            book["url"] = domain + href
        elif href.startswith("http"):
            book["url"] = href

    img = card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src", "")
        if src and src.startswith("/"):
            book["cover"] = domain + src
        elif src.startswith("http"):
            book["cover"] = src

    title_el = card.select_one('[slot="title"]')
    if title_el:
        book["title"] = title_el.get_text(strip=True)

    author_el = card.select_one('[slot="author"]')
    if author_el:
        book["author"] = author_el.get_text(strip=True)

    return book


def _parse_book_card_legacy(card, domain: str) -> dict:
    """解析旧版卡片结构"""
    book = {
        "id": "",
        "title": "",
        "author": "",
        "extension": "",
        "size": "",
        "url": "",
    }

    link = card.select_one("a[href*='/book/']")
    if link:
        href = link.get("href", "")
        if href.startswith("/"):
            book["url"] = domain + href
        elif href.startswith("http"):
            book["url"] = href
        m = re.search(r"/book/([^/]+)", href)
        if m:
            book["id"] = m.group(1)
        book["title"] = link.get_text(strip=True)

    author_el = card.select_one(".author, [class*=author], [class*=by]")
    if author_el:
        book["author"] = author_el.get_text(strip=True)

    ext_el = card.select_one(".format, [class*=format], [class*=extension]")
    if ext_el:
        book["extension"] = ext_el.get_text(strip=True)

    size_el = card.select_one(".size, [class*=size]")
    if size_el:
        book["size"] = size_el.get_text(strip=True)

    return book


# ── 下载 ──────────────────────────────────


def download(book_id: str, dest_dir: str, timeout: int = 600) -> Optional[dict]:
    """下载图书到本地目录"""
    session_data = load_session()
    if not session_data:
        raise ZLibraryError("未登录")

    domain = session_data.get("domain", "")
    if not domain:
        raise ZLibraryError("session 中没有域名信息")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    sess = _make_requests_session(session_data)

    # 尝试多个下载路径
    dl_url = None
    for path in [f"/dl/{book_id}", f"/file/{book_id}"]:
        url = domain + path
        try:
            resp = sess.get(url, allow_redirects=True, stream=True, timeout=30)
            if resp.status_code == 200 and resp.url:
                dl_url = resp.url
                break
            resp.close()
        except Exception:
            continue

    if not dl_url:
        print(f"  [!] 无法获取下载链接 (ID: {book_id})")
        return None

    print(f"  下载中... (通过 zlib cookie 认证)")
    try:
        resp = sess.get(dl_url, stream=True, timeout=timeout)
        if resp.status_code != 200:
            print(f"  [!] 下载 HTTP {resp.status_code}")
            return None

        filename = _extract_filename(resp, dl_url, book_id)
        filepath = dest_dir / filename

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        last_log = 0

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and downloaded - last_log > 5 * 1024 * 1024:
                        pct = downloaded * 100 / total
                        print(f"  {filename}: {downloaded//1024//1024}MB/{total//1024//1024}MB ({pct:.0f}%)")
                        last_log = downloaded

        return {
            "filepath": str(filepath),
            "filename": filename,
            "size": downloaded,
            "book_id": book_id,
        }
    except Exception as e:
        print(f"  [!] 下载异常: {e}")
        return None


def _extract_filename(resp, dl_url: str, book_id: str) -> str:
    """从 HTTP 响应提取文件名"""
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\s*=\s*["\']?([^"\';:\n]+)', cd)
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        if name:
            # 处理 URL 编码
            from urllib.parse import unquote
            name = unquote(name)
            return name

    parts = dl_url.rstrip("/").split("/")
    candidate = parts[-1].split("?")[0] if parts else ""
    if candidate and "." in candidate:
        return candidate

    return f"{book_id}.pdf"


# ── 限额查询 ──────────────────────────────────


def _parse_profile_text(output: str) -> dict:
    """解析 zlib profile 输出中的限额"""
    m = re.search(r"(\d+)\s*/\s*(\d+)", output)
    if m:
        used = int(m.group(1))
        total = int(m.group(2))
        return {
            "used": used,
            "total": total,
            "remaining": total - used,
        }
    return {"used": 0, "total": 10, "remaining": 10}


def get_daily_limit() -> dict:
    """查询当日下载限额"""
    output = _run_zlib("profile", timeout=15)
    return _parse_profile_text(output)
