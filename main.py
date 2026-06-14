#!/usr/bin/env python3
"""
Z-Library 每日图书下载器
========================
完全通过 zlib (Go) CLI 操作，避免 Cloudflare 反爬。

工作流: 登录 -> 搜索 -> 下载 -> 上传阿里云盘

环境变量:
  ZLIB_EMAIL                    Z-Library 邮箱
  ZLIB_PASSWORD                 Z-Library 密码
  ALIYUNDRIVE_REFRESH_TOKEN     阿里云盘 refresh_token
  ALIYUNDRIVE_PARENT_ID         阿里云盘上传目录 ID (默认 root)
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

import zlib_client

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

    # 每天轮换一个关键词
    day_of_year = datetime.now().timetuple().tm_yday
    idx = day_of_year % len(keywords)
    return [keywords[idx]]


def upload_to_aliyundrive(
    local_path: str,
    refresh_token: str,
    parent_id: str = "",
) -> dict:
    """使用 aliyunpan CLI 上传文件到阿里云盘"""
    import os, subprocess, tempfile
    file_size = os.path.getsize(local_path)
    filename = os.path.basename(local_path)

    # 检查 aliyunpan 是否已安装
    try:
        subprocess.run(["aliyunpan", "version"], capture_output=True, timeout=5)
        has_aliyunpan = True
    except (FileNotFoundError, Exception):
        has_aliyunpan = False

    if not has_aliyunpan:
        print("  正在安装 aliyunpan CLI...")
        r = subprocess.run(
            ["sudo", "bash", "-c", "wget -q -O /tmp/aliyunpan.zip https://github.com/tickstep/aliyunpan/releases/download/v0.3.7/aliyunpan-v0.3.7-linux-amd64.zip && cd /tmp && unzip -q aliyunpan.zip && sudo cp aliyunpan-v0.3.7-linux-amd64/aliyunpan /usr/local/bin/ && rm -rf /tmp/aliyunpan* 2>/dev/null; which aliyunpan"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return {"success": False, "error": f"aliyunpan 安装失败: {r.stderr.strip()[:200]}"}
        print("  aliyunpan 安装完成")

    # 登录阿里云盘
    r = subprocess.run(
        ["aliyunpan", "login", "-refreshToken", refresh_token],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return {"success": False, "error": f"aliyunpan 登录失败: {r.stderr.strip()[:200]}"}

    # 上传到 /zlib_books/ 目录
    result = subprocess.run(
        ["aliyunpan", "upload", local_path, parent_id],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        return {"success": False, "error": f"aliyunpan 上传失败: {result.stderr.strip()[:200]}"}

    return {"success": True, "file_name": filename, "size": file_size}


def human_size(n: int) -> str:
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
    parser.add_argument("--skip-upload", action="store_true", help="跳过上传，只下载（调试用）")
    args = parser.parse_args()

    config = load_config()
    keywords = select_keywords(args.keywords, config)
    max_downloads = min(args.max_downloads, config.get("max_daily", 10))

    upload_enabled = args.upload and not args.skip_upload

    # 环境变量
    zlib_email = os.environ.get("ZLIB_EMAIL", "")
    zlib_password = os.environ.get("ZLIB_PASSWORD", "")
    aliyun_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
    aliyun_parent = os.environ.get("ALIYUNDRIVE_PARENT_ID") or "zlib-github-books"

    if not zlib_email or not zlib_password:
        print("ERROR: ZLIB_EMAIL and ZLIB_PASSWORD must be set")
        sys.exit(1)
    if upload_enabled and not aliyun_token:
        print("ERROR: ALIYUNDRIVE_REFRESH_TOKEN must be set for upload")
        sys.exit(1)

    # 已下载历史
    downloaded_ids = load_history()
    results = []
    total_size = 0
    success_count = 0

    print(f"\n{'='*60}")
    print(f"  Z-Library 图书下载 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
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

    # Step 2: 查询今日限额
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
        print(f"        作者: {author} | 格式: {ext} | ID: {bid}")

        try:
            result = zlib_client.download(bid, str(dl_dir))
        except Exception as e:
            print(f"  [!] 下载异常: {e}")
            continue

        if result:
            print(f"  [✓] 下载完成: {result['filename']} ({human_size(result['size'])})")
            total_size += result["size"]

            # 上传阿里云盘
            upload_ok = False
            if upload_enabled:
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

            # 只有下载+上传都成功才记入历史
            if upload_ok:
                downloaded_ids.add(bid)
                save_history(downloaded_ids)
        else:
            print(f"  [!] 下载失败，跳过")

        # 下载间隔
        if i < len(books_collected):
            delay = random.uniform(3, 8)
            print(f"  等待 {delay:.0f}s...")
            time.sleep(delay)

    # 清理临时文件（只删除上传成功的文件）
    if upload_enabled and results:
        print(f"\n[4/4] 清理临时文件...")
        cleaned = 0
        for r in results:
            upload_ok = r.get("upload", {}).get("success", False)
            fp = r.get("filepath", "")
            if upload_ok and fp and os.path.exists(fp):
                os.remove(fp)
                print(f"  [x] 删除: {r['filename']}")
                cleaned += 1
        if cleaned == 0:
            print(f"  无文件可清理（上传均失败，文件保留在 {dl_dir}）")
    elif not upload_enabled:
        print(f"\n[4/4] 跳过清理 (--skip-upload，文件保存在 {dl_dir})")
    else:
        print(f"\n[4/4] 跳过清理（无下载文件）")

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
            print(f"  上传成功: {sum(1 for r in results if r.get('upload',{}).get('success'))}")
        print(f"{'='*60}")

    save_history(downloaded_ids)
    sys.exit(0 if success_count > 0 else 1)


if __name__ == "__main__":
    main()
