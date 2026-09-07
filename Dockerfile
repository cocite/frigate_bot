FROM python:3.11-slim-trixie

WORKDIR /app

# ffmpeg + VAAPI driver + diagnostics, all from Debian main (no extra repos).
# The free intel-media-va-driver is enough for h264_vaapi on an Intel iGPU, the non-free variant is not needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      intel-media-va-driver \
      vainfo \
      intel-gpu-tools \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "mqtt_dispatcher.py"]
