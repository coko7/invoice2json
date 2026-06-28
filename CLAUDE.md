# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

`invoice2json` is a single-file FastAPI service that accepts PDF invoices or receipt images and returns structured JSON by passing the image to a locally-running Ollama vision model. No cloud APIs are used.

## Setup

```bash
pip install -r requirements.txt
# Optional: faster PDF rasterisation
pip install pypdfium2
```

For PDF support without `pypdfium2`, install `poppler-utils` (provides `pdftoppm`).

Ollama must be running locally (`ollama serve`) with at least one vision model pulled (default: `llava`).

## Running the server

```bash
uvicorn main:app --reload --port 8000
```

Interactive API docs at <http://localhost:8000/docs>.

## Testing endpoints manually

```bash
# Health check
curl http://localhost:8000/health

# List available Ollama models
curl http://localhost:8000/models | jq .

# Extract from an image
curl -s -X POST "http://localhost:8000/extract" -F "file=@data/receipt.png" | jq .

# Extract from a PDF
curl -s -X POST "http://localhost:8000/extract" -F "file=@data/plane-booking.pdf" | jq .

# Use a specific model
curl -s -X POST "http://localhost:8000/extract?model=llava:13b" -F "file=@data/receipt.png" | jq .
```

## Architecture

Everything lives in `main.py`. The pipeline for a single request:

1. **Validate** — `_validate_upload()` checks file extension against the allowlist.
2. **Decode to images** — `_pdf_pages_to_images()` rasterises PDFs page-by-page (tries `pypdfium2` first, falls back to `pdftoppm`). Images are opened directly with Pillow.
3. **Resize** — `_resize_image()` caps the long edge at `IMAGE_MAX_LONG` (1600 px) before encoding.
4. **Encode** — `_image_to_b64()` converts each PIL image to a base64 JPEG string.
5. **Call Ollama** — `_call_ollama()` posts to `http://localhost:11434/api/generate` with `stream: false` and `temperature: 0`.
6. **Parse** — `_parse_json_from_text()` strips markdown fences and extracts the JSON blob from the model's raw text output.
7. **Merge** — `_merge_page_results()` concatenates `line_items` across pages and takes the first non-null value for all scalar fields.

Key constants at the top of `main.py` control behaviour:
- `OLLAMA_BASE_URL` — Ollama endpoint
- `DEFAULT_MODEL` — model used when the `model` query param is omitted
- `MAX_PDF_PAGES` — safety cap on pages processed per PDF (default 10)
- `IMAGE_MAX_LONG` / `IMAGE_QUALITY` — resize and JPEG quality settings
- `EXTRACTION_PROMPT` — the full prompt sent to the model; edit here to change output schema

The output JSON schema is defined entirely within `EXTRACTION_PROMPT` as a plain-text description. There is no Pydantic model validating the model's response — `_parse_json_from_text()` does a best-effort parse and raises HTTP 422 on failure.

## Sample data

`data/` contains test files: `receipt.png`, `receipt_hemkop.jpg`, and `plane-booking.pdf`.
