# Build context is the STACK ROOT, not this repository:
#
#     docker build -f DeleGate/Dockerfile .
#
# The image installs the canonical protocol package from the sibling LegiVellum
# checkout. `legivellum` is a hard dependency and is not published to an index,
# so a repo-scoped context cannot satisfy it -- the build fails with
# "No matching distribution found for legivellum" rather than silently
# producing an image that cannot validate receipts.

# DeleGate - The Pure Planner
# Multi-stage build for production deployment

FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# The canonical protocol package first: receipt models, validation, and the
# schema, which ships as package data so validation needs no source checkout.
COPY LegiVellum/pyproject.toml LegiVellum/README.md /src/LegiVellum/
COPY LegiVellum/shared/ /src/LegiVellum/shared/
RUN pip install --no-cache-dir /src/LegiVellum

COPY DeleGate/pyproject.toml DeleGate/README.md .
COPY DeleGate/src/ ./src/
# legivellum is wheeled too, and resolved from /wheels rather than an index.
# `pip wheel .` resolves dependencies from a package index regardless of what
# is already installed in this stage, so installing the protocol package above
# is not enough on its own -- the wheel build still fails with
# "No matching distribution found for legivellum".
RUN pip install --no-cache-dir build && \
    pip wheel --no-cache-dir --wheel-dir /wheels /src/LegiVellum && \
    pip wheel --no-cache-dir --wheel-dir /wheels --find-links /wheels .

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
COPY DeleGate/src/ ./src/
COPY DeleGate/migrations/ ./migrations/

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
