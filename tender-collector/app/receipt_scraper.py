"""标书上传/撤回回执目录抓取与 HTML 解析。

流程与公告采集一致：
1. 从用户提交的回执目录列表页出发，沿 ``nav.pagination`` 抓取全部分页
   （同源、去重、防环、限量）；
2. 收集列表页中的回执详情链接（``ul.receipt-list a.receipt-link``）；
3. 逐页抓取并解析为结构化回执（上传 / 撤回两类）。

任一列表页或详情页读取失败（网络错误、非 2xx、HTML 缺关键字段）都抛出
ReceiptScrapeError。该错误属于“相关页面抓取失败”，核对结果必须标记为
无法判定，而不是当作没有回执。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings


class ReceiptScrapeError(Exception):
    """回执目录任一页面抓取或解析失败。"""


@dataclass
class ParsedReceipt:
    code: str                       # 回执编号
    kind: str                       # upload | withdrawal
    original_code: str              # 原公告编号
    section_code: str               # 标段号
    accepted_at: str                # 受理时间 YYYY-MM-DD HH:MM
    file_sha256: str | None         # 上传文件 SHA-256
    withdraws_code: str | None      # 撤回引用的上传回执编号
    detail_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "original_code": self.original_code,
            "section_code": self.section_code,
            "accepted_at": self.accepted_at,
            "file_sha256": self.file_sha256,
            "withdraws_code": self.withdraws_code,
            "detail_url": self.detail_url,
        }


def _same_origin(url: str, base: str) -> bool:
    u, b = urlparse(url), urlparse(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc)


def _get(client: httpx.Client, url: str) -> str:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        raise ReceiptScrapeError(f"读取回执页面失败 {url}: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise ReceiptScrapeError(f"读取回执页面失败 {url}: HTTP {resp.status_code}")
    return resp.text


def _parse_list(html_text: str, list_url: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    detail_urls: set[str] = set()
    page_urls: set[str] = set()

    for a in soup.select("ul.receipt-list a.receipt-link[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            detail_urls.add(href)

    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            page_urls.add(href)

    if not detail_urls:
        raise ReceiptScrapeError(f"回执目录页 {list_url} 未解析到任何回执链接")
    return detail_urls, page_urls


def _field(soup: BeautifulSoup, label: str) -> str | None:
    cell = soup.select_one(f'th[data-field="{label}"] + td')
    if cell is None:
        return None
    text = cell.get_text(strip=True)
    return text or None


def _parse_detail(html_text: str, detail_url: str) -> ParsedReceipt:
    soup = BeautifulSoup(html_text, "html.parser")
    article = soup.select_one("article.receipt-detail")
    if article is None:
        raise ReceiptScrapeError(
            f"回执详情页 {detail_url} 缺少 article.receipt-detail"
        )

    code = article.get("data-code") or _field(soup, "回执编号")
    kind_raw = article.get("data-kind")
    original_code = article.get("data-original-code") or _field(soup, "原公告编号")
    section_code = article.get("data-section-code") or _field(soup, "标段号")
    accepted_at = article.get("data-accepted-at") or _field(soup, "受理时间")
    withdraws = article.get("data-withdraws") or _field(soup, "撤回上传编号")
    file_sha = _field(soup, "文件摘要SHA-256")

    kind = {"upload": "upload", "withdrawal": "withdrawal"}.get(kind_raw or "", "")
    if not kind:
        label = _field(soup, "回执类型") or ""
        kind = "withdrawal" if "撤回" in label else "upload"

    missing = [
        name
        for name, val in (
            ("回执编号", code),
            ("原公告编号", original_code),
            ("标段号", section_code),
            ("受理时间", accepted_at),
        )
        if not val
    ]
    if missing:
        raise ReceiptScrapeError(
            f"回执详情页 {detail_url} 缺少必填字段: {', '.join(missing)}"
        )

    if kind == "upload" and not file_sha:
        raise ReceiptScrapeError(f"上传回执 {code}（{detail_url}）缺少文件摘要SHA-256")
    if kind == "withdrawal" and not withdraws:
        raise ReceiptScrapeError(f"撤回回执 {code}（{detail_url}）缺少撤回上传编号")

    return ParsedReceipt(
        code=code.strip(),
        kind=kind,
        original_code=original_code.strip(),
        section_code=section_code.strip(),
        accepted_at=accepted_at.strip(),
        file_sha256=file_sha.strip() if file_sha else None,
        withdraws_code=withdraws.strip() if withdraws else None,
        detail_url=detail_url,
    )


def collect_receipts(receipt_list_url: str) -> list[dict[str, Any]]:
    """抓取整个回执目录并返回结构化回执列表（顺序不保证，判定层不依赖顺序）。"""
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        # 1) 遍历全部回执目录分页
        seen_pages: set[str] = set()
        queue: list[str] = [receipt_list_url]
        detail_urls: set[str] = set()

        while queue:
            if len(seen_pages) >= settings.max_list_pages:
                raise ReceiptScrapeError(
                    f"回执目录页数超过上限 {settings.max_list_pages}，疑似分页环"
                )
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            html_text = _get(client, page_url)
            details, more_pages = _parse_list(html_text, page_url)
            detail_urls.update(details)
            for u in more_pages:
                if u not in seen_pages and u not in queue:
                    queue.append(u)

        # 2) 逐页抓取回执详情 —— 任一失败即整体失败（上层据此标记无法判定）
        receipts: list[dict[str, Any]] = []
        for detail_url in sorted(detail_urls):  # 排序仅为日志/测试的确定性
            html_text = _get(client, detail_url)
            receipts.append(_parse_detail(html_text, detail_url).as_dict())

    return receipts
