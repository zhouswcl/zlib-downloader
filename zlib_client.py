"""
Z-Library API Client
基于 heartleo/zlib (Go) 源码分析的 HTTP API 封装
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Z-Library 域名优先级链（z-lib.sk 是 Go 客户端的默认域名）
DEFAULT_DOMAINS = [
    "https://z-lib.sk",
    "https://singlelogin.re",
    "https://z-lib.io",
    "https://z-lib.is",
]


class ZLibraryError(Exception):
    pass


class ZLibraryClient:
    """Z-Library API 客户端
    
    基于 heartleo/zlib (Go) 的 API 逆向工程：
    - 登录: POST {domain}/rpc.php (form-encoded)
    - 搜索: GET {domain}/s/{query}?page=N (HTML -> 解析)
    - 下载: GET {domain}/dl/{book_id} (302 -> CDN)
    """

    def __init__(self, email: str, password: str, domains: list[str] = None):
        self.email = email
        self.password = password
        self.domains = domains or DEFAULT_DOMAINS.copy()
        self.domain = None  # 登录成功后确定的可用域名
        self.logged_in = False

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })

    # ---- 登录 ----

    def login(self) -> bool:
        """尝试登录 Z-Library，返回是否成功"""
        for domain in self.domains:
            if self._try_login(domain):
                self.domain = domain
                self.logged_in = True
                print(f"[✓] 登录成功: {domain}")
                return True
        raise ZLibraryError("所有域名登录失败，请检查账号或 Z-Library 可用性")

    def _try_login(self, domain: str) -> bool:
        """向单个域名发起登录"""
        url = f"{domain}/rpc.php"
        data = {
            "isModal": "true",
            "email": self.email,
            "password": self.password,
            "site_mode": "books",
            "action": "login",
            "isSingleLogin": "1",
            "redirectUrl": "",
            "gg_json_mode": "1",
        }
        try:
            resp = self.session.post(url, data=data, timeout=30)
            # 有些域名返回空或非 JSON（如 singlelogin.re），跳过
            ct = resp.headers.get("content-type", "")
            if "json" not in ct.lower():
                print(f"[!] {domain}: 非 JSON 响应 (content-type: {ct})")
                return False
            result = resp.json()
            err = result.get("response", {}).get("validationError")
            if err is None:
                return True
            print(f"[!] {domain}: 登录被拒 - {err}")
        except requests.Timeout:
            print(f"[!] {domain}: 超时")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[!] {domain}: 非 JSON 响应 - {e}")
        except Exception as e:
            print(f"[!] {domain}: {e}")
        return False

    # ---- 搜索 ----

    def search(
        self, query: str, page: int = 1, count: int = 10
    ) -> list[dict]:
        """搜索图书，返回列表"""
        if not self.logged_in:
            raise ZLibraryError("未登录")

        # singlelogin.re 是登录门户，不能用于搜索
        search_domains = [d for d in self.domains if "singlelogin" not in d]
        targets = [self.domain] + [d for d in search_domains if d != self.domain]

        for domain in targets:
            try:
                encoded = requests.utils.quote(query)
                url = f"{domain}/s/{encoded}?&page={page}"
                print(f"  搜索 URL: {url}")
                resp = self.session.get(url, timeout=30, headers={
                    "Referer": f"{domain}/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                })

                # 503 重试一次
                if resp.status_code == 503:
                    print(f"  503 服务忙，等待 5 秒重试...")
                    time.sleep(5)
                    resp = self.session.get(url, timeout=30)
                    if resp.status_code == 503:
                        print(f"  仍返回 503，跳过 {domain}")
                        continue

                if resp.status_code != 200:
                    print(f"  HTTP {resp.status_code}, 跳过 {domain}")
                    continue

                html = resp.text

                # 检测 JS 反爬挑战页面（z-lib.is 等）
                challenge_url = self._detect_challenge(html)
                if challenge_url:
                    print(f"  检测到 JS 挑战，跟随重定向: {challenge_url}")
                    resp2 = self.session.get(challenge_url, timeout=30)
                    if resp2.status_code == 200:
                        html = resp2.text

                books = self._parse_search_results(html, domain)
                if books:
                    return books[:count]

                snippet = re.sub(r'\s+', ' ', html[:1500])[:300]
                print(f"  页面加载但未解析到书: {snippet}...")

                # 备用: /search/ 路径
                url2 = f"{domain}/search/{encoded}/?page={page}"
                resp2 = self.session.get(url2, timeout=30)
                if resp2.status_code == 200:
                    html2 = resp2.text
                    challenge2 = self._detect_challenge(html2)
                    if challenge2:
                        resp2 = self.session.get(challenge2, timeout=30)
                        html2 = resp2.text
                    books = self._parse_search_results(html2, domain)
                    if books:
                        return books[:count]

            except Exception as e:
                print(f"[!] 搜索失败 ({domain}): {type(e).__name__}: {e}")
                continue
        return []

    def _detect_challenge(self, html: str) -> Optional[str]:
        """检测 JS 反爬挑战页面，返回重定向 URL"""
        m = re.search(r"redirect_link\s*=\s*'([^']+)'", html)
        if m:
            return m.group(1)
        if "fingerprint" in html.lower() and "challenge" in html.lower():
            m = re.search(r"window\.location\s*=\s*'([^']+)'", html)
            if m:
                return m.group(1)
        return None

    def _parse_search_results(self, html: str, domain: str) -> list[dict]:
        """解析搜索结果的 HTML（多策略）"""
        soup = BeautifulSoup(html, "html.parser")
        books = []

        # 策略 1: z-bookcard 自定义元素 (新版 Z-Library)
        cards = soup.select("z-bookcard, [is='z-bookcard'], book-card, [class*=bookCard]")
        for card in cards:
            book = self._parse_book_card(card, domain)
            if book.get("id") or book.get("title"):
                books.append(book)

        # 策略 2: 传统 .book-item 结构
        if not books:
            for card in soup.select(".book-item, [class*=resItemCard], [class*=bookItem]"):
                book = self._parse_book_card_legacy(card, domain)
                if book.get("title"):
                    books.append(book)

        # 策略 3: 所有带 book 链接的卡片 (最通用的匹配)
        if not books:
            for card in soup.select('[class*="card"]:has(a[href*="/book/"]), [class*="item"]:has(a[href*="/book/"])'):
                book = self._parse_book_card_legacy(card, domain)
                if book.get("title"):
                    books.append(book)

        # 策略 4: 直接从搜索结果框解析所有链接
        if not books:
            result_box = soup.select_one("#searchResultBox, [class*=searchResult], [class*=resultBox]")
            if result_box:
                for a in result_box.select('a[href*="/book/"]'):
                    book = {
                        "id": "",
                        "title": a.get_text(strip=True),
                        "url": urljoin(domain, a.get("href", "")),
                        "author": "",
                        "extension": "",
                        "size": "",
                    }
                    m = re.search(r"/book/([^/]+)", a.get("href", ""))
                    if m:
                        book["id"] = m.group(1)
                    # 找父级中的其他信息
                    parent = a.parent
                    for _ in range(5):
                        if parent:
                            text = parent.get_text(" ", strip=True)
                            # 尝试提取格式和大小
                            fm = re.search(r"\b(pdf|epub|mobi|txt|djvu|fb2|docx?)\b", text, re.I)
                            if fm:
                                book["extension"] = fm.group(1).lower()
                            sm = re.search(r"(\d+(?:[.,]\d+)?)\s*(MB|KB|GB)", text, re.I)
                            if sm:
                                book["size"] = sm.group(0)
                            parent = parent.parent if parent.name != "html" else None
                    if book.get("title"):
                        books.append(book)

        return books

    def _parse_book_card(self, card, domain: str) -> dict:
        """解析新版 z-bookcard 元素"""
        book = {
            "id": (card.get("id") or "").strip(),
            "isbn": (card.get("isbn") or "").strip(),
            "title": "",
            "author": "",
            "publisher": (card.get("publisher") or "").strip(),
            "year": (card.get("year") or "").strip(),
            "language": (card.get("language") or "").strip(),
            "extension": (card.get("extension") or "").strip(),
            "size": (card.get("filesize") or "").strip(),
            "rating": (card.get("rating") or "").strip(),
            "quality": (card.get("quality") or "").strip(),
            "url": "",
            "cover": "",
        }

        href = card.get("href", "")
        if href:
            book["url"] = urljoin(domain, href)

        img = card.select_one("img")
        if img:
            src = img.get("data-src") or img.get("src", "")
            if src:
                book["cover"] = urljoin(domain, src)

        title_el = card.select_one('[slot="title"]')
        if title_el:
            book["title"] = title_el.get_text(strip=True)

        author_el = card.select_one('[slot="author"]')
        if author_el:
            book["author"] = author_el.get_text(strip=True)

        return book

    def _parse_book_card_legacy(self, card, domain: str) -> dict:
        """解析旧版 .book-item 结构"""
        book = {
            "id": "",
            "title": "",
            "author": "",
            "extension": "",
            "size": "",
            "url": "",
        }
        link = card.select_one("a[href*='/book/']")
        if link:
            book["url"] = urljoin(domain, link.get("href", ""))
            m = re.search(r"/book/([^/]+)", link.get("href", ""))
            if m:
                book["id"] = m.group(1)
            book["title"] = link.get_text(strip=True)

        author_el = card.select_one(".author, [class*=author]")
        if author_el:
            book["author"] = author_el.get_text(strip=True)

        ext_el = card.select_one(".format, [class*=format], [class*=extension]")
        if ext_el:
            book["extension"] = ext_el.get_text(strip=True)

        size_el = card.select_one(".size, [class*=size]")
        if size_el:
            book["size"] = size_el.get_text(strip=True)

        return book

    # ---- 获取下载链接 ----

    def get_download_url(self, book_id: str) -> Optional[str]:
        """获取图书的真实下载 URL (跟随重定向)"""
        targets = [self.domain] + [d for d in self.domains if d != self.domain]
        for domain in targets:
            for prefix in ("/dl/", "/file/"):
                url = f"{domain}{prefix}{book_id}"
                try:
                    resp = self.session.head(url, allow_redirects=True, timeout=15)
                    if resp.status_code == 200 and resp.url:
                        return resp.url
                except Exception:
                    continue
        return None

    def get_download_url_from_page(self, book_url: str) -> Optional[str]:
        """从图书详情页提取下载链接"""
        try:
            resp = self.session.get(book_url, timeout=30)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select('a[href*="/dl/"], a[href*="/file/"], a.dlButton, a[class*=download]'):
                href = a.get("href", "")
                if href:
                    return urljoin(self.domain or "", href)
        except Exception as e:
            print(f"[!] 提取下载链接失败: {e}")
        return None

    # ---- 下载 ----

    def download(self, book_id: str, dest_dir: str, book_url: str = "") -> Optional[dict]:
        """下载图书到本地目录，返回文件信息"""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        dl_url = self.get_download_url(book_id)
        if not dl_url and book_url:
            dl_url = self.get_download_url_from_page(book_url)
        if not dl_url:
            print(f"[!] 无法获取下载链接: {book_id}")
            return None

        print(f"  下载中: {dl_url}")
        try:
            resp = self.session.get(dl_url, stream=True, timeout=600)
            if resp.status_code != 200:
                print(f"[!] 下载 HTTP {resp.status_code}")
                return None

            filename = self._extract_filename(resp, dl_url, book_id)
            filepath = dest_dir / filename

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            last_log = 0

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total and downloaded - last_log > 5 * 1024 * 1024:
                            pct = downloaded * 100 / total
                            print(f"  {filename}: {downloaded//1024//1024}MB/{total//1024//1024}MB ({pct:.0f}%)")
                            last_log = downloaded

            return {
                "filepath": str(filepath),
                "filename": filename,
                "size": downloaded,
                "book_id": book_id,
            }
        except Exception as e:
            print(f"[!] 下载失败: {e}")
            return None

    def _extract_filename(self, resp, dl_url: str, book_id: str) -> str:
        """从 HTTP 响应提取文件名"""
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename\s*=\s*["\']?([^"\';\n]+)', cd)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            if name:
                return name

        parts = dl_url.rstrip("/").split("/")
        candidate = parts[-1].split("?")[0] if parts else ""
        if candidate and "." in candidate:
            return candidate

        return f"{book_id}.pdf"

    # ---- 限额查询 ----

    def get_daily_limit(self) -> dict:
        """查询当日下载限额"""
        if not self.logged_in:
            raise ZLibraryError("未登录")

        for domain in [self.domain] + self.domains:
            try:
                resp = self.session.get(f"{domain}/users/downloads", timeout=30)
                soup = BeautifulSoup(resp.text, "html.parser")
                text = soup.get_text()
                m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if m:
                    return {
                        "used": int(m.group(1)),
                        "total": int(m.group(2)),
                        "remaining": int(m.group(2)) - int(m.group(1)),
                    }
            except Exception:
                continue
        return {"used": 0, "total": 10, "remaining": 10}
