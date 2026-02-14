FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 确保下载目录存在
RUN mkdir -p downloads
EXPOSE 5000
CMD ["python", "app.py"]
