FROM python:3.11-slim

# System dependencies for Playwright and other tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    libffi-dev \
    libssl-dev \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    # Additional tools
    curl \
    wget \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers — set the path BEFORE install so chromium lands
# where the runtime expects it (otherwise it goes to ~/.cache and is never found)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium
RUN playwright install-deps chromium

# Application code
COPY . .

# Data directories (mirrors settings.yaml base_path: "./data" with WORKDIR /app)
RUN mkdir -p /app/data/raw/html \
    /app/data/raw/pdf \
    /app/data/processed \
    /app/data/qa_output \
    /app/data/temp \
    /app/logs

# Environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5050/health || exit 1

# Expose dashboard port (matches settings.yaml dashboard.port: 5050)
EXPOSE 5050

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["run"]
