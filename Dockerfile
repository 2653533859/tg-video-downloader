FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 确保下载目录存在
RUN mkdir -p downloads
EXPOSE 5003
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["sh", "/app/healthcheck.sh"]
CMD ["python", "app.py"]
