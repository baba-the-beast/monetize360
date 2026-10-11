# One image, three processes (pricing-api, control-api, audit-worker) - see docker-compose.yml
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY packs ./packs

# never run as root
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "--factory", "monetize360.api.app:create_pricing_app", "--host", "0.0.0.0", "--port", "8000"]
