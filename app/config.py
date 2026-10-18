import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://postgres:postgres@localhost:5432/ops")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
