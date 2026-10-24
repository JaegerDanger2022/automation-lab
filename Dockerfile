# Stage 1: build the Close Desk test set (seeded, so identical every time):
# invoices, labels and the seed migration
FROM python:3.12-slim AS samples
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir reportlab pillow numpy
WORKDIR /gen
COPY data/closedesk/generate.py .
RUN python generate.py \
    && for f in invoices/*.pdf; do pdftoppm -png -r 90 -singlefile "$f" "${f%.pdf}_preview"; done

# Stage 2: the API
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lab ./lab
COPY db ./db
COPY --from=samples /gen/seed.sql ./db/migrations/003_closedesk_seed.sql
COPY --from=samples /gen/invoices ./data/closedesk/invoices
COPY --from=samples /gen/labels.json ./data/closedesk/labels.json
CMD ["sh", "-c", "uvicorn lab.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
