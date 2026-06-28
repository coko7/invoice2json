# 🧾 invoice2json

Turns a PDF invoice or photo of a receipt into structured JSON using a local
[Ollama](https://ollama.com) vision model — no cloud APIs, no data leaving your machine.

---

## Quick start

### 1. Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- A vision-capable model pulled, e.g.:

```bash
ollama pull llava          # default model used by the API
# or
ollama pull llava:13b      # more accurate, needs ~10 GB VRAM
# or
ollama pull moondream      # very fast, lower accuracy
```

For PDF support you also need **poppler-utils** (only needed if you don't install `pypdfium2`):

```bash
# macOS
brew install poppler

# Ubuntu / Debian
sudo apt install poppler-utils
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt

# Optional: faster PDF rasterisation
pip install pypdfium2
```

### 3. Run the server

```bash
uvicorn main:app --reload --port 8000
```

Interactive docs: <http://localhost:8000/docs>

---

## API reference

### `POST /extract`

Upload a file and get back structured JSON.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file` | form-data file | — | PDF, PNG, JPG, or WEBP |
| `model` | query string | `llava` | Ollama model name |

**Example — curl:**

```bash
# Image
curl -s -X POST "http://localhost:8000/extract" \
     -F "file=@receipt.jpg" | jq .

# PDF
curl -s -X POST "http://localhost:8000/extract" \
     -F "file=@invoice.pdf" | jq .

# Use a different model
curl -s -X POST "http://localhost:8000/extract?model=llava:13b" \
     -F "file=@invoice.pdf" | jq .
```

**Example — Python:**

```python
import httpx

with open("invoice.pdf", "rb") as f:
    resp = httpx.post(
        "http://localhost:8000/extract",
        files={"file": ("invoice.pdf", f, "application/pdf")},
    )
resp.raise_for_status()
data = resp.json()
print(data["total_amount"])
print(data["line_items"])
```

**Example response:**

```json
{
  "document_type": "invoice",
  "vendor": {
    "name": "Acme Corp",
    "address": "123 Main St, Springfield, IL 62701",
    "phone": "555-867-5309",
    "email": "billing@acme.example",
    "website": null,
    "tax_id": "12-3456789"
  },
  "customer": {
    "name": "Jane Smith",
    "address": "456 Oak Ave, Portland, OR 97201",
    "email": null,
    "account_number": "CUST-00412"
  },
  "document_number": "INV-2024-0087",
  "document_date": "2024-03-15",
  "due_date": "2024-04-14",
  "currency": "USD",
  "line_items": [
    { "description": "Widget Pro × 3", "quantity": 3, "unit_price": 49.99, "total": 149.97 },
    { "description": "Shipping", "quantity": 1, "unit_price": 9.95, "total": 9.95 }
  ],
  "subtotal": 159.92,
  "tax_rate": 8.5,
  "tax_amount": 13.59,
  "discount_amount": null,
  "total_amount": 173.51,
  "amount_paid": 0,
  "amount_due": 173.51,
  "payment_method": null,
  "notes": "Net 30 payment terms",
  "_source_file": "invoice.pdf",
  "_model_used": "llava",
  "_pages_processed": 1
}
```

### `GET /models`

Lists all models currently available in your local Ollama instance.

```bash
curl http://localhost:8000/models | jq .
```

### `GET /health`

Liveness check — returns `{"status": "ok"}`.

---

## Multi-page PDFs

PDFs are rasterised page-by-page (up to 10 pages by default, configurable via
`MAX_PDF_PAGES` in `main.py`). Line items are merged across all pages; scalar
fields (totals, vendor name, etc.) use the first non-null value found.

## Tips

- **Accuracy**: `llava:13b` or `llava:34b` are noticeably more accurate than the
  default 7B for dense invoices. `moondream` is fast but misses fields more often.
- **Scanned / low-res images**: pre-process with an upscaler or ensure the source
  is at least 150 DPI before uploading.
- **Batch processing**: wrap the `/extract` endpoint in an async loop with
  `asyncio.gather()` or use a task queue (Celery, ARQ) for large volumes.
