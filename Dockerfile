# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies needed to compile Python packages
RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    libssl-dev \
    python3-dev \
    libasound2-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*


# Copy files
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ .

# Match schedule used for reminders (lives at the repo root)
COPY worldcup.json .

# Set environment
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "bot.py"]

