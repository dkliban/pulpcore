"""
Task dispatch functions for Redis-based worker implementation.

This module contains dispatch logic specific to the Redis worker that uses
Redis distributed locks for task coordination.
"""

import contextvars
import logging
import sys
from asgiref.sync import sync_to_async

from pulpcore.app.models import Task, TaskGroup, AppStatus
from pulpcore.app.redis_connection import get_redis_connection
from pulpcore.app.util import get_domain
from pulpcore.app.contexts import with_task_context, awith_task_context
from pulpcore.constants import TASK_STATES, TASK_FINAL_STATES
from pulpcore.tasking.redis_locks import (
    resource_to_lock_key,
    release_resource_locks,
    async_release_resource_locks,
)
from pulpcore.tasking.tasks import (
    called_from_content_app,
    get_function_name,
    get_version,
    get_resources,
    get_task_payload,
    get_task_function,
    aget_task_function,
    log_task_start,
    log_task_completed,
    log_task_failed,
    using_workdir,
)
from pulpcore.tasking.kafka import send_task_notification


_logger = logging.getLogger(__name__)

# Redis key prefix for task cancellation
REDIS_CANCEL_PREFIX = "pulp:task:cancel:"


def publish_cancel_signal(task_id):
    """
    Publish a cancellation signal for a task via Redis.

    Args:
        task_id (str): The task ID to cancel

    Returns:
        bool: True if signal was published, False otherwise
    """
    redis_conn = get_redis_connection()
    if not redis_conn:
        _logger.error("Redis connection not available for task cancellation")
        return False

    try:
        # Publish to the task-specific cancellation channel
        cancel_key = f"{REDIS_CANCEL_PREFIX}{task_id}"
        # Set a value with expiration (24 hours) in case worker missed it
        redis_conn.setex(cancel_key, 86400, "cancel")
        _logger.info("Published cancellation signal for task %s", task_id)
        return True
    except Exception as e:
        _logger.error("Error publishing cancellation signal for task %s: %s", task_id, e)
        return False


def check_cancel_signal(task_id):
    """
    Check if a cancellation signal exists for a task.

    Args:
        task_id (str): The task ID to check

    Returns:
        bool: True if cancellation signal exists, False otherwise
    """
    redis_conn = get_redis_connection()
    if not redis_conn:
        return False

    try:
        cancel_key = f"{REDIS_CANCEL_PREFIX}{task_id}"
        return redis_conn.exists(cancel_key) > 0
    except Exception as e:
        _logger.error("Error checking cancellation signal for task %s: %s", task_id, e)
        return False


def clear_cancel_signal(task_id):
    """
    Clear a cancellation signal for a task.

    Args:
        task_id (str): The task ID to clear cancellation signal for
    """
    redis_conn = get_redis_connection()
    if not redis_conn:
        return

    try:
        cancel_key = f"{REDIS_CANCEL_PREFIX}{task_id}"
        redis_conn.delete(cancel_key)
        _logger.debug("Cleared cancellation signal for task %s", task_id)
    except Exception as e:
        _logger.error("Error clearing cancellation signal for task %s: %s", task_id, e)


def cancel_task(task_id):
    """
    Cancel a task using Redis-based signaling.

    This method cancels only the task with given task_id, not the spawned tasks.
    This also updates task's state to 'canceling'.

    Args:
        task_id (str): The ID of the task you wish to cancel

    Returns:
        Task: The task object

    Raises:
        Task.DoesNotExist: If a task with given task_id does not exist
    """
    task = Task.objects.select_related("pulp_domain").get(pk=task_id)

    if task.state in TASK_FINAL_STATES:
        # If the task is already done, just stop.
        _logger.debug(
            "Task [%s] in domain: %s already in a final state: %s",
            task_id,
            task.pulp_domain.name,
            task.state
        )
        return task

    _logger.info(
        "Canceling task: %s in domain: %s",
        task_id,
        task.pulp_domain.name
    )

    # This is the only valid transition without holding the task lock.
    task.set_canceling()

    # Publish cancellation signal via Redis
    publish_cancel_signal(task.pk)

    return task


def cancel_task_group(task_group_id):
    """
    Cancel the task group that is represented by the given task_group_id using Redis.

    This method attempts to cancel all tasks in the task group.

    Args:
        task_group_id (str): The ID of the task group you wish to cancel

    Returns:
        TaskGroup: The task group object

    Raises:
        TaskGroup.DoesNotExist: If a task group with given task_group_id does not exist
    """
    task_group = TaskGroup.objects.get(pk=task_group_id)
    task_group.all_tasks_dispatched = True
    task_group.save(update_fields=["all_tasks_dispatched"])

    TASK_RUNNING_STATES = (TASK_STATES.RUNNING, TASK_STATES.WAITING)
    tasks = task_group.tasks.filter(state__in=TASK_RUNNING_STATES).values_list("pk", flat=True)
    for task_id in tasks:
        try:
            cancel_task(task_id)
        except RuntimeError:
            pass
    return task_group


def execute_task(task):
    """Redis-aware task execution that releases Redis locks for immediate tasks."""
    # This extra stack is needed to isolate the current_task ContextVar
    contextvars.copy_context().run(_execute_task, task)


def _execute_task(task):
    try:
        # Log execution context information
        current_app = AppStatus.objects.current()
        if current_app:
            _logger.info(
                "TASK EXECUTION: Task %s being executed by %s (app_type=%s)",
                task.pk,
                current_app.name,
                current_app.app_type
            )
        else:
            _logger.info(
                "TASK EXECUTION: Task %s being executed with no AppStatus.current()",
                task.pk
            )

        with with_task_context(task):
            task.set_running()
            domain = get_domain()
            try:
                log_task_start(task, domain)
                task_function = get_task_function(task)
                result = task_function()
            except Exception:
                exc_type, exc, tb = sys.exc_info()
                task.set_failed(exc, tb)
                log_task_failed(task, exc_type, exc, tb, domain)
                send_task_notification(task)
            else:
                task.set_completed(result)
                log_task_completed(task, domain)
                send_task_notification(task)
                return result
            return None
    finally:
        # Release Redis locks if this was an immediate task
        if hasattr(task, '_locked_resources') and task._locked_resources:
            current_app = AppStatus.objects.current()
            redis_conn = get_redis_connection()
            lock_owner = current_app.name if current_app else f"immediate-{task.pk}"
            _logger.info(
                "RESOURCE LOCK RELEASE: Task %s releasing resource locks with owner=%s (AppStatus.current=%s) for resources: %s",
                task.pk,
                lock_owner,
                current_app.name if current_app else "None",
                task._locked_resources
            )
            release_resource_locks(redis_conn, lock_owner, task._locked_resources)
            # Clear the attribute so worker knows locks were released
            del task._locked_resources


async def aexecute_task(task):
    """Redis-aware async task execution that releases Redis locks for immediate tasks."""
    # This extra stack is needed to isolate the current_task ContextVar
    await contextvars.copy_context().run(_aexecute_task, task)


async def _aexecute_task(task):
    try:
        # Log execution context information
        current_app = await sync_to_async(AppStatus.objects.current)()
        if current_app:
            _logger.info(
                "TASK EXECUTION (async): Task %s being executed by %s (app_type=%s)",
                task.pk,
                current_app.name,
                current_app.app_type
            )
        else:
            _logger.info(
                "TASK EXECUTION (async): Task %s being executed with no AppStatus.current()",
                task.pk
            )

        async with awith_task_context(task):
            await sync_to_async(task.set_running)()
            domain = get_domain()
            try:
                task_coroutine_fn = await aget_task_function(task)
                result = await task_coroutine_fn()
            except Exception:
                exc_type, exc, tb = sys.exc_info()
                await sync_to_async(task.set_failed)(exc, tb)
                log_task_failed(task, exc_type, exc, tb, domain)
                send_task_notification(task)
            else:
                await sync_to_async(task.set_completed)(result)
                send_task_notification(task)
                log_task_completed(task, domain)
                return result
            return None
    finally:
        # Release Redis locks if this was an immediate task
        if hasattr(task, '_locked_resources') and task._locked_resources:
            current_app = await sync_to_async(AppStatus.objects.current)()
            redis_conn = get_redis_connection()
            lock_owner = current_app.name if current_app else f"immediate-{task.pk}"
            _logger.info(
                "RESOURCE LOCK RELEASE (async): Task %s releasing resource locks with owner=%s (AppStatus.current=%s) for resources: %s",
                task.pk,
                lock_owner,
                current_app.name if current_app else "None",
                task._locked_resources
            )
            await async_release_resource_locks(redis_conn, lock_owner, task._locked_resources)
            # Clear the attribute so worker knows locks were released
            del task._locked_resources


def are_resources_available(colliding_resources, task: Task) -> bool:
    """
    Try to acquire Redis locks for the task's exclusive resources.

    Returns True if all locks were acquired, False otherwise.
    Stores acquired locks on the task object for later release.
    """
    redis_conn = get_redis_connection()
    if not redis_conn:
        _logger.error("Redis connection not available for immediate task locking")
        return False

    # Get exclusive resources (those not prefixed with "shared:")
    exclusive_resources = [
        resource
        for resource in task.reserved_resources_record or []
        if not resource.startswith("shared:")
    ]

    if not exclusive_resources:
        # No exclusive resources, so locks are available
        task._locked_resources = []
        return True

    # Sort resources deterministically to prevent deadlocks
    sorted_resources = sorted(exclusive_resources)

    # Use AppStatus.current() to get a worker identifier for the lock value
    # For immediate tasks, we use a special identifier
    current_app = AppStatus.objects.current()
    lock_owner = current_app.name if current_app else f"immediate-{task.pk}"

    try:
        for resource in sorted_resources:
            lock_key = resource_to_lock_key(resource)

            # Try to acquire lock using SET with NX (only set if not exists)
            acquired = redis_conn.set(lock_key, lock_owner, nx=True)

            if not acquired:
                _logger.debug(
                    "Failed to acquire lock for immediate task %s resource: %s",
                    task.pk,
                    resource
                )
                # Release any locks we acquired so far
                release_resource_locks(redis_conn, lock_owner, sorted_resources[:sorted_resources.index(resource)])
                return False

        # All locks acquired successfully, store them for later release
        task._locked_resources = sorted_resources
        _logger.debug("Successfully acquired all locks for immediate task %s", task.pk)
        return True

    except Exception as e:
        _logger.error("Error acquiring locks for immediate task %s: %s", task.pk, e)
        # Try to release any locks we may have acquired
        release_resource_locks(redis_conn, lock_owner, sorted_resources)
        return False


async def async_are_resources_available(colliding_resources, task: Task) -> bool:
    """
    Try to acquire Redis locks for the task's exclusive resources.

    Returns True if all locks were acquired, False otherwise.
    Stores acquired locks on the task object for later release.
    """
    redis_conn = get_redis_connection()
    if not redis_conn:
        _logger.error("Redis connection not available for immediate task locking")
        return False

    # Get exclusive resources (those not prefixed with "shared:")
    exclusive_resources = [
        resource
        for resource in task.reserved_resources_record or []
        if not resource.startswith("shared:")
    ]

    if not exclusive_resources:
        # No exclusive resources, so locks are available
        task._locked_resources = []
        return True

    # Sort resources deterministically to prevent deadlocks
    sorted_resources = sorted(exclusive_resources)

    # Use AppStatus.current() to get a worker identifier for the lock value
    # For immediate tasks, we use a special identifier
    current_app = AppStatus.objects.current()
    lock_owner = current_app.name if current_app else f"immediate-{task.pk}"

    try:
        for resource in sorted_resources:
            lock_key = resource_to_lock_key(resource)

            # Try to acquire lock using SET with NX (only set if not exists)
            acquired = await sync_to_async(redis_conn.set)(lock_key, lock_owner, nx=True)

            if not acquired:
                _logger.debug(
                    "Failed to acquire lock for immediate task %s resource: %s",
                    task.pk,
                    resource
                )
                # Release any locks we acquired so far
                await async_release_resource_locks(redis_conn, lock_owner, sorted_resources[:sorted_resources.index(resource)])
                return False

        # All locks acquired successfully, store them for later release
        task._locked_resources = sorted_resources
        _logger.debug("Successfully acquired all locks for immediate task %s", task.pk)
        return True

    except Exception as e:
        _logger.error("Error acquiring locks for immediate task %s: %s", task.pk, e)
        # Try to release any locks we may have acquired
        await async_release_resource_locks(redis_conn, lock_owner, sorted_resources)
        return False


def dispatch(
    func,
    args=None,
    kwargs=None,
    task_group=None,
    exclusive_resources=None,
    shared_resources=None,
    immediate=False,
    deferred=True,
    versions=None,
):
    """
    Enqueue a message to Pulp workers with Redis-based resource locking.

    This version uses Redis distributed locks instead of PostgreSQL advisory locks.

    Args:
        func (callable | str): The function to be run when the necessary locks are acquired.
        args (tuple): The positional arguments to pass on to the task.
        kwargs (dict): The keyword arguments to pass on to the task.
        task_group (pulpcore.app.models.TaskGroup): A TaskGroup to add the created Task to.
        exclusive_resources (list): A list of resources this task needs exclusive access to while
            running. Each resource can be either a `str` or a `django.models.Model` instance.
        shared_resources (list): A list of resources this task needs non-exclusive access to while
            running. Each resource can be either a `str` or a `django.models.Model` instance.
        immediate (bool): Whether to allow running this task immediately. It must be guaranteed to
            execute fast without blocking. If not all resource constraints are met, the task will
            either be returned in a canceled state or, if `deferred` is `True` be left in the queue
            to be picked up by a worker eventually. Defaults to `False`.
        deferred (bool): Whether to allow defer running the task to a pulpcore_worker. Defaults to
            `True`. `immediate` and `deferred` cannot both be `False`.
        versions (Optional[Dict[str, str]]): Minimum versions of components by app_label the worker
            must provide to handle the task.

    Returns (pulpcore.app.models.Task): The Pulp Task that was created.

    Raises:
        ValueError: When `resources` is an unsupported type.
    """

    execute_now = immediate and not called_from_content_app()
    assert deferred or immediate, "A task must be at least `deferred` or `immediate`."
    function_name = get_function_name(func)
    versions = get_version(versions, function_name)
    colliding_resources, resources = get_resources(exclusive_resources, shared_resources, immediate)
    app_lock = None if not execute_now else AppStatus.objects.current()  # Lazy evaluation...
    task_payload = get_task_payload(
        function_name, task_group, args, kwargs, resources, versions, immediate, deferred, app_lock
    )
    task = Task.objects.create(**task_payload)
    if execute_now:
        # Try to acquire Redis task lock to prevent workers from picking up this task
        redis_conn = get_redis_connection()
        task_lock_key = f"task:{task.pk}"
        current_app = AppStatus.objects.current()
        lock_owner = current_app.name if current_app else f"immediate-{task.pk}"

        # Use SET with NX (only set if not exists) and EX (expiration in seconds)
        # 24 hours = 86400 seconds
        task_lock_acquired = redis_conn.set(task_lock_key, lock_owner, nx=True, ex=86400)

        if task_lock_acquired:
            # Try to acquire resource locks
            if are_resources_available(colliding_resources, task):
                try:
                    _logger.info(
                        "IMMEDIATE DISPATCH: Task %s acquired task lock and resources available, executing immediately in API process (AppStatus.current=%s)",
                        task.pk,
                        lock_owner
                    )
                    with using_workdir():
                        execute_task(task)
                except Exception:
                    # Exception before execute_task() completed
                    # Release locks if they weren't already released by _execute_task()
                    if hasattr(task, '_locked_resources') and task._locked_resources:
                        redis_conn = get_redis_connection()
                        release_resource_locks(redis_conn, lock_owner, task._locked_resources)
                        del task._locked_resources
                        # Also release task lock since we couldn't complete execution
                        redis_conn.delete(task_lock_key)
                    raise
            elif deferred:
                # Resources not available, release task lock and defer to worker
                redis_conn.delete(task_lock_key)
                _logger.info(
                    "IMMEDIATE DISPATCH: Task %s resources not available, released task lock and deferring to worker",
                    task.pk
                )
            else:
                # Resources not available and can't be deferred
                redis_conn.delete(task_lock_key)
                task.set_canceling()
                task.set_canceled(TASK_STATES.CANCELED, "Resources temporarily unavailable.")
        elif deferred:
            # Another process acquired the task lock, defer to worker
            _logger.info(
                "IMMEDIATE DISPATCH: Task %s could not acquire task lock, deferring to worker",
                task.pk
            )
        else:
            # Can't acquire task lock and can't be deferred
            task.set_canceling()
            task.set_canceled(TASK_STATES.CANCELED, "Resources temporarily unavailable.")
    return task


async def adispatch(
    func,
    args=None,
    kwargs=None,
    task_group=None,
    exclusive_resources=None,
    shared_resources=None,
    immediate=False,
    deferred=True,
    versions=None,
):
    """Async version of Redis-based dispatch."""
    execute_now = immediate and not called_from_content_app()
    assert deferred or immediate, "A task must be at least `deferred` or `immediate`."
    function_name = get_function_name(func)
    versions = get_version(versions, function_name)
    colliding_resources, resources = get_resources(exclusive_resources, shared_resources, immediate)
    app_lock = None if not execute_now else AppStatus.objects.current()  # Lazy evaluation...
    task_payload = get_task_payload(
        function_name, task_group, args, kwargs, resources, versions, immediate, deferred, app_lock
    )
    task = await Task.objects.acreate(**task_payload)
    if execute_now:
        # Try to acquire Redis task lock to prevent workers from picking up this task
        redis_conn = get_redis_connection()
        task_lock_key = f"task:{task.pk}"
        current_app = await sync_to_async(AppStatus.objects.current)()
        lock_owner = current_app.name if current_app else f"immediate-{task.pk}"

        # Use SET with NX (only set if not exists) and EX (expiration in seconds)
        # 24 hours = 86400 seconds
        task_lock_acquired = redis_conn.set(task_lock_key, lock_owner, nx=True, ex=86400)

        if task_lock_acquired:
            # Try to acquire resource locks
            if await async_are_resources_available(colliding_resources, task):
                try:
                    _logger.info(
                        "IMMEDIATE DISPATCH (async): Task %s acquired task lock and resources available, executing immediately in API process (AppStatus.current=%s)",
                        task.pk,
                        lock_owner
                    )
                    with using_workdir():
                        await aexecute_task(task)
                except Exception:
                    # Exception before aexecute_task() completed
                    # Release locks if they weren't already released by _aexecute_task()
                    if hasattr(task, '_locked_resources') and task._locked_resources:
                        redis_conn = get_redis_connection()
                        await async_release_resource_locks(redis_conn, lock_owner, task._locked_resources)
                        del task._locked_resources
                        # Also release task lock since we couldn't complete execution
                        redis_conn.delete(task_lock_key)
                    raise
            elif deferred:
                # Resources not available, release task lock and defer to worker
                redis_conn.delete(task_lock_key)
                _logger.info(
                    "IMMEDIATE DISPATCH (async): Task %s resources not available, released task lock and deferring to worker",
                    task.pk
                )
            else:
                # Resources not available and can't be deferred
                redis_conn.delete(task_lock_key)
                task.set_canceling()
                task.set_canceled(TASK_STATES.CANCELED, "Resources temporarily unavailable.")
        elif deferred:
            # Another process acquired the task lock, defer to worker
            _logger.info(
                "IMMEDIATE DISPATCH (async): Task %s could not acquire task lock, deferring to worker",
                task.pk
            )
        else:
            # Can't acquire task lock and can't be deferred
            task.set_canceling()
            task.set_canceled(TASK_STATES.CANCELED, "Resources temporarily unavailable.")
    return task
