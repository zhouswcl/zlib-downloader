#!/usr/bin/env python3
"""
阿里云盘上传模块
================
通过阿里云盘官方 API 直接上传。
从中国 IP 调用不会有 SSL 问题。

环境变量:
  ALIYUNDRIVE_REFRESH_TOKEN   阿里云盘 refresh_token
  ALIYUNDRIVE_FOLDER          上传文件夹名（默认 zlib-github-books）
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

ALIYUN_TOKEN_URL = "https://api.aliyundrive.com/v2/account/token"
ALIYUN_CREATE_URL = "https://api.aliyundrive.com/adrive/v2/file/create"
ALIYUN_LIST_URL = "https://api.aliyundrive.com/adrive/v2/file/list"
ALIYUN_GET_URL = "https://api.aliyundrive.com/adrive/v2/file/get_upload_url"
ALIYUN_COMPLETE_URL = "https://api.aliyundrive.com/adrive/v2/file/complete"


class AliyunError(Exception):
    pass


def _get_token(refresh_token: str) -> tuple[str, str]:
    """用 refresh_token 换 access_token 和 drive_id"""
    r = requests.post(ALIYUN_TOKEN_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=15)
    if r.status_code != 200:
        raise AliyunError(f"token 刷新失败: HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    at = data.get("access_token", "")
    did = data.get("default_drive_id", "")
    if not at or not did:
        raise AliyunError(f"token 响应缺少 access_token/drive_id")
    return at, did


def _find_or_create_folder(
    access_token: str, drive_id: str, folder_name: str, parent_id: str = "root"
) -> str:
    """查找或创建目标文件夹，返回 file_id"""
    headers = {"Authorization": f"Bearer {access_token}"}

    # 列目录
    r = requests.post(ALIYUN_LIST_URL, headers=headers, json={
        "drive_id": drive_id,
        "parent_file_id": parent_id,
        "limit": 100,
    }, timeout=15)
    if r.status_code != 200:
        raise AliyunError(f"列目录失败: HTTP {r.status_code}: {r.text[:200]}")

    items = r.json().get("items", [])
    for item in items:
        if item.get("type") == "folder" and item.get("name") == folder_name:
            return item["file_id"]

    # 创建文件夹
    r2 = requests.post(ALIYUN_CREATE_URL, headers=headers, json={
        "drive_id": drive_id,
        "name": folder_name,
        "parent_file_id": parent_id,
        "type": "folder",
        "check_name_mode": "auto_rename",
    }, timeout=15)
    if r2.status_code not in (200, 201):
        raise AliyunError(f"创建文件夹失败: HTTP {r2.status_code}: {r2.text[:200]}")
    fid = r2.json().get("file_id", "")
    if not fid:
        raise AliyunError("创建文件夹未返回 file_id")
    print(f"  已创建文件夹: {folder_name} -> {fid}")
    return fid


def _upload_file_part(
    access_token: str, drive_id: str, file_id: str,
    upload_id: str, part_number: int, data: bytes,
    upload_url: str, max_retries: int = 5,
) -> bool:
    """上传一个分片，支持 URL 过期刷新"""
    url = upload_url
    for attempt in range(max_retries):
        try:
            r = requests.put(url, data=data, timeout=600)
            if r.status_code in (200, 201, 204):
                return True

            # URL 过期（403 + AccessDenied）
            if r.status_code == 403:
                try:
                    j = r.json()
                    if "AccessDenied" in j.get("code", ""):
                        # 刷新 URL
                        hr = requests.post(ALIYUN_GET_URL, headers={
                            "Authorization": f"Bearer {access_token}",
                        }, json={
                            "drive_id": drive_id,
                            "file_id": file_id,
                            "upload_id": upload_id,
                            "part_info_list": [{"part_number": part_number}],
                        }, timeout=15)
                        if hr.status_code == 200:
                            parts = hr.json().get("part_info_list", [])
                            if parts:
                                url = parts[0].get("upload_url", url)
                                print(f"    part{part_number} URL 已刷新")
                                continue
                except json.JSONDecodeError:
                    pass

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            print(f"    part{part_number} 重试 {attempt+1}/{max_retries}: {type(e).__name__}")

        time.sleep(3)

    return False


def upload_to_aliyundrive(
    local_path: str,
    refresh_token: str,
    folder_name: str = "zlib-github-books",
) -> dict:
    """上传文件到阿里云盘

    Args:
        local_path: 本地文件路径
        refresh_token: 阿里云盘 refresh_token
        folder_name: 上传到的文件夹名

    Returns:
        {"success": True/False, "file_name": ..., "size": ..., "error": ...}
    """
    if not os.path.exists(local_path):
        return {"success": False, "error": f"文件不存在: {local_path}"}

    file_size = os.path.getsize(local_path)
    filename = os.path.basename(local_path)

    print(f"  文件: {filename} ({file_size} bytes)")

    # 1. 获取 token
    print(f"  获取 token...")
    try:
        access_token, drive_id = _get_token(refresh_token)
        print(f"  drive_id={drive_id}")
    except AliyunError as e:
        return {"success": False, "error": str(e)}

    headers = {"Authorization": f"Bearer {access_token}"}

    # 2. 查找/创建文件夹
    print(f"  准备上传目录...")
    try:
        folder_id = _find_or_create_folder(access_token, drive_id, folder_name)
        print(f"  目标文件夹: {folder_id}")
    except AliyunError as e:
        return {"success": False, "error": str(e)}

    # 3. 创建文件记录
    print(f"  创建文件记录...")
    r = requests.post(ALIYUN_CREATE_URL, headers=headers, json={
        "drive_id": drive_id,
        "name": filename,
        "parent_file_id": folder_id,
        "type": "file",
        "size": file_size,
        "check_name_mode": "auto_rename",
    }, timeout=15)

    if r.status_code not in (200, 201):
        return {"success": False, "error": f"创建文件记录失败: HTTP {r.status_code}: {r.text[:200]}"}

    cr = r.json()

    # 4. 秒传检测
    if cr.get("rapid_upload"):
        print(f"  [✓] 秒传成功!")
        return {"success": True, "file_name": filename, "size": file_size, "rapid_upload": True}

    # 5. 分片上传
    part_info_list = cr.get("part_info_list", [])
    file_id = cr.get("file_id", "")
    upload_id = cr.get("upload_id", "")

    if not part_info_list:
        return {"success": False, "error": "未返回分片信息"}

    print(f"  分片上传 ({len(part_info_list)} parts)...")

    with open(local_path, "rb") as f:
        file_data = f.read()

    # 简单分片：每片 1MB，或单文件直接整片
    chunk_size = 1024 * 1024  # 1MB
    parts = []
    for i in range(0, file_size, chunk_size):
        parts.append(file_data[i:i + chunk_size])

    for i, (part, part_info) in enumerate(zip(parts, part_info_list)):
        part_number = part_info["part_number"]
        upload_url = part_info["upload_url"]

        ok = _upload_file_part(
            access_token, drive_id, file_id, upload_id,
            part_number, part, upload_url,
        )
        if not ok:
            return {
                "success": False,
                "error": f"分片 {part_number}/{len(part_info_list)} 上传失败（已重试5次）",
            }

        if (i + 1) % 5 == 0 or i == len(parts) - 1:
            print(f"    {i+1}/{len(part_info_list)} parts 完成")

    # 6. 完成上传
    r2 = requests.post(ALIYUN_COMPLETE_URL, headers=headers, json={
        "drive_id": drive_id,
        "file_id": file_id,
        "upload_id": upload_id,
    }, timeout=15)

    if r2.status_code in (200, 201):
        print(f"  [✓] 上传完成!")
        return {"success": True, "file_name": filename, "size": file_size}
    else:
        return {"success": False, "error": f"完成上传失败: HTTP {r2.status_code}: {r2.text[:200]}"}
