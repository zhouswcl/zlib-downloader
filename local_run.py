#!/usr/bin/env python3
"""
Z-Library 每日下载器 - 服务器本地版
====================================
直接在腾讯云服务器上运行，下载后经中国 IP 上传阿里云盘。

用法:
  python local_run.py                                # 默认配置
  python local_run.py --keywords "python,rust"       # 指定关键词
  python local_run.py --max-downloads 5              # 指定下载数
  python local_run.py --skip-upload                  # 只下载不上传（调试）

环境变量:
  ALIYUNDRIVE_REFRESH_TOKEN     阿里云盘 refresh_token
  ZLIB_EMAIL                    Z-Library 邮箱
  ZLIB_PASSWORD                 Z-Library 密码
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import aliyundrive_upload
import zlib_client

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = ROOT / "downloads"
CONFIG_FILE = ROOT / "config.json"
HISTORY_FILE = DATA_DIR / "downloaded_ids.json"


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_history() -> set:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    return set()


def save_history(book_ids: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(book_ids), f)


def select_keywords(args_keywords: str, config: dict) -> list[str]:
    if args_keywords:
        return [k.strip() for k in args_keywords.split(",")]

    keywords = config.get("keywords", [])
    if not keywords:
        return []

    day_of_year = datetime.now().timetuple().tm_yday
    idx = day_of_year % len(keywords)
    return [keywords[idx]]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    parser = argparse.ArgumentParser(description="Z-Library 每日下载器 (服务器版)")
    parser.add_argument("--keywords", help="搜索关键词 (逗号分隔)")
    parser.add_argument("--max-downloads", type=int, default=10, help="最多下载本数")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--skip-upload", action="store_true", help="跳过上传（调试用）")
    args = parser.parse_args()

    config = load_config()
    keywords = select_keywords(args.keywords, config)
    max_downloads = min(args.max_downloads, config.get("max_daily", 10))

    upload_enabled = not args.skip_upload

    # 检查环境变量
    zlib_email = os.environ.get("ZLIB_EMAIL", "")
    zlib_password = os.environ.get("ZLIB_PASSWORD", "")
    refresh_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")

    if not zlib_email or not zlib_password:
        # 尝试从 config 轮换账号
        accounts = config.get("accounts", [])
        if accounts:
            day_of_year = datetime.now().timetuple().tm_yday
            idx = day_of_year % len(accounts)
            zlib_email = accounts[idx]
            print(f"  轮换账号: {zlib_email[:10]}*** (第 {idx+1}/{len(accounts)} 个)")

    if not zlib_email or not zlib_password:
        print("ERROR: ZLIB_EMAIL and ZLIB_PASSWORD must be set")
        sys.exit(1)

    if upload_enabled and not refresh_token:
        print("ERROR: ALIYUNDRIVE_REFRESH_TOKEN must be set for upload")
        sys.exit(1)

    downloaded_ids = load_history()
    results = []
    total_size = 0
    success_count = 0

    print(f"\n{'='*60}")
    print(f"  Z-Library 每日下载 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  关键词: {', '.join(keywords)}")
    print(f"  最多: {max_downloads} 本")
    print(f"  上传: {'是' if upload_enabled else '否'}")
    print(f"{'='*60}\n")

    # Step 1: 登录
    print("[1/4] 登录 Z-Library...")
    print("  登录中...")
    try:
        zlib_client.login(zlib_email, zlib_password)
        print("  [✓] 登录成功")
        session = zlib_client.load_session()
        print(f"  域: {session.get('domain', '?')}")
    except Exception as e:
        print(f"  [FAIL] 登录失败: {e}")
        sys.exit(1)

    # Step 2: 查询限额
    print("[2/4] 查询下载限额...")
    try:
        limit = zlib_client.get_daily_limit()
        used = limit.get("used", 0)
        total = limit.get("total", 10)
        remaining = limit.get("remaining", 10)
        print(f"  今日已下载: {used}/{total} | 剩余: {remaining}")
        max_downloads = min(max_downloads, remaining)
    except Exception as e:
        print(f"  [!] 查询限额失败: {e}")
        print(f"  假设剩余: {max_downloads}")

    if max_downloads <= 0:
        print("[!] 今日额度已用完")
        sys.exit(0)

    # Step 3: 搜索并下载
    print(f"[3/4] 搜索并下载 (最多 {max_downloads} 本)...")
    books_collected = []

    for kw in keywords:
        if len(books_collected) >= max_downloads:
            break
        print(f"\n  搜索: '{kw}'...")
        try:
            books = zlib_client.search(kw, page=1, count=max_downloads * 2)
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
        print("\n[!] 没有可下载的新书")
        sys.exit(0)

    print(f"\n  将下载 {len(books_collected)} 本...")

    date_str = datetime.now().strftime("%Y%m%d")
    dl_dir = DOWNLOAD_DIR / date_str
    dl_dir.mkdir(parents=True, exist_ok=True)

    for i, book in enumerate(books_collected, 1):
        bid = book.get("id", "")
        title = book.get("title", "未知")[:50]
        author = (book.get("author") or "未知")[:20]
        ext = book.get("extension", "pdf")
        print(f"\n  [{i}/{len(books_collected)}] {title}")
        print(f"        作者: {author} | 格式: {ext} | ID: {bid}")

        try:
            result = zlib_client.download(bid, str(dl_dir))
        except Exception as e:
            print(f"  [!] 下载异常: {e}")
            continue

        if result:
            print(f"  [✓] 下载完成: {result['filename']} ({human_size(result['size'])})")
            total_size += result["size"]

            upload_ok = False
            if upload_enabled:
                print(f"  上传阿里云盘...")
                upload_result = aliyundrive_upload.upload_to_aliyundrive(
                    result["filepath"], refresh_token,
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

            if upload_ok:
                downloaded_ids.add(bid)
                save_history(downloaded_ids)

            # 删除本地文件（上传成功或跳过了上传）
            if upload_ok and os.path.exists(result["filepath"]):
                os.remove(result["filepath"])
                print(f"  [x] 已删除本地文件")
        else:
            print(f"  [!] 下载失败，跳过")

        if i < len(books_collected):
            delay = random.uniform(3, 8)
            print(f"  等待 {delay:.0f}s...")
            time.sleep(delay)

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
                "title": r.get("filename", ""),
                "filename": r.get("filename", ""),
                "size": r.get("size", 0),
                "upload": r.get("upload", {}).get("success", False) if upload_enabled else True,
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
        if upload_enabled:
            up_count = sum(1 for r in results if r.get("upload", {}).get("success"))
            print(f"  上传成功: {up_count}")
        print(f"{'='*60}")

    save_history(downloaded_ids)
    sys.exit(0 if success_count > 0 else 1)


if __name__ == "__main__":
    main()
