FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data
ENV PUBLIC_BASE_URL=https://bywaymap.hornesys.co.uk
ENV OVERPASS_URL=https://overpass-api.de/api/interpreter
ENV UPDATE_HOUR=3
ENV MIN_ZOOM=7
ENV MAX_ZOOM=16
ENV PREGENERATE_MAX_ZOOM=12
ENV TRO_MATCH_METRES=75
ENV ADMIN_USERNAME=admin
ENV ADMIN_PASSWORD=changeme-now

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
