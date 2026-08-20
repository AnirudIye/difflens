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

COPY pyproject.toml ./
COPY app ./app
COPY worker ./worker

RUN pip install --no-cache-dir .

# After the Python install layer: changing a lint rule should not re-run pip.
# /srv/eslint-runtime is where the adapter looks, because app/ sits beside it
# here exactly as it does in the repository.
#
# Lifting one binary across two Debian releases is deliberate and checked.
# The node stage is pinned to bookworm while python:3.12-slim has moved on to
# trixie, and that direction is the safe one: the official node binary needs
# at most GLIBC_2.28 and GLIBCXX_3.4.21, both of which every Debian release
# since bookworm provides, and every library it links (libstdc++, libgcc_s,
# libm, libc) is already in the python image. Pinning node to the older
# release keeps it that way, because python:3.12-slim can only move forward.
COPY --from=eslint /usr/local/bin/node /usr/local/bin/node
COPY --from=eslint /eslint-runtime /srv/eslint-runtime
COPY eslint-runtime/eslint.config.mjs /srv/eslint-runtime/eslint.config.mjs

# The assertion that makes the paragraph above maintainable. If either base
# image ever changes such that node cannot run, or the runtime moves, this
# fails the build on this line instead of quietly shipping an image whose
# TypeScript reviews are all "eslint unavailable".
RUN node --version \
    && node /srv/eslint-runtime/node_modules/eslint/bin/eslint.js --version

# After the install layer: editing a migration should not re-run pip
COPY alembic.ini ./
COPY alembic ./alembic
COPY start.sh ./

EXPOSE 8000

CMD ["sh", "start.sh"]
