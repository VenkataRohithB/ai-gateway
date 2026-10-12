FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements.txt
COPY . .
RUN useradd --system --uid 10001 gateway
USER gateway
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

# Test image: same app + pytest. Tests are mounted at /tests by docker-compose.test.yml.
FROM base AS test
USER root
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
    pytest==9.1.1 iniconfig==2.3.1 packaging==26.3 pluggy==1.6.0 Pygments==2.21.0
USER gateway
