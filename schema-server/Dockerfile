FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 5001

CMD ["python", "main.py", "--schema-dir", "/data/schemas", "--listen", "0.0.0.0:5001"]