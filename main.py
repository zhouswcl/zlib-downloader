#!/usr/bin/env python3
"""
Z-Library 每日图书下载器
========================
工作流程: 搜索 -> 下载 -> 上传阿里云盘

两种模式:
  1. 自动模式: 按 config.json 的 keywords 轮换搜索
  2. 手动模式: python main.py --keywords "机器学习,深度学习"

环境变量:
  ZLIB_EMAIL            Z-Library 邮箱
  ZLIB_PASSWORD         Z-Library 密码
  ALIYUNDRIVE_REFRESH_TOKEN  阿里云盘 refresh_token
  ALIYUNDRIVE_PARENT_ID      阿里云盘上传目录 ID (默认 root)
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# 项目根目录
ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = ROOT / "downloads"
CONFIG_FILE = ROOT / "config.json"
HISTORY_FILE = DATA_DIR / "downloaded_ids.json"

# Aliyun Drive API
ALIYUN_TOKEN_URL = "https://api.aliyundrive.com/v2/account/token"
ALIYUN_CREATE_URL = "https://api.aliyundrive.com/v2/file/create"
ALIYUN_COMPLETE_URL = "https://api.aliyundrive.com/v2/file/complete"

# 只在 GitHub Actions Runner 上能访问 Z-Library，
# 所以这里只做文件系统操作和上传；登录/搜索/下载由 Runner 的海外网络完成。
# 但在 GitHub Actions 中运行的是同一个脚本，不需要区分。


def load_config() -> dict:
    """加载配置文件"""
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_history() -> set:
    """加载已下载的图书 ID"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    return set()


def save_history(book_ids: set):
    """保存已下载的图书 ID"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(book_ids), f)


def select_keywords(args_keywords: str, config: dict) -> list[str]:
    """选择搜索关键词"""
    if args_keywords:
        return [k.strip() for k in args_keywords.split(",")]

    keywords = config.get("keywords", [])
    if not keywords:
        return []

    # 轮换策略: 每天取一个关键词
    day_of_year = datetime.now().timetuple().tm_yday
    idx = day_of_year % len(keywords)
    return [keywords[idx]]


def upload_to_aliyundrive(
    local_path: str,
    refresh_token: str,
    parent_id: str = "root",
) -> dict:
    """上传文件到阿里云盘"""
    # Step 1: refresh_token -> access_token
    resp = requests.post(ALIYUN_TOKEN_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    token_data = resp.json()
    access_token = token_data["access_token"]
    drive_id = token_data["default_drive_id"]

    headers = {"Authorization": f"Bearer {access_token}"}
    file_size = os.path.getsize(local_path)
    filename = os.path.basename(local_path)

    # Step 2: 创建文件记录，获取上传 URL
    create_resp = requests.post(ALIYUN_CREATE_URL, headers=headers, json={
        "drive_id": drive_id,
        "name": filename,
        "parent_file_id": parent_id,
        "type": "file",
        "size": file_size,
        "check_name_mode": "auto_rename",
    })
    create_resp.raise_for_status()
    create_data = create_resp.json()

    if create_data.get("rapid_upload"):
        # 秒传成功
        return {
            "success": True,
            "file_name": filename,
            "file_id": create_data.get("file_id", ""),
            "rapid_upload": True,
        }

    # Step 3: 上传文件内容
    upload_url = create_data.get("part_info_list", [{}])[0].get("upload_url", "")
    if not upload_url:
        upload_url = create_data.get("upload_url", "")
    if not upload_url:
        return {"success": False, "error": "无上传 URL"}

    with open(local_path, "rb") as f:
        upload_resp = requests.put(upload_url, data=f)
        if upload_resp.status_code not in (200, 201):
            return {
                "success": False,
                "error": f"上传失败: HTTP {upload_resp.status_code}",
            }

    # Step 4: 完成上传
    file_id = create_data.get("file_id", "")
    if file_id:
        requests.post(ALIYUN_COMPLETE_URL, headers=headers, json={
            "drive_id": drive_id,
            "file_id": file_id,
        })

    return {
        "success": True,
        "file_name": filename,
        "file_id": file_id,
        "size": file_size,
    }


def human_size(n: int) -> str:
    """可读的文件大小"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    parser = argparse.ArgumentParser(description="Z-Library 每日图书下载器")
    parser.add_argument("--keywords", help="搜索关键词 (逗号分隔，不传则从 config 轮换)")
    parser.add_argument("--max-downloads", type=int, default=10, help="最多下载本数")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--upload", action="store_true", default=True, help="上传阿里云盘")
    args = parser.parse_args()

    config = load_config()
    keywords = select_keywords(args.keywords, config)
    max_downloads = min(args.max_downloads, config.get("max_daily", 10))

    # 环境变量
    zlib_email = os.environ.get("ZLIB_EMAIL", "")
    zlib_password = os.environ.get("ZLIB_PASSWORD", "")
    aliyun_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
    aliyun_parent = os.environ.get("ALIYUNDRIVE_PARENT_ID", "root")

    if not zlib_email or not zlib_password:
        print("ERROR: ZLIB_EMAIL and ZLIB_PASSWORD must be set")
        sys.exit(1)
    if not aliyun_token:
        print("ERROR: ALIYUNDRIVE_REFRESH_TOKEN must be set")
        sys.exit(1)

    # 初始化 Z-Library 客户端
    from zlib_client import ZLibraryClient
    zlib = ZLibraryClient(zlib_email, zlib_password)

    # 已下载历史
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
    print("[1/4] 登录 Z-Library...")
    try:
        zlib.login()
    except Exception as e:
        print(f"[FAIL] 登录失败: {e}")
        sys.exit(1)

    # Step 2: 查询今日限额
    print("[2/4] 查询下载限额...")
    limit = zlib.get_daily_limit()
    print(f"  今日已下载: {limit['used']}/{limit['total']} | 剩余: {limit['remaining']}")
    max_downloads = min(max_downloads, limit["remaining"])
    if max_downloads <= 0:
        print("[!] 今日额度已用完")
        sys.exit(0)

    # Step 3: 搜索并下载
    print(f"[3/4] 搜索并下载 (最多 {max_downloads} 本)...")
    books_collected = []

    for kw in keywords:
        if len(books_collected) >= max_downloads:
            break
        print(f"\n  搜索: '{kw}'")
        try:
            books = zlib.search(kw, page=1, count=max_downloads * 2)
        except Exception as e:
            print(f"  [!] 搜索失败: {e}")
            continue

        if not books:
            print(f"  无结果")
            continue

        print(f"  找到 {len(books)} 本")
        for b in books:
            bid = b.get("id", "")
            if not bid or bid in downloaded_ids:
                continue
            books_collected.append(b)
            if len(books_collected) >= max_downloads:
                break

    if not books_collected:
        print("[!] 没有可下载的新书")
        sys.exit(0)

    print(f"\n  将下载 {len(books_collected)} 本...")

    # 下载目录
    date_str = datetime.now().strftime("%Y%m%d")
    dl_dir = DOWNLOAD_DIR / date_str
    dl_dir.mkdir(parents=True, exist_ok=True)

    for i, book in enumerate(books_collected, 1):
        bid = book.get("id", "")
        title = book.get("title", "未知")[:50]
        author = (book.get("author") or "未知")[:20]
        ext = book.get("extension", "pdf")
        print(f"\n  [{i}/{len(books_collected)}] {title}")
        print(f"        作者: {author} | 格式: {ext} | 大小: {book.get('size', '?')}")

        result = zlib.download(bid, str(dl_dir), book.get("url", ""))
        if result:
            print(f"  [✓] 下载完成: {result['filename']} ({human_size(result['size'])})")
            total_size += result["size"]

            # Step 4: 上传阿里云盘
            upload_ok = False
            if args.upload:
                print(f"  上传阿里云盘...")
                upload_result = upload_to_aliyundrive(
                    result["filepath"], aliyun_token, aliyun_parent
                )
                if upload_result.get("success"):
                    rapid = " (秒传)" if upload_result.get("rapid_upload") else ""
                    print(f"  [✓] 上传成功{rapid}")
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

            # 只有下载+上传都成功才记入历史，失败的下次重试
            if upload_ok:
                downloaded_ids.add(bid)
                save_history(downloaded_ids)
        else:
            print(f"  [!] 下载失败，跳过")

        # 下载间隔，避免限流
        if i < len(books_collected):
            delay = random.uniform(3, 8)
            print(f"  等待 {delay:.0f}s...")
            time.sleep(delay)

    # Step 4: 清理临时文件
    print(f"\n[4/4] 清理临时文件...")
    for r in results:
        fp = r.get("filepath", "")
        if fp and os.path.exists(fp):
            os.remove(fp)
            print(f"  [x] 删除: {r['filename']}")

    # 输出结果
    summary = {
        "date": date_str,
        "keywords": keywords,
        "total": len(books_collected),
        "success": success_count,
        "fail": len(books_collected) - success_count,
        "total_size": total_size,
        "human_size": human_size(total_size),
        "books": [
            {
                "title": r.get("book", {}).get("title", r.get("filename", "")),
                "filename": r.get("filename", ""),
                "size": r.get("size", 0),
                "upload": r.get("upload", {}).get("success", False),
            }
            for r in results
        ],
    }

    if args.json:
        print(f"\n{json.dumps(summary, ensure_ascii=False, indent=2)}")
    else:
        print(f"\n{'='*60}")
        print(f"  下载完成: {summary['success']}/{summary['total']}")
        print(f"  总大小: {summary['human_size']}")
        print(f"  上传成功: {sum(1 for r in results if r.get('upload',{}).get('success'))}")
        print(f"{'='*60}")

    save_history(downloaded_ids)
    sys.exit(0 if success_count > 0 else 1)


if __name__ == "__main__":
    main()
