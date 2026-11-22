# ScoutOSS demo pipeline: lightweight session QC, launched by XNAT Container Service.
FROM python:3.12.12-slim
# Pick up Debian security fixes released after the pinned base image (CI Trivy gate).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pydicom==3.0.2 pillow==13.0.0 && useradd -u 10001 -r -s /usr/sbin/nologin qc
COPY qc.py /app/qc.py
USER 10001
ENTRYPOINT ["python", "/app/qc.py"]
