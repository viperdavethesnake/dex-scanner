FROM python:3.12-slim
WORKDIR /app
# curl needed for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl libgomp1 && rm -rf /var/lib/apt/lists/*
COPY dex-trader/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dex-trader/ .
COPY analysis/features.py .
CMD ["python", "-u", "main.py"]
