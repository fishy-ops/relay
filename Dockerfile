FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY relay ./relay
COPY migrations ./migrations
COPY models ./models
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 relay
USER relay

EXPOSE 8000

# One image runs the API, worker and dispatcher. Each service overrides the command.
CMD ["uvicorn", "relay.api:app", "--host", "0.0.0.0", "--port", "8000"]
