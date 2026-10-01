FROM python:3.12-slim
RUN pip install --no-cache-dir requests==2.32.3 prometheus-client==0.21.0 cryptography==43.0.3
WORKDIR /app
COPY exporter.py .
USER nobody
EXPOSE 9850
CMD ["python", "-u", "exporter.py"]
