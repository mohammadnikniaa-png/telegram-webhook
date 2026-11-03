FROM python:3.12-slim
WORKDIR /app
COPY webhook_server.py /app/
ENV WEBHOOK_PORT=10000
EXPOSE 10000
CMD ["python3", "webhook_server.py"]
