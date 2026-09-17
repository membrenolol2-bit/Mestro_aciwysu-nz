FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all bot files
COPY . .

# Create data directory (persistent volume should mount here in Railway)
RUN mkdir -p /app/data

# Entry point
CMD ["python", "main.py"]
