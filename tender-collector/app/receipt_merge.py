"""回执核对纯判定层：截止时间过滤、撤回生效、末份上传定位与无法判定规则。

输入为已抓取的全部回执字典（顺序任意），输出与页面排列、抓取顺序完全无关。

核对规则：
1. 只处理“该原公告 + 该标段”且受理时间不晚于现行（更正后）投标截止时间的
   回执；截止时间之后受理的上传/撤回一律不纳入。
2. 撤回按所引用的上传回执编号生效：引用编号在该标段没有对应上传记录时，
   数据无法自洽，标记为无法判定。
3. 同一受理时间存在多份上传且文件摘要不一致时，无法确定哪一份才是平台
   实际受理的末份，标记为无法判定。
4. 在从未被撤回的上传中，取受理时间最新（时间相同按回执编号排序）的一份
   作为平台受理版本；若全部上传均已撤回，则没有有效上传。
5. 调用方在任一相关页面抓取失败时应直接给出 undecidable，不得静默当作
   没有回执处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

TIME_FORMAT = "%Y-%m-%d %H:%M"

# 核对结论状态
MATCHED = "matched"
MISMATCH = "mismatch"
NO_VALID_UPLOAD = "no_valid_upload"
UNDECIDABLE = "undecidable"


class ReceiptMergeError(ValueError):
    """核对输入本身不合法（如截止时间无法解析）。"""


@dataclass
class ReceiptVerification:
    status: str
    file_matches: bool | None
    message: str
    original_code: str
    section_code: str
    current_deadline: str
    effective_upload: dict[str, Any] | None = None
    valid_receipts: list[dict[str, Any]] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    ignored_after_deadline: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file_matches": self.file_matches,
            "message": self.message,
            "original_code": self.original_code,
            "section_code": self.section_code,
            "current_deadline": self.current_deadline,
            "effective_upload": self.effective_upload,
            "valid_receipts": self.valid_receipts,
            "source_urls": self.source_urls,
            "ignored_after_deadline": self.ignored_after_deadline,
            "reasons": self.reasons,
        }


def _parse_time(value: str) -> datetime:
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        # 容错：支持带秒或 ISO 形式
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ReceiptMergeError(f"时间格式无法解析: {value}") from exc


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    """回执对外字段（不含内部归属过滤用字段以外的多余内容）。"""
    return {
        "code": row["code"],
        "kind": row["kind"],
        "accepted_at": row["accepted_at"],
        "file_sha256": row.get("file_sha256"),
        "withdraws_code": row.get("withdraws_code"),
        "detail_url": row["detail_url"],
    }


def verify_receipts(
    rows: Iterable[dict[str, Any]],
    *,
    original_code: str,
    section_code: str,
    deadline_text: str,
    file_sha256: str,
) -> ReceiptVerification:
    """对单个标段执行回执核对（纯函数，不做任何 IO）。"""
    deadline = _parse_time(deadline_text)
    scope = [
        r
        for r in rows
        if r.get("original_code") == original_code
        and r.get("section_code") == section_code
    ]

    base = ReceiptVerification(
        status=UNDECIDABLE,
        file_matches=None,
        message="",
        original_code=original_code,
        section_code=section_code,
        current_deadline=deadline_text,
    )

    if not scope:
        base.status = NO_VALID_UPLOAD
        base.message = (
            f"回执目录中没有原公告 {original_code} 标段 {section_code} "
            "在投标截止时间之前受理的任何上传回执，当前没有有效上传，"
            "无法确认手头文件为平台受理版本。"
        )
        return base

    reasons: list[str] = []

    # 1) 受理时间解析 + 截止时间过滤：只处理截止时间（含）之前受理的记录
    accepted: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for r in scope:
        try:
            accepted_at = _parse_time(r["accepted_at"])
        except ReceiptMergeError:
            reasons.append(
                f"回执 {r.get('code')} 的受理时间格式无法解析: {r.get('accepted_at')}"
            )
            continue
        if accepted_at <= deadline:
            accepted.append(r)
        else:
            ignored.append(r)
    base.ignored_after_deadline = [_public_view(r) for r in ignored]

    uploads: dict[str, dict[str, Any]] = {}
    withdrawals: list[dict[str, Any]] = []
    for r in accepted:
        if r["kind"] == "upload":
            if r["code"] in uploads:
                reasons.append(f"上传回执编号重复: {r['code']}")
            uploads[r["code"]] = r
        elif r["kind"] == "withdrawal":
            withdrawals.append(r)

    # 2) 同一受理时间的最后上传摘要必须一致，否则无法判定末份
    by_time: dict[str, set[str]] = {}
    for r in uploads.values():
        by_time.setdefault(r["accepted_at"], set()).add(
            (r.get("file_sha256") or "").lower()
        )
    for accepted_at, digests in sorted(by_time.items()):
        if len(digests) > 1:
            reasons.append(
                f"同一受理时间 {accepted_at} 存在多份上传且文件摘要不一致，"
                "无法确定平台受理的末份文件。"
            )

    # 3) 撤回引用的上传编号必须存在，否则撤回链无法自洽
    resolved_withdrawals: list[dict[str, Any]] = []
    withdrawn_codes: set[str] = set()
    for w in withdrawals:
        ref = w.get("withdraws_code")
        if not ref or ref not in uploads:
            reasons.append(
                f"撤回回执 {w['code']} 引用的上传编号 {ref} "
                "在该标段回执中没有对应记录。"
            )
            continue
        resolved_withdrawals.append(w)
        withdrawn_codes.add(ref)

    # 可自洽的有效回执：全部截止前上传 + 引用可解析的撤回
    base.valid_receipts = [
        _public_view(r)
        for r in sorted(
            [*uploads.values(), *resolved_withdrawals],
            key=lambda r: (r["accepted_at"], r["code"]),
        )
    ]
    base.source_urls = [r["detail_url"] for r in base.valid_receipts]

    if reasons:
        base.status = UNDECIDABLE
        base.reasons = reasons
        base.message = (
            "回执数据存在冲突或缺失，无法判定手头文件是否为平台受理的末份版本。"
        )
        return base

    # 4) 从未撤回的上传中取受理时间最新的一份
    active = [u for u in uploads.values() if u["code"] not in withdrawn_codes]
    if not active:
        base.status = NO_VALID_UPLOAD
        base.message = (
            f"原公告 {original_code} 标段 {section_code} 截止时间之前受理的"
            f" {len(uploads)} 份上传已全部被撤回，当前没有有效上传，"
            "手头文件无法对应任何平台受理版本。"
        )
        return base

    effective = max(active, key=lambda r: (r["accepted_at"], r["code"]))
    effective_view = _public_view(effective)
    base.effective_upload = effective_view
    if effective_view["detail_url"] not in base.source_urls:
        base.source_urls.append(effective_view["detail_url"])

    expected = (effective.get("file_sha256") or "").lower()
    matches = expected == file_sha256.strip().lower()
    base.file_matches = matches
    if matches:
        base.status = MATCHED
        base.message = (
            f"手头文件与平台受理的末份未撤回上传 {effective['code']} 摘要一致，"
            "确认是当前受理版本。"
        )
    else:
        base.status = MISMATCH
        base.message = (
            f"手头文件与平台受理的末份未撤回上传 {effective['code']} 摘要不一致，"
            "平台当前受理的是另一份文件，请以回执为准。"
        )
    return base
