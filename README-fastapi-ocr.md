# Contract OCR Write-Package Microservice (FastAPI + Ollama)

บริการนี้เป็น **OCR / extraction microservice แบบ stateless** สำหรับโปรเจกต์ **RA Contract Checker**
โดยรับไฟล์ PDF สแกน แล้วตอบกลับเป็น **write package** ที่พร้อมให้แอปหลักนำไป `upsert`
ลงฐานข้อมูลตาม schema ของระบบ เช่น `OcrDocSummary`, `OcrPage`, `PageExtraction`, `DocExtraction`
และ `ScreeningExtraction` (เฉพาะกรณี `docType=draft_contract`) 

> แนวคิดสำคัญ
> - microservice นี้ **ไม่เขียน DB เอง**
> - แอปหลัก / use-case layer เป็นคน validate, authorize, enqueue job และ upsert DB เอง
> - OCR ทำแบบ **ต่อหน้า** เพื่อหลีกเลี่ยง context overflow และ retry ได้รายหน้า

---

## 1) บทบาทของ service นี้ใน architecture

บริการนี้อยู่ในชั้น **compute / adapter** ของระบบ และเหมาะกับ flow แบบนี้

1. แอปหลักรับ upload หรือมี `DocumentVersion` แล้ว
2. แอปหลักเรียก service นี้พร้อม `caseId`, `docType`, `docVersionId`, และไฟล์ PDF
3. service นี้ render PDF เป็นภาพทีละหน้า
4. ส่งภาพแต่ละหน้าเข้า Ollama (vision OCR)
5. optional: normalize ข้อความด้วย stage 2
6. รวมผลเป็น **write package v1**
7. แอปหลักค่อยนำผลไป `upsert` ลงตารางของระบบ

### สิ่งที่ service นี้ทำ
- OCR แบบต่อหน้า
- normalize ข้อความ OCR
- สร้าง payload ต่อหน้าและต่อเอกสาร
- สร้าง extraction เบื้องต้นระดับหน้าและระดับเอกสาร
- สร้าง screening extraction เบื้องต้นสำหรับ `draft_contract`
- คืนผลลัพธ์เป็น JSON 100% serializable

### สิ่งที่ service นี้ไม่ทำ
- ไม่ตัดสิน `ScreeningDecision`
- ไม่รัน Phase 2 checks
- ไม่สร้าง comments / report export
- ไม่เขียน PostgreSQL / Prisma โดยตรง
- ไม่จัดการ auth / RBAC

---

## 2) ตารางที่ response นี้ออกแบบมาให้รองรับ

response ถูกออกแบบให้ map เข้ากับ schema หลักของระบบได้โดยตรง:

- `OcrDocSummary`
- `OcrPage`
- `PageExtraction`
- `DocExtraction`
- `ScreeningExtraction` (optional)

> หมายเหตุ: `ScreeningDecision`, `AnalysisRun`, `AnalysisCheckResult`, `CommentRun`, `CommentItem`
> เป็น phase ถัดไปและควรให้แอปหลักหรือ worker อื่นจัดการ

---

## 3) Endpoints

### 3.1 Health check

```bash
curl http://localhost:8002/health
```

ตัวอย่าง response:

```json
{
  "ok": true,
  "app_name": "contract-ocr-write-package-service",
  "ollama_base_url": "http://localhost:11434",
  "ollama_stage1_model": "gemma3:27b",
  "ollama_stage2_model": "gemma3:27b"
}
```

---

### 3.2 Main endpoint: upload PDF แล้วรับ write package

รองรับ 2 path เพื่อความเข้ากันได้:

- `POST /v1/ocr/extract-upload`
- `POST /ocr/upload-pdf` (alias เดิม)

#### Query params

| param | required | description |
|---|---:|---|
| `caseId` | ✅ | id ของเคส |
| `docType` | ✅ | `origin_letter \| tor \| draft_contract \| cr04 \| proposal_tech_price` |
| `docVersionId` | ✅ | id ของ document version |
| `start_page` | - | หน้าเริ่มต้นแบบ 1-based |
| `end_page` | - | หน้าสิ้นสุดแบบ 1-based |
| `dpi` | - | ค่า render DPI ของ PDF เป็นภาพ |
| `num_ctx` | - | context window ที่ส่งให้ Ollama |
| `num_predict` | - | output budget ของ model |
| `temperature_stage1` | - | temperature สำหรับ OCR stage |
| `temperature_stage2` | - | temperature สำหรับ normalize stage |
| `enable_stage2` | - | เปิด/ปิด text normalization |
| `include_screening` | - | สร้าง screening extraction ถ้าเป็น `draft_contract` |

#### Multipart body

- `file`: PDF file (`application/pdf`)

#### ตัวอย่างเรียก

```bash
curl -X POST "http://localhost:8002/v1/ocr/extract-upload?caseId=case_001&docType=draft_contract&docVersionId=ver_001&start_page=1&end_page=3&dpi=220&num_predict=4096&enable_stage2=true&include_screening=true" \
  -H "accept: application/json" \
  -F "file=@./contract.pdf;type=application/pdf"
```

---

## 4) Response contract

response หลักเป็น `WritePackageResponse`

```json
{
  "ok": true,
  "data": {
    "schemaVersion": "ocr-write-package.v1",
    "caseId": "case_001",
    "docType": "draft_contract",
    "docVersionId": "ver_001",
    "docSummaryUpsert": {},
    "ocrPagesUpsert": [],
    "pageExtractionsUpsert": [],
    "docExtractionUpsert": {},
    "screeningExtractionUpsert": {},
    "telemetry": {}
  }
}
```

### 4.1 `docSummaryUpsert`
สรุประดับเอกสารสำหรับ upsert ลง `OcrDocSummary`

ฟิลด์หลัก:
- `ocrStatus`
- `totalPages`
- `readyPages`
- `failedPages`
- `verifiedPages`
- `needsReviewPages`
- `unverifiedPages`
- `lastUpdatedAt`

### 4.2 `ocrPagesUpsert`
รายการต่อหน้าสำหรับ upsert ลง `OcrPage`

ฟิลด์หลักต่อหน้า:
- `caseId`, `docType`, `docVersionId`, `pageNo`
- `ocrStatus`
- `ocrRunStartedAt`
- `textStage1Raw`
- `textStage2Normalized`
- `textSystem`
- `textEdited`
- `hasEdits`
- `imageBucket`, `imageKey`
- `hallucinationScore`, `hallucinationFlags`
- `reviewStatus`, `reviewReason`
- `verifiedAt`, `verifiedByUserId`
- `lastError`

### 4.3 `pageExtractionsUpsert`
รายการ extraction ต่อหน้าสำหรับ `PageExtraction`

ฟิลด์หลัก:
- `caseId`, `docType`, `docVersionId`, `pageNo`
- `data`
- `parserMeta`

### 4.4 `docExtractionUpsert`
extraction รวมระดับเอกสารสำหรับ `DocExtraction`

ฟิลด์หลัก:
- `fields`
- `fieldSources`
- `computed`
- `sourceFingerprint`
- `stale`

### 4.5 `screeningExtractionUpsert`
มีเฉพาะเมื่อ
- `include_screening=true`
- `docType=draft_contract`

ฟิลด์หลัก:
- `partyAName`, `partyAType`
- `partyBName`, `partyBType`
- `recommendation`
- `recommendationReason`
- `signals`
- `data`
- `computedAt`

### 4.6 `telemetry`
ข้อมูลประกอบสำหรับ logging / observability

ตัวอย่าง:
- `pageCount`
- `stage1Model`
- `stage2Model`
- `durationMs`

---

## 5) ตัวอย่าง response แบบย่อ

```json
{
  "ok": true,
  "data": {
    "schemaVersion": "ocr-write-package.v1",
    "caseId": "case_001",
    "docType": "draft_contract",
    "docVersionId": "ver_001",
    "docSummaryUpsert": {
      "caseId": "case_001",
      "docType": "draft_contract",
      "docVersionId": "ver_001",
      "ocrStatus": "READY",
      "totalPages": 3,
      "readyPages": 3,
      "failedPages": 0,
      "verifiedPages": 0,
      "needsReviewPages": 0,
      "unverifiedPages": 3,
      "lastUpdatedAt": "2026-03-13T08:00:00+00:00"
    },
    "ocrPagesUpsert": [
      {
        "caseId": "case_001",
        "docType": "draft_contract",
        "docVersionId": "ver_001",
        "pageNo": 1,
        "ocrStatus": "READY",
        "ocrRunStartedAt": "2026-03-13T07:59:30+00:00",
        "textStage1Raw": "...",
        "textStage2Normalized": "...",
        "textSystem": "# สัญญา...",
        "textEdited": null,
        "hasEdits": false,
        "imageBucket": "local-upload",
        "imageKey": "case_001/ver_001/page-1.jpg",
        "hallucinationScore": 0.0,
        "hallucinationFlags": [],
        "reviewStatus": "UNVERIFIED",
        "reviewReason": null,
        "verifiedAt": null,
        "verifiedByUserId": null,
        "lastError": null
      }
    ],
    "pageExtractionsUpsert": [
      {
        "caseId": "case_001",
        "docType": "draft_contract",
        "docVersionId": "ver_001",
        "pageNo": 1,
        "data": {
          "headings": ["ข้อ 1. ข้อตกลงว่าจ้าง"],
          "entities": [],
          "snippets": []
        },
        "parserMeta": {
          "extractorVersion": "v1",
          "source": "ollama-microservice",
          "normalized": true
        }
      }
    ],
    "docExtractionUpsert": {
      "caseId": "case_001",
      "docType": "draft_contract",
      "docVersionId": "ver_001",
      "fields": {
        "documentTitle": "สัญญาจ้าง...",
        "partyAName": "มหาวิทยาลัย...",
        "partyBName": "บริษัท..."
      },
      "fieldSources": [],
      "computed": {
        "pageCount": 3,
        "successfulPages": 3,
        "failedPages": 0,
        "fieldCount": 3
      },
      "sourceFingerprint": "sha256:...",
      "stale": false
    },
    "screeningExtractionUpsert": {
      "caseId": "case_001",
      "draftDocVersionId": "ver_001",
      "partyAName": "มหาวิทยาลัย...",
      "partyAType": "GOVERNMENT",
      "partyBName": "บริษัท...",
      "partyBType": "PRIVATE",
      "recommendation": "LIKELY_GOV",
      "recommendationReason": "partyA มีคำบ่งชี้หน่วยงานรัฐ",
      "signals": [],
      "data": {
        "source": "docExtraction"
      },
      "computedAt": "2026-03-13T08:00:00+00:00"
    },
    "telemetry": {
      "pageCount": 3,
      "stage1Model": "gemma3:27b",
      "stage2Model": "gemma3:27b",
      "durationMs": null
    }
  }
}
```

---

## 6) Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

---

## 7) Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

หรือ

```bash
python main.py
```

---

## 8) Environment variables

ตัวอย่าง `.env.example`

```env
APP_NAME=contract-ocr-write-package-service
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_STAGE1_MODEL=gemma3:27b
OLLAMA_STAGE2_MODEL=gemma3:27b
OLLAMA_TIMEOUT_CONNECT_SECONDS=10
OLLAMA_TIMEOUT_READ_SECONDS=300
DEFAULT_DPI=220
DEFAULT_NUM_CTX=8192
DEFAULT_NUM_PREDICT=4096
DEFAULT_TEMPERATURE_STAGE1=0.0
DEFAULT_TEMPERATURE_STAGE2=0.0
PORT=8002
```

> ถ้าคุณใช้ 2-stage จริง ควรตั้ง stage 1 เป็น vision OCR model และ stage 2 เป็น text model

---

## 9) Validation / Error behavior

### รองรับเฉพาะ PDF
- filename ต้องลงท้าย `.pdf`
- content-type ควรเป็น `application/pdf` หรือ `application/octet-stream`

### Behavior เมื่อ OCR บางหน้าล้ม
service จะยังคงตอบ `ok: true` ได้ ถ้า request โดยรวมทำงานสำเร็จ แต่ใน `ocrPagesUpsert` ของหน้าที่ล้มจะได้:

- `ocrStatus = FAILED`
- `textStage1Raw = ""`
- `textStage2Normalized = ""`
- `textSystem = ""`
- `reviewStatus = NEEDS_REVIEW`
- `hallucinationFlags = ["OCR_FAILED"]`
- `lastError = "..."`

แนวทางนี้ช่วยให้แอปหลักยังสามารถ upsert ข้อมูลรายหน้าได้ และรองรับ retry เฉพาะหน้าที่ fail ภายหลัง

---

## 10) Recommended write path ในแอปหลัก

แนะนำให้ฝั่งแอปหลักจัดการแบบนี้:

1. validate `caseId`, `docType`, `docVersionId`
2. ตรวจว่า `Document` / `activeVersion` ตรงกับ request
3. สร้างหรืออัปเดต `Job` = `RUNNING`
4. เรียก OCR microservice นี้
5. upsert `OcrPage` ทีละหน้า
6. upsert `PageExtraction` ทีละหน้า
7. upsert `DocExtraction`
8. ถ้าเป็น `draft_contract` ค่อย upsert `ScreeningExtraction`
9. recompute / upsert `OcrDocSummary`
10. ปิด `Job` เป็น `SUCCEEDED` หรือ `FAILED`

---

## 11) Operational notes

### OCR แบบต่อหน้าเท่านั้น
ไม่ควรส่งหลายหน้ารวมกันเข้า model ใน request เดียว เพราะจะเสี่ยง timeout และ context overflow

### ค่าที่แนะนำสำหรับหนังสือสัญญา
- `num_predict=4096` เป็น default ที่เหมาะกับหน้าสัญญาทั่วไป
- ถ้าหน้าหนักมากค่อยขยับขึ้น
- ถ้า timeout บ่อยให้ลด `dpi` หรือแบ่งช่วงหน้าให้สั้นลง

### Timeout
- connect timeout ควรสั้น
- read timeout ควรยาวกว่าปกติ เพราะ OCR ใช้เวลาประมวลผล

### Stateless by design
service นี้ไม่เก็บ session และไม่เก็บ state งานระยะยาว
เหมาะให้เรียกจาก worker หรือ use-case layer

---

## 12) Mapping กับ workflow Phase 1–3

### Phase 1
- รับเอกสารและ OCR
- สร้าง screening extraction สำหรับ `draft_contract`

### Phase 2
- ใช้ `DocExtraction` และ `PageExtraction` เป็น input ให้ deterministic checks

### Phase 3
- ใช้ผล checks ไป derive comments และรายงานส่งออก

---

## 13) Future improvements

แนะนำสำหรับรอบถัดไป:

- แยก stage 1 / stage 2 adapter คนละไฟล์ชัดเจน
- ใส่ structured logging ต่อหน้า เช่น `pageNo`, `requestBytes`, `durationMs`
- รองรับ storage reference แทนการ upload file ตรง
- รองรับ async job mode
- เพิ่ม `/v1/ocr/extract-from-storage`
- เพิ่ม retry API รายหน้า
- เพิ่ม metrics เช่น success rate / failed page count / avg duration per page

---

## 14) Quick test checklist

- [ ] `/health` ตอบกลับได้
- [ ] upload PDF 1–3 หน้าแล้วได้ `schemaVersion = ocr-write-package.v1`
- [ ] `ocrPagesUpsert.length` ตรงกับจำนวนหน้าที่ประมวลผล
- [ ] หน้า fail มี `ocrStatus=FAILED` และ `lastError`
- [ ] `draft_contract` + `include_screening=true` ได้ `screeningExtractionUpsert`
- [ ] response ทุก field เป็น JSON serializable

