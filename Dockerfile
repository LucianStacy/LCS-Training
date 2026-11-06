FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LCS_HOST=0.0.0.0 \
    LCS_PORT=8765 \
    LCS_DATA_DIR=/data \
    LCS_NO_BROWSER=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lcs_training ./lcs_training

# Client data (database + uploaded spreadsheets) lives on a volume, never in the image.
RUN useradd --create-home --uid 10001 lcs && mkdir -p /data && chown lcs /data
USER lcs
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/static/style.css')" || exit 1

CMD ["python", "-m", "lcs_training"]
