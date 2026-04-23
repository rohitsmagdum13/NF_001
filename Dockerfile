FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps required by: lxml, pdfplumber, ocrmypdf (Tesseract + Ghostscript),
# Playwright Chromium runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev libxslt1-dev \
        tesseract-ocr ghostscript qpdf unpaper pngquant \
        fonts-liberation libnss3 libxkbcommon0 libgbm1 libasound2 \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/inbox /app/data/cache /app/outputs

EXPOSE 4200 8501

CMD ["python", "main.py", "run"]
