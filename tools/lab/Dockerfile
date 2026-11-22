# Lab image: the scorer's labeling environment plus tools for experiments.
# Not the submission image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update -qq && apt-get install -y -qq patch git >/dev/null && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -q -r /tmp/requirements.lock && pip install --no-cache-dir -q coverage
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tgz
RUN mkdir -p /src && tar xzf /tmp/sqlglot.tgz -C /src && mv /src/sqlglot-* /src/sqlglot && rm /tmp/sqlglot.tgz
ENV PYTHONPATH=/src/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src/sqlglot
