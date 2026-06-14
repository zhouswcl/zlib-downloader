#!/usr/bin/env python3
"""
夸克网盘中转服务器
===================
接收 GitHub Actions 上传的文件，通过中国 IP 转存到夸克网盘。

API:
  POST /upload  上传文件（multipart）
       Header: Authorization: Bearer <token>
       Body:  multipart/form-data, field "file" = 文件内容
       Returns: {"success": true/false, ...}

  GET /health   健康检查

环境变量:
  QUARK_COOKIE         夸克网盘 Cookie
  RELAY_TOKEN          请求鉴权 Token（Authorization Bearer）
  RELAY_PORT           监听端口（默认 8099）
  UPLOAD_DIR           临时文件目录（默认 /tmp/quark-relay）
"""
import hashlib
import json
import os
import shutil
import socket
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
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

QUARK_COOKIE = os.environ.get("QUARK_COOKIE", "")
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "zlib-relay-key-2026")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/quark-relay"))
os.makedirs(UPLOAD_DIR, exist_ok=True)


class QuarkError(Exception):
    pass


def quark_get_st(cookie: str) -> tuple[str, str]:
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


def quark_find_or_create_folder(cookie: str, st: str, folder_name: str, parent_fid: str = "0") -> str:
    """查找或创建上传目录，返回 fid"""
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

    for item in data.get("data", {}).get("list", []):
        if item.get("file_type") == 0 and item.get("file_name") == folder_name:
            return item["fid"]

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


def quark_upload_file(cookie: str, local_path: str, filename: str, target_fid: str) -> dict:
    """上传文件到夸克，返回结果"""
    file_size = os.path.getsize(local_path)
    st, _ = quark_get_st(cookie)

    # 计算 MD5
    with open(local_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()

    ts = int(time.time() * 1000)
    upload_url = f"{QUARK_UPLOAD}?pr=ucpro&fr=pc&uc_param_str=&st={st}&pdir_fid={target_fid}&_={ts}"

    with open(local_path, "rb") as f:
        file_data = f.read()

    files = {"file": (filename, file_data, "application/octet-stream")}
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
            timeout=600,  # 10 min for large files
        )
    except requests.exceptions.Timeout:
        return {"success": False, "error": "upload request timed out (600s)"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"connection error: {e}"}

    if r.status_code not in (200, 201):
        return {"success": False, "error": f"upload failed: HTTP {r.status_code}: {r.text[:200]}"}

    resp = r.json()
    if resp.get("status") == 200:
        file_info = resp.get("data", {})
        return {
            "success": True,
            "file_name": filename,
            "size": file_info.get("file_size", file_size),
            "quark_fid": file_info.get("fid", ""),
        }
    elif resp.get("status") == 40201:
        return {"success": True, "file_name": filename, "size": file_size, "rapid_upload": True}
    else:
        return {"success": False, "error": f"upload failed: {resp.get('message', r.text[:200])}"}


class ThreadingRelayServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器，支持并发请求"""
    allow_reuse_address = True
    daemon_threads = True


class RelayHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {RELAY_TOKEN}"
        if auth != expected:
            self._send_json(401, {"success": False, "error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._send_json(200, {"status": "ok", "cookie_set": bool(QUARK_COOKIE)})
        if path == "/debug/cookie_prefix":
            return self._send_json(200, {"prefix": QUARK_COOKIE[:20] + "..." if QUARK_COOKIE else "(empty)"})
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/upload":
            return self._send_json(404, {"error": "not found"})

        if not self._check_auth():
            return

        if not QUARK_COOKIE:
            return self._send_json(500, {"success": False, "error": "QUARK_COOKIE not set on relay"})

        # 解析上传的文件
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return self._send_json(400, {"success": False, "error": "empty body"})

        # 读原始 body，手动找 boundary
        content_type = self.headers.get("Content-Type", "")
        if "boundary=" not in content_type:
            return self._send_json(400, {"success": False, "error": "missing boundary in Content-Type"})

        raw_body = self.rfile.read(content_length)
        LOG_MSG = f"[relay] received {content_length} bytes"
        print(LOG_MSG)
        boundary = content_type.split("boundary=")[1].strip()

        # 从 multipart 提取文件名和文件数据
        filename, filepath = None, None
        try:
            filename, filepath = self._parse_multipart(raw_body, boundary)
        except Exception as e:
            return self._send_json(400, {"success": False, "error": f"parse failed: {e}"})

        if not filepath:
            return self._send_json(400, {"success": False, "error": "no file received"})

        # 上传到夸克
        try:
            print(f"\n[relay] 收到: {filename} ({os.path.getsize(filepath)} bytes)")
            # 获取 st + 查找/创建目录
            st, _ = quark_get_st(QUARK_COOKIE)
            folder_fid = quark_find_or_create_folder(QUARK_COOKIE, st, "zlib-github-books")
            # 上传
            result = quark_upload_file(QUARK_COOKIE, filepath, filename, folder_fid)
            if result.get("success"):
                print(f"  [✓] 上传成功: {filename}")
            else:
                print(f"  [!] 上传失败: {result.get('error', '?')}")
            self._send_json(200 if result.get("success") else 500, result)
        except QuarkError as e:
            print(f"  [FAIL] 夸克错误: {e}")
            self._send_json(500, {"success": False, "error": str(e)})
        except Exception as e:
            print(f"  [FAIL] 未知错误: {e}")
            self._send_json(500, {"success": False, "error": str(e)})
        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)

    def _parse_multipart(self, body: bytes, boundary: str) -> tuple[Optional[str], Optional[str]]:
        """从 multipart body 提取文件，保存到临时目录"""
        b_boundary = f"--{boundary}".encode("utf-8")
        b_end = f"--{boundary}--".encode("utf-8")

        # 找到第一个文件部分的起止
        parts = body.split(b_boundary)
        for part in parts:
            if b"--{boundary}--" in body:
                pass  # end marker handled below

            # 跳过空的部分和结束标记
            part = part.strip(b"\r\n")
            if not part or part.startswith(b"--"):
                continue

            # 找空行分隔 header/body
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue

            headers_raw = part[:header_end].decode("utf-8", errors="replace")
            content = part[header_end + 4:]

            # 去掉尾部 boundary 残留
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if content.endswith(b"--"):
                content = content[:-2]

            # 是否含 Content-Disposition: file
            if "name=\"file\"" not in headers_raw and "name=\"upload\"" not in headers_raw:
                continue

            # 提取文件名
            filename = None
            for line in headers_raw.split("\r\n"):
                if line.lower().startswith("content-disposition"):
                    # filename="..." or filename='...'
                    for q in ('"', "'"):
                        m = line.find(f"filename={q}")
                        if m >= 0:
                            start = m + len(f"filename={q}")
                            end = line.find(q, start)
                            if end > start:
                                filename = line[start:end]
                                break
                    if filename:
                        break

            if not filename:
                filename = f"file_{int(time.time())}"

            # 保存到临时文件
            safe_filename = filename.replace("/", "_").replace("\\", "_")
            tmp_path = str(UPLOAD_DIR / safe_filename)
            with open(tmp_path, "wb") as f:
                f.write(content)
            return filename, tmp_path

        return None, None

    def log_message(self, fmt, *args):
        print(f"[relay] {fmt % args}")


def main():
    port = int(os.environ.get("RELAY_PORT", "8099"))

    if not QUARK_COOKIE:
        print("WARNING: QUARK_COOKIE not set! Uploads will fail.")

    server = ThreadingRelayServer(("0.0.0.0", port), RelayHandler)
    server.socket.settimeout(600)  # 10 min socket timeout for large uploads
    print(f"\n{'='*50}")
    print(f"  夸克网盘中转服务器")
    print(f"  监听: 0.0.0.0:{port}")
    print(f"  公网: http://81.70.194.76:{port}")
    print(f"  健康: GET /health")
    print(f"  上传: POST /upload (multipart, Bearer token)")
    print(f"  Cookie: {'已设置' if QUARK_COOKIE else '未设置!'}")
    print(f"{'='*50}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[relay] 关闭服务器...")
        server.shutdown()


if __name__ == "__main__":
    main()
