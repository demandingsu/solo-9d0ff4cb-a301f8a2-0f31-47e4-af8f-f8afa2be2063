from __future__ import annotations

from pydantic import BaseModel, Field

SHA256_PATTERN = r"^[A-Fa-f0-9]{64}$"


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ReceiptCheckRequest(BaseModel):
    original_code: str = Field(
        ..., min_length=1, description="已采集的原公告编号（如 SZ-2026-001）"
    )
    section_code: str = Field(
        ..., min_length=1, description="标段号（如 G1）"
    )
    file_sha256: str = Field(
        ...,
        pattern=SHA256_PATTERN,
        description="手头投标文件的 SHA-256（64 位十六进制）",
    )
    receipt_list_url: str = Field(
        ..., min_length=1, description="回执目录列表页地址（服务会自动跟随分页链接）"
    )


class ReceiptOut(BaseModel):
    code: str
    kind: str
    accepted_at: str
    file_sha256: str | None
    withdraws_code: str | None
    detail_url: str


class EffectiveUploadOut(BaseModel):
    code: str
    kind: str
    accepted_at: str
    file_sha256: str | None
    withdraws_code: str | None
    detail_url: str


class ReceiptCheckResponse(BaseModel):
    status: str = Field(
        ...,
        description=(
            "matched=匹配；mismatch=不匹配；no_valid_upload=没有有效上传；"
            "undecidable=无法判定（摘要冲突/撤回悬空/页面抓取失败等）"
        ),
    )
    file_matches: bool | None
    message: str
    original_code: str
    section_code: str
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    receipt_list_url: str
    effective_upload: EffectiveUploadOut | None
    valid_receipts: list[ReceiptOut]
    source_urls: list[str]
    ignored_after_deadline: list[ReceiptOut]
    reasons: list[str]


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str
