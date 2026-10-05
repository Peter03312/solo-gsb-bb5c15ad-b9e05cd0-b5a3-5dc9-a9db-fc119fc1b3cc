FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATABASE_PATH=/data/royalties.db \
    PORT=8000

VOLUME /data
EXPOSE 8000

# 启动时自动初始化数据库（schema 幂等创建）
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
