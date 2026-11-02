# 零依赖镜像：只用 Python 标准库
FROM python:3.12-slim

WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# 健康检查走 /healthz（不校验令牌）。
# 用 /api/packs 会让开了 ACCESS_TOKEN 的实例被判为 unhealthy 而被反复重启。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=2)"

CMD ["python", "-m", "harness.server"]
