FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py relay.py ./
COPY templates ./templates
RUN useradd --uid 10001 --create-home relay
USER relay
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 290 app:app"]
