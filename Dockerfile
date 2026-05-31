FROM python:3.12-slim

# JRE for the ThetaData Terminal + curl for the readiness probe + bash for the entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless curl bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# ThetaTerminal.jar is NOT baked in (it's ThetaData's binary) — mount it at /opt via compose.
ENTRYPOINT ["/app/entrypoint.sh"]
