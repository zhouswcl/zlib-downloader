"""
Z-Library 直接下载模块
======================
替代 `zlib download` CLI，用 Python 直接下载。
1. 通过 `zlib login` 获取 session cookie
2. Python 请求图书详情页解析下载链接
3. Python 下载文件

如果 Cloudflare 拦截，fallback 到 `curl` 命令。
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

ZLIB_CONFIG_DIR = Path.home() / ".config" / "zlib"
SESSION_FILE = ZLIB_CONFIG_DIR / "session.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _load_session() -> dict:
    """加载 zlib CLI 的 session cookie"""
    if not SESSION_FILE.exists():
        return {}
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _make_cookie_header(session: dict) -> str:
    """构建 Cookie header 字符串"""
    cookies = session.get("cookies", {})
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _get_headers(session: dict) -> dict:
    """构建请求头"""
    domain = session.get("domain", "https://z-lib.sk")
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": domain + "/",
        "Cookie": _make_cookie_header(session),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }


def _extract_download_url(html: str, domain: str) -> Optional[str]:
    """从图书详情页 HTML 中提取下载链接"""
    soup = BeautifulSoup(html, "html.parser")

    # 尝试多种选择器（Z-Library 页面结构可能有变化）
    download_selectors = [
        # 常见下载按钮
        "a.addDownloadedBook",
        "a[href*='/dl/']",
        "a[href*='/file/']",
        "a.btn-primary[href]",
        "a.btn[href]",
        "a.download-button",
        # 通用匹配
        "a[download]",
        "a:has(span:contains('Download'))",
        "a:has(i.fa-download)",
        # HTML5 data 属性
        "a[data-book-id]",
        # 最后手段：找包含 'dl' 或 'download' 的链接
    ]

    for selector in download_selectors:
        try:
            link = soup.select_one(selector)
            if link and link.get("href"):
                href = link["href"]
                # 排除"不可用"的链接
                text = (link.get_text(strip=True) or "").lower()
                if "unavail" in text or "unable" in text:
                    continue
                # 构造绝对 URL
                if href.startswith("http"):
                    return href
                else:
                    return domain.rstrip("/") + "/" + href.lstrip("/")
        except Exception:
            continue

    # 正则搜索：找 /dl/xxxxx 或 /file/xxxxx 模式的链接
    for pattern in [r'href=["\'](/dl/[^"\']+)["\']', r'href=["\'](/file/[^"\']+)["\']']:
        m = re.search(pattern, html)
        if m:
            return domain.rstrip("/") + m.group(1)

    return None


def download_book(
    book_id: str,
    domain: str,
    session: dict,
    dest_dir: Path,
    timeout: int = 600,
) -> Optional[dict]:
    """直接下载图书（不依赖 zlib download CLI）"""
    book_url = f"{domain.rstrip('/')}/book/{book_id}"
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"  获取图书页面: {book_url}")

    # 1. 尝试 Python requests
    try:
        headers = _get_headers(session)
        resp = requests.get(book_url, headers=headers, timeout=30)

        if resp.status_code == 200:
            download_url = _extract_download_url(resp.text, domain)
            if download_url:
                print(f"  下载链接: {download_url[:80]}...")
                return _do_download(download_url, session, dest_dir, timeout)
            else:
                print(f"  [!] Python 解析不到下载链接")
        else:
            print(f"  [!] Python HTTP {resp.status_code}")

    except Exception as e:
        print(f"  [!] Python 请求失败: {e}")

    # 2. Fallback: 用 curl 带 cookie
    print(f"  尝试 curl fallback...")
    try:
        cookie_header = _make_cookie_header(session)
        # 先获取页面
        cmd = [
            "curl", "-s", "-L",
            "-H", f"User-Agent: {USER_AGENT}",
            "-H", f"Cookie: {cookie_header}",
            book_url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            download_url = _extract_download_url(r.stdout, domain)
            if download_url:
                print(f"  下载链接: {download_url[:80]}...")
                return _do_download_curl(download_url, dest_dir, timeout)

        print(f"  [!] curl 也解析不到下载链接")
    except Exception as e:
        print(f"  [!] curl fallback 失败: {e}")

    return None


def _do_download(
    url: str, session: dict, dest_dir: Path, timeout: int
) -> Optional[dict]:
    """通过 Python requests 下载文件"""
    filename = url.split("/")[-1].split("?")[0]
    if not filename or "." not in filename:
        filename = f"book_{int(time.time())}.pdf"

    filepath = dest_dir / filename
    headers = _get_headers(session)

    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        if resp.status_code not in (200, 302, 303):
            # 可能被重定向了
            if resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", "")
                if redirect_url:
                    return _do_download(redirect_url, session, dest_dir, timeout)
            return None

        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        if pct % 25 == 0:
                            print(f"    {pct}%...")

        if filepath.exists() and filepath.stat().st_size > 0:
            return {
                "filepath": str(filepath),
                "filename": filename,
                "size": filepath.stat().st_size,
            }

    except Exception as e:
        print(f"  [!] 下载异常: {e}")

    return None


def _do_download_curl(url: str, dest_dir: Path, timeout: int) -> Optional[dict]:
    """通过 curl 下载文件"""
    filename = url.split("/")[-1].split("?")[0]
    if not filename or "." not in filename:
        filename = f"book_{int(time.time())}.pdf"

    filepath = dest_dir / filename

    cmd = [
        "curl", "-s", "-L",
        "-H", f"User-Agent: {USER_AGENT}",
        "--connect-timeout", "30",
        "--max-time", str(timeout),
        "-o", str(filepath),
        url,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        if r.returncode == 0 and filepath.exists() and filepath.stat().st_size > 0:
            return {
                "filepath": str(filepath),
                "filename": filename,
                "size": filepath.stat().st_size,
            }
        print(f"  [!] curl 下载失败: exit {r.returncode}")
    except Exception as e:
        print(f"  [!] curl 异常: {e}")

    return None