import asyncio
import contextvars
import importlib
import logging
import os
import sys
import traceback
import tempfile
from functools import partial
from gettext import gettext as _
from contextlib import contextmanager
from asgiref.sync import sync_to_async, async_to_sync

from django.conf import settings
from django.db import connection
from django.db.models import Model
from django_guid import get_guid, set_guid
from pulpcore.app.apps import MODULE_PLUGIN_VERSIONS
from pulpcore.app.models import Task, TaskGroup, AppStatus
from pulpcore.app.redis_connection import get_redis_connection
from pulpcore.app.util import (
    get_domain,
    get_prn,
)
from pulpcore.app.contexts import with_task_context, awith_task_context
from pulpcore.constants import (
    TASK_FINAL_STATES,
    TASK_INCOMPLETE_STATES,
    TASK_STATES,
    IMMEDIATE_TIMEOUT,
    TASK_WAKEUP_HANDLE,
    TASK_WAKEUP_UNBLOCK,
)
from pulpcore.middleware import x_task_diagnostics_var
from pulpcore.tasking.kafka import send_task_notification
from pulpcore.tasking._util import (
    resource_to_lock_key,
    release_resource_locks,
    async_release_resource_locks,
)

_logger = logging.getLogger(__name__)


def _validate_and_get_resources(resources):
    resource_set = set()
    for r in resources:
        if isinstance(r, str):
            resource_set.add(r)
        elif isinstance(r, Model):
            resource_set.add(get_prn(r))
        elif r is None:
            # Silently drop None values
            pass
        else:
            raise ValueError(_("Must be (str|Model)"))
    return list(resource_set)


def wakeup_worker(reason):
    # Notify workers
    with connection.connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify('pulp_worker_wakeup', %s)", (reason,))


def execute_task(task):
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
        # Restore the task's GUID for proper logging (contexts.py bug workaround)
        set_guid(task.logging_cid)
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
        # Restore the task's GUID for proper logging (contexts.py bug workaround)
        set_guid(task.logging_cid)
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


def log_task_start(task, domain):
    _logger.info(
        "Starting task id: %s in domain: %s, task_type: %s, immediate: %s, deferred: %s",
        task.pk,
        domain.name,
        task.name,
        str(task.immediate),
        str(task.deferred),
    )


def log_task_completed(task, domain):
    execution_time = task.finished_at - task.started_at
    execution_time_us = int(execution_time.total_seconds() * 1_000_000)  # μs
    _logger.info(
        "Task completed %s in domain:"
        " %s, task_type: %s, immediate: %s, deferred: %s, execution_time: %s μs",
        task.pk,
        domain.name,
        task.name,
        str(task.immediate),
        str(task.deferred),
        execution_time_us,
    )


def log_task_failed(task, exc_type, exc, tb, domain):
    _logger.info(
        "Task[{task_type}] {task_pk} failed ({exc_type}: {exc}) in domain: {domain}".format(
            task_type=task.name,
            task_pk=task.pk,
            exc_type=exc_type.__name__,
            exc=exc,
            domain=domain.name,
        )
    )
    _logger.info("\n".join(traceback.format_list(traceback.extract_tb(tb))))


async def aget_task_function(task):
    """Get and handle task function running from ASYNC context.

    This exists to handle the combinations of:
    * context: sync | async
    * Task.function: regular-function | coroutine-function
    * Task.immediate: True | False
    """
    func, is_coroutine_fn = _load_function(task)

    if task.immediate and not is_coroutine_fn:
        raise ValueError("Immediate tasks must be async functions.")
    elif not task.immediate:
        raise ValueError("Non-immediate tasks can't run in async context.")

    return _add_timeout_to(func, task.pk)


def get_task_function(task):
    """Get and handle task function running from SYNC context.

    This exists to handle the combinations of:
    * context: sync | async
    * Task.function: regular-function | coroutine-function
    * Task.immediate: True | False
    """
    func, is_coroutine_fn = _load_function(task)

    if task.immediate and not is_coroutine_fn:
        raise ValueError("Immediate tasks must be async functions.")

    # no sync wrapper required
    if not is_coroutine_fn:
        return func

    # async function in sync context requires wrapper
    if task.immediate:
        coro_fn_with_timeout = _add_timeout_to(func, task.pk)
        return async_to_sync(coro_fn_with_timeout)
    return async_to_sync(func)


def _load_function(task):
    module_name, function_name = task.name.rsplit(".", 1)
    module = importlib.import_module(module_name)
    func = getattr(module, function_name)
    args = task.enc_args or ()
    kwargs = task.enc_kwargs or {}

    func_with_args = partial(func, *args, **kwargs)
    is_coroutine_fn = asyncio.iscoroutinefunction(func)
    return func_with_args, is_coroutine_fn


def _add_timeout_to(coro_fn, task_pk):

    async def _wrapper():
        try:
            return await asyncio.wait_for(coro_fn(), timeout=IMMEDIATE_TIMEOUT)
        except asyncio.TimeoutError:
            msg_template = "Immediate task %s timed out after %s seconds."
            error_msg = msg_template % (task_pk, IMMEDIATE_TIMEOUT)
            _logger.info(error_msg)
            raise RuntimeError(error_msg)

    return _wrapper


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
    Enqueue a message to Pulp workers with a reservation.

    This method provides normal enqueue functionality, while also requesting necessary locks for
    serialized urls. No two tasks that claim the same resource can execute concurrently. It
    accepts resources which it transforms into a list of urls (one for each resource).

    This method creates a [pulpcore.app.models.Task][] object and returns it.

    The values in `args` and `kwargs` must be JSON serializable, but may contain instances of
    ``uuid.UUID``.

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
    app_lock = None
    task_payload = get_task_payload(
        function_name, task_group, args, kwargs, resources, versions, immediate, deferred, app_lock
    )
    task = Task.objects.create(**task_payload)
    task.refresh_from_db()  # The database will have assigned a timestamp for us.
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
                task.app_lock = None
                task.save()
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
            task.app_lock = None
            task.save()
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
    """Async version of dispatch."""
    execute_now = immediate and not called_from_content_app()
    assert deferred or immediate, "A task must be at least `deferred` or `immediate`."
    function_name = get_function_name(func)
    versions = get_version(versions, function_name)
    colliding_resources, resources = get_resources(exclusive_resources, shared_resources, immediate)
    app_lock = None
    task_payload = get_task_payload(
        function_name, task_group, args, kwargs, resources, versions, immediate, deferred, app_lock
    )
    task = await Task.objects.acreate(**task_payload)
    await task.arefresh_from_db()  # The database will have assigned a timestamp for us.
    task.pulp_domain = get_domain()
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
                task.app_lock = None
                await task.asave()
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
            task.app_lock = None
            await task.asave()
        else:
            # Can't acquire task lock and can't be deferred
            task.set_canceling()
            task.set_canceled(TASK_STATES.CANCELED, "Resources temporarily unavailable.")
    return task


def get_task_payload(
    function_name, task_group, args, kwargs, resources, versions, immediate, deferred, app_lock
):
    payload = {
        "state": TASK_STATES.WAITING,
        "logging_cid": (get_guid()),
        "task_group": task_group,
        "name": function_name,
        "enc_args": args,
        "enc_kwargs": kwargs,
        "parent_task": Task.current(),
        "reserved_resources_record": resources,
        "versions": versions,
        "immediate": immediate,
        "deferred": deferred,
        "profile_options": x_task_diagnostics_var.get(None),
        "app_lock": app_lock,
    }
    return payload


@contextmanager
def using_workdir():
    cur_dir = os.getcwd()
    with tempfile.TemporaryDirectory(dir=settings.WORKING_DIRECTORY) as working_dir:
        os.chdir(working_dir)
        try:
            yield
        finally:
            # Whether the task fails or not, we should always restore the workdir.
            os.chdir(cur_dir)


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


def called_from_content_app() -> bool:
    current_app = AppStatus.objects.current()
    return current_app is not None and current_app.app_type == "content"


def get_function_name(func):
    if callable(func):
        function_name = f"{func.__module__}.{func.__name__}"
    else:
        function_name = func
    return function_name


def get_version(versions, function_name):
    if versions is None:
        versions = MODULE_PLUGIN_VERSIONS[function_name.split(".", maxsplit=1)[0]]
    return versions


def get_resources(exclusive_resources, shared_resources, immediate):
    domain_prn = get_prn(get_domain())
    if exclusive_resources is None:
        exclusive_resources = []
    else:
        exclusive_resources = _validate_and_get_resources(exclusive_resources)
    if shared_resources is None:
        shared_resources = []
    else:
        shared_resources = _validate_and_get_resources(shared_resources)

    # A task that is exclusive on a domain will block all tasks within that domain
    if domain_prn not in exclusive_resources:
        shared_resources.append(domain_prn)
    resources = exclusive_resources + [f"shared:{resource}" for resource in shared_resources]

    # Compile a list of resources that must not be taken by other tasks.
    colliding_resources = []
    if immediate:
        colliding_resources = (
            shared_resources
            + exclusive_resources
            + [f"shared:{resource}" for resource in exclusive_resources]
        )
    return colliding_resources, resources


def cancel_task(task_id):
    """
    Cancel the task that is represented by the given task_id.

    This method cancels only the task with given task_id, not the spawned tasks. This also updates
    task's state to 'canceling'.

    Args:
        task_id (str): The ID of the task you wish to cancel

    Raises:
        rest_framework.exceptions.NotFound: If a task with given task_id does not exist
    """
    task = Task.objects.select_related("pulp_domain").get(pk=task_id)

    if task.state in TASK_FINAL_STATES:
        # If the task is already done, just stop.
        _logger.debug(
            "Task [{task_id}] in domain: {name} already in a final state: {state}".format(
                task_id=task_id, name=task.pulp_domain.name, state=task.state
            )
        )
        return task
    _logger.info(
        "Canceling task: {id} in domain: {name}".format(id=task_id, name=task.pulp_domain.name)
    )

    # This is the only valid transition without holding the task lock.
    task.set_canceling()
    # Notify the worker that might be running that task.
    with connection.cursor() as cursor:
        if task.app_lock is None:
            wakeup_worker(TASK_WAKEUP_HANDLE)
        else:
            cursor.execute("SELECT pg_notify('pulp_worker_cancel', %s)", (str(task.pk),))
    return task


def cancel_task_group(task_group_id):
    """
    Cancel the task group that is represented by the given task_group_id.

    This method attempts to cancel all tasks in the task group.

    Args:
        task_group_id (str): The ID of the task group you wish to cancel

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
