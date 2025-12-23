import asyncio
import importlib
import logging
import os
import resource
import signal
import sys
import threading
import time
import tempfile
from gettext import gettext as _
from asgiref.sync import sync_to_async

from django.conf import settings
from django.db import connection, transaction, IntegrityError
from django.db.models import Q
from django.utils import timezone
from django_guid import set_guid
from django_guid.utils import generate_guid
from pulpcore.app.models import Artifact, Content, Task, TaskSchedule, ProfileArtifact
from pulpcore.app.redis_connection import get_redis_connection
from pulpcore.app.util import (
    configure_analytics,
    configure_cleanup,
    configure_periodic_telemetry,
)
from pulpcore.constants import TASK_FINAL_STATES, TASK_STATES

from pulp_service.app.tasks.util import (
    content_sources_periodic_telemetry,
    rhel_ai_repos_periodic_telemetry,
)

_logger = logging.getLogger(__name__)

# Redis key prefix for resource locks
REDIS_LOCK_PREFIX = "pulp:resource_lock:"

# Lua script for atomic lock release (only release if we own the lock)
REDIS_UNLOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

REDIS_ACQUIRE_LOCKS_SCRIPT = """
-- KEYS: [exclusive_lock_keys..., shared_lock_keys...]
-- ARGV[1]: lock_owner (task identifier)
-- ARGV[2]: number of exclusive resources
-- ARGV[3...]: exclusive resource names (for error reporting)
-- Returns: empty table if success, table of blocked exclusive resource names if failed

local num_exclusive = tonumber(ARGV[2])
local lock_owner = ARGV[1]
local acquired_exclusive = {}
local acquired_shared = {}
local blocked_resources = {}

-- Try to acquire exclusive locks
for i = 1, num_exclusive do
    local key = KEYS[i]
    local resource_name = ARGV[2 + i]

    -- Check if lock exists (either exclusive or shared)
    local lock_type = redis.call("type", key)
    if lock_type["ok"] == "string" then
        -- Exclusive lock already held
        table.insert(blocked_resources, resource_name)
    elseif lock_type["ok"] == "set" then
        -- Shared lock exists, check if set has members
        if redis.call("scard", key) > 0 then
            -- Resource is in shared use, can't acquire exclusive lock
            table.insert(blocked_resources, resource_name)
        end
    end
end

-- If any exclusive locks were blocked, don't proceed
if #blocked_resources > 0 then
    return blocked_resources
end

-- Check shared resources - ensure no exclusive locks exist
for i = num_exclusive + 1, #KEYS do
    local key = KEYS[i]
    local shared_resource_name = ARGV[2 + i]

    -- Check if there's an exclusive lock (string value)
    local lock_type = redis.call("type", key)
    if lock_type["ok"] == "string" then
        -- Exclusive lock exists on a shared resource we need
        -- This counts as a blocked resource
        table.insert(blocked_resources, shared_resource_name)
    end
end

-- If any shared resources are blocked by exclusive locks, fail
if #blocked_resources > 0 then
    return blocked_resources
end

-- All checks passed, acquire the locks
for i = 1, num_exclusive do
    local key = KEYS[i]
    redis.call("set", key, lock_owner)
    table.insert(acquired_exclusive, key)
end

for i = num_exclusive + 1, #KEYS do
    local key = KEYS[i]
    redis.call("sadd", key, lock_owner)
    table.insert(acquired_shared, key)
end

-- Return empty table to indicate success
return {}
"""


def resource_to_lock_key(resource_name):
    """
    Convert a resource name to a Redis lock key.

    Args:
        resource_name (str): The resource name (e.g., "prn:rpm.repository:abc123")

    Returns:
        str: A Redis key for the resource lock
    """
    return f"{REDIS_LOCK_PREFIX}{resource_name}"


def acquire_locks(redis_conn, lock_owner, exclusive_resources, shared_resources):
    """
    Atomically try to acquire exclusive and shared locks for resources.

    Args:
        redis_conn: Redis connection
        lock_owner (str): The identifier of the lock owner (worker/task)
        exclusive_resources (list): List of exclusive resource names
        shared_resources (list): List of shared resource names

    Returns:
        list: Empty list if all locks acquired successfully,
              list of blocked resource names if acquisition failed
    """
    if not redis_conn:
        return []

    # Sort resources deterministically to prevent deadlocks
    exclusive_resources = sorted(exclusive_resources) if exclusive_resources else []
    shared_resources = sorted(shared_resources) if shared_resources else []

    if not exclusive_resources and not shared_resources:
        return []

    # Build KEYS list: exclusive lock keys + shared lock keys
    keys = []
    for resource in exclusive_resources:
        keys.append(resource_to_lock_key(resource))
    for resource in shared_resources:
        keys.append(resource_to_lock_key(resource))

    # Build ARGV list: lock_owner, num_exclusive, resource names (for error reporting)
    args = [lock_owner, str(len(exclusive_resources))]
    args.extend(exclusive_resources)
    args.extend(shared_resources)

    # Register and execute the Lua script
    acquire_script = redis_conn.register_script(REDIS_ACQUIRE_LOCKS_SCRIPT)
    try:
        blocked_resources = acquire_script(keys=keys, args=args)
        # Redis returns list of blocked resources or empty list
        return blocked_resources if blocked_resources else []
    except Exception as e:
        _logger.error("Error acquiring locks: %s", e)
        return ["error"]  # Return non-empty list to indicate failure


def release_shared_resource_locks(redis_conn, lock_owner, shared_resources):
    """
    Release shared resource locks by removing task from Redis sets.

    Args:
        redis_conn: Redis connection
        lock_owner (str): The identifier of the lock owner (task ID)
        shared_resources (list): List of shared resource names
    """
    if not redis_conn or not shared_resources:
        return

    for resource in shared_resources:
        try:
            lock_key = resource_to_lock_key(resource)
            # Remove this task from the shared resource set
            removed = redis_conn.srem(lock_key, lock_owner)
            if removed:
                _logger.debug("Released shared resource: %s", resource)
            else:
                _logger.warning("Shared resource %s did not contain %s", resource, lock_owner)
        except Exception as e:
            _logger.error("Error releasing shared resource %s: %s", resource, e)


def release_resource_locks(redis_conn, lock_owner, resources, shared_resources=None):
    """
    Release Redis distributed locks for exclusive and shared resources.

    Uses a Lua script to ensure we only release exclusive locks that we own.
    Removes task from shared resource sets.

    Args:
        redis_conn: Redis connection
        lock_owner (str): The identifier of the lock owner
        resources (list): List of exclusive resource names to release locks for
        shared_resources (list): Optional list of shared resource names
    """
    if not redis_conn:
        return

    # Release exclusive locks
    if resources:
        # Register the unlock script
        unlock_script = redis_conn.register_script(REDIS_UNLOCK_SCRIPT)

        for resource in resources:
            try:
                lock_key = resource_to_lock_key(resource)
                # Use Lua script to atomically check and delete only if we own the lock
                released = unlock_script(keys=[lock_key], args=[lock_owner])
                if released:
                    _logger.debug("Released exclusive lock for resource: %s", resource)
                else:
                    _logger.warning("Lock for resource %s was not owned by %s", resource, lock_owner)
            except Exception as e:
                _logger.error("Error releasing lock for resource %s: %s", resource, e)

    # Release shared resources
    if shared_resources:
        release_shared_resource_locks(redis_conn, lock_owner, shared_resources)


async def async_release_resource_locks(redis_conn, lock_owner, resources):
    """
    Async version: Release Redis distributed locks for the given resources.

    Uses a Lua script to ensure we only release locks that we own.

    Args:
        redis_conn: Redis connection
        lock_owner (str): The identifier of the lock owner
        resources (list): List of resource names to release locks for
    """
    if not redis_conn:
        return

    # Register the unlock script
    unlock_script = await sync_to_async(redis_conn.register_script)(REDIS_UNLOCK_SCRIPT)

    for resource in resources:
        try:
            lock_key = resource_to_lock_key(resource)
            # Use Lua script to atomically check and delete only if we own the lock
            released = await sync_to_async(unlock_script)(keys=[lock_key], args=[lock_owner])
            if released:
                _logger.debug("Released lock for resource: %s", resource)
            else:
                _logger.warning("Lock for resource %s was not owned by %s", resource, lock_owner)
        except Exception as e:
            _logger.error("Error releasing lock for resource %s: %s", resource, e)


def startup_hook():
    configure_analytics()
    configure_cleanup()
    configure_periodic_telemetry()
    content_sources_periodic_telemetry()
    rhel_ai_repos_periodic_telemetry()


def delete_incomplete_resources(task):
    """
    Delete all incomplete created-resources on a canceled task.

    Args:
        task (Task): A task.
    """
    if task.state != TASK_STATES.CANCELING:
        raise RuntimeError(_("Task must be canceling."))
    for model in (r.content_object for r in task.created_resources.all()):
        if isinstance(model, (Artifact, Content)):
            continue
        try:
            if model.complete:
                continue
        except AttributeError:
            continue
        try:
            with transaction.atomic():
                model.delete()
        except Exception as error:
            _logger.error(_("Delete created resource, failed: {}").format(str(error)))


def write_memory_usage(stop_event, path):
    with open(path, "w") as file:
        file.write("# Seconds\tMemory in MB\n")
        seconds = 0
        while not stop_event.is_set():
            current_mb_in_use = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            file.write(f"{seconds}\t{current_mb_in_use:.2f}\n")
            file.flush()
            time.sleep(2)
            seconds += 2


def child_signal_handler(sig, frame):
    _logger.debug("Signal %s recieved by %s.", sig, os.getpid())
    # Reset signal handlers to default
    # If you kill the process a second time it's not graceful anymore.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)

    if sig == signal.SIGUSR1:
        sys.exit()


def perform_task(task_pk, task_working_dir_rel_path):
    """Setup the environment to handle a task and execute it.
    This must be called as a subprocess, while the parent holds the advisory lock of the task."""
    from pulpcore.tasking.tasks import execute_task

    signal.signal(signal.SIGINT, child_signal_handler)
    signal.signal(signal.SIGTERM, child_signal_handler)
    signal.signal(signal.SIGHUP, child_signal_handler)
    signal.signal(signal.SIGUSR1, child_signal_handler)
    # All processes need to create their own postgres connection
    connection.connection = None
    # enc_args and enc_kwargs are deferred by default but we actually want them
    task = Task.objects.defer(None).select_related("pulp_domain").get(pk=task_pk)
    # Isolate from the parent asyncio.
    asyncio.set_event_loop(asyncio.new_event_loop())
    # Set current contexts
    os.chdir(task_working_dir_rel_path)

    if task.profile_options:
        profilers = set(task.profile_options) & set(settings.TASK_DIAGNOSTICS)
        if unavailable_profilers := set(task.profile_options) - set(settings.TASK_DIAGNOSTICS):
            _logger.warning(
                "Requested task diagnostic profilers are not available: %s",
                unavailable_profilers,
            )
        _execute_task_and_profile(task, profilers)
    else:
        execute_task(task)


def _execute_task_and_profile(task, profile_options):
    from pulpcore.tasking.tasks import execute_task

    with tempfile.TemporaryDirectory(dir=settings.WORKING_DIRECTORY) as temp_dir:
        _execute_task = execute_task

        if "memory" in profile_options:
            _execute_task = _memory_diagnostic_decorator(temp_dir, _execute_task)
        if "pyinstrument" in profile_options:
            _execute_task = _pyinstrument_diagnostic_decorator(temp_dir, _execute_task)
        if "memray" in profile_options:
            _execute_task = _memray_diagnostic_decorator(temp_dir, _execute_task)

        _execute_task(task)


def _memory_diagnostic_decorator(temp_dir, func):
    def __memory_diagnostic_decorator(task):
        mem_diagnostics_file_path = os.path.join(temp_dir, "memory_profile.datum")
        # It would be better to have this recording happen in the parent process instead of here
        # https://github.com/pulp/pulpcore/issues/2337
        stop_event = threading.Event()
        mem_diagnostics_thread = threading.Thread(
            target=write_memory_usage, args=(stop_event, mem_diagnostics_file_path), daemon=True
        )
        mem_diagnostics_thread.start()

        func(task)

        stop_event.set()
        artifact = Artifact.init_and_validate(mem_diagnostics_file_path)
        try:
            # it is possible for the diagnostic artifact (memory report) to be identical to
            # a previous report, in which case we need to handle the case where saving a new
            # artifact fails.
            artifact.save()
        except IntegrityError:
            artifact = Artifact.objects.get(sha256=artifact.sha256)

        ProfileArtifact.objects.get_or_create(artifact=artifact, name="memory_profile", task=task)
        _logger.info("Created memory diagnostic data.")

    return __memory_diagnostic_decorator


def _pyinstrument_diagnostic_decorator(temp_dir, func):
    def __pyinstrument_diagnostic_decorator(task):
        if importlib.util.find_spec("pyinstrument") is not None:
            from pyinstrument import Profiler

            with Profiler(interval=0.002) as profiler:
                func(task)

            profile_file_path = os.path.join(temp_dir, "pyinstrument.html")
            with open(profile_file_path, "w+") as f:
                f.write(profiler.output_html())
                f.flush()

            artifact = Artifact.init_and_validate(str(profile_file_path))
            try:
                # it is possible for the diagnostic artifact (memory report) to be identical to
                # a previous report, in which case we need to handle the case where saving a new
                # artifact fails.
                artifact.save()
            except IntegrityError:
                artifact = Artifact.objects.get(sha256=artifact.sha256)

            ProfileArtifact.objects.get_or_create(
                artifact=artifact, name="pyinstrument_profile", task=task
            )
            _logger.info("Created pyinstrument profile data.")
        else:
            func(task)

    return __pyinstrument_diagnostic_decorator


def _memray_diagnostic_decorator(temp_dir, func):
    def __memray_diagnostic_decorator(task):
        if importlib.util.find_spec("memray") is not None:
            import memray

            profile_file_path = os.path.join(temp_dir, "memray_profile.bin")
            with memray.Tracker(
                profile_file_path,
                native_traces=False,
                file_format=memray.FileFormat.AGGREGATED_ALLOCATIONS,
            ):
                func(task)

            artifact = Artifact.init_and_validate(str(profile_file_path))
            try:
                # it is possible for the diagnostic artifact (memory report) to be identical to
                # a previous report, in which case we need to handle the case where saving a new
                # artifact fails.
                artifact.save()
            except IntegrityError:
                artifact = Artifact.objects.get(sha256=artifact.sha256)

            ProfileArtifact.objects.get_or_create(
                artifact=artifact, name="memray_profile", task=task
            )
            _logger.info("Created memray memory profile data.")
        else:
            func(task)

    return __memray_diagnostic_decorator


def dispatch_scheduled_tasks():
    from pulpcore.tasking.tasks import dispatch

    # Warning, dispatch_scheduled_tasks is not race condition free!
    now = timezone.now()
    # Dispatch all tasks old enough and not still running
    for task_schedule in TaskSchedule.objects.filter(next_dispatch__lte=now).filter(
        Q(last_task=None) | Q(last_task__state__in=TASK_FINAL_STATES)
    ):
        try:
            if task_schedule.dispatch_interval is None:
                # This was a timed one shot task schedule
                task_schedule.next_dispatch = None
            else:
                # This is a recurring task schedule
                while task_schedule.next_dispatch < now:
                    # Do not schedule in the past
                    task_schedule.next_dispatch += task_schedule.dispatch_interval
            set_guid(generate_guid())
            with transaction.atomic():
                task_schedule.last_task = dispatch(
                    task_schedule.task_name,
                )
                task_schedule.save(update_fields=["next_dispatch", "last_task"])

            _logger.info(
                "Dispatched scheduled task {task_name} as task id {task_id}".format(
                    task_name=task_schedule.task_name, task_id=task_schedule.last_task.pk
                )
            )
        except Exception as e:
            _logger.warning(
                "Dispatching scheduled task {task_name} failed. {error}".format(
                    task_name=task_schedule.task_name, error=str(e)
                )
            )
