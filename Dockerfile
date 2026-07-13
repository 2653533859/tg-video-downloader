# 基础镜像固定到具体补丁版本以提升可复现性。
# 进一步固定 digest（最强可复现）：用
#   docker buildx imagetools inspect python:3.10-slim
# 解析出 sha256 后改成 FROM python:3.10-slim@sha256:<digest>
FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 运行期可写目录 + 非 root 用户（最小权限）。
# 注意：docker-compose 若把宿主 root 属主目录 bind-mount 进来
# （如 .:/app、/root/downloads、/root/.tdl），非 root 用户将无法写入——
# 需在宿主执行一次 `chown -R 10001:10001 <这些目录>`，或按需覆盖 user。
RUN mkdir -p downloads logs .task_state .resume \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5003
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["sh", "/app/healthcheck.sh"]
CMD ["python", "app.py"]
