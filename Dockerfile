
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN if [ -s requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
ENV PORT=8080 DATABASE_PATH=/data/app.sqlite3
EXPOSE 8080
# 迁移在启动时执行：compose 的命名卷会覆盖镜像内 /data，构建期迁移不会进入挂载卷
CMD ["sh", "-c", "python -m scripts.migrate && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
