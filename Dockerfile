FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RERO_DB_PATH=/data/rero.db
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home rero && mkdir /data && chown rero /data
USER 10001
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/live').status==200 else 1)"
CMD ["uvicorn", "rero.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
