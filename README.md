# FilmTraficBot

This project uses Python 3.12 and `uv` for dependency management.

1. Copy the environment template: `cp .env.example .env`, then add a real `BOT_TOKEN`.
2. Start PostgreSQL and the bot: `docker compose up --build`.
3. The bot answers `pong` to `/ping` once it is running.
4. Apply migrations with `uv run alembic upgrade head` (or `docker compose exec bot alembic upgrade head`).
5. To run without Docker, point `DATABASE_URL` at a local PostgreSQL instance (use `localhost` instead of `postgres`).
6. Install dependencies with `uv sync`, then start the bot with `uv run python -m app.main`.

