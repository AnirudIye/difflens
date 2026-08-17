FROM python:3.12-slim

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

# After the install layer: editing a migration should not re-run pip
COPY alembic.ini ./
COPY alembic ./alembic
COPY start.sh ./

EXPOSE 8000

CMD ["sh", "start.sh"]
