# ScoutOSS demo pipeline: lightweight session QC, launched by XNAT Container Service.
FROM python:3.12.12-slim
RUN pip install --no-cache-dir pydicom==3.0.2 && useradd -u 10001 -r -s /usr/sbin/nologin qc
COPY qc.py /app/qc.py
USER 10001
ENTRYPOINT ["python", "/app/qc.py"]
