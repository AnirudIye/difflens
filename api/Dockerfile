# ESLint is the one analyzer that is not a Python package, so it is installed
# by npm in its own stage and copied into the runtime image along with the
# node binary that runs it. The adapter degrades cleanly when either is
# missing (the review just loses its JavaScript findings), so a problem here
# costs TypeScript support and never a Python review.
FROM node:20-bookworm-slim AS eslint

WORKDIR /eslint-runtime
COPY eslint-runtime/package.json eslint-runtime/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund


FROM python:3.12-slim

WORKDIR /srv

# node is dynamically linked against libstdc++, which the slim Python image
# does not promise to carry. Two megabytes to make the copy below dependable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY worker ./worker

RUN pip install --no-cache-dir .

# After the Python install layer: changing a lint rule should not re-run pip.
# /srv/eslint-runtime is where the adapter looks, because app/ sits beside it
# here exactly as it does in the repository.
COPY --from=eslint /usr/local/bin/node /usr/local/bin/node
COPY --from=eslint /eslint-runtime /srv/eslint-runtime
COPY eslint-runtime/eslint.config.mjs /srv/eslint-runtime/eslint.config.mjs

# After the install layer: editing a migration should not re-run pip
COPY alembic.ini ./
COPY alembic ./alembic
COPY start.sh ./

EXPOSE 8000

CMD ["sh", "start.sh"]
