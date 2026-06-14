"""
夸克网盘上传模块
================
通过夸克网盘官方 API 上传文件（Cookie 鉴权）。

使用方式:
   1. 浏览器登录 https://pan.quark.cn
   2. F12 → Application → Cookies → pan.quark.cn
   3. 复制 Cookie 值（或 key=value 格式）
   4. 设为环境变量 QUARK_COOKIE

API 参考: 基于 quark-auto-save 等开源项目逆向工程
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

QUARK_BASE = "https://drive.quark.cn/1/clouddrive"
QUARK_UPLOAD = "https://upload.quark.cn/1/clouddrive/file/upload"
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://pan.quark.cn/",
}


class QuarkError(Exception):
    pass


def _get_st(cookie: str) -> tuple[str, str]:
    """获取 st (security token) 和 puid"""
    r = requests.get(
        f"{QUARK_BASE}/config",
        headers={**COMMON_HEADERS, "Cookie": cookie},
        timeout=15,
    )
    if r.status_code != 200:
        raise QuarkError(f"get config failed: HTTP {r.status_code}")
    data = r.json()
    if data.get("status") != 200:
        raise QuarkError(f"get config failed: {data.get('message', r.text[:200])}")
    d = data.get("data", {})
    st = d.get("st", "")
    puid = d.get("puid", "")
    if not st:
        raise QuarkError("no st token in config response")
    return st, puid


def _find_or_create_folder(
    cookie: str, st: str, folder_name: str, parent_fid: str = "0"
) -> str:
    """查找或创建上传目录，返回 fid"""
    # 先查根目录
    r = requests.post(
        f"{QUARK_BASE}/file/sort",
        params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        headers={**COMMON_HEADERS, "Cookie": cookie},
        json={
            "pdir_fid": parent_fid,
            "page": 1,
            "size": 200,
            "sort_by": "file_name",
            "sort_order": "asc",
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise QuarkError(f"list files failed: HTTP {r.status_code}")
    data = r.json()
    if data.get("status") != 200:
        raise QuarkError(f"list files failed: {data.get('message', '?')}")

    # 查找已有文件夹
    for item in data.get("data", {}).get("list", []):
        if item.get("file_type") == 0 and item.get("file_name") == folder_name:
            return item["fid"]

    # 创建新文件夹
    r2 = requests.post(
        f"{QUARK_BASE}/file",
        params={"pr": "ucpro", "fr": "pc", "uc_param_str": "", "st": st},
        headers={**COMMON_HEADERS, "Cookie": cookie},
        json={
            "pdir_fid": parent_fid,
            "file_name": folder_name,
            "dir_path": "",
            "dir_init_lock": False,
        },
        timeout=15,
    )
    if r2.status_code not in (200, 201):
        raise QuarkError(f"create folder failed: HTTP {r2.status_code}")
    d2 = r2.json()
    if d2.get("status") != 200:
        raise QuarkError(f"create folder failed: {d2.get('message', '?')}")
    fid = d2.get("data", {}).get("fid", "")
    if not fid:
        raise QuarkError("create folder returned no fid")
    print(f"  已创建目录: {folder_name} -> {fid}")
    return fid


def upload_to_quark(
    local_path: str,
    cookie: str,
    parent_fid: str = "0",
    folder_name: str = "zlib-github-books",
) -> dict:
    """上传文件到夸克网盘

    Args:
        local_path: 本地文件路径
        cookie: 夸克网盘 Cookie 字符串
        parent_fid: 父目录 fid (默认 0=根目录)
        folder_name: 上传到的文件夹名（自动创建）

    Returns:
        {"success": True/False, "file_name": ..., "size": ..., "error": ...}
    """
    file_size = os.path.getsize(local_path)
    filename = os.path.basename(local_path)

    print(f"  文件: {filename} ({file_size} bytes)")

    # 1. 获取 st token
    print(f"  获取鉴权...")
    try:
        st, puid = _get_st(cookie)
    except QuarkError as e:
        return {"success": False, "error": str(e)}

    # 2. 查找/创建上传目录
    print(f"  准备上传目录...")
    try:
        target_fid = _find_or_create_folder(cookie, st, folder_name, parent_fid)
        print(f"  目标目录 fid: {target_fid}")
    except QuarkError as e:
        return {"success": False, "error": str(e)}

    # 3. 计算文件 MD5
    print(f"  计算 MD5...")
    with open(local_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    print(f"  MD5: {md5}")

    # 4. 发起上传
    print(f"  上传中...")
    ts = int(time.time() * 1000)
    upload_url = f"{QUARK_UPLOAD}?pr=ucpro&fr=pc&uc_param_str=&st={st}&pdir_fid={target_fid}&_={ts}"

    with open(local_path, "rb") as f:
        file_data = f.read()

    files = {
        "file": (filename, file_data, "application/octet-stream"),
    }
    data = {
        "pdir_fid": target_fid,
        "file_name": filename,
        "file_size": str(file_size),
        "file_type": "file",
        "md5": md5,
    }

    try:
        r = requests.post(
            upload_url,
            headers={**COMMON_HEADERS, "Cookie": cookie},
            files=files,
            data=data,
            timeout=300,
        )
    except requests.exceptions.Timeout:
        return {"success": False, "error": "upload request timed out (300s)"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"connection error: {e}"}

    if r.status_code not in (200, 201):
        return {
            "success": False,
            "error": f"upload failed: HTTP {r.status_code}: {r.text[:200]}",
        }

    resp = r.json()
    if resp.get("status") == 200:
        file_info = resp.get("data", {})
        size = file_info.get("file_size", file_size)
        print(f"  [✓] 上传完成: {resp.get('data', {}).get('file_name', filename)}")
        return {
            "success": True,
            "file_name": filename,
            "size": size,
            "quark_fid": file_info.get("fid", ""),
        }
    elif resp.get("status") == 40201:
        # 文件已存在（秒传）
        print(f"  [✓] 文件已存在（秒传）")
        return {"success": True, "file_name": filename, "size": file_size, "rapid_upload": True}
    else:
        return {
            "success": False,
            "error": f"upload failed: {resp.get('message', r.text[:200])}",
        }