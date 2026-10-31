FROM mysterysd/wzmlx:wzadv
COPY --from=mysterysd/wzmlx:m-tools /usr/local /usr/local
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /usr/src/app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN test -x /wzvenv/bin/python || (echo "ERROR: base image does not provide /wzvenv/bin/python" >&2; exit 1)

COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt
ENV CANTARELLABOTS_MEGA_SDK_VERSION=10.20.20
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir --no-deps "mega.py>=1.0.8"

COPY . .

CMD ["bash", "start.sh"]
