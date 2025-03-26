from __future__ import annotations

import contextlib
import time
import types
import typing
from collections.abc import Iterator

import redis_lock  # type: ignore[import-untyped]
import structlog

import instrumentation
from core.models import GithubRepo, PullRequest
from main import redis_client

# Considering that the maximum expiration time is set to 600 seconds, setting
# the cap at 1024 looks reasonable. Most of the locks should finish in a second
# or so, and these lock metrics are used to tell which locks have an anomaly.
# Because of this, we intentionally set the buckets to be higher than the
# average; which is the opposite to the typical latency metrics.
LOCK_BUCKETS = (0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, float("inf"))

logger = structlog.stdlib.get_logger()
_redis_lock_acquire_latency = instrumentation.Histogram(
    "redis_lock_acquire_latency",
    "Time taken to acquire a Redis lock (seconds)",
    ["lock_type"],
    buckets=LOCK_BUCKETS,
)
_redis_lock_hold_time = instrumentation.Histogram(
    "redis_lock_hold_time",
    "Time that a redis lock was held (seconds)",
    ["lock_type"],
    buckets=LOCK_BUCKETS,
)


@contextlib.contextmanager
def for_repo(repo: GithubRepo, *, expire: int = 600) -> Iterator[None]:
    lock_name = f"repo-{repo.id}"
    with lock("repo", lock_name, expire=expire):
        yield


@contextlib.contextmanager
def for_account(account_id: int, *, expire: int = 600) -> Iterator[None]:
    lock_name = f"account-{account_id}"
    with lock("account", lock_name, expire=expire):
        yield


@contextlib.contextmanager
def for_pr(
    pr: PullRequest, *, expire: int = 90, ignore_not_acquired: bool = False
) -> Iterator[None]:
    lock_name = f"pr-{pr.repo_id}-{pr.number}"
    try:
        with lock("pr", lock_name, expire=expire):
            yield
    except redis_lock.NotAcquired:
        if not ignore_not_acquired:
            raise
        logger.info(
            "Ignoring the lock not acquired exception for PR lock",
            pr=pr,
        )


@contextlib.contextmanager
def lock(
    lock_type: str,
    lock_name: str,
    *,
    expire: int = 90,
    log: bool = False,
) -> Iterator[None]:
    start = time.monotonic()
    with redis_lock.Lock(redis_client, lock_name, expire=expire):
        acquired = time.monotonic()
        latency = acquired - start
        if log or latency > 1:
            logger.info(
                "Acquired lock",
                lock_type=lock_type,
                lock_name=lock_name,
                lock_acquire_latency=latency,
            )
        _redis_lock_acquire_latency.labels(lock_type=lock_type).observe(latency)
        try:
            yield
        finally:
            released = time.monotonic()
            held = released - acquired
            if log or held > 1:
                logger.info(
                    "Released lock",
                    lock_type=lock_type,
                    lock_name=lock_name,
                    lock_hold_time=held,
                )
            _redis_lock_hold_time.labels(lock_type=lock_type).observe(held)


def is_locked(lock_name: str) -> bool:
    return redis_lock.Lock(redis_client, lock_name).locked()


class TimeoutBeforeLockAcquisition(RuntimeError):
    """Raised when the lock cannot be acquired within the timeout."""

    pass


class Lock(contextlib.AbstractContextManager):
    """A context manager for acquiring and releasing a Redis lock.

    lock_type is just for logging / monitoring purposes. It is not used for any other
    purpose.

    lock_name is the lock name that is used to acquire the lock.

    expire is the expiration time for the lock in seconds. The lock is automatically
    released once it's expired. (TODO: We should consider adding an option to renew the
    lock). Upon contextmanager exit, if the lock is already expired, it'll raise
    redis_lock.NotAcquired.

    log is a flag to whether log the lock acquisition and release events. Even if it's
    False (the default), it'll log the acquisition and release events if the latency is
    greater than 1 second.

    blocking is a flag to determine whether the lock acquisition should be blocking or
    non-blocking. If it's False, it'll try to acquire the lock and return immediately.
    You can check Lock.acquired to see if the lock is acquired or not. The default is
    blocking.

    timeout is the maximum time to wait for the lock acquisition. If the lock is not
    acquired, it'll raise TimeoutBeforeLockAcquisition. This can only be used when
    blocking mode.
    """

    def __init__(
        self,
        lock_type: str,
        lock_name: str,
        *,
        expire: int = 90,
        log: bool = False,
        blocking: bool = True,
        timeout: int | None = None,
    ):
        self._lock_type = lock_type
        self._lock_name = lock_name
        self._expire = expire
        self._log = log
        self._blocking = blocking
        self._timeout = timeout
        self._lock = redis_lock.Lock(redis_client, self._lock_name, expire=self._expire)

    @typing.override
    def __enter__(self) -> Lock:
        start_time = time.monotonic()
        self.acquired = self._lock.acquire(
            blocking=self._blocking, timeout=self._timeout
        )

        if not self.acquired and self._timeout is not None:
            raise TimeoutBeforeLockAcquisition()
        if not self.acquired:
            if self._blocking:
                # This should never happen unless we lose the connection to Redis or
                # whatever.
                raise redis_lock.NotAcquired()
            if self._log:
                logger.info(
                    "Did not acquire lock. Non-blocking mode, so continuing without a lock.",
                    lock_type=self._lock_type,
                    lock_name=self._lock_name,
                )
            # The lock is not acquired. The caller should check the lock status on
            # self.acquired and handle the case where the lock is not acquired.
            return self

        self._acquired_time = time.monotonic()
        latency = self._acquired_time - start_time
        if self._log or latency > 1:
            logger.info(
                "Acquired lock",
                lock_type=self._lock_type,
                lock_name=self._lock_name,
                lock_acquire_latency=latency,
            )
        _redis_lock_acquire_latency.labels(lock_type=self._lock_type).observe(latency)
        return self

    @typing.override
    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: types.TracebackType | None,
    ) -> typing.Literal[False]:
        if not self.acquired:
            return False
        self._lock.release()
        released = time.monotonic()
        held = released - self._acquired_time
        if self._log or held > 1:
            logger.info(
                "Released lock",
                lock_type=self._lock_type,
                lock_name=self._lock_name,
                lock_hold_time=held,
            )
        _redis_lock_hold_time.labels(lock_type=self._lock_type).observe(held)
        return False
