#!/usr/bin/env python3
"""
Z-Library 每日图书下载器
========================
完全通过 zlib (Go) CLI 操作，避免 Cloudflare 反爬。

工作流: 登录 -> 搜索 -> 下载 -> 上传夸克网盘（通过腾讯云中转）

环境变量:
  ZLIB_EMAIL                    Z-Library 邮箱
  ZLIB_PASSWORD                 Z-Library 密码
  RELAY_URL                     中转服务器地址 (如 http://81.70.194.76:8099)
  RELAY_TOKEN                   中转服务器鉴权 Token
"""
import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import quark_relay_client
import zlib_client

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = ROOT / "downloads"
CONFIG_FILE = ROOT / "config.json"
HISTORY_FILE = DATA_DIR / "downloaded_ids.json"


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_history() -> tuple[set, set]:
    """加载已下载记录，返回 (已下载的ID集合, 已下载的规范化书名集合)"""
    ids = set()
    titles = set()
    if HISTORY_FILE.exists():
        try:
            data = json.load(open(HISTORY_FILE))
            if isinstance(data, list):
                ids = set(data)
            elif isinstance(data, dict):
                ids = set(data.get("ids", []))
                titles = set(data.get("titles", []))
        except (json.JSONDecodeError, TypeError):
            pass
    return ids, titles


def save_history(book_ids: set, book_titles: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump({"ids": list(book_ids), "titles": list(book_titles)}, f)


def select_keywords(args_keywords: str, config: dict) -> list[str]:
    if args_keywords:
        return [k.strip() for k in args_keywords.split(",")]

    keywords = config.get("keywords", [])
    if not keywords:
        return []

    day_of_year = datetime.now().timetuple().tm_yday
    idx = day_of_year % len(keywords)
    return [keywords[idx]]


def upload_file(local_path: str, file_size: int) -> dict:
    """通过腾讯云中转服务器上传到夸克网盘"""
    return quark_relay_client.upload_via_relay(local_path)


def _check_upload_config() -> bool:
    url = os.environ.get("RELAY_URL", "")
    token = os.environ.get("RELAY_TOKEN", "")
    if not url:
        print("ERROR: RELAY_URL must be set for upload")
        return False
    if not token:
        print("ERROR: RELAY_TOKEN must be set for upload")
        return False
    return True


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


_FORMAT_PRIORITY = {"epub": 0, "mobi": 1, "pdf": 2, "azw3": 3, "txt": 4, "docx": 5, "html": 6}


def _normalize_title(title: str) -> str:
    """规范化书名，去掉副标题、作者名后缀、特殊符号，用于格式去重对比"""
    t = title.strip()
    # 去掉括号及其内容（副标题、作者注解等）
    t = re.sub(r'[（(][^）)]*[）)]', '', t)
    # 去掉 "= " 及后面的英文副标题
    t = re.split(r'\s*=\s*', t)[0]
    # 去掉 "——" 及后面的副标题
    t = re.split(r'[—–]', t)[0]
    # 去掉常见后缀
    for suffix in ["(套装)", "(全本)", "(全册)", "(上册)", "(下册)", "（套装）", "（全本）"]:
        t = t.replace(suffix, "")
    # 去掉首尾空格和多余空格
    t = re.sub(r'\s+', '', t)
    return t.strip().lower()


def _dedup_by_format(books: list[dict]) -> list[dict]:
    """同一本书只保留一种格式，优先 EPUB → MOBI → 其他"""
    groups: dict[str, list[dict]] = {}
    for b in books:
        key = _normalize_title(b.get("title", ""))
        if key:
            groups.setdefault(key, []).append(b)

    result = []
    for key, versions in groups.items():
        # 按格式优先级排序
        versions.sort(key=lambda v: _FORMAT_PRIORITY.get(v.get("extension", ""), 99))
        chosen = versions[0]
        if len(versions) > 1:
            kept = chosen.get("extension", "?").upper()
            skipped = ", ".join(f"{v.get('extension','?').upper()}({v.get('id','')})" for v in versions[1:])
            print(f"  [i] 去重: \"{chosen.get('title','')[:40]}...\" 保留{kept}，跳过{skipped}")
        result.append(chosen)

    return result


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
    max_downloads = min(args.max_downloads, config.get("max_daily", 50))

    upload_enabled = args.upload and not args.skip_upload

    # 收集所有可用的 Z-Library 账号
    zlib_password = os.environ.get("ZLIB_PASSWORD", "")
    if not zlib_password:
        print("ERROR: ZLIB_PASSWORD must be set")
        sys.exit(1)

    env_email = os.environ.get("ZLIB_EMAIL", "")
    all_accounts = list(config.get("accounts", []))
    if env_email and env_email not in all_accounts:
        all_accounts.insert(0, env_email)

    if not all_accounts:
        print("ERROR: No Z-Library accounts configured (ZLIB_EMAIL or config.accounts)")
        sys.exit(1)

    print(f"  Z-Library 账号数: {len(all_accounts)}")

    upload_ready = False
    if upload_enabled:
        if not _check_upload_config():
            upload_enabled = False
        else:
            upload_ready = True

    downloaded_ids, downloaded_titles = load_history()
    results = []
    total_size = 0
    success_count = 0

    date_str = datetime.now().strftime("%Y%m%d")
    dl_dir = DOWNLOAD_DIR / date_str
    dl_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Z-Library 图书下载 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  关键词: {', '.join(keywords)}")
    print(f"  最多: {max_downloads} 本")
    print(f"  上传: {'是' if upload_enabled else '否'}")
    print(f"{'='*60}\n")


    # ── 辅助函数 ──

    def _login_account(acct_idx: int) -> bool:
        """登录指定账号，返回是否成功"""
        email = all_accounts[acct_idx]
        print(f"  登录账号 [{acct_idx+1}/{len(all_accounts)}]: {email[:10]}***")
        try:
            zlib_client.login(email, zlib_password)
            print(f"  [✓] 登录成功 (域: {(zlib_client.load_session() or {}).get('domain','?')})")
            return True
        except Exception as e:
            print(f"  [FAIL] 登录失败: {e}")
            return False

    def _get_remaining(acct_idx: int) -> int:
        """查询当前账号的剩余下载额度"""
        try:
            limit = zlib_client.get_daily_limit()
            remaining = limit.get("remaining", 0)
            used = limit.get("used", 0)
            total = limit.get("total", 10)
            print(f"  账号 [{acct_idx+1}/{len(all_accounts)}]: 今日 {used}/{total}，剩余 {remaining}")
            return remaining
        except Exception as e:
            print(f"  [!] 查询限额失败: {e}")
            return -1  # 未知，假设有额度


    # ── 第1步：搜索图书 ──

    print("[1/4] 登录 Z-Library 并搜索图书...")
    current_account_idx = 0
    if not _login_account(current_account_idx):
        sys.exit(1)

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
            title_key = _normalize_title(b.get("title", ""))
            if not bid or bid in downloaded_ids or title_key in downloaded_titles:
                continue
            books_collected.append(b)
            if len(books_collected) >= max_downloads:
                break

    if not books_collected:
        print("\n[!] 没有可下载的新书")
        sys.exit(0)

    # 格式去重
    books_collected = _dedup_by_format(books_collected)

    print(f"\n  将下载 {len(books_collected)} 本（去重后）...")


    # ── 第2步：检查账号额度 ──

    print("\n[2/4] 检查账号额度...")
    remaining = _get_remaining(current_account_idx)
    if remaining == 0:
        print("  [!] 当前账号已用完额度，尝试切换...")
        # 换个账号
        for i in range(1, len(all_accounts)):
            next_idx = (current_account_idx + i) % len(all_accounts)
            print(f"\n  [>>] 尝试账号 {next_idx+1}/{len(all_accounts)}...")
            if _login_account(next_idx):
                current_account_idx = next_idx
                remaining = _get_remaining(current_account_idx)
                if remaining > 0:
                    break
                elif remaining == 0:
                    continue
        if remaining == 0:
            print("  [!] 所有账号额度已用完")
            sys.exit(0)
    elif remaining < 0:
        remaining = max_downloads  # 未知，按最大值算


    # ── 第3步：下载并上传 ──

    print(f"\n[3/4] 下载并上传 ({len(books_collected)} 本)...")

    for i, book in enumerate(books_collected, 1):
        bid = book.get("id", "")
        title = book.get("title", "未知")[:50]
        author = (book.get("author") or "未知")[:20]
        ext = book.get("extension", "pdf")
        fmt_upper = ext.upper()
        print(f"\n  [{i}/{len(books_collected)}] {title}")
        print(f"        作者: {author} | 格式: {fmt_upper} | ID: {bid}")

        # 检查当前账号是否有额度，无则切换
        remaining = _get_remaining(current_account_idx)
        if remaining == 0:
            # 找下一个有额度的账号
            switched = False
            for j in range(1, len(all_accounts)):
                next_idx = (current_account_idx + j) % len(all_accounts)
                if next_idx == current_account_idx:
                    break
                print(f"  [>>] 当前账号无额度，尝试账号 {next_idx+1}/{len(all_accounts)}...")
                if _login_account(next_idx):
                    current_account_idx = next_idx
                    r = _get_remaining(current_account_idx)
                    if r > 0:
                        switched = True
                        break
            if not switched:
                print(f"  [!] 所有账号额度已用完，停止下载")
                break

        # 下载
        try:
            result = zlib_client.download(bid, str(dl_dir))
        except Exception as e:
            print(f"  [!] 下载异常: {e}")
            continue

        if result:
            print(f"  [✓] 下载完成: {result['filename']} ({human_size(result['size'])})")
            total_size += result["size"]

            upload_ok = False
            if upload_ready:
                print(f"  上传夸克网盘...")
                upload_result = upload_file(result["filepath"], result["size"])
                if upload_result.get("success"):
                    print(f"  [✓] 上传成功")
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
                downloaded_titles.add(_normalize_title(book.get("title", "")))
                save_history(downloaded_ids, downloaded_titles)
        else:
            print(f"  [!] 下载失败，跳过")

        if i < len(books_collected):
            delay = random.uniform(3, 8)
            print(f"  等待 {delay:.0f}s...")
            time.sleep(delay)

    # 清理临时文件
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

    save_history(downloaded_ids, downloaded_titles)
    sys.exit(0 if success_count > 0 else 1)


if __name__ == "__main__":
    main()
