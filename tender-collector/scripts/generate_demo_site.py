#!/usr/bin/env python3
"""生成本地分页 HTML 示例公告站（市政工程招投标公告 + 标书回执目录）。

生成结果写入 demo_site/，纯静态文件，可用任意 HTTP 服务器托管：
    python3 -m http.server 8001 --directory demo_site

数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的“生效截止时间”由发布时间最新的那份公告（原公告或更正公告）决定；
- 含已过期与未过期两种项目；
- receipts/ 子目录为标书上传/撤回回执站，同样分页且跨页乱序，覆盖：
  正常撤回链、同一受理时间摘要冲突、撤回引用悬空、截止时间之后记录忽略、
  全部撤回导致无有效上传、无关标段噪声等核对场景。
"""
from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from pathlib import Path

PAGE_SIZE = 4
RECEIPT_PAGE_SIZE = 6
OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site"
RECEIPT_DIR = OUT_DIR / "receipts"


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 示例数据：5 个项目，其中 3 个含更正公告，共 10 条公告
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
    ),
]


# ---------------------------------------------------------------------------
# 标书上传/撤回回执数据（receipts/ 子站）
#
# 字段说明：
# - kind: upload（平台受理的上传回执）/ withdrawal（撤回回执）
# - accepted_at: 平台受理时间 YYYY-MM-DD HH:MM
# - section_code: 标段号
# - original_code: 所属原公告编号（每个标段归属于一个原公告项目）
# - file_sha256: 上传文件摘要（撤回回执为空）
# - withdraws_code: 撤回回执所引用的上传回执编号（仅撤回回执）
#
# 场景设计（截止时间均取公告归并后的“现行更正截止时间”）：
#   001/G1 正常多轮上传+撤回，末份未撤回上传可定位
#   002/G1 同一受理时间两份上传摘要不一致 -> 无法判定
#   002/G2 撤回链后末份可定位；另含 G9 无关标段噪声
#   003/G1 上传被全部撤回 -> 没有有效上传
#   004/G1 撤回引用了不存在的上传编号 -> 无法判定
#   005/G1 截止时间之后的上传/撤回一律不受理（不影响截止前结果）
# ---------------------------------------------------------------------------
@dataclass
class Receipt:
    code: str                                   # 回执编号
    kind: str                                   # upload | withdrawal
    original_code: str                          # 原公告编号
    section_code: str                           # 标段号
    accepted_at: str                            # 受理时间 YYYY-MM-DD HH:MM
    file_sha256: str | None = None              # 上传文件 SHA-256（撤回为空）
    withdraws_code: str | None = None           # 撤回引用的上传回执编号
    bidder: str = "示例市政建设有限公司"

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


def _sha(seed: str) -> str:
    """构造确定性的 64 位十六进制伪 SHA-256（仅用于离线样例）。"""
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# 各场景用于核对的“手头文件”摘要（测试与文档直接引用）
SAMPLE_FILE_HASHES: dict[str, str] = {
    "SZ-2026-001/G1": _sha("SZ-2026-001/G1/bid-doc-v3"),
    "SZ-2026-002/G2": _sha("SZ-2026-002/G2/bid-doc-v2"),
    "SZ-2026-005/G1": _sha("SZ-2026-005/G1/bid-doc-v2"),
}

RECEIPTS: list[Receipt] = [
    # --- 001/G1（截止 2026-11-25 14:30）：上传-撤回-再上传，末份未撤回 = UP-001-3 ---
    Receipt("UP-001-1", "upload", "SZ-2026-001", "G1",
            "2026-11-10 09:15", _sha("SZ-2026-001/G1/bid-doc-v1")),
    Receipt("UP-001-2", "upload", "SZ-2026-001", "G1",
            "2026-11-16 14:20", _sha("SZ-2026-001/G1/bid-doc-v2")),
    Receipt("WD-001-1", "withdrawal", "SZ-2026-001", "G1",
            "2026-11-17 10:05", withdraws_code="UP-001-2"),
    Receipt("UP-001-3", "upload", "SZ-2026-001", "G1",
            "2026-11-22 16:40", _sha("SZ-2026-001/G1/bid-doc-v3")),
    Receipt("WD-001-2", "withdrawal", "SZ-2026-001", "G1",
            "2026-11-12 11:00", withdraws_code="UP-001-1"),
    # 截止时间之后的记录：核对时必须忽略
    Receipt("UP-001-LATE", "upload", "SZ-2026-001", "G1",
            "2026-11-25 15:10", _sha("SZ-2026-001/G1/bid-doc-late")),

    # --- 002/G1（截止 2026-11-30 10:00）：同一受理时间两份摘要不同 -> 无法判定 ---
    Receipt("UP-002G1-1", "upload", "SZ-2026-002", "G1",
            "2026-11-12 09:30", _sha("SZ-2026-002/G1/bid-doc-a")),
    Receipt("UP-002G1-2", "upload", "SZ-2026-002", "G1",
            "2026-11-20 15:00", _sha("SZ-2026-002/G1/bid-doc-b1")),
    Receipt("UP-002G1-3", "upload", "SZ-2026-002", "G1",
            "2026-11-20 15:00", _sha("SZ-2026-002/G1/bid-doc-b2")),
    # G9 无关标段噪声：按标段过滤后不得参与 G1/G2 判定
    Receipt("UP-002G9-1", "upload", "SZ-2026-002", "G9",
            "2026-11-18 10:00", _sha("SZ-2026-002/G9/bid-doc-x")),

    # --- 002/G2（截止 2026-11-30 10:00）：撤回链后末份未撤回 = UP-002G2-2 ---
    Receipt("UP-002G2-1", "upload", "SZ-2026-002", "G2",
            "2026-11-14 09:45", _sha("SZ-2026-002/G2/bid-doc-v1")),
    Receipt("WD-002G2-1", "withdrawal", "SZ-2026-002", "G2",
            "2026-11-19 13:30", withdraws_code="UP-002G2-1"),
    Receipt("UP-002G2-2", "upload", "SZ-2026-002", "G2",
            "2026-11-25 17:05", _sha("SZ-2026-002/G2/bid-doc-v2")),

    # --- 003/G1（截止 2026-10-30 17:00）：上传被全部撤回 -> 没有有效上传 ---
    Receipt("UP-003-1", "upload", "SZ-2026-003", "G1",
            "2026-10-12 10:20", _sha("SZ-2026-003/G1/bid-doc-v1")),
    Receipt("WD-003-1", "withdrawal", "SZ-2026-003", "G1",
            "2026-10-18 09:40", withdraws_code="UP-003-1"),
    Receipt("UP-003-2", "upload", "SZ-2026-003", "G1",
            "2026-10-22 15:30", _sha("SZ-2026-003/G1/bid-doc-v2")),
    Receipt("WD-003-2", "withdrawal", "SZ-2026-003", "G1",
            "2026-10-27 11:10", withdraws_code="UP-003-2"),

    # --- 004/G1（截止 2026-11-28 09:30）：撤回引用悬空 -> 无法判定 ---
    Receipt("UP-004-1", "upload", "SZ-2026-004", "G1",
            "2026-11-08 14:10", _sha("SZ-2026-004/G1/bid-doc-v1")),
    Receipt("WD-004-1", "withdrawal", "SZ-2026-004", "G1",
            "2026-11-15 10:35", withdraws_code="UP-004-GHOST"),

    # --- 005/G1（截止 2026-11-20 15:00）：截止后的撤回不生效，末份仍是 UP-005-2 ---
    Receipt("UP-005-1", "upload", "SZ-2026-005", "G1",
            "2026-11-02 09:05", _sha("SZ-2026-005/G1/bid-doc-v1")),
    Receipt("WD-005-1", "withdrawal", "SZ-2026-005", "G1",
            "2026-11-06 16:20", withdraws_code="UP-005-1"),
    Receipt("UP-005-2", "upload", "SZ-2026-005", "G1",
            "2026-11-12 11:45", _sha("SZ-2026-005/G1/bid-doc-v2")),
    Receipt("WD-005-LATE", "withdrawal", "SZ-2026-005", "G1",
            "2026-11-21 09:00", withdraws_code="UP-005-2"),
]


def _detail_page(a: Announcement) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def _list_page(items: list[Announcement], page: int, total_pages: int) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def _receipt_detail_page(r: Receipt) -> str:
    kind_label = "上传回执" if r.kind == "upload" else "撤回回执"
    title = f"{r.original_code}-{r.section_code} 标书{kind_label}（{r.code}）"
    rows = [
        ("回执编号", r.code),
        ("回执类型", kind_label),
        ("原公告编号", r.original_code),
        ("标段号", r.section_code),
        ("受理时间", r.accepted_at),
        ("投标企业", r.bidder),
    ]
    if r.kind == "upload":
        rows.append(("文件摘要SHA-256", r.file_sha256 or ""))
    if r.kind == "withdrawal":
        rows.append(("撤回上传编号", r.withdraws_code or ""))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
</head>
<body>
  <article class="receipt-detail" data-code="{html.escape(r.code)}"
           data-kind="{r.kind}"
           data-original-code="{html.escape(r.original_code)}"
           data-section-code="{html.escape(r.section_code)}"
           data-accepted-at="{html.escape(r.accepted_at)}"
           {f'data-withdraws="{html.escape(r.withdraws_code)}"' if r.withdraws_code else ""}>
    <h1 class="detail-title">{html.escape(title)}</h1>
    <table class="detail-table">
{trs}
    </table>
    <p><a href="receipt_list_1.html">返回回执目录</a></p>
  </article>
</body>
</html>
"""


def _receipt_list_page(items: list[Receipt], page: int, total_pages: int) -> str:
    lis = []
    for r in items:
        kind_label = "上传" if r.kind == "upload" else "撤回"
        lis.append(
            f'      <li class="receipt-item" data-code="{html.escape(r.code)}">\n'
            f'        <a class="receipt-link" href="{html.escape(r.filename)}">'
            f'{html.escape(r.original_code)}-{html.escape(r.section_code)} '
            f'{kind_label}回执 {html.escape(r.code)}</a>\n'
            f'        <span class="accepted-at">{html.escape(r.accepted_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(
                f'<a class="page-link" href="receipt_list_{p}.html">{p}</a>'
            )
    next_link = (
        f'<a class="page-next" href="receipt_list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>标书上传/撤回回执目录 - 第 {page} 页</title>
</head>
<body>
  <h1>标书上传/撤回回执目录（第 {page}/{total_pages} 页）</h1>
  <ul class="receipt-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(ANNOUNCEMENTS, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE

    for a in ordered:
        (OUT_DIR / a.filename).write_text(_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (OUT_DIR / f"list_{page}.html").write_text(
            _list_page(chunk, page, total_pages), encoding="utf-8"
        )

    # 回执子站：按受理时间倒序分页（最新记录在第一页，制造跨页乱序）
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    receipts_ordered = sorted(RECEIPTS, key=lambda r: r.accepted_at, reverse=True)
    receipt_pages = (
        len(receipts_ordered) + RECEIPT_PAGE_SIZE - 1
    ) // RECEIPT_PAGE_SIZE
    for r in receipts_ordered:
        (RECEIPT_DIR / r.filename).write_text(
            _receipt_detail_page(r), encoding="utf-8"
        )
    for page in range(1, receipt_pages + 1):
        chunk = receipts_ordered[
            (page - 1) * RECEIPT_PAGE_SIZE: page * RECEIPT_PAGE_SIZE
        ]
        (RECEIPT_DIR / f"receipt_list_{page}.html").write_text(
            _receipt_list_page(chunk, page, receipt_pages), encoding="utf-8"
        )

    (OUT_DIR / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…"
        '<p><a href="receipts/receipt_list_1.html">标书回执目录</a></p>'
        "</body></html>\n",
        encoding="utf-8",
    )
    print(
        f"generated {len(ordered)} detail pages, {total_pages} list pages; "
        f"{len(receipts_ordered)} receipt pages in {RECEIPT_DIR}"
    )


if __name__ == "__main__":
    main()
