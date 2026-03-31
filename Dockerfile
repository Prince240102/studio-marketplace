FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl jq rsync unzip \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY scripts /app/scripts

ENV PYTHONUNBUFFERED=1
ENV MARKETPLACE_HOST=0.0.0.0
ENV MARKETPLACE_PORT=3001

EXPOSE 5006

CMD ["python", "-m", "app.main"]
