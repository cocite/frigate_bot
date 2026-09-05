FROM python:3.11-slim-trixie

WORKDIR /app

# ffmpeg + VAAPI-драйвер + диагностика (всё из main, доп. репозитории не нужны;
# проверено: свободный intel-media-va-driver даёт полный набор энкодеров на нашем GPU)
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
