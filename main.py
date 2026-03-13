from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "contract-ocr-write-package-service"
    ollama_base_url: str = "http://localhost:11434"
    ollama_stage1_model: str = "gemma3:27b"
    ollama_stage2_model: str = "gemma3:27b"
    ollama_timeout_connect_seconds: int = 10
    ollama_timeout_read_seconds: int = 300
    default_dpi: int = 220
    default_num_ctx: int = 8192
    default_num_predict: int = 4096
    default_temperature_stage1: float = 0.0
    default_temperature_stage2: float = 0.0


settings = Settings()
app = FastAPI(title=settings.app_name, version="0.2.0")


class DocType(str, Enum):
    origin_letter = "origin_letter"
    tor = "tor"
    draft_contract = "draft_contract"
    cr04 = "cr04"
    proposal_tech_price = "proposal_tech_price"


class OcrDocStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


class OcrPageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


class OcrReviewStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


THAI_MONTHS = (
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
)

STAGE1_OCR_PROMPT = """Extract all text from the image.

Instructions:
- Return ONLY clean Markdown.
- No explanations, no code fences.
- Include all visible text on the page.
- Keep tables as HTML <table>...</table> where possible.
- Keep signatures/seals/figures as <figure>...</figure> with brief Thai descriptions.
- Remove page numbers if obviously decorative only.
"""

STAGE2_NORMALIZE_PROMPT = """You are given Markdown extracted from a Thai contract or official document by OCR.

Transform it into a clean canonical Markdown WITHOUT changing meaning:
- Keep the document title at the top if present.
- If a main clause heading begins with 'ข้อ' + number, normalize it as a heading line.
- Merge split heading numbers and titles when obvious.
- Keep <table> and <figure> blocks.
- Fix broken spacing and duplicated blank lines.
- Return ONLY the final Markdown.
"""


class HealthResponse(BaseModel):
    ok: bool
    app_name: str
    ollama_base_url: str
    ollama_stage1_model: str
    ollama_stage2_model: str


class OcrPageWrite(BaseModel):
    caseId: str
    docType: DocType
    docVersionId: str
    pageNo: int
    ocrStatus: OcrPageStatus
    ocrRunStartedAt: str | None = None
    textStage1Raw: str = ""
    textStage2Normalized: str = ""
    textSystem: str = ""
    textEdited: str | None = None
    hasEdits: bool = False
    imageBucket: str = "local-upload"
    imageKey: str
    hallucinationScore: float = 0.0
    hallucinationFlags: list[str] = Field(default_factory=list)
    reviewStatus: OcrReviewStatus = OcrReviewStatus.UNVERIFIED
    reviewReason: str | None = None
    verifiedAt: str | None = None
    verifiedByUserId: str | None = None
    lastError: str | None = None


class PageExtractionWrite(BaseModel):
    caseId: str
    docType: DocType
    docVersionId: str
    pageNo: int
    data: dict[str, Any]
    parserMeta: dict[str, Any] | None = None


class DocExtractionWrite(BaseModel):
    caseId: str
    docType: DocType
    docVersionId: str
    fields: dict[str, Any]
    fieldSources: list[dict[str, Any]] | None = None
    computed: dict[str, Any] | None = None
    sourceFingerprint: str
    stale: bool = False


class ScreeningExtractionWrite(BaseModel):
    caseId: str
    draftDocVersionId: str
    partyAName: str | None = None
    partyAType: str | None = None
    partyBName: str | None = None
    partyBType: str | None = None
    recommendation: str | None = None
    recommendationReason: str | None = None
    signals: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    computedAt: str


class OcrDocSummaryWrite(BaseModel):
    caseId: str
    docType: DocType
    docVersionId: str
    ocrStatus: OcrDocStatus
    totalPages: int
    readyPages: int
    failedPages: int
    verifiedPages: int
    needsReviewPages: int
    unverifiedPages: int
    lastUpdatedAt: str


class Telemetry(BaseModel):
    pageCount: int
    stage1Model: str
    stage2Model: str
    durationMs: int | None = None


class WritePackageData(BaseModel):
    schemaVersion: str = "ocr-write-package.v1"
    caseId: str
    docType: DocType
    docVersionId: str
    docSummaryUpsert: OcrDocSummaryWrite
    ocrPagesUpsert: list[OcrPageWrite]
    pageExtractionsUpsert: list[PageExtractionWrite]
    docExtractionUpsert: DocExtractionWrite
    screeningExtractionUpsert: ScreeningExtractionWrite | None = None
    telemetry: Telemetry


class WritePackageResponse(BaseModel):
    ok: bool
    data: WritePackageData


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        ok=True,
        app_name=settings.app_name,
        ollama_base_url=settings.ollama_base_url,
        ollama_stage1_model=settings.ollama_stage1_model,
        ollama_stage2_model=settings.ollama_stage2_model,
    )


@app.post("/v1/ocr/extract-upload", response_model=WritePackageResponse)
@app.post("/ocr/upload-pdf", response_model=WritePackageResponse)
async def extract_upload_pdf(
    caseId: str = Query(..., min_length=1),
    docType: DocType = Query(...),
    docVersionId: str = Query(..., min_length=1),
    file: UploadFile = File(...),
    start_page: int = Query(1, ge=1),
    end_page: int | None = Query(None, ge=1),
    dpi: int = Query(settings.default_dpi, ge=120, le=400),
    num_ctx: int = Query(settings.default_num_ctx, ge=2048, le=65536),
    num_predict: int = Query(settings.default_num_predict, ge=512, le=8192),
    temperature_stage1: float = Query(settings.default_temperature_stage1, ge=0.0, le=1.0),
    temperature_stage2: float = Query(settings.default_temperature_stage2, ge=0.0, le=1.0),
    enable_stage2: bool = Query(True),
    include_screening: bool = Query(True),
) -> WritePackageResponse:
    validate_upload(file)

    temp_dir = Path(tempfile.gettempdir()) / "contract-ocr-service"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_pdf_path = temp_dir / f"{uuid.uuid4()}.pdf"

    try:
        temp_pdf_path.write_bytes(await file.read())
        total_pages = get_pdf_page_count(temp_pdf_path)
        resolved_end_page = min(end_page or total_pages, total_pages)
        if start_page > resolved_end_page:
            raise HTTPException(status_code=400, detail="start_page ต้องไม่มากกว่า end_page")

        started = now_iso()
        ocr_pages: list[OcrPageWrite] = []
        page_extractions: list[PageExtractionWrite] = []

        for page_no in range(start_page, resolved_end_page + 1):
            image_bytes = render_pdf_page_to_jpeg_bytes(temp_pdf_path, page_no - 1, dpi=dpi)
            image_key = f"{caseId}/{docVersionId}/page-{page_no}.jpg"

            try:
                md_raw = call_ollama_chat_image(
                    image_bytes=image_bytes,
                    model=settings.ollama_stage1_model,
                    prompt=STAGE1_OCR_PROMPT,
                    num_ctx=num_ctx,
                    num_predict=num_predict,
                    temperature=temperature_stage1,
                )

                md_norm = md_raw
                if enable_stage2:
                    md_norm = call_ollama_chat_text(
                        input_text=md_raw,
                        model=settings.ollama_stage2_model,
                        prompt=STAGE2_NORMALIZE_PROMPT,
                        num_ctx=num_ctx,
                        num_predict=num_predict,
                        temperature=temperature_stage2,
                    )

                canonical_text = normalize_markdown(md_norm)
                hallucination_flags = detect_hallucination_flags(canonical_text)
                hallucination_score = min(1.0, round(len(hallucination_flags) * 0.15, 2))

                ocr_pages.append(
                    OcrPageWrite(
                        caseId=caseId,
                        docType=docType,
                        docVersionId=docVersionId,
                        pageNo=page_no,
                        ocrStatus=OcrPageStatus.READY,
                        ocrRunStartedAt=started,
                        textStage1Raw=md_raw,
                        textStage2Normalized=md_norm,
                        textSystem=canonical_text,
                        imageKey=image_key,
                        hallucinationScore=hallucination_score,
                        hallucinationFlags=hallucination_flags,
                    )
                )

                page_extractions.append(
                    PageExtractionWrite(
                        caseId=caseId,
                        docType=docType,
                        docVersionId=docVersionId,
                        pageNo=page_no,
                        data=extract_page_data(canonical_text, page_no),
                        parserMeta={
                            "extractorVersion": "v1",
                            "source": "ollama-microservice",
                            "normalized": enable_stage2,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                ocr_pages.append(
                    OcrPageWrite(
                        caseId=caseId,
                        docType=docType,
                        docVersionId=docVersionId,
                        pageNo=page_no,
                        ocrStatus=OcrPageStatus.FAILED,
                        ocrRunStartedAt=started,
                        textStage1Raw="",
                        textStage2Normalized="",
                        textSystem="",
                        imageKey=image_key,
                        reviewStatus=OcrReviewStatus.NEEDS_REVIEW,
                        reviewReason="OCR failed",
                        hallucinationScore=1.0,
                        hallucinationFlags=["OCR_FAILED"],
                        lastError=str(exc),
                    )
                )
                page_extractions.append(
                    PageExtractionWrite(
                        caseId=caseId,
                        docType=docType,
                        docVersionId=docVersionId,
                        pageNo=page_no,
                        data={"entities": [], "snippets": [], "warnings": ["OCR_FAILED"]},
                        parserMeta={"extractorVersion": "v1", "source": "ollama-microservice", "error": str(exc)},
                    )
                )

        doc_extraction = build_doc_extraction(caseId, docType, docVersionId, ocr_pages)
        screening = None
        if include_screening and docType == DocType.draft_contract:
            screening = build_screening_extraction(caseId, docVersionId, doc_extraction)

        ready_pages = sum(1 for p in ocr_pages if p.ocrStatus == OcrPageStatus.READY)
        failed_pages = sum(1 for p in ocr_pages if p.ocrStatus == OcrPageStatus.FAILED)
        verified_pages = sum(1 for p in ocr_pages if p.reviewStatus == OcrReviewStatus.VERIFIED)
        needs_review_pages = sum(1 for p in ocr_pages if p.reviewStatus == OcrReviewStatus.NEEDS_REVIEW)
        unverified_pages = sum(1 for p in ocr_pages if p.reviewStatus == OcrReviewStatus.UNVERIFIED)
        doc_status = OcrDocStatus.READY if failed_pages == 0 else OcrDocStatus.FAILED

        return WritePackageResponse(
            ok=True,
            data=WritePackageData(
                caseId=caseId,
                docType=docType,
                docVersionId=docVersionId,
                docSummaryUpsert=OcrDocSummaryWrite(
                    caseId=caseId,
                    docType=docType,
                    docVersionId=docVersionId,
                    ocrStatus=doc_status,
                    totalPages=resolved_end_page - start_page + 1,
                    readyPages=ready_pages,
                    failedPages=failed_pages,
                    verifiedPages=verified_pages,
                    needsReviewPages=needs_review_pages,
                    unverifiedPages=unverified_pages,
                    lastUpdatedAt=now_iso(),
                ),
                ocrPagesUpsert=ocr_pages,
                pageExtractionsUpsert=page_extractions,
                docExtractionUpsert=doc_extraction,
                screeningExtractionUpsert=screening,
                telemetry=Telemetry(
                    pageCount=resolved_end_page - start_page + 1,
                    stage1Model=settings.ollama_stage1_model,
                    stage2Model=settings.ollama_stage2_model if enable_stage2 else settings.ollama_stage1_model,
                    durationMs=None,
                ),
            ),
        )
    finally:
        try:
            temp_pdf_path.unlink(missing_ok=True)
        except OSError:
            pass


def validate_upload(file: UploadFile) -> None:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()

    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ .pdf")

    if content_type and content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail=f"content-type ไม่รองรับ: {content_type}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_pdf_page_count(pdf_path: Path) -> int:
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def render_pdf_page_to_jpeg_bytes(pdf_path: Path, page_index: int, dpi: int) -> bytes:
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("jpeg")
    finally:
        doc.close()


def call_ollama_chat_image(
    image_bytes: bytes,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int,
    temperature: float,
) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    return call_ollama(payload)


def call_ollama_chat_text(
    input_text: str,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int,
    temperature: float,
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": f"{prompt}\n\n{input_text}"}],
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    return call_ollama(payload)


def call_ollama(payload: dict[str, Any]) -> str:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=(settings.ollama_timeout_connect_seconds, settings.ollama_timeout_read_seconds),
        )
        response.raise_for_status()
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError("เชื่อมต่อ Ollama ไม่ทันเวลา") from exc
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError("Ollama ใช้เวลาประมวลผลนานเกินไป") from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"เรียก Ollama ไม่สำเร็จ: {exc}") from exc

    data = response.json()
    content = data.get("message", {}).get("content")
    if not content:
        raise RuntimeError("Ollama response missing message.content")
    return content.strip()


def normalize_markdown(md: str) -> str:
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    lines = md.splitlines()
    out: list[str] = []

    clause_only = re.compile(r"^ข้อ\s*([0-9๐-๙]+)\.?\s*$")
    clause_inline = re.compile(r"^ข้อ\s*([0-9๐-๙]+)\.?\s*(.+)$")

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            out.append("")
            continue

        m_only = clause_only.match(s)
        if m_only and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt:
                out.append(f"## ข้อ {m_only.group(1)}. {nxt}")
                lines[i + 1] = ""
                continue

        m_inline = clause_inline.match(s)
        if m_inline:
            out.append(f"## ข้อ {m_inline.group(1)}. {m_inline.group(2).strip()}")
            continue

        out.append(raw.rstrip())

    text = "\n".join(out).strip()
    return text + "\n"


def detect_hallucination_flags(text: str) -> list[str]:
    flags: list[str] = []
    if text.count("[อ่านไม่ชัด]") > 10:
        flags.append("MANY_UNCLEAR_TOKENS")
    if text.count("```") > 0:
        flags.append("UNEXPECTED_CODE_FENCE")
    if len(text.strip()) < 20:
        flags.append("VERY_SHORT_OUTPUT")
    return flags


def extract_page_data(text: str, page_no: int) -> dict[str, Any]:
    headings = re.findall(r"^##\s+(.+)$", text, flags=re.MULTILINE)

    snippets = []
    for line in text.splitlines():
        s = line.strip()
        if s:
            snippets.append({"pageNo": page_no, "snippet": s[:300]})
        if len(snippets) >= 5:
            break

    entities = []
    for value in re.findall(r"(?:บริษัท|มหาวิทยาลัย|กรม|กระทรวง|องค์การ|สำนักงาน)[^\n]{0,80}", text):
        entities.append({"type": "ORG_CANDIDATE", "value": clean_inline(value)})

    return {
        "headings": headings[:20],
        "entities": entities[:20],
        "snippets": snippets,
    }


def build_doc_extraction(case_id: str, doc_type: DocType, doc_version_id: str, pages: list[OcrPageWrite]) -> DocExtractionWrite:
    successful_pages = [p for p in pages if p.ocrStatus == OcrPageStatus.READY]
    joined = "\n".join(p.textSystem for p in successful_pages)

    fields: dict[str, Any] = {}
    field_sources: list[dict[str, Any]] = []

    def set_field(name: str, value: str | None, snippet: str | None) -> None:
        if value and name not in fields:
            fields[name] = value
            if snippet:
                field_sources.append({"field": name, "pageNo": find_page(successful_pages, snippet), "snippet": snippet[:500]})

    title = None
    for page in successful_pages:
        for line in page.textSystem.splitlines():
            s = line.strip().lstrip("#").strip()
            if len(s) > 8 and ("สัญญา" in s or "หนังสือ" in s):
                title = s
                break
        if title:
            break
    set_field("documentTitle", title, title)

    project_match = re.search(r"(?:โครงการ|เรื่อง)\s*[:：]?\s*([^\n]+)", joined)
    set_field("projectName", clean_inline(project_match.group(1)) if project_match else None, project_match.group(0) if project_match else None)

    date_match = find_date(joined)
    if date_match:
        set_field("contractDate", date_match[0], date_match[2])

    party_a = find_party(joined, ["ฝ่ายที่หนึ่ง", "ผู้ว่าจ้าง", "ผู้จ้าง", "หน่วยงาน"])
    if party_a:
        set_field("partyAName", party_a[0], party_a[2])

    party_b = find_party(joined, ["ฝ่ายที่สอง", "ที่ปรึกษา", "ผู้รับจ้าง", "คู่สัญญา"])
    if party_b:
        set_field("partyBName", party_b[0], party_b[2])

    signatory = re.search(r"(?:ลงชื่อ|ผู้ลงนาม)\s*[:：]?\s*([^\n]+)", joined)
    set_field("signatoryName", clean_inline(signatory.group(1)) if signatory else None, signatory.group(0) if signatory else None)

    fingerprint = hashlib.sha256(joined.encode("utf-8")).hexdigest()

    return DocExtractionWrite(
        caseId=case_id,
        docType=doc_type,
        docVersionId=doc_version_id,
        fields=fields,
        fieldSources=field_sources or None,
        computed={
            "pageCount": len(pages),
            "successfulPages": len(successful_pages),
            "failedPages": len(pages) - len(successful_pages),
            "fieldCount": len(fields),
        },
        sourceFingerprint=f"sha256:{fingerprint}",
        stale=False,
    )


def build_screening_extraction(case_id: str, doc_version_id: str, doc: DocExtractionWrite) -> ScreeningExtractionWrite:
    party_a = doc.fields.get("partyAName")
    party_b = doc.fields.get("partyBName")
    party_a_type = infer_party_type(party_a)
    party_b_type = infer_party_type(party_b)

    recommendation = None
    recommendation_reason = None
    if party_a_type == "GOVERNMENT":
        recommendation = "LIKELY_GOV"
        recommendation_reason = "partyA มีคำบ่งชี้หน่วยงานรัฐ"
    elif party_b_type == "GOVERNMENT":
        recommendation = "LIKELY_GOV"
        recommendation_reason = "partyB มีคำบ่งชี้หน่วยงานรัฐ"
    elif party_a or party_b:
        recommendation = "NEED_REVIEW"
        recommendation_reason = "พบชื่อคู่สัญญาแต่ยังไม่ชัดพอ"

    signals = []
    for item in doc.fieldSources or []:
        if item.get("field") in {"partyAName", "partyBName"}:
            signals.append({"pageNo": item.get("pageNo"), "snippet": item.get("snippet")})

    return ScreeningExtractionWrite(
        caseId=case_id,
        draftDocVersionId=doc_version_id,
        partyAName=party_a,
        partyAType=party_a_type,
        partyBName=party_b,
        partyBType=party_b_type,
        recommendation=recommendation,
        recommendationReason=recommendation_reason,
        signals=signals,
        data={"source": "docExtraction"},
        computedAt=now_iso(),
    )


def infer_party_type(name: str | None) -> str | None:
    if not name:
        return None

    govt_keywords = ["มหาวิทยาลัย", "กระทรวง", "กรม", "องค์การ", "เทศบาล", "อบจ", "อบต", "สำนักงาน", "ราชการ"]
    private_keywords = ["บริษัท", "จำกัด", "มหาชน", "ห้างหุ้นส่วน", "เอกชน"]

    if any(k in name for k in govt_keywords):
        return "GOVERNMENT"
    if any(k in name for k in private_keywords):
        return "PRIVATE"
    return "UNKNOWN"


def find_party(text: str, labels: list[str]) -> tuple[str, int | None, str] | None:
    for label in labels:
        m = re.search(rf"{re.escape(label)}\s*[:：]?\s*([^\n]+)", text)
        if m:
            snippet = m.group(0)
            return clean_inline(m.group(1)), None, snippet
    return None


def find_date(text: str) -> tuple[str, int | None, str] | None:
    pattern = rf"([0-9๐-๙]{{1,2}}\s+(?:{'|'.join(THAI_MONTHS)})\s+[0-9๐-๙]{{2,4}})"
    m = re.search(pattern, text)
    if m:
        return m.group(1), None, m.group(0)

    m2 = re.search(r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", text)
    if m2:
        return m2.group(1), None, m2.group(0)

    return None


def find_page(pages: list[OcrPageWrite], snippet: str) -> int | None:
    for page in pages:
        if snippet in page.textSystem:
            return page.pageNo
    return None


def clean_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" :-—\t")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8002")), reload=True)