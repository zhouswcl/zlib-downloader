#!/usr/bin/env python3
"""
Z-Library 图书下载器
用 zlib login 获取认证 Cookie，Python 通过 HTTP 完成搜索和下载
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = ROOT / "downloads"
HISTORY_FILE = DATA_DIR / "downloaded_ids.json"

ALIYUN_TOKEN_URL = "https://api.aliyundrive.com/v2/account/token"
ALIYUN_CREATE_URL = "https://api.aliyundrive.com/v2/file/create"
ALIYUN_COMPLETE_URL = "https://api.aliyundrive.com/v2/file/complete"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ── History ────────────────────────────────────────────────────

def load_history() -> set:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    return set()

def save_history(book_ids: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(book_ids), f)


# ── Z-Library (via zlib session) ──────────────────────────────

ZLIB_SESSION_FILE = Path.home() / ".config" / "zlib" / "session.json"
DEFAULT_DOMAINS = ["https://z-lib.sk", "https://z-lib.is"]


def zlib_login(email: str, password: str):
    """用 zlib CLI 登录，得到 session 文件"""
    print(f"  登录中...")
    result = subprocess.run(
        ["zlib", "login", "--email", email, "--password", password],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"登录失败: {result.stderr[:300]}")
    if not ZLIB_SESSION_FILE.exists():
        raise RuntimeError("登录成功但未找到 session 文件")
    print(f"  [✓] 登录成功")


def create_session() -> requests.Session:
    """从 zlib session 文件创建已认证的 requests Session"""
    with open(ZLIB_SESSION_FILE) as f:
        session_data = json.load(f)

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    domain = session_data.get("domain", "https://z-lib.sk")
    cookies = session_data.get("cookies", {})

    # 恢复 cookie
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=domain.replace("https://", ""))

    s.domain = domain
    s.logged_in = bool(cookies)
    return s


def search_books(session: requests.Session, query: str, page: int = 1, count: int = 10) -> list[dict]:
    """搜索图书，返回列表"""
    domain = getattr(session, "domain", "https://z-lib.sk")
    encoded = requests.utils.quote(query)
    url = f"{domain}/s/{encoded}?&page={page}"
    print(f"  URL: {url}")

    resp = session.get(url, timeout=30, headers={
        "Referer": f"{domain}/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    if resp.status_code == 503:
        print(f"  503 服务忙，等 5 秒重试...")
        time.sleep(5)
        resp = session.get(url, timeout=30)

    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}")
        return []

    html = resp.text

    # 检测并跟随 JS 挑战重定向
    m = re.search(r"redirect_link\s*=\s*'([^']+)'", html)
    if m:
        print(f"  检测到挑战，跟随...")
        resp2 = session.get(m.group(1), timeout=30)
        if resp2.status_code == 200:
            html = resp2.text

    return parse_search_results(html, domain, count)


def parse_search_results(html: str, domain: str, limit: int = 10) -> list[dict]:
    """解析图书列表"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    books = []

    # 策略 1: z-bookcard
    for tag in soup.select("z-bookcard, [is='z-bookcard']"):
        book = {
            "id": (tag.get("id") or "").strip(),
            "title": "",
            "author": "",
            "extension": (tag.get("extension") or "").strip(),
            "size": (tag.get("filesize") or "").strip(),
            "url": "",
        }
        href = tag.get("href", "")
        if href:
            book["url"] = domain + href if href.startswith("/") else href

        title_el = tag.select_one('[slot="title"]')
        if title_el:
            book["title"] = title_el.get_text(strip=True)

        author_el = tag.select_one('[slot="author"]')
        if author_el:
            book["author"] = author_el.get_text(strip=True)

        if book.get("id") or book.get("title"):
            books.append(book)

    # 策略 2: a[href*="/book/"] 通用匹配
    if not books:
        for a in soup.select('a[href*="/book/"]'):
            href = a.get("href", "")
            m = re.search(r"/book/([^/]+)", href)
            bid = m.group(1) if m else ""
            title = a.get_text(strip=True)
            if bid and title and len(bid) > 3:
                url = domain + href if href.startswith("/") else href
                books.append({"id": bid, "title": title, "url": url})

    # 去重，限制数量
    seen = set()
    unique = []
    for b in books:
        bid = b.get("id", "")
        if bid and bid not in seen:
            seen.add(bid)
            unique.append(b)
        if len(unique) >= limit:
            break

    return unique


def download_book(session: requests.Session, book_id: str, dest_dir: str) -> dict | None:
    """下载图书"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    domain = getattr(session, "domain", "https://z-lib.sk")

    # 构造下载 URL
    dl_url = None
    for prefix in ("/dl/", "/file/"):
        try:
            resp = session.head(f"{domain}{prefix}{book_id}", allow_redirects=True, timeout=15)
            if resp.status_code == 200 and resp.url:
                dl_url = resp.url
                break
        except Exception:
            continue

    if not dl_url:
        print(f"  [!] 无法获取下载链接")
        return None

    print(f"  下载: {dl_url}")
    try:
        resp = session.get(dl_url, stream=True, timeout=600)
        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code}")
            return None

        # 提取文件名
        filename = f"{book_id}.pdf"
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename\s*=\s*["\']?([^"\';\n]+)', cd)
        if m:
            filename = m.group(1).strip().strip('"').strip("'")
        else:
            parts = dl_url.rstrip("/").split("/")
            if parts and "." in (parts[-1].split("?")[0] or ""):
                filename = parts[-1].split("?")[0]

        filepath = dest_dir / filename
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

        print(f"  [✓] {filename} ({human_size(downloaded)})")
        return {"filepath": str(filepath), "filename": filename, "size": downloaded, "book_id": book_id}
    except Exception as e:
        print(f"  [!] 下载异常: {e}")
        return None


# ── 阿里云盘 ──────────────────────────────────────────────────

def upload_to_aliyundrive(local_path: str, refresh_token: str, parent_id: str = "root") -> dict:
    """上传文件到阿里云盘"""
    resp = requests.post(ALIYUN_TOKEN_URL, json={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    token_data = resp.json()
    access_token = token_data["access_token"]
    drive_id = token_data["default_drive_id"]

    headers = {"Authorization": f"Bearer {access_token}"}
    file_size = os.path.getsize(local_path)
    filename = os.path.basename(local_path)

    create_resp = requests.post(ALIYUN_CREATE_URL, headers=headers, json={
        "drive_id": drive_id, "name": filename, "parent_file_id": parent_id,
        "type": "file", "size": file_size, "check_name_mode": "auto_rename",
    })
    create_resp.raise_for_status()
    create_data = create_resp.json()

    if create_data.get("rapid_upload"):
        return {"success": True, "file_name": filename, "file_id": create_data.get("file_id", ""), "rapid_upload": True}

    upload_url = create_data.get("part_info_list", [{}])[0].get("upload_url", "")
    if not upload_url:
        upload_url = create_data.get("upload_url", "")
    if not upload_url:
        return {"success": False, "error": "无上传 URL"}

    with open(local_path, "rb") as f:
        upload_resp = requests.put(upload_url, data=f)
        if upload_resp.status_code not in (200, 201):
            return {"success": False, "error": f"上传失败: HTTP {upload_resp.status_code}"}

    file_id = create_data.get("file_id", "")
    if file_id:
        requests.post(ALIYUN_COMPLETE_URL, headers=headers, json={
            "drive_id": drive_id, "file_id": file_id,
        })

    return {"success": True, "file_name": filename, "file_id": file_id, "size": file_size}


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Z-Library 图书下载")
    parser.add_argument("--keywords", default="", help="搜索关键词（逗号分隔）")
    parser.add_argument("--max-downloads", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--upload", action="store_true", default=True)
    args = parser.parse_args()

    # 关键词
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",")]
    else:
        keywords = ["编程", "科技", "人工智能", "Python", "机器学习", "经济管理", "历史", "传记"]

    max_downloads = min(args.max_downloads, 10)
    zlib_email = os.environ.get("ZLIB_EMAIL", "")
    zlib_password = os.environ.get("ZLIB_PASSWORD", "")
    aliyun_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
    aliyun_parent = os.environ.get("ALIYUNDRIVE_PARENT_ID", "root")

    if not zlib_email or not zlib_password:
        print("ERROR: ZLIB_EMAIL and ZLIB_PASSWORD required")
        sys.exit(1)

    downloaded_ids = load_history()
    results = []
    total_size = 0
    success_count = 0

    print(f"\n{'='*60}")
    print(f"  Z-Library - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  关键词: {', '.join(keywords)}")
    print(f"  最多: {max_downloads} 本")
    print(f"{'='*60}\n")

    # Step 1: 登录
    print("[1/3] 登录...")
    try:
        zlib_login(zlib_email, zlib_password)
    except Exception as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    # 创建已认证的 session
    session = create_session()
    print(f"  域: {getattr(session, 'domain', '?')}")

    # Step 2: 搜索并下载
    print(f"[2/3] 搜索并下载...")
    books_to_download = []

    for kw in keywords:
        if len(books_to_download) >= max_downloads:
            break
        print(f"\n  搜索 '{kw}'...")
        try:
            books = search_books(session, kw)
        except Exception as e:
            print(f"  [!] 搜索异常: {e}")
            continue

        if not books:
            print(f"  无结果")
            continue

        print(f"  找到 {len(books)} 本")
        for b in books:
            bid = b.get("id", "")
            if bid and bid not in downloaded_ids:
                b["_keyword"] = kw
                books_to_download.append(b)
                if len(books_to_download) >= max_downloads:
                    break

    if not books_to_download:
        print("\n[!] 没有可下载的新书")
        sys.exit(0)

    print(f"\n  将下载 {len(books_to_download)} 本...")
    date_str = datetime.now().strftime("%Y%m%d")
    dl_dir = DOWNLOAD_DIR / date_str
    dl_dir.mkdir(parents=True, exist_ok=True)

    for i, book in enumerate(books_to_download, 1):
        bid = book["id"]
        title = (book.get("title") or "?")[:40]
        ext = book.get("extension", "?")
        size = book.get("size", "?")
        kw = book.get("_keyword", "")
        print(f"\n  [{i}/{len(books_to_download)}] [{kw}] {title}")
        print(f"      ID: {bid} | {ext} | {size}")

        result = download_book(session, bid, str(dl_dir))
        if not result:
            print(f"  [!] 下载失败，跳过")
            continue

        total_size += result["size"]

        upload_ok = False
        if args.upload and aliyun_token:
            print(f"  上传阿里云盘...")
            upload_result = upload_to_aliyundrive(result["filepath"], aliyun_token, aliyun_parent)
            if upload_result.get("success"):
                print(f"  [✓] 上传成功{' (秒传)' if upload_result.get('rapid_upload') else ''}")
                result["upload"] = upload_result
                upload_ok = True
                success_count += 1
            else:
                print(f"  [!] 上传失败: {upload_result.get('error', '?')}")
                result["upload"] = upload_result
        else:
            upload_ok = True
            success_count += 1

        results.append(result)
        if upload_ok:
            downloaded_ids.add(bid)
            save_history(downloaded_ids)

        if i < len(books_to_download):
            delay = random.uniform(3, 8)
            print(f"  等待 {delay:.0f}s...")
            time.sleep(delay)

    # 清理
    for r in results:
        fp = r.get("filepath", "")
        if fp and os.path.exists(fp):
            os.remove(fp)

    summary = {
        "date": date_str, "keywords": keywords,
        "total": len(books_to_download), "success": success_count,
        "fail": len(books_to_download) - success_count,
        "total_size": total_size, "human_size": human_size(total_size),
    }

    if args.json:
        print(f"\n{json.dumps(summary, ensure_ascii=False, indent=2)}")
    else:
        print(f"\n{'='*60}")
        print(f"  完成: {summary['success']}/{summary['total']} | {summary['human_size']}")
        print(f"{'='*60}")

    save_history(downloaded_ids)
    sys.exit(0 if success_count > 0 else 1)


if __name__ == "__main__":
    main()
