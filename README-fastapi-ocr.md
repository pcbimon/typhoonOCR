# FastAPI OCR PDF Upload (Ollama)

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

## API

### Health

```bash
curl http://localhost:8002/health
```

### Upload PDF แล้ว OCR ทีละหน้า

```bash
curl -X POST "http://localhost:8002/ocr/upload-pdf?start_page=1&end_page=3&dpi=220&num_predict=4096" \
  -H "accept: application/json" \
  -F "file=@./contract.pdf;type=application/pdf"
```

## Notes

- API นี้ OCR แบบต่อหน้า เพื่อหลีกเลี่ยง context ใหญ่เกิน
- ถ้าหน้าแน่นมาก แนะนำเริ่มที่ `num_predict=4096`
- ถ้า timeout บ่อย ให้ลองลด `dpi` หรือ OCR ทีละช่วงหน้า
