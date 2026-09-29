FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY reconciler.py /app/reconciler.py
COPY runner.py /app/runner.py

ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/runner.py"]
