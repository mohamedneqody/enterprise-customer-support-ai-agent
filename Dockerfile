# نظام الدعم الذكي — حاوية الإنتاج
# البناء:  docker build -t support-agent .
# التشغيل: docker compose up -d   (يفضل — يربط Ollama والحجم الدائم)
FROM python:3.12-slim

WORKDIR /app

# طبقة الاعتماديات منفصلة لاستغلال كاش البناء
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || \
    echo "⚠️ تعذر تثبيت بعض الاعتماديات — النظام سيعمل بوضع الاستخراج بدون Reranker"

# كود المشروع (الموديلات والبيانات تُمرر كأحجام Volumes لا كصور)
COPY app.py train.py seed.py run_regression.py ./
COPY engine/ ./engine/
COPY static/ ./static/
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1 \
    OLLAMA_HOST=http://host.docker.internal:11434 \
    TENANT_ID=default

EXPOSE 9090
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:9090/api/health',timeout=4)"

CMD ["python", "app.py"]
