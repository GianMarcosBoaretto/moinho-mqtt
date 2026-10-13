FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY simulador.py .
ENV PYTHONUNBUFFERED=1 PASTA_ESTADO=/app/estado
CMD ["python", "simulador.py"]
