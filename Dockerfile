# Use Python 3.12 slim base
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .

RUN pip install -r requirements.txt

# Entrypoint
ENTRYPOINT ["python", "/app/onvif-bot.py"]