# PolyYield — container image
#
# Builds a single image that runs the FastAPI dashboard + background scan/settlement
# loops via `python main.py` (see main.py's __main__ block). Persistent state (the
# SQLite database and the Fernet encryption key for the Key Vault) lives under
# /app/data — mount that path as a volume in production so it survives redeploys
# and restarts. NEVER bake real secrets into the image; pass them as environment
# variables / platform secrets at runtime instead.

FROM python:3.11-slim

WORKDIR /app

# build-essential/libffi/libssl as a fallback in case a platform lacks a prebuilt
# wheel for cryptography/coincurve on this architecture; safe to keep even when
# unused since it's discarded from the final layer cache footprint by apt cleanup.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data directory — mount a volume here in production.
RUN mkdir -p /app/data
ENV SQLITE_PATH=/app/data/poly_yield.db

# Must bind 0.0.0.0 inside a container, or nothing outside the container can reach it.
ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

CMD ["python", "main.py"]
