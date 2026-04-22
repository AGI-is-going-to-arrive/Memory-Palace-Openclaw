> [中文版](TOOLS.md)

# Memory Palace MCP Tool Reference

> Memory Palace exposes durable memory capabilities through
> [MCP (Model Context Protocol)](https://modelcontextprotocol.io/).
> This page is the English reference for the core tool surface.

<p align="center">
  <img src="images/runtime_architecture_en_4k.png" width="1100" alt="Runtime architecture overview" />
</p>

---

## Table of Contents

- [Quick Reference](#quick-reference)
- [Core Concepts](#core-concepts)
- [Tool Details](#tool-details)
  - [`read_memory`](#read_memory)
  - [`create_memory`](#create_memory)
  - [`update_memory`](#update_memory)
  - [`delete_memory`](#delete_memory)
  - [`add_alias`](#add_alias)
  - [`search_memory`](#search_memory)
  - [`compact_context`](#compact_context)
  - [`compact_context_reflection`](#compact_context_reflection)
  - [`rebuild_index`](#rebuild_index)
  - [`index_status`](#index_status)
  - [`ensure_visual_namespace_chain`](#ensure_visual_namespace_chain)

---

## Quick Reference

| Tool | Category | What it does |
|---|---|---|
| `read_memory` | read | read a memory by URI |
| `create_memory` | write | create a new memory under a parent URI |
| `update_memory` | write | patch or append to an existing memory |
| `delete_memory` | write | remove a memory path |
| `add_alias` | write | create another URI path pointing to the same memory |
| `search_memory` | search | keyword / semantic / hybrid memory search |
| `compact_context` | maintenance | persist a summary of the current session |
| `compact_context_reflection` | maintenance | reflect on the latest compact summary |
| `rebuild_index` | maintenance | rebuild retrieval indexes |
| `index_status` | maintenance | inspect index/runtime status |
| `ensure_visual_namespace_chain` | maintenance | pre-create visual namespace parents |

---

## Core Concepts

### URI shape

Memory Palace addresses memories by `domain://path`.

Examples:

```text
core://agent
writer://chapter_1/scene_2
system://boot
```

Common domains:

- `core`
- `writer`
- `system`

`system://*` URIs are read-only.

### Priority

Priority is an integer.
Lower numbers mean higher retrieval priority.

### Write Guard

`create_memory` and `update_memory` run through Write Guard first.
Write Guard may decide:

- create is safe
- update an existing memory instead
- no change is needed

That is how the system reduces duplicate durable writes.

---

## Tool Details

<a id="read_memory"></a>

### `read_memory`

Read a memory by URI.

Common uses:

- load `system://boot` at session start
- inspect a known durable fact
- partially read a large memory with `chunk_id`, `range`, or `max_chars`

System URIs:

| URI | Purpose |
|---|---|
| `system://boot` | startup context |
| `system://index` | full memory index |
| `system://index-lite` | lightweight index summary |
| `system://audit` | audit / observability summary |
| `system://recent` | latest updated memories |

<a id="create_memory"></a>

### `create_memory`

Create a memory under a parent URI.

Common inputs:

- `parent_uri`
- `content`
- `priority`
- optional `title`
- optional `disclosure`

If `title` is omitted, the system can assign the next numeric segment.

<a id="update_memory"></a>

### `update_memory`

Update an existing memory.

Two supported modes:

- patch mode
  - `old_string` + `new_string`
- append mode
  - `append`

Use patch mode when you want a precise replacement.
Use append mode when you want to add material to the end.

<a id="delete_memory"></a>

### `delete_memory`

Delete a memory path by URI.

Use carefully:

- deleting the path removes that route to the content
- read first if you are not completely sure

<a id="add_alias"></a>

### `add_alias`

Create another URI path pointing to the same underlying memory.

Use this when:

- one durable fact should be reachable from two namespaces
- you want a more user-facing path without duplicating content

<a id="search_memory"></a>

### `search_memory`

Search durable memory.

Supported search modes:

- `keyword`
- `semantic`
- `hybrid`

Useful knobs:

- `max_results`
- `candidate_multiplier`
- `include_session`
- `filters`
- `scope_hint`

Important boundary:

- search quality depends on the active profile and provider readiness
- `Profile B` is the safe bootstrap baseline
- `Profile C/D` unlock real embedding + reranker depth

<a id="compact_context"></a>

### `compact_context`

Persist a summary of the current session.

Use it when:

- the conversation has become long
- you want to preserve the important context before moving on

This is part of the promotion path from session context to durable memory.

<a id="compact_context_reflection"></a>

### `compact_context_reflection`

Reflect on the latest compact summary.

Use it when:

- you want to recover missed signals from the last compact
- you want a more stable reusable summary lane

<a id="rebuild_index"></a>

### `rebuild_index`

Trigger index rebuild or related index maintenance work.

Use it after:

- changing profile/provider shape
- changing embedding dimension
- large import / migration / replay work

<a id="index_status"></a>

### `index_status`

Inspect runtime and index health.

This is the tool to call when you want to check:

- FTS/vector availability
- embedding backend
- reranker enablement
- queue depth
- recent runtime status

<a id="ensure_visual_namespace_chain"></a>

### `ensure_visual_namespace_chain`

Pre-create visual-memory parent paths.

Use it when:

- you plan to store visual memories in a structured namespace
- you want to reduce repeated parent-creation round-trips

---

## Recommended Reading

- user install path:
  - [openclaw-doc/01-INSTALL_AND_RUN.en.md](openclaw-doc/01-INSTALL_AND_RUN.en.md)
- technical architecture:
  - [TECHNICAL_OVERVIEW.en.md](TECHNICAL_OVERVIEW.en.md)
- troubleshooting:
  - [TROUBLESHOOTING.en.md](TROUBLESHOOTING.en.md)
