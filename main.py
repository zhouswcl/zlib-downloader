#!/usr/bin/env python3
"""
Z-Library 下载器（使用 heartleo/zlib Go CLI）
通过 subprocess 调用 zlib 二进制处理 Z-Library 交互，
Python 只负责阿里云盘上传。
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


def load_history() -> set:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    return set()


def save_history(book_ids: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(book_ids), f)


def zlib_login(email: str, password: str):
    """登录 Z-Library"""
    print(f"  登录中...")
    result = subprocess.run(
        ["zlib", "login", "--email", email, "--password", password],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"登录失败: {result.stderr[:200]}")
    print(f"  [✓] 登录成功")


def zlib_search(query: str) -> list[dict]:
    """搜索图书，返回列表"""
    print(f"  搜索: '{query}'")
    result = subprocess.run(
        ["zlib", "search", query, "--page", "1"],
        capture_output=True, text=True, timeout=30,
    )
    print(f"  zlib 返回码: {result.returncode}")
    print(f"  stdout ({len(result.stdout)}B): {result.stdout[:500]}")
    if result.stderr:
        print(f"  stderr ({len(result.stderr)}B): {result.stderr[:300]}")

    if result.returncode != 0:
        print(f"  [!] 搜索失败")
        return []

    books = _parse_zlib_search_output(result.stdout)
    print(f"  解析到 {len(books)} 本书")
    return books


def _parse_zlib_search_output(output: str) -> list[dict]:
    """解析 zlib search 的文本表格输出"""
    books = []
    lines = output.strip().split("\n")

    for line in lines:
        # 从表格行提取: 编号. ID 标题 | 作者 | 格式 | 评分
        line = line.strip()

        # 跳过非数据行
        if not line or line.startswith("No results") or line.startswith("Found"):
            continue

        # 尝试提取 book ID (通常是第一列的数字+字母)
        m = re.match(r"\s*\d+\.\s*(\S+)", line)
        if not m:
            continue

        book_id = m.group(1)
        if not book_id or len(book_id) < 5:
            continue

        books.append({"id": book_id})

    return books


def zlib_get_book_info(book_id: str) -> dict:
    """获取图书详细信息（通过 book 页面 URL）"""
    # 从详情页提取信息
    # 使用 ZLIB_DOMAIN 环境变量
    domain = os.environ.get("ZLIB_DOMAIN", "https://z-lib.sk")
    try:
        import requests
        resp = requests.get(f"{domain}/book/{book_id}", timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            title = ""
            author = ""
            ext = ""
            size_text = ""

            zcover = soup.select_one("z-cover")
            if zcover:
                title = (zcover.get("title") or "").strip()

            if not title:
                h1 = soup.select_one("h1, [class*=title]")
                if h1:
                    title = h1.get_text(strip=True)

            author_el = soup.select_one("i.authors a, [class*=author] a")
            if author_el:
                author = author_el.get_text(strip=True)

            for prop in soup.select(".bookDetailsBox .property__file .property_value, "
                                    ".bookDetailsBox [class*=file] .property_value"):
                text = prop.get_text(strip=True)
                m = re.search(r"\b(pdf|epub|mobi|txt|djvu|fb2|docx?)\b", text, re.I)
                if m:
                    ext = m.group(1).lower()
                m = re.search(r"(\d+(?:[.,]\d+)?)\s*(MB|KB|GB)", text, re.I)
                if m:
                    size_text = m.group(0)

            return {"title": title, "author": author, "extension": ext, "size": size_text}
    except Exception:
        pass
    return {}


def zlib_download(book_id: str, dest_dir: str) -> dict | None:
    """下载图书"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"  下载: {book_id}")
    try:
        result = subprocess.run(
            ["zlib", "download", book_id, "--dir", str(dest_dir)],
            capture_output=True, text=True, timeout=300,
        )

        stdout = result.stdout + result.stderr
        print(f"  zlib 输出: {stdout[:200]}")

        if result.returncode != 0:
            print(f"  [!] 下载失败: {result.stderr[:200]}")
            return None

        # 从输出中提取文件名和大小
        m = re.search(r"Saved to:\s*(\S+)\s*\((\d+)\s*bytes\)", stdout)
        if m:
            filepath = m.group(1)
            size = int(m.group(2))
            return {
                "filepath": filepath,
                "filename": Path(filepath).name,
                "size": size,
                "book_id": book_id,
            }

        # fallback: 找目录中最新文件
        files = sorted(dest_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            f = files[0]
            return {
                "filepath": str(f),
                "filename": f.name,
                "size": f.stat().st_size,
                "book_id": book_id,
            }

        return None
    except subprocess.TimeoutExpired:
        print(f"  [!] 下载超时")
        return None
    except Exception as e:
        print(f"  [!] 下载异常: {e}")
        return None


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


def main():
    parser = argparse.ArgumentParser(description="Z-Library 图书下载 (zlib CLI)")
    parser.add_argument("--keywords", default="热门图书", help="搜索关键词")
    parser.add_argument("--max-downloads", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--upload", action="store_true", default=True)
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",")] if args.keywords else ["热门图书"]
    max_downloads = args.max_downloads

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
    print(f"  Z-Library 图书下载 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  关键词: {', '.join(keywords)}")
    print(f"  最多: {max_downloads} 本")
    print(f"{'='*60}\n")

    # Step 1: 登录
    print("[1/3] 登录 Z-Library...")
    try:
        zlib_login(zlib_email, zlib_password)
    except Exception as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    # Step 2: 搜索并下载
    print(f"[2/3] 搜索并下载...")
    books_to_download = []

    for kw in keywords:
        if len(books_to_download) >= max_downloads:
            break
        books = zlib_search(kw)
        for b in books:
            bid = b.get("id", "")
            if bid and bid not in downloaded_ids:
                books_to_download.append(b)
                if len(books_to_download) >= max_downloads:
                    break

    if not books_to_download:
        print("[!] 没有可下载的新书")
        sys.exit(0)

    print(f"\n  将下载 {len(books_to_download)} 本...")
    date_str = datetime.now().strftime("%Y%m%d")
    dl_dir = DOWNLOAD_DIR / date_str
    dl_dir.mkdir(parents=True, exist_ok=True)

    for i, book in enumerate(books_to_download, 1):
        bid = book["id"]
        print(f"\n  [{i}/{len(books_to_download)}] ID: {bid}")

        result = zlib_download(bid, str(dl_dir))
        if not result:
            print(f"  [!] 下载失败，跳过")
            continue

        print(f"  [✓] 下载完成: {result['filename']} ({human_size(result['size'])})")
        total_size += result["size"]

        # 上传阿里云盘
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

        # 间隔
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
