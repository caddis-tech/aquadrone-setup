# Aquadrone sensor bridge, packaged as a BlueOS Extension.
#
# Build context is the REPO ROOT, not bridge/ — VERSION lives at the root and
# has to land beside the bridge script in the image (see _read_version()).
#
#   docker buildx build --platform linux/arm/v7 -f bridge/Dockerfile .
#
# linux/arm/v7 is not a preference. The BlueBoat runs kernel 5.10.33-v7l+, i.e.
# 32-bit ARMv7 userland, despite the Pi 4's 64-bit-capable CPU. An arm64 image
# installs cleanly and then refuses to start with an exec-format error.
FROM python:3.11-slim-bookworm

# ---------------------------------------------------------------------------
# BlueOS / Kraken extension metadata.
#
# `permissions` is the Docker create-container config Kraken applies on install.
# It is modelled on bluerobotics.cellular-modem-manager, which performs the same
# USB CDC ACM passthrough on this exact hardware — so this is a permission set
# already proven in production on a BlueBoat, not one we invented.
# ---------------------------------------------------------------------------
LABEL version="1.2.0"
LABEL permissions='{\
  "HostConfig": {\
    "Privileged": true,\
    "NetworkMode": "host",\
    "Binds": [\
      "/dev:/dev:rw",\
      "/usr/blueos/extensions/aquadrone:/app/data:rw",\
      "/media/sensor_data:/media/sensor_data:rw"\
    ],\
    "ExtraHosts": ["blueos.internal:host-gateway"]\
  }\
}'
LABEL authors='[{"name":"Caddis Tech","email":"michael.klobutcher@gmail.com"}]'
LABEL company='{"name":"Caddis Tech","about":"Aquadrone water-quality telemetry"}'
LABEL type="device-integration"
LABEL tags='["telemetry","sensors","water-quality"]'
LABEL links='{"source":"https://github.com/caddis-tech/AquadronePicoFirmwareExperimental"}'
LABEL requirements="core >= 1.1"

WORKDIR /app

# Requirements first so the (slow, emulated) armv7 pip layer caches across builds.
COPY bridge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bridge/aquadrone_bridge.py /app/aquadrone_bridge.py
COPY VERSION /app/VERSION

# The Pico is found by USB vendor ID (0x2E8A), never by enumeration order, so
# SERIAL_PORT stays deliberately unset — /dev is bound in whole and the bridge
# picks its own board out of it. Hardcoding ttyACM0 here would be the one way to
# make this extension grab the autopilot or the cellular modem's AT port.
ENV QUEUE_FALLBACK_DIR=/app/data/queue
ENV SD_MOUNT_PATH=/media/sensor_data
ENV MAVLINK2REST_URL=http://127.0.0.1:6040

# /app/data is the persistent bind mount (/usr/blueos/extensions/aquadrone on the
# host). Two things live there and must outlive the container: the durable upload
# queue — unsent readings — and .env, which holds the API token. WORKDIR anchors the
# queue's relative fallback here and keeps a `docker pull` of a new version from
# discarding readings that have not reached caddis-api yet.
#
# The token is NOT read via WORKDIR: load_dotenv() with no path resolves .env from the
# script's own directory (/app), not the working directory, so the bridge loads an
# EXPLICIT /app/data/.env path at import (see aquadrone_bridge.py). AQUADRONE_DATA_DIR
# overrides that path if the bind mount ever moves.
WORKDIR /app/data

CMD ["python", "/app/aquadrone_bridge.py"]
