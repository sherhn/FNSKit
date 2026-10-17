# Alpine removes the unused Perl runtime with unfixed CVEs from Debian slim.
# The digest pins the reviewed base image so rebuilds cannot silently change it.
FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# PyMuPDF's musllinux wheel links against libstdc++; tiff-tools supplies tiffcp.
RUN apk add --no-cache libstdc++ tiff-tools

WORKDIR /service

COPY requirements.txt .
RUN pip install --requirement requirements.txt

RUN adduser -D -u 10001 processor
COPY --chown=processor:processor app ./app

USER processor

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
