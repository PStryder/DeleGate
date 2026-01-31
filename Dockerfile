# DeleGate - The Pure Planner
# Multi-stage build for production deployment

FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir build && \
    pip wheel --no-cache-dir --wheel-dir /wheels .

# Production image
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels and install
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Copy source code
COPY src/ ./src/
COPY migrations/ ./migrations/

# Set environment variables
ENV PYTHONPATH=/app/src
ENV DELEGATE_HOST=0.0.0.0
ENV DELEGATE_PORT=8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import json, os, urllib.request; url=os.environ.get('DELEGATE_MCP_URL','http://localhost:8000/mcp'); token=os.environ.get('DELEGATE_API_KEY'); payload={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'delegate.health','arguments':{}}}; req=urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}); token and req.add_header('Authorization','Bearer '+token); resp=urllib.request.urlopen(req, timeout=5); data=json.load(resp); assert 'result' in data"

# Run the application
EXPOSE 8000
CMD ["python", "-m", "delegate.main"]
