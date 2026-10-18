import signal
import time

from .db import pool
from .outbox import process_one

stopping = False


def _stop(*_):
    global stopping
    stopping = True


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print("worker started", flush=True)
    while not stopping:
        try:
            if not process_one():
                time.sleep(1)
        except Exception as e:  # e.g. database not ready yet or briefly down: log and keep going
            print("worker loop error:", e, flush=True)
            time.sleep(2)
    pool.close()


if __name__ == "__main__":
    main()
