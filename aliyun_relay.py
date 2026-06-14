#!/usr/bin/env python3
"""
阿里云盘中转服务器
===================
接收 GitHub Actions 上传的文件，通过腾讯云中国 IP 转存到阿里云盘。
阿里云盘用 refresh_token 鉴权，不受 IP 限制。

API:
  POST /upload  上传文件（multipart）
       Header: Authorization: Bearer <token>
       Body:  multipart/form-data, field "file" = 文件内容
       Returns: {"success": true/false, ...}

  GET /health   健康检查

环境变量:
  ALIYUNDRIVE_REFRESH_TOKEN   阿里云盘 refresh_token
  RELAY_TOKEN                 请求鉴权 Token（Authorization Bearer）
  RELAY_PORT                  监听端口（默认 8099）
"""
import hashlib
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

import requests

ALIYUN_TOKEN_URL = "https://api.aliyundrive.com/v2/account/token"
ALIYUN_CREATE_URL = "https://api.aliyundrive.com/adrive/v2/file/create"
ALIYUN_GET_URL = "https://api.aliyundrive.com/adrive/v2/file/get_upload_url"
ALIYUN_COMPLETE_URL = "https://api.aliyundrive.com/adrive/v2/file/complete"
ALIYUN_LIST_URL = "https://api.aliyundrive.com/adrive/v2/file/list"

RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "zlib-relay-key-2026")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/aliyun-relay"))
os.makedirs(UPLOAD_DIR, exist_ok=True)


class AliyunRelayError(Exception):
    pass


def _get_token(refresh_token: str) -> tuple[str, str]:
    r = requests.post(ALIYUN_TOKEN_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=15)
    if r.status_code != 200:
        raise AliyunRelayError(f"token 刷新失败: HTTP {r.status_code}")
    data = r.json()
    return data.get("access_token", ""), data.get("default_drive_id", "")


def _find_folder(access_token: str, drive_id: str, folder_name: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.post(ALIYUN_LIST_URL, headers=headers, json={
        "drive_id": drive_id, "parent_file_id": "root", "limit": 100,
    }, timeout=15)
    if r.status_code != 200:
        raise AliyunRelayError(f"列目录失败: HTTP {r.status_code}")
    for item in r.json().get("items", []):
        if item.get("type") == "folder" and item.get("name") == folder_name:
            return item["file_id"]
    return None


def _create_folder(access_token: str, drive_id: str, folder_name: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.post(ALIYUN_CREATE_URL, headers=headers, json={
        "drive_id": drive_id, "name": folder_name,
        "parent_file_id": "root", "type": "folder",
        "check_name_mode": "auto_rename",
    }, timeout=15)
    if r.status_code not in (200, 201):
        raise AliyunRelayError(f"创建文件夹失败: HTTP {r.status_code}")
    fid = r.json().get("file_id", "")
    if not fid:
        raise AliyunRelayError("创建文件夹未返回 file_id")
    return fid


def _upload_parts(access_token: str, drive_id: str, file_id: str,
                  upload_id: str, file_data: bytes, part_list: list) -> None:
    headers = {"Authorization": f"Bearer {access_token}"}
    chunk_size = 1024 * 1024  # 1MB
    parts = [file_data[i:i + chunk_size] for i in range(0, len(file_data), chunk_size)]

    for i, (part_data, pinfo) in enumerate(zip(parts, part_list)):
        pnum = pinfo["part_number"]
        url = pinfo["upload_url"]
        ok = False

        for attempt in range(5):
            try:
                r = requests.put(url, data=part_data, timeout=600)
                if r.status_code in (200, 201, 204):
                    ok = True
                    break
                if r.status_code == 403:
                    # 刷新 URL
                    r2 = requests.post(ALIYUN_GET_URL, headers=headers, json={
                        "drive_id": drive_id, "file_id": file_id,
                        "upload_id": upload_id,
                        "part_info_list": [{"part_number": pnum}],
                    }, timeout=15)
                    if r2.status_code == 200:
                        nl = r2.json().get("part_info_list", [])
                        if nl:
                            url = nl[0].get("upload_url", url)
                            continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                pass
            time.sleep(3)

        if not ok:
            raise AliyunRelayError(f"分片 {pnum}/{len(part_list)} 上传失败")

        if (i + 1) % 5 == 0 or i == len(parts) - 1:
            print(f"    {i+1}/{len(part_list)} parts 完成")


def relay_upload_to_aliyun(local_path: str, refresh_token: str,
                           folder_name: str = "zlib-github-books") -> dict:
    """接收文件并上传到阿里云盘"""
    if not os.path.exists(local_path):
        return {"success": False, "error": f"文件不存在: {local_path}"}

    file_size = os.path.getsize(local_path)
    filename = os.path.basename(local_path)

    print(f"[relay] 文件: {filename} ({file_size} bytes)")

    # 1. 获取 token
    print("  获取 token...")
    access_token, drive_id = _get_token(refresh_token)
    print(f"  drive_id={drive_id}")
    headers = {"Authorization": f"Bearer {access_token}"}

    # 2. 查找/创建文件夹
    print("  准备目录...")
    folder_id = _find_folder(access_token, drive_id, folder_name)
    if not folder_id:
        folder_id = _create_folder(access_token, drive_id, folder_name)
        print(f"  已创建文件夹: {folder_id}")
    else:
        print(f"  目标文件夹: {folder_id}")

    # 3. 创建文件记录
    print("  创建文件记录...")
    r = requests.post(ALIYUN_CREATE_URL, headers=headers, json={
        "drive_id": drive_id, "name": filename,
        "parent_file_id": folder_id, "type": "file",
        "size": file_size, "check_name_mode": "auto_rename",
    }, timeout=15)
    if r.status_code not in (200, 201):
        return {"success": False, "error": f"创建文件记录失败: HTTP {r.status_code}"}

    cr = r.json()
    if cr.get("rapid_upload"):
        print("  [✓] 秒传成功!")
        return {"success": True, "file_name": filename, "size": file_size, "rapid_upload": True}

    part_list = cr.get("part_info_list", [])
    file_id = cr.get("file_id", "")
    upload_id = cr.get("upload_id", "")
    if not part_list:
        return {"success": False, "error": "未返回分片信息"}

    # 4. 分片上传
    print(f"  上传 {len(part_list)} parts...")
    with open(local_path, "rb") as f:
        file_data = f.read()
    _upload_parts(access_token, drive_id, file_id, upload_id, file_data, part_list)

    # 5. 完成
    r2 = requests.post(ALIYUN_COMPLETE_URL, headers=headers, json={
        "drive_id": drive_id, "file_id": file_id, "upload_id": upload_id,
    }, timeout=15)
    if r2.status_code in (200, 201):
        print("  [✓] 阿里云盘上传完成!")
        return {"success": True, "file_name": filename, "size": file_size}
    return {"success": False, "error": f"完成上传失败: HTTP {r2.status_code}"}


class RelayHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {RELAY_TOKEN}":
            self._send_json(401, {"success": False, "error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            rt = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
            return self._send_json(200, {"status": "ok", "refresh_token_set": bool(rt)})
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/upload":
            return self._send_json(404, {"error": "not found"})

        if not self._check_auth():
            return

        refresh_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
        if not refresh_token:
            return self._send_json(500, {"success": False, "error": "ALIYUNDRIVE_REFRESH_TOKEN not set"})

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return self._send_json(400, {"success": False, "error": "empty body"})

        content_type = self.headers.get("Content-Type", "")
        if "boundary=" not in content_type:
            return self._send_json(400, {"success": False, "error": "missing boundary"})

        raw = self.rfile.read(content_length)
        boundary = content_type.split("boundary=")[1].strip()

        filename, filepath = None, None
        try:
            filename, filepath = self._parse_multipart(raw, boundary)
        except Exception as e:
            return self._send_json(400, {"success": False, "error": f"parse failed: {e}"})

        if not filepath:
            return self._send_json(400, {"success": False, "error": "no file"})

        try:
            result = relay_upload_to_aliyun(filepath, refresh_token)
            code = 200 if result.get("success") else 500
            self._send_json(code, result)
        except Exception as e:
            print(f"  [FAIL] {e}")
            self._send_json(500, {"success": False, "error": str(e)})
        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)

    def _parse_multipart(self, body: bytes, boundary: str) -> tuple:
        b_boundary = f"--{boundary}".encode()
        for part in body.split(b_boundary):
            part = part.strip(b"\r\n")
            if not part or part.startswith(b"--"):
                continue
            h_end = part.find(b"\r\n\r\n")
            if h_end == -1:
                continue
            hdr = part[:h_end].decode("utf-8", errors="replace")
            content = part[h_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if b"name=\"file\"" not in hdr.encode() and b"name=\"upload\"" not in hdr.encode():
                continue
            filename = None
            for line in hdr.split("\r\n"):
                if "filename=" in line.lower():
                    for q in ('"', "'"):
                        m = line.find(f"filename={q}")
                        if m >= 0:
                            s = m + len(f"filename={q}")
                            e = line.find(q, s)
                            if e > s:
                                filename = line[s:e]
                                break
                    if filename:
                        break
            if not filename:
                filename = f"file_{int(time.time())}"
            safe = filename.replace("/", "_").replace("\\", "_")
            tmp = str(UPLOAD_DIR / safe)
            with open(tmp, "wb") as f:
                f.write(content)
            return filename, tmp
        return None, None

    def log_message(self, fmt, *args):
        print(f"[relay] {fmt % args}")


def main():
    port = int(os.environ.get("RELAY_PORT", "8099"))
    rt = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
    if not rt:
        print("WARNING: ALIYUNDRIVE_REFRESH_TOKEN not set!")

    server = HTTPServer(("0.0.0.0", port), RelayHandler)
    server.allow_reuse_address = True
    server.socket.settimeout(600)  # 10 min socket timeout for large uploads
    print(f"\n{'='*50}")
    print(f"  阿里云盘中转服务器")
    print(f"  监听: 0.0.0.0:{port}")
    print(f"  公网: http://81.70.194.76:{port}")
    print(f"  Token: {'已设置' if rt else '未设置!'}")
    print(f"{'='*50}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
