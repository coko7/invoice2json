"""
Invoice / Receipt → JSON extractor
Uses a local Ollama vision model to parse uploaded files.

Supported inputs: PDF, PNG, JPG, JPEG, WEBP
Run with:  uvicorn main:app --reload --port 8000
"""

import base64
import io
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pdfplumber
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL   = "llava"          # change to any Ollama vision model you have
MAX_PDF_PAGES   = 10               # safety cap; receipts are almost always 1-2 pages
IMAGE_MAX_LONG  = 1600             # resize long edge to this before sending to model
IMAGE_QUALITY   = 88               # JPEG quality for the resized image

EXTRACTION_PROMPT = """
You are a data-extraction assistant.  Examine this invoice or receipt image carefully
and return ONLY a JSON object — no markdown, no code fences, no explanation.
The invoice/receipt may be Swedish/English/French.

The JSON must follow this exact schema (use null for any field you cannot find):

{
  "document_type": "invoice" | "receipt" | "unknown",
  "vendor": {
    "name": string | null,
    "address": string | null,
    "phone": string | null,
    "email": string | null,
    "website": string | null,
    "tax_id": string | null
  },
  "customer": {
    "name": string | null,
    "address": string | null,
    "email": string | null,
    "account_number": string | null
  },
  "document_number": string | null,
  "document_date": string | null,
  "due_date": string | null,
  "currency": string | null,
  "line_items": [
    {
      "description": string,
      "quantity": number | null,
      "unit_price": number | null,
      "total": number | null
    }
  ],
  "subtotal": number | null,
  "tax_rate": number | null,
  "tax_amount": number | null,
  "discount_amount": number | null,
  "total_amount": number | null,
  "amount_paid": number | null,
  "amount_due": number | null,
  "payment_method": string | null,
  "notes": string | null
}

Return ONLY the JSON object.
""".strip()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Invoice / Receipt Extractor",
    description="Upload a PDF or image; get back structured JSON via a local Ollama vision model.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def _validate_upload(file: UploadFile) -> None:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )


def _pdf_pages_to_images(data: bytes, max_pages: int = MAX_PDF_PAGES) -> list[Image.Image]:
    """Rasterise each PDF page to a PIL Image using pdfplumber + pillow."""
    images: list[Image.Image] = []
    try:
        import pypdfium2 as pdfium  # faster if available

        pdf = pdfium.PdfDocument(data)
        for i, page in enumerate(pdf):
            if i >= max_pages:
                break
            bitmap = page.render(scale=2)          # 144 DPI
            images.append(bitmap.to_pil())
    except ImportError:
        # Fallback: write to a temp file and use pdf2image / pdftoppm
        import subprocess, shutil

        if shutil.which("pdftoppm") is None:
            raise HTTPException(
                status_code=500,
                detail="pdftoppm not found. Install poppler-utils or pypdfium2 to process PDFs.",
            )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "input.pdf"
            pdf_path.write_bytes(data)
            out_prefix = Path(tmp) / "page"
            subprocess.run(
                ["pdftoppm", "-jpeg", "-r", "144",
                 "-f", "1", "-l", str(max_pages),
                 str(pdf_path), str(out_prefix)],
                check=True, capture_output=True,
            )
            for img_path in sorted(Path(tmp).glob("page-*.jpg")):
                images.append(Image.open(img_path).copy())
    return images


def _resize_image(img: Image.Image) -> Image.Image:
    """Downscale if the long edge exceeds IMAGE_MAX_LONG."""
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= IMAGE_MAX_LONG:
        return img
    scale = IMAGE_MAX_LONG / long_edge
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _image_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=IMAGE_QUALITY)
    return base64.b64encode(buf.getvalue()).decode()


def _call_ollama(model: str, b64_image: str) -> dict[str, Any]:
    """Send one image to Ollama and return the parsed JSON dict."""
    payload = {
        "model": model,
        "prompt": EXTRACTION_PROMPT,
        "images": [b64_image],
        "stream": False,
        "options": {"temperature": 0},   # deterministic extraction
    }
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach Ollama at {OLLAMA_BASE_URL}. Is it running?",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Ollama error: {exc.response.text}")

    raw_text: str = resp.json().get("response", "")
    return _parse_json_from_text(raw_text)


def _parse_json_from_text(text: str) -> dict[str, Any]:
    """Extract a JSON object from the model's raw output."""
    # Strip markdown code fences if the model ignores the instruction
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a {...} block anywhere in the output
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    raise HTTPException(
        status_code=422,
        detail=f"Model returned output that could not be parsed as JSON: {text[:300]!r}",
    )


def _merge_page_results(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """
    For multi-page PDFs merge line items and keep the best values for scalar fields.
    Simple strategy: first non-null wins for scalars; line_items are concatenated.
    """
    if len(pages) == 1:
        return pages[0]

    merged = pages[0].copy()
    merged["line_items"] = list(merged.get("line_items") or [])

    for page in pages[1:]:
        # Append any extra line items found on later pages
        merged["line_items"].extend(page.get("line_items") or [])
        # Fill in nulls from later pages
        for key, value in page.items():
            if key == "line_items":
                continue
            if merged.get(key) is None and value is not None:
                merged[key] = value

    merged["_pages_processed"] = len(pages)
    return merged


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Quick liveness check."""
    return {"status": "ok"}


@app.get("/models")
def list_models():
    """Return vision-capable models currently available in Ollama."""
    try:
        resp = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return {"models": models}
    except httpx.ConnectError:
        raise HTTPException(503, f"Cannot reach Ollama at {OLLAMA_BASE_URL}")


@app.post("/extract")
async def extract(
    file: UploadFile = File(..., description="PDF or image of an invoice / receipt"),
    model: str = Query(DEFAULT_MODEL, description="Ollama vision model to use"),
) -> JSONResponse:
    """
    Upload an invoice or receipt (PDF or image) and receive structured JSON.

    - **file**: PDF, PNG, JPG, or WEBP
    - **model**: Ollama model name (default: llava). Must be a vision-capable model.
    """
    _validate_upload(file)

    raw = await file.read()
    ext = Path(file.filename or "").suffix.lower()

    # --- Convert input to a list of PIL images ---
    if ext == ".pdf":
        images = _pdf_pages_to_images(raw)
        if not images:
            raise HTTPException(400, "PDF appears to be empty or unreadable.")
    else:
        try:
            images = [Image.open(io.BytesIO(raw))]
        except Exception as exc:
            raise HTTPException(400, f"Cannot open image: {exc}")

    # --- Run extraction on each page ---
    page_results: list[dict[str, Any]] = []
    for img in images:
        img = _resize_image(img)
        b64 = _image_to_b64(img)
        result = _call_ollama(model, b64)
        page_results.append(result)

    # --- Merge multi-page results ---
    final = _merge_page_results(page_results)
    final["_source_file"] = file.filename
    final["_model_used"] = model

    return JSONResponse(content=final)
