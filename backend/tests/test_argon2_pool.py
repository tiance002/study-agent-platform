import threading
import time

import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.argon2_pool import Argon2WorkPool


def test_pool_limits_queue_and_releases_after_work():
    pool = Argon2WorkPool(max_concurrency=1, queue_limit=0, wait_seconds=0.01)
    started = threading.Event()
    release = threading.Event()

    def work():
        started.set()
        release.wait(1)

    thread = threading.Thread(target=lambda: pool.run(work))
    thread.start()
    assert started.wait(1)
    with pytest.raises(PlatformError) as exc:
        pool.run(lambda: None)
    assert exc.value.code is ErrorCode.AUTH_POOL_SATURATED
    release.set()
    thread.join(1)
    assert pool.run(lambda: 1) == 1


def test_login_is_admitted_before_waiting_registration():
    pool = Argon2WorkPool(max_concurrency=1, queue_limit=4, wait_seconds=1)
    started = threading.Event()
    release = threading.Event()
    order = []

    thread = threading.Thread(target=lambda: pool.run(lambda: (started.set(), release.wait(1)), priority="registration"))
    thread.start()
    assert started.wait(1)
    registration = threading.Thread(target=lambda: pool.run(lambda: order.append("registration"), priority="registration"))
    login = threading.Thread(target=lambda: pool.run(lambda: order.append("login"), priority="login"))
    registration.start()
    login.start()
    time.sleep(0.03)
    release.set()
    thread.join(1)
    login.join(1)
    registration.join(1)
    assert order[0] == "login"
