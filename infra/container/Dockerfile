FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder
WORKDIR /build
COPY shared/pyproject.toml ./shared/pyproject.toml
COPY shared/src ./shared/src
COPY backend/pyproject.toml backend/README.md ./backend/
COPY backend/src ./backend/src
COPY backend/migrations ./backend/migrations
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels --no-deps ./shared ./backend \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels \
       --find-links=/wheels 'buddy-backend[api]' boto3==1.40.50
COPY frontend/pyproject.toml frontend/README.md ./frontend/
COPY frontend/src ./frontend/src
RUN python -m pip wheel --no-cache-dir --wheel-dir /validator-wheels --no-deps ./shared ./frontend \
    && python -m pip wheel --no-cache-dir --wheel-dir /validator-wheels \
       --find-links=/validator-wheels buddy-desktop

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl libegl1 libgl1 libglib2.0-0 libfontconfig1 libxkbcommon0 libdbus-1-3 fonts-dejavu-core \
    && curl --fail --silent --show-error --proto '=https' \
       https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
       -o /tmp/rds-bundle.pem \
    && cat /tmp/rds-bundle.pem >> /etc/ssl/certs/ca-certificates.crt \
    && rm /tmp/rds-bundle.pem \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 buddy && useradd --uid 10001 --gid 10001 --no-create-home buddy
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels 'buddy-backend[api]' boto3 \
    && rm -rf /wheels
COPY --from=builder /validator-wheels /validator-wheels
RUN python -m venv /opt/buddy-validator \
    && /opt/buddy-validator/bin/python -m pip install --no-cache-dir --no-index --find-links=/validator-wheels buddy-desktop \
    && rm -rf /validator-wheels
COPY --chmod=0555 infra/container/container_entrypoint.py infra/container/bootstrap_database.py infra/container/migrate_database.py /opt/buddy/
RUN mkdir -p /var/lib/buddy-upload && chown 10001:10001 /var/lib/buddy-upload && chmod 0700 /var/lib/buddy-upload
VOLUME ["/var/lib/buddy-upload"]
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt TMPDIR=/var/lib/buddy-upload
ENV BUDDY_VALIDATOR_PYTHON=/opt/buddy-validator/bin/python
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "/opt/buddy/container_entrypoint.py"]
CMD ["python", "-m", "buddy_api"]
