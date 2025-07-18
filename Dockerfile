# Use official Python image
FROM python:3.14.0b4-slim

# Set working directory
WORKDIR /app

# Copy files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Set environment
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "bot.py"]
