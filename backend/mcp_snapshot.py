from typing import Any, Callable, Optional


async def snapshot_memory_content(
    uri: str,
    *,
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    get_sqlite_client: Callable[[], Any],
) -> bool:
    """
    Snapshot memory content before modification.

    Uses memory:{id} as resource_id so it never collides with path snapshots.
    Idempotent per URI per session: when a memory is updated multiple times,
    each update produces a new memory_id (version chain), but only the FIRST
    version is snapshotted. Subsequent updates to the same URI are no-ops.
    """
    manager = get_snapshot_manager()
    session_id = get_session_id()

    domain, path = parse_uri(uri)
    full_uri = make_uri(domain, path)
    client = get_sqlite_client()
    memory = await client.get_memory_by_path(path, domain)

    if not memory:
        return False

    resource_id = f"memory:{memory['id']}"

    if manager.has_snapshot(session_id, resource_id):
        return False

    if manager.find_memory_snapshot_by_uri(session_id, full_uri):
        return False

    memory_full = await client.get_memory_by_id(memory["id"])
    all_paths = memory_full.get("paths", []) if memory_full else []

    return manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="memory",
        snapshot_data={
            "operation_type": "modify_content",
            "memory_id": memory["id"],
            "uri": full_uri,
            "domain": domain,
            "path": path,
            "all_paths": all_paths,
        },
    )


async def snapshot_path_meta(
    uri: str,
    *,
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    get_sqlite_client: Callable[[], Any],
) -> bool:
    """Snapshot path metadata (priority/disclosure) before modification."""
    manager = get_snapshot_manager()
    session_id = get_session_id()

    if manager.has_snapshot(session_id, uri):
        return False

    domain, path = parse_uri(uri)
    client = get_sqlite_client()
    memory = await client.get_memory_by_path(path, domain)

    if not memory:
        return False

    return manager.create_snapshot(
        session_id=session_id,
        resource_id=uri,
        resource_type="path",
        snapshot_data={
            "operation_type": "modify_meta",
            "domain": domain,
            "path": path,
            "uri": uri,
            "memory_id": memory["id"],
            "priority": memory.get("priority"),
            "disclosure": memory.get("disclosure"),
        },
    )


async def snapshot_path_create(
    uri: str,
    memory_id: int,
    *,
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    operation_type: str = "create",
    target_uri: Optional[str] = None,
) -> bool:
    """Record that a path was created (for rollback = remove the path)."""
    manager = get_snapshot_manager()
    session_id = get_session_id()

    domain, path = parse_uri(uri)

    data = {
        "operation_type": operation_type,
        "domain": domain,
        "path": path,
        "uri": uri,
        "memory_id": memory_id,
    }
    if target_uri:
        data["target_uri"] = target_uri

    return manager.create_snapshot(
        session_id=session_id,
        resource_id=uri,
        resource_type="path",
        snapshot_data=data,
    )


async def snapshot_path_delete(
    uri: str,
    *,
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    get_sqlite_client: Callable[[], Any],
) -> bool:
    """
    Record that a path is being deleted (for rollback = re-create).

    Two cases depending on what path snapshot already exists for this URI:
    1. Existing create/create_alias snapshot: create+delete cancel out.
    2. Otherwise capture current state as a delete snapshot.
    """
    manager = get_snapshot_manager()
    session_id = get_session_id()

    existing = manager.get_snapshot(session_id, uri)
    if existing:
        existing_op = existing.get("data", {}).get("operation_type")
        if existing_op in ("create", "create_alias"):
            content_snap_id = manager.find_memory_snapshot_by_uri(session_id, uri)
            if content_snap_id:
                manager.delete_snapshot(session_id, content_snap_id)
            manager.delete_snapshot(session_id, uri)
            return False

    domain, path = parse_uri(uri)
    client = get_sqlite_client()
    memory = await client.get_memory_by_path(path, domain)

    if not memory:
        return False

    priority = memory.get("priority")
    disclosure = memory.get("disclosure")
    if existing and existing.get("data", {}).get("operation_type") == "modify_meta":
        priority = existing["data"].get("priority", priority)
        disclosure = existing["data"].get("disclosure", disclosure)

    return manager.create_snapshot(
        session_id=session_id,
        resource_id=uri,
        resource_type="path",
        snapshot_data={
            "operation_type": "delete",
            "domain": domain,
            "path": path,
            "uri": uri,
            "memory_id": memory["id"],
            "priority": priority,
            "disclosure": disclosure,
        },
        force=True,
    )
