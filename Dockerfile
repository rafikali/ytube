FROM python:3.12-slim

WORKDIR /app

# yt-dlp occasionally shells out to ffmpeg for format merging
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Railway injects $PORT; default to 8000 locally
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
