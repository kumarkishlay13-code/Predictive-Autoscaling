# syntax=docker/dockerfile:1
# Forecaster service image. Build from the repository root:
#   docker build -t pas/forecaster:<tag> -f docker/forecaster.Dockerfile .   (`pas build` does this)
# Set --build-arg WITH_LSTM=true to include PyTorch (CPU) for the LSTM models.
FROM python:3.12-slim

ARG WITH_LSTM=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN if [ "$WITH_LSTM" = "true" ]; then \
      pip install --index-url https://download.pytorch.org/whl/cpu torch && pip install ".[lstm]"; \
    else \
      pip install .; \
    fi

# Trained models are baked into the image so every run is pinned to an exact model version.
COPY artifacts/models ./artifacts/models

USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["pas", "serve", "--host", "0.0.0.0", "--port", "8000"]
