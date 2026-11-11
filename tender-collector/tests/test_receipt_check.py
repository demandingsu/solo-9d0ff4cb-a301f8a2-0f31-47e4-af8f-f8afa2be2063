"""回执核对端到端 + 纯判定单元测试。

端到端：线程内静态 HTTP 服务器托管生成的示例站（含 receipts/ 回执子站），
通过 FastAPI TestClient 走完整的 采集 -> 回执核对 流程，并支持故障注入
验证“相关页面抓取失败 -> 无法判定”。
"""
from __future__ import annotations

import functools
import http.server
import importlib
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"

# 加载示例数据生成器（脚本名带连字符，用 importlib）
spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


# ---------------------------------------------------------------------------
# 静态文件服务器（支持可选的故障注入）
# ---------------------------------------------------------------------------
class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    fail_paths: set[str] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(scope="module")
def demo_server():
    handler = functools.partial(FlakyHandler)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


def _collect(c, base):
    resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _check(c, base, original_code, section_code, file_sha):
    return c.post(
        "/api/receipt-check",
        json={
            "original_code": original_code,
            "section_code": section_code,
            "file_sha256": file_sha,
            "receipt_list_url": f"{base}/receipts/receipt_list_1.html",
        },
    )


# ---------------------------------------------------------------------------
# 端到端：核对结论
# ---------------------------------------------------------------------------
def test_matched_file_and_deadline_filtering(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(
        c, base, "SZ-2026-001", "G1",
        gen.SAMPLE_FILE_HASHES["SZ-2026-001/G1"],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 现行截止时间取自更正公告 SZ-2026-001-G1
    assert data["current_deadline"] == "2026-11-25 14:30"
    assert data["deadline_source_code"] == "SZ-2026-001-G1"
    assert data["status"] == "matched"
    assert data["file_matches"] is True
    # 末份未撤回上传 = UP-001-3（UP-001-1/2 均已撤回）
    assert data["effective_upload"]["code"] == "UP-001-3"
    # 截止时间之后的上传被忽略并显式列出
    assert [r["code"] for r in data["ignored_after_deadline"]] == ["UP-001-LATE"]
    # 有效回执含全部截止前上传 + 两条可解析撤回，来源链接齐全
    codes = {r["code"] for r in data["valid_receipts"]}
    assert codes == {"UP-001-1", "UP-001-2", "UP-001-3", "WD-001-1", "WD-001-2"}
    assert all(u.startswith("http://") for u in data["source_urls"])
    assert len(data["source_urls"]) == len(codes)


def test_mismatch_when_handheld_file_differs(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(c, base, "SZ-2026-001", "G1", "a" * 64)
    data = resp.json()
    assert data["status"] == "mismatch"
    assert data["file_matches"] is False
    assert data["effective_upload"]["code"] == "UP-001-3"


def test_undecidable_when_same_accepted_at_hashes_differ(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(c, base, "SZ-2026-002", "G1", gen._sha("whatever"))
    data = resp.json()
    assert data["status"] == "undecidable"
    assert data["file_matches"] is None
    assert data["effective_upload"] is None
    assert any("同一受理时间" in r and "摘要不一致" in r for r in data["reasons"])


def test_section_isolation_ignores_other_section_noise(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(
        c, base, "SZ-2026-002", "G2",
        gen.SAMPLE_FILE_HASHES["SZ-2026-002/G2"],
    )
    data = resp.json()
    assert data["status"] == "matched"
    assert data["effective_upload"]["code"] == "UP-002G2-2"
    # G9 标段噪声不得混入 G2 的有效回执
    got = {r["code"] for r in data["valid_receipts"]}
    assert got == {"UP-002G2-1", "UP-002G2-2", "WD-002G2-1"}
    assert "UP-002G9-1" not in got
    # 撤回生效：UP-002G2-1 不在末份候选
    assert data["effective_upload"]["file_sha256"] == gen.SAMPLE_FILE_HASHES[
        "SZ-2026-002/G2"
    ]


def test_no_valid_upload_when_all_withdrawn(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(c, base, "SZ-2026-003", "G1", gen._sha("handheld"))
    data = resp.json()
    assert data["status"] == "no_valid_upload"
    assert data["file_matches"] is None
    assert data["effective_upload"] is None
    assert "全部被撤回" in data["message"]
    # 回执本身仍然有效、来源链接仍返回
    assert {r["code"] for r in data["valid_receipts"]} == {
        "UP-003-1", "UP-003-2", "WD-003-1", "WD-003-2",
    }


def test_undecidable_when_withdrawal_references_missing_upload(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(c, base, "SZ-2026-004", "G1", gen._sha("handheld"))
    data = resp.json()
    assert data["status"] == "undecidable"
    assert data["file_matches"] is None
    assert any("UP-004-GHOST" in r for r in data["reasons"])
    # 悬空撤回不计入有效回执；上传仍保留
    assert {r["code"] for r in data["valid_receipts"]} == {"UP-004-1"}


def test_withdrawal_after_deadline_does_not_take_effect(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(
        c, base, "SZ-2026-005", "G1",
        gen.SAMPLE_FILE_HASHES["SZ-2026-005/G1"],
    )
    data = resp.json()
    assert data["status"] == "matched"
    assert data["effective_upload"]["code"] == "UP-005-2"
    # 截止时间之后受理的撤回被忽略，UP-005-2 仍视为有效
    assert [r["code"] for r in data["ignored_after_deadline"]] == ["WD-005-LATE"]


def test_scrape_failure_marks_undecidable_not_422(client, demo_server):
    c, _, _ = client
    base, _, handler = demo_server
    _collect(c, base)

    handler.fail_paths = {"/receipts/receipt_list_2.html"}
    try:
        resp = _check(c, base, "SZ-2026-001", "G1", "a" * 64)
    finally:
        handler.fail_paths = set()
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "undecidable"
    assert data["file_matches"] is None
    assert any("receipt_list_2" in r for r in data["reasons"])


def test_detail_page_failure_marks_undecidable(client, demo_server):
    c, _, _ = client
    base, _, handler = demo_server
    _collect(c, base)

    handler.fail_paths = {"/receipts/UP-001-3.html"}
    try:
        resp = _check(c, base, "SZ-2026-001", "G1", "a" * 64)
    finally:
        handler.fail_paths = set()
    data = resp.json()
    assert data["status"] == "undecidable"
    assert any("UP-001-3" in r for r in data["reasons"])


def test_unknown_original_code_returns_422(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(c, base, "SZ-2099-999", "G1", "a" * 64)
    assert resp.status_code == 422
    assert "SZ-2099-999" in resp.json()["detail"]


def test_invalid_sha256_rejected(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _check(c, base, "SZ-2026-001", "G1", "not-a-sha256")
    assert resp.status_code == 422


def test_check_requires_prior_collection(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    # 未采集任何公告
    resp = _check(c, base, "SZ-2026-001", "G1", "a" * 64)
    assert resp.status_code == 422
    assert "先调用" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 纯判定单元测试
# ---------------------------------------------------------------------------
def _receipt(code, kind, accepted_at, **kw):
    row = {
        "code": code,
        "kind": kind,
        "original_code": "P-1",
        "section_code": "G1",
        "accepted_at": accepted_at,
        "file_sha256": None,
        "withdraws_code": None,
        "detail_url": f"http://x/{code}.html",
    }
    row.update(kw)
    return row


def test_pure_result_independent_of_input_order():
    from app.receipt_merge import verify_receipts

    rows = [
        _receipt("U1", "upload", "2026-11-01 09:00", file_sha256="a" * 64),
        _receipt("U2", "upload", "2026-11-02 09:00", file_sha256="b" * 64),
        _receipt("W1", "withdrawal", "2026-11-03 09:00", withdraws_code="U2"),
        _receipt("U3", "upload", "2026-11-04 09:00", file_sha256="c" * 64),
    ]
    kwargs = dict(
        original_code="P-1", section_code="G1",
        deadline_text="2026-11-10 09:00", file_sha256="c" * 64,
    )
    baseline = verify_receipts(rows, **kwargs).as_dict()
    shuffled = list(reversed(rows))
    import random

    random.Random(7).shuffle(shuffled)
    assert verify_receipts(shuffled, **kwargs).as_dict() == baseline
    assert baseline["status"] == "matched"
    assert baseline["effective_upload"]["code"] == "U3"


def test_receipt_exactly_at_deadline_is_accepted():
    from app.receipt_merge import MATCHED, verify_receipts

    rows = [
        _receipt("U1", "upload", "2026-11-10 09:00", file_sha256="a" * 64),
        # 晚一分钟的撤回不生效
        _receipt("W1", "withdrawal", "2026-11-10 09:01", withdraws_code="U1"),
    ]
    result = verify_receipts(
        rows, original_code="P-1", section_code="G1",
        deadline_text="2026-11-10 09:00", file_sha256="a" * 64,
    )
    assert result.status == MATCHED
    assert [r["code"] for r in result.ignored_after_deadline] == ["W1"]


def test_same_accepted_at_same_digest_is_decidable():
    from app.receipt_merge import MATCHED, verify_receipts

    rows = [
        _receipt("U1", "upload", "2026-11-01 09:00", file_sha256="a" * 64),
        _receipt("U2", "upload", "2026-11-01 09:00", file_sha256="A" * 64),
    ]
    result = verify_receipts(
        rows, original_code="P-1", section_code="G1",
        deadline_text="2026-11-10 09:00", file_sha256="a" * 64,
    )
    # 摘要仅大小写不同视为一致；编号次序决定末份
    assert result.status == MATCHED
    assert result.effective_upload["code"] == "U2"


def test_empty_scope_is_no_valid_upload():
    from app.receipt_merge import NO_VALID_UPLOAD, verify_receipts

    result = verify_receipts(
        [], original_code="P-1", section_code="G1",
        deadline_text="2026-11-10 09:00", file_sha256="a" * 64,
    )
    assert result.status == NO_VALID_UPLOAD
    assert result.file_matches is None


def test_all_rows_after_deadline_is_no_valid_upload():
    from app.receipt_merge import NO_VALID_UPLOAD, verify_receipts

    rows = [
        _receipt("U1", "upload", "2026-11-11 09:00", file_sha256="a" * 64),
    ]
    result = verify_receipts(
        rows, original_code="P-1", section_code="G1",
        deadline_text="2026-11-10 09:00", file_sha256="a" * 64,
    )
    assert result.status == NO_VALID_UPLOAD
    assert [r["code"] for r in result.ignored_after_deadline] == ["U1"]


def test_withdrawal_unknown_code_is_undecidable_even_if_upload_matches():
    from app.receipt_merge import UNDECIDABLE, verify_receipts

    rows = [
        _receipt("U1", "upload", "2026-11-01 09:00", file_sha256="a" * 64),
        _receipt("W1", "withdrawal", "2026-11-02 09:00", withdraws_code="GHOST"),
    ]
    result = verify_receipts(
        rows, original_code="P-1", section_code="G1",
        deadline_text="2026-11-10 09:00", file_sha256="a" * 64,
    )
    assert result.status == UNDECIDABLE
    assert result.file_matches is None


# ---------------------------------------------------------------------------
# 抓取层解析测试（直接解析生成的 HTML）
# ---------------------------------------------------------------------------
def test_scraper_parses_generated_receipt_pages():
    from app.receipt_scraper import _parse_detail, _parse_list

    list_html = (DEMO_DIR / "receipts" / "receipt_list_1.html").read_text(
        encoding="utf-8"
    )
    details, pages = _parse_list(
        list_html, "http://demo/receipts/receipt_list_1.html"
    )
    assert len(details) == gen.RECEIPT_PAGE_SIZE
    assert {u.rsplit("/", 1)[-1] for u in pages} == {
        "receipt_list_2.html", "receipt_list_3.html", "receipt_list_4.html",
    }

    up_html = (DEMO_DIR / "receipts" / "UP-001-3.html").read_text(encoding="utf-8")
    up = _parse_detail(up_html, "http://demo/receipts/UP-001-3.html")
    assert up.kind == "upload"
    assert up.section_code == "G1"
    assert up.original_code == "SZ-2026-001"
    assert len(up.file_sha256) == 64

    wd_html = (DEMO_DIR / "receipts" / "WD-001-1.html").read_text(encoding="utf-8")
    wd = _parse_detail(wd_html, "http://demo/receipts/WD-001-1.html")
    assert wd.kind == "withdrawal"
    assert wd.withdraws_code == "UP-001-2"
    assert wd.file_sha256 is None
