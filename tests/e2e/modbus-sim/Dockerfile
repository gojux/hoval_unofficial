FROM python:3.12-slim

RUN pip install --no-cache-dir "pymodbus>=3.11.0,<3.12"

COPY server.py /app/server.py

EXPOSE 502
CMD ["python", "/app/server.py"]
