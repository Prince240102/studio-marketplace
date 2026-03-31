FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl jq rsync unzip git \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

# Clone the repo for scripts only
RUN git clone --depth 1 https://github.com/Prince240102/studio-marketplace.git /tmp/repo \
  && mkdir -p /app/scripts \
  && cp /tmp/repo/scripts/sync_from_github.sh /app/scripts/ \
  && chmod +x /app/scripts/sync_from_github.sh \
  && rm -rf /tmp/repo

# Create empty plugins dir (populated at runtime via update button)
RUN mkdir -p /app/plugins

ENV PYTHONUNBUFFERED=1
ENV MARKETPLACE_HOST=0.0.0.0
ENV MARKETPLACE_PORT=3001

EXPOSE 5006

CMD ["python", "-m", "app.main"]
