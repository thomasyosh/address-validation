FROM python:3.11-slim

WORKDIR /app

# Corporate proxy during image build (Jenkins passes --build-arg).
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY address_validation/ address_validation/
COPY main.py .
COPY config.example.yaml .
COPY scripts/docker-entrypoint.sh scripts/docker-entrypoint.sh

RUN chmod +x scripts/docker-entrypoint.sh

ENV DATA_DIR=/data

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["validate", "--compare-with-previous", "--max-rate-delta", "1", "--label", "docker"]
