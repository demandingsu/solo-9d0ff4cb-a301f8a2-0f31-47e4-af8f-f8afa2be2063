"""回执核对编排服务。

1. 从本地库读取已采集公告，按全量归并规则确定该原公告项目“现行更正后”的
   投标截止时间；
2. 沿用户提交的回执目录 URL 分页抓取上传/撤回回执；
3. 交给纯判定层核对，返回文件是否匹配、有效回执与来源链接。

抓取失败不抛出 422：它属于题目要求的“相关页面抓取失败 -> 无法判定”，
因此返回正常的 undecidable 核对结论，并在 reasons 中给出失败页面原因。
输入层面的错误（原公告未采集、摘要格式非法等）仍由 API 以 422 拒绝。
"""
from __future__ import annotations

from . import database
from .merge import MergeError, effective_deadline_for_original, merge_projects
from .receipt_merge import UNDECIDABLE, verify_receipts
from .receipt_scraper import ReceiptScrapeError, collect_receipts


class ReceiptCheckError(Exception):
    """核对请求本身不合法（区别于“无法判定”的核对结论）。"""


def check_receipt(
    *,
    original_code: str,
    section_code: str,
    file_sha256: str,
    receipt_list_url: str,
) -> dict:
    # 1) 本地库取已采集公告，确定现行（更正后）投标截止时间
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
    if not rows:
        raise ReceiptCheckError("尚未采集任何公告，请先调用 /api/collect")
    try:
        # 先做全量归并校验，保证更正引用等整体数据自洽
        merge_projects(rows)
        effective = effective_deadline_for_original(rows, original_code)
    except MergeError as exc:
        raise ReceiptCheckError(str(exc)) from exc

    # 2) 抓取回执目录（失败 -> 无法判定，而非 422）
    scrape_error: str | None = None
    receipts: list[dict] = []
    try:
        receipts = collect_receipts(receipt_list_url)
    except ReceiptScrapeError as exc:
        scrape_error = str(exc)

    # 3) 纯判定
    result = verify_receipts(
        receipts,
        original_code=original_code,
        section_code=section_code,
        deadline_text=effective.deadline,
        file_sha256=file_sha256,
    )
    payload = result.as_dict()
    payload["project_name"] = effective.project_name
    payload["deadline_source_code"] = effective.source_code
    payload["deadline_source_url"] = effective.source_url
    payload["receipt_list_url"] = receipt_list_url

    if scrape_error is not None:
        payload["status"] = UNDECIDABLE
        payload["file_matches"] = None
        payload["message"] = (
            "回执目录相关页面抓取失败，无法判定手头文件是否为平台受理版本。"
        )
        payload["reasons"] = [scrape_error, *payload["reasons"]]
    return payload
