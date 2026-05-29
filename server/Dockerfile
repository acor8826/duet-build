FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DUET_TRANSPORT=http \
    DUET_STATE_DIR=/tmp/duet-state \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /tmp/duet-state/sessions

EXPOSE 8080

CMD ["python", "server.py"]
