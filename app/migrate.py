from .db import migrate, pool

if __name__ == "__main__":
    migrate()
    pool.close()
