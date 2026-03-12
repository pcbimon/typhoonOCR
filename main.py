from __future__ import annotations

import base64
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "contract-ocr-service"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:27b"
    ollama_timeout_connect_seconds: int = 10
    ollama_timeout_read_seconds: int = 300
    default_dpi: int = 220
    default_num_ctx: int = 8192
    default_num_predict: int = 4096
    default_temperature: float = 0.0


settings = Settings()
app = FastAPI(title=settings.app_name, version="0.1.0")


OCR_PROMPT = """คุณเป็น OCR assistant สำหรับเอกสารราชการไทยและหนังสือสัญญา
งานของคุณคือถอดข้อความจากภาพนี้ให้ครบถ้วนที่สุด โดยยึดตามต้นฉบับ
กติกา:
1) ห้ามสรุปความ
2) ห้ามตีความเพิ่ม
3) รักษาลำดับบรรทัดและย่อหน้า
4) ถ้าอ่านไม่ชัด ให้ใส่ [อ่านไม่ชัด]
5) คงเลขไทย/เลขอารบิกตามต้นฉบับ
6) ตอบเป็น plain text เท่านั้น
"""


class OcrPageResult(BaseModel):
    page_no: int
    success: bool
    text: str | None = None
    error: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    total_duration_ns: int | None = None


class OcrPdfResponse(BaseModel):
    filename: str
    total_pages: int
    processed_pages: int
    results: list[OcrPageResult]


class HealthResponse(BaseModel):
    ok: bool
    app_name: str
    ollama_base_url: str
    ollama_model: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        ok=True,
        app_name=settings.app_name,
        ollama_base_url=settings.ollama_base_url,
        ollama_model=settings.ollama_model,
    )


@app.post("/ocr/upload-pdf", response_model=OcrPdfResponse)
async def ocr_upload_pdf(
    file: UploadFile = File(...),
    start_page: int = Query(1, ge=1, description="เลขหน้าเริ่มต้นแบบ 1-based"),
    end_page: int | None = Query(None, ge=1, description="เลขหน้าสิ้นสุดแบบ 1-based"),
    dpi: int = Query(settings.default_dpi, ge=120, le=400),
    model: str = Query(settings.ollama_model),
    num_ctx: int = Query(settings.default_num_ctx, ge=2048, le=65536),
    num_predict: int = Query(settings.default_num_predict, ge=512, le=8192),
    temperature: float = Query(settings.default_temperature, ge=0.0, le=1.0),
) -> OcrPdfResponse:
    validate_upload(file)

    suffix = Path(file.filename or "upload.pdf").suffix or ".pdf"
    temp_dir = Path(tempfile.gettempdir()) / "contract-ocr-service"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_pdf_path = temp_dir / f"{uuid.uuid4()}{suffix}"

    try:
        temp_pdf_path.write_bytes(await file.read())
        total_pages = get_pdf_page_count(temp_pdf_path)

        resolved_end_page = min(end_page or total_pages, total_pages)
        if start_page > resolved_end_page:
            raise HTTPException(status_code=400, detail="start_page ต้องไม่มากกว่า end_page")

        results: list[OcrPageResult] = []
        for page_no in range(start_page, resolved_end_page + 1):
            try:
                image_bytes = render_pdf_page_to_jpeg_bytes(temp_pdf_path, page_no - 1, dpi=dpi)
                page_result = call_ollama_ocr(
                    image_bytes=image_bytes,
                    model=model,
                    num_ctx=num_ctx,
                    num_predict=num_predict,
                    temperature=temperature,
                )
                results.append(
                    OcrPageResult(
                        page_no=page_no,
                        success=True,
                        text=page_result.get("text"),
                        prompt_eval_count=page_result.get("prompt_eval_count"),
                        eval_count=page_result.get("eval_count"),
                        total_duration_ns=page_result.get("total_duration_ns"),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                results.append(OcrPageResult(page_no=page_no, success=False, error=str(exc)))

        return OcrPdfResponse(
            filename=file.filename or temp_pdf_path.name,
            total_pages=total_pages,
            processed_pages=len(results),
            results=results,
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



def call_ollama_ocr(
    image_bytes: bytes,
    model: str,
    num_ctx: int,
    num_predict: int,
    temperature: float,
) -> dict[str, Any]:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": OCR_PROMPT,
                "images": [image_b64],
            }
        ],
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }

    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=(
                settings.ollama_timeout_connect_seconds,
                settings.ollama_timeout_read_seconds,
            ),
        )
        response.raise_for_status()
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError("เชื่อมต่อ Ollama ไม่ทันเวลา") from exc
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError("Ollama ใช้เวลาประมวลผลนานเกินไป") from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"เรียก Ollama ไม่สำเร็จ: {exc}") from exc

    data = response.json()
    text = data.get("message", {}).get("content")
    if not text:
        raise RuntimeError("Ollama response missing message.content")

    return {
        "text": text,
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
        "total_duration_ns": data.get("total_duration"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8002")), reload=True)
