#!/usr/bin/env python3
"""
夸克网盘中转客户端
===================
从 GitHub Actions 调用，将文件 POST 到腾讯云中转服务器上传夸克网盘。

环境变量:
  RELAY_URL     中转服务器地址 (如 http://81.70.194.76:8099)
  RELAY_TOKEN   鉴权 Token
"""
import json
import os

import requests


def upload_via_relay(local_path: str) -> dict:
    """通过中转服务器上传文件到夸克

    Args:
        local_path: 本地文件路径

    Returns:
        {"success": True/False, "file_name": ..., "size": ..., "error": ...}
    """
    relay_url = os.environ.get("RELAY_URL", "").rstrip("/")
    token = os.environ.get("RELAY_TOKEN", "")

    if not relay_url:
        return {"success": False, "error": "RELAY_URL not set"}
    if not token:
        return {"success": False, "error": "RELAY_TOKEN not set"}
    if not os.path.exists(local_path):
        return {"success": False, "error": f"file not found: {local_path}"}

    upload_url = f"{relay_url}/upload"
    filename = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)

    print(f"  通过中转上传: {filename} ({file_size} bytes)")
    print(f"  目标: {upload_url}")

    try:
        with open(local_path, "rb") as f:
            files = {"file": (filename, f, "application/octet-stream")}
            r = requests.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                timeout=600,  # 10 min for upload
            )
    except requests.exceptions.Timeout:
        return {"success": False, "error": "relay upload timed out (600s)"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"cannot connect to relay: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    try:
        result = r.json()
    except Exception:
        return {
            "success": False,
            "error": f"relay returned HTTP {r.status_code}: {r.text[:200]}",
        }

    if r.status_code == 401:
        return {"success": False, "error": "relay rejected: invalid token"}
    if r.status_code != 200:
        return {
            "success": False,
            "error": f"relay returned HTTP {r.status_code}: {result.get('error', r.text[:200])}",
        }

    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error", "relay upload failed"),
        }

    print(f"  [✓] 中转上传成功: {result.get('file_name', filename)}")
    if result.get("rapid_upload"):
        print(f"  [i] 秒传")
    return {
        "success": True,
        "file_name": result.get("file_name", filename),
        "size": result.get("size", file_size),
        "rapid_upload": result.get("rapid_upload", False),
    }
