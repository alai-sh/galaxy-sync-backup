FROM docker.io/library/python:3.15.0-slim-trixie

# Static (CGO_ENABLED=0), so the Alpine-built binary runs here. Pinned by digest, as a tag can be moved.
COPY --from=docker.io/rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5 /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --no-editable && rm /usr/local/bin/uv
ENV PATH=/app/.venv/bin:$PATH

USER 65534:65534
ENV HOME=/tmp
# Remotes come from RCLONE_CONFIG_* variables; this stops rclone's "config file not found" notice.
ENV RCLONE_CONFIG=/dev/null
ENTRYPOINT ["galaxy-sync-backup"]
