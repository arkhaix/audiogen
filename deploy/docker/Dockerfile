# The serial_audio web app for the containers VM. It runs quick library jobs
# itself and dispatches the rest to the Nomad cluster (deploy/README.md).
# Build from the repo root:
#   docker build -f deploy/docker/Dockerfile -t serial-audio:local .
FROM python:3.13.17-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml==6.0.3

WORKDIR /app
COPY serial_audio /app/serial_audio
COPY prompts /app/prompts
COPY schemas /app/schemas
RUN mkdir -p /app/library /app/.webapp /app/incoming

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
ENTRYPOINT ["python", "-u", "-m", "serial_audio.web"]
CMD ["--host", "0.0.0.0", "--executor", "nomad"]
