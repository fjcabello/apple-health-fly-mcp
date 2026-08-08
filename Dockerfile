FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py server.py wrapper.py ./

ENV PYTHONUNBUFFERED=1
ENV APPLE_HEALTH_DATA_DIR=/data

EXPOSE 8080

CMD ["python", "wrapper.py"]
