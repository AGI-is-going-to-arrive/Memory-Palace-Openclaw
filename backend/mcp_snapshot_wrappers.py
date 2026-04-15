from typing import Any, Callable, Optional


async def snapshot_memory_content_wrapper_impl(
    uri: str,
    *,
    snapshot_impl: Callable[..., Any],
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    get_sqlite_client: Callable[[], Any],
) -> bool:
    return await snapshot_impl(
        uri,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        make_uri=make_uri,
        get_sqlite_client=get_sqlite_client,
    )


async def snapshot_path_meta_wrapper_impl(
    uri: str,
    *,
    snapshot_impl: Callable[..., Any],
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    get_sqlite_client: Callable[[], Any],
) -> bool:
    return await snapshot_impl(
        uri,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        get_sqlite_client=get_sqlite_client,
    )


async def snapshot_path_create_wrapper_impl(
    uri: str,
    memory_id: int,
    *,
    snapshot_impl: Callable[..., Any],
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    operation_type: str = "create",
    target_uri: Optional[str] = None,
) -> bool:
    return await snapshot_impl(
        uri,
        memory_id,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        operation_type=operation_type,
        target_uri=target_uri,
    )


async def snapshot_path_delete_wrapper_impl(
    uri: str,
    *,
    snapshot_impl: Callable[..., Any],
    get_snapshot_manager: Callable[[], Any],
    get_session_id: Callable[[], str],
    parse_uri: Callable[[str], tuple[str, str]],
    get_sqlite_client: Callable[[], Any],
) -> bool:
    return await snapshot_impl(
        uri,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        get_sqlite_client=get_sqlite_client,
    )
