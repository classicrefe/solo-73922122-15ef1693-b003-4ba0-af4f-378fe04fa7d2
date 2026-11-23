FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY app ./app
COPY demo ./demo

ENV DB_PATH=/data/closing.db
EXPOSE 8000

CMD ["python", "run.py"]
