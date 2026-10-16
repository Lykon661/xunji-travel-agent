"""Windows lifetime lock shared by the background runner and service scripts."""
import errno
from contextlib import contextmanager
from time import monotonic, sleep

import msvcrt


@contextmanager
def service_lock(path, timeout=30):
    with path.open("a+b") as lock:
        lock.seek(0,2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        deadline = monotonic()+timeout
        waiting = False
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}:
                    raise
                if monotonic() >= deadline:
                    raise TimeoutError("The previous backend still holds server.lock. Wait for stop.bat to finish, then retry.") from exc
                if not waiting:
                    print("[startup] Waiting for the previous backend to release its lifetime lock...",flush=True)
                    waiting = True
                sleep(0.25)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
