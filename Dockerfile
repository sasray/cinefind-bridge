FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    CINEFIND_BRIDGE_CONFIG_PATH=/data/config.json

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py ./

# TrueNAS custom-app ixVolumes are created root-owned. Running this tiny
# outbound-only worker as the container default user lets it persist its
# revocable device token at /data/config.json without a privileged host mount.
# It exposes no host port, device, capability, or shell service.
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["python", "bridge.py"]
