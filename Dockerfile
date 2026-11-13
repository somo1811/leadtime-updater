FROM python:3.12-slim
WORKDIR /app
COPY app.py .
ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "app.py"]
