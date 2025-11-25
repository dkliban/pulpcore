"""
Redis distributed lock utilities for task resource coordination.

This module provides functions and Lua scripts for managing exclusive and shared
resource locks using Redis.
"""

import logging
from asgiref.sync import sync_to_async

from pulpcore.app.redis_connection import get_redis_connection


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

    -- Check if lock exists
    if redis.call("exists", key) == 1 then
        -- Lock already held, add to blocked list
        table.insert(blocked_resources, resource_name)
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
