# Blast Radius entry. Built on the exact image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit (its own repository), read-only reference tree.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tgz
RUN mkdir -p /opt && tar xzf /tmp/sqlglot.tgz -C /opt \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
 && rm /tmp/sqlglot.tgz && chmod -R a+rX /opt/sqlglot

# The labeling environment's packages, so candidate tests can be run as labeled.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -q -r /app/requirements.lock

# Bytecode for the reference tree: fresh imports in verification forks stay cheap.
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests && chmod -R a+rX /opt/sqlglot

COPY selector /app/selector
COPY maps /app/maps
RUN chmod -R a+rX /app && python -m compileall -q /app/selector

ENV BR_SRC=/opt/sqlglot PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector/main.py"]
