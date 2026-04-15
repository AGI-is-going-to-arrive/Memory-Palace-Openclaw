#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import locale
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import venv
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


PLUGIN_ID = "memory-palace"
MEMORY_CORE_COMPAT_PLUGIN_ID = "memory-core"
PROFILE_VALUES = ("a", "b", "c", "d")
MODE_VALUES = ("basic", "full", "dev")
TRANSPORT_VALUES = ("stdio", "sse")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5173
DASHBOARD_START_TIMEOUT_SECONDS = 20.0
BACKEND_API_HOST = "127.0.0.1"
BACKEND_API_PORT = 8000
BACKEND_API_START_TIMEOUT_SECONDS = 20.0
RUNTIME_REQUIREMENTS_FILE_NAMES = ("requirements-runtime.txt", "requirements.txt")
RUNTIME_REQUIREMENTS_INSTALL_RETRIES = 3
DASHBOARD_DEPENDENCY_INSTALL_RETRIES = 3
DASHBOARD_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 600
SUPPORTED_PYTHON_MIN_MINOR = 10
SUPPORTED_PYTHON_MAX_MINOR = 14
MIN_OPENCLAW_VERSION = (2026, 3, 2)
MIN_OPENCLAW_VERSION_TEXT = ".".join(str(part) for part in MIN_OPENCLAW_VERSION)
EMBEDDING_DIMENSION_PROBE_MAX = 8192
RESTART_RELEVANT_ENV_KEYS = (
    "DATABASE_URL",
    "MCP_API_KEY",
    "MCP_API_KEY_ALLOW_INSECURE_LOCAL",
    "SEARCH_DEFAULT_MODE",
    "RETRIEVAL_EMBEDDING_BACKEND",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_API_BASE",
    "RETRIEVAL_EMBEDDING_DIM",
    "RETRIEVAL_RERANKER_ENABLED",
    "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_MODEL",
    "WRITE_GUARD_LLM_ENABLED",
    "WRITE_GUARD_LLM_API_BASE",
    "WRITE_GUARD_LLM_MODEL",
    "COMPACT_GIST_LLM_ENABLED",
    "COMPACT_GIST_LLM_API_BASE",
    "COMPACT_GIST_LLM_MODEL",
    "RUNTIME_INDEX_WORKER_ENABLED",
    "RUNTIME_INDEX_DEFER_ON_WRITE",
    "RUNTIME_AUTO_FLUSH_ENABLED",
)
REINDEX_RELEVANT_ENV_KEYS = (
    "SEARCH_DEFAULT_MODE",
    "RETRIEVAL_EMBEDDING_BACKEND",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_API_BASE",
    "RETRIEVAL_EMBEDDING_DIM",
    "RETRIEVAL_RERANKER_ENABLED",
    "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_MODEL",
    "RETRIEVAL_VECTOR_ENGINE",
    "RETRIEVAL_SQLITE_VEC_ENABLED",
    "RETRIEVAL_SQLITE_VEC_READ_RATIO",
)
CLI_I18N_MESSAGES = {
    "en": {
        "profile_env_prompt_intro": "Profile {profile} needs additional model settings before setup can continue.",
        "profile_env_prompt_missing": "Missing fields: {fields}",
        "profile_prompt_choice": "Choose how to provide Profile {profile} settings: [1] .env file  [2] manual input  [Enter] fall back to Profile B: ",
        "profile_prompt_choice_invalid": "Please enter 1, 2, or press Enter.",
        "profile_c_llm_intro": "Profile C can optionally enable an LLM assist suite after embedding + reranker are ready.",
        "profile_c_llm_option_write_guard": "write_guard: screens risky or contradictory durable writes before they are committed.",
        "profile_c_llm_option_compact_gist": "compact_gist: produces richer compact_context summaries than the extractive fallback.",
        "profile_c_llm_option_intent": "intent_llm: improves intent routing/classification for ambiguous queries; it remains experimental.",
        "profile_c_llm_choice": "Enable the optional Profile C LLM assist suite? [y] yes  [Enter/n] no: ",
        "profile_c_llm_choice_invalid": "Please answer y/yes or press Enter/n to skip.",
        "profile_c_llm_skip": "Skipping optional LLM assists for Profile C.",
        "profile_c_llm_probe_failed": "Profile C optional LLM probe failed. Keeping Profile C, but leaving optional LLM assists disabled for now.",
        "profile_c_llm_probe_failed_detail": "LLM probe detail: {detail}",
        "profile_shared_llm_intro": "Provide one shared OpenAI-compatible chat configuration. It will be used for: {features}.",
        "profile_shared_llm_base_prompt": "Shared LLM API base URL: ",
        "profile_shared_llm_key_prompt": "Shared LLM API key: ",
        "profile_shared_llm_model_prompt": "Shared LLM model name: ",
        "profile_shared_llm_example": "Example: {example}",
        "profile_shared_llm_hint": "How to fill: {hint}",
        "profile_shared_llm_cancelled": "Optional LLM setup was cancelled because one or more fields were left blank.",
        "profile_shared_llm_action": "captured shared LLM settings for: {features}",
        "profile_env_prompt_request": "Enter a .env file path for Profile {profile}: ",
        "profile_env_prompt_invalid_path": "The env file does not exist: {path}",
        "profile_env_prompt_empty_file": "The env file did not contain usable values: {path}",
        "profile_env_prompt_imported": "Imported profile env from {path}.",
        "profile_env_prompt_still_missing": "The imported env file is still missing required fields: {fields}",
        "profile_env_prompt_fallback": "No env file provided. Falling back to Profile B.",
        "profile_env_prompt_action": "imported profile env from {path}",
        "profile_manual_intro": "Enter the missing Profile {profile} values below. Press Enter to leave a field unchanged.",
        "profile_manual_example": "Example: {example}",
        "profile_manual_hint": "How to fill: {hint}",
        "profile_manual_prompt": "{label}: ",
        "profile_manual_input_action": "captured manual profile fields: {fields}",
        "profile_probe_intro": "Checking model connectivity for Profile {profile}...",
        "profile_probe_failed": "These Profile {profile} model probes failed: {components}. Falling back to Profile B for now.",
        "profile_probe_failed_strict": "These Profile {profile} model probes failed: {components}.",
        "profile_probe_component_detail": "{component}: {detail}",
        "profile_probe_retry_env": "Fix your model settings in {path} and rerun setup with Profile {profile}.",
        "profile_probe_retry_flags": "Alternatively rerun setup with explicit flags such as --embedding-api-base / --reranker-api-base / --llm-api-base.",
        "profile_probe_embedding_dim_aligned": "Profile {profile} embedding probe detected dimension {detected}; updated RETRIEVAL_EMBEDDING_DIM from {configured}.",
        "profile_prompted_env_saved": "saved prompted profile values to {path}",
    },
    "zh": {
        "profile_env_prompt_intro": "Profile {profile} 继续 setup 前需要补充模型配置。",
        "profile_env_prompt_missing": "缺失字段: {fields}",
        "profile_prompt_choice": "请选择 Profile {profile} 的配置方式：[1] 提供 .env 文件  [2] 手动逐项输入  [回车] 自动回退到 Profile B：",
        "profile_prompt_choice_invalid": "请输入 1、2，或直接回车。",
        "profile_c_llm_intro": "Profile C 在 embedding + reranker 之外，还可以额外开启一组可选 LLM 辅助能力。",
        "profile_c_llm_option_write_guard": "write_guard：在 durable write 真正落盘前，先筛掉高风险或自相矛盾的写入。",
        "profile_c_llm_option_compact_gist": "compact_gist：比纯抽取回退更适合生成 compact_context 摘要。",
        "profile_c_llm_option_intent": "intent_llm：提升模糊查询的意图分类/路由，当前仍属实验性能力。",
        "profile_c_llm_choice": "是否为 Profile C 开启可选 LLM 辅助套件？[y] 开启  [回车/n] 跳过：",
        "profile_c_llm_choice_invalid": "请输入 y/yes，或直接回车/n 跳过。",
        "profile_c_llm_skip": "当前跳过 Profile C 的可选 LLM 辅助能力。",
        "profile_c_llm_probe_failed": "Profile C 的可选 LLM 探测失败。当前继续保留 Profile C，但先把可选 LLM 辅助保持关闭。",
        "profile_c_llm_probe_failed_detail": "LLM 探测详情：{detail}",
        "profile_shared_llm_intro": "请填写一套共享的 OpenAI-compatible chat 配置。它会被用于：{features}。",
        "profile_shared_llm_base_prompt": "共享 LLM API Base URL：",
        "profile_shared_llm_key_prompt": "共享 LLM API Key：",
        "profile_shared_llm_model_prompt": "共享 LLM 模型名：",
        "profile_shared_llm_example": "示例：{example}",
        "profile_shared_llm_hint": "填写说明：{hint}",
        "profile_shared_llm_cancelled": "由于有字段留空，当前已取消可选 LLM 配置。",
        "profile_shared_llm_action": "已录入共享 LLM 配置，用途：{features}",
        "profile_env_prompt_request": "请输入 Profile {profile} 对应的 .env 文件路径：",
        "profile_env_prompt_invalid_path": "找不到 env 文件: {path}",
        "profile_env_prompt_empty_file": "env 文件里没有可用配置: {path}",
        "profile_env_prompt_imported": "已导入 profile env: {path}",
        "profile_env_prompt_still_missing": "导入的 env 后仍缺少这些字段: {fields}",
        "profile_env_prompt_fallback": "未提供 env 文件，当前将自动回退到 Profile B。",
        "profile_env_prompt_action": "已导入 profile env: {path}",
        "profile_manual_intro": "请继续补全 Profile {profile} 缺失的字段。直接回车表示先跳过该项。",
        "profile_manual_example": "示例: {example}",
        "profile_manual_hint": "填写说明: {hint}",
        "profile_manual_prompt": "{label}: ",
        "profile_manual_input_action": "已手动录入这些字段: {fields}",
        "profile_probe_intro": "正在检查 Profile {profile} 的模型连通性...",
        "profile_probe_failed": "这些 Profile {profile} 模型不可用: {components}。当前会临时回退到 Profile B 继续安装。",
        "profile_probe_failed_strict": "这些 Profile {profile} 模型不可用: {components}。",
        "profile_probe_component_detail": "{component}: {detail}",
        "profile_probe_retry_env": "请在 {path} 中修好模型配置，然后重新执行 setup --profile {profile}。",
        "profile_probe_retry_flags": "也可以改为通过 --embedding-api-base / --reranker-api-base / --llm-api-base 等参数重新配置。",
        "profile_probe_embedding_dim_aligned": "Profile {profile} 的 embedding 探测返回维度 {detected}，已将 RETRIEVAL_EMBEDDING_DIM 从 {configured} 自动调整为 {detected}。",
        "profile_prompted_env_saved": "已将本次填写的 profile 配置保存到 {path}",
    },
}
PROFILE_MANUAL_FIELD_SPECS = {
    "RETRIEVAL_EMBEDDING_API_BASE": {
        "label_en": "Embedding API base URL",
        "label_zh": "Embedding API Base URL",
        "example": "https://router.example.com/v1/embeddings",
        "hint_en": "Use your OpenAI-compatible embedding endpoint; /embeddings suffix is allowed.",
        "hint_zh": "填写 OpenAI-compatible embedding 接口地址；可以直接带 /embeddings 后缀。",
        "secret": False,
    },
    "RETRIEVAL_EMBEDDING_API_KEY": {
        "label_en": "Embedding API key",
        "label_zh": "Embedding API Key",
        "example": "sk-embed-xxxxxxxx",
        "hint_en": "Paste the embedding service API key.",
        "hint_zh": "填写 embedding 服务的 API key。",
        "secret": True,
    },
    "RETRIEVAL_EMBEDDING_MODEL": {
        "label_en": "Embedding model name",
        "label_zh": "Embedding 模型名",
        "example": "Qwen3-Embedding-8B",
        "hint_en": "Use the exact model name accepted by the embedding endpoint.",
        "hint_zh": "填写 embedding 服务实际接受的模型名。",
        "secret": False,
    },
    "RETRIEVAL_RERANKER_API_BASE": {
        "label_en": "Reranker API base URL",
        "label_zh": "Reranker API Base URL",
        "example": "https://router.example.com/v1/rerank",
        "hint_en": "Use the reranker endpoint base; /rerank suffix is allowed.",
        "hint_zh": "填写 reranker 接口地址；可以直接带 /rerank 后缀。",
        "secret": False,
    },
    "RETRIEVAL_RERANKER_API_KEY": {
        "label_en": "Reranker API key",
        "label_zh": "Reranker API Key",
        "example": "sk-rerank-xxxxxxxx",
        "hint_en": "Paste the reranker service API key.",
        "hint_zh": "填写 reranker 服务的 API key。",
        "secret": True,
    },
    "RETRIEVAL_RERANKER_MODEL": {
        "label_en": "Reranker model name",
        "label_zh": "Reranker 模型名",
        "example": "Qwen/Qwen3-Reranker-8B",
        "hint_en": "Use the exact model name accepted by the reranker endpoint.",
        "hint_zh": "填写 reranker 服务实际接受的模型名。",
        "secret": False,
    },
    "WRITE_GUARD_LLM_API_BASE": {
        "label_en": "LLM API base URL",
        "label_zh": "LLM API Base URL",
        "example": "https://router.example.com/v1",
        "hint_en": "Use an OpenAI-compatible chat endpoint base; /chat/completions suffix is optional.",
        "hint_zh": "填写 OpenAI-compatible 对话接口地址；/chat/completions 后缀可省略。",
        "secret": False,
    },
    "WRITE_GUARD_LLM_API_KEY": {
        "label_en": "LLM API key",
        "label_zh": "LLM API Key",
        "example": "sk-llm-xxxxxxxx",
        "hint_en": "Paste the LLM service API key.",
        "hint_zh": "填写 LLM 服务的 API key。",
        "secret": True,
    },
    "WRITE_GUARD_LLM_MODEL": {
        "label_en": "LLM model name",
        "label_zh": "LLM 模型名",
        "example": "gpt-5.4",
        "hint_en": "Use the exact model name accepted by the chat endpoint.",
        "hint_zh": "填写聊天接口实际接受的模型名。",
        "secret": False,
    },
    "INTENT_LLM_API_BASE": {
        "label_en": "Intent LLM API base URL",
        "label_zh": "Intent LLM API Base URL",
        "example": "https://router.example.com/v1",
        "hint_en": "Use an OpenAI-compatible chat endpoint base for intent classification.",
        "hint_zh": "填写意图分类使用的 OpenAI-compatible 对话接口地址。",
        "secret": False,
    },
    "INTENT_LLM_API_KEY": {
        "label_en": "Intent LLM API key",
        "label_zh": "Intent LLM API Key",
        "example": "sk-llm-xxxxxxxx",
        "hint_en": "Paste the API key used for intent classification.",
        "hint_zh": "填写意图分类使用的 API key。",
        "secret": True,
    },
    "INTENT_LLM_MODEL": {
        "label_en": "Intent LLM model name",
        "label_zh": "Intent LLM 模型名",
        "example": "gpt-5.4-mini",
        "hint_en": "Use the exact model name used for intent classification.",
        "hint_zh": "填写意图分类使用的模型名。",
        "secret": False,
    },
}

PROVIDER_PROBE_COMPONENT_FIELDS = {
    "embedding": (
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
    ),
    "reranker": (
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
    ),
    "llm": (
        "WRITE_GUARD_LLM_API_BASE",
        "WRITE_GUARD_LLM_API_KEY",
        "WRITE_GUARD_LLM_MODEL",
    ),
}

ONBOARDING_PROVIDER_COMPONENT_ORDER = ("embedding", "reranker", "llm")

ONBOARDING_PROVIDER_BASE_URL_FORMS = {
    "embedding": [
        "https://provider.example/v1",
        "https://provider.example/v1/embeddings",
    ],
    "reranker": [
        "https://provider.example/v1",
        "https://provider.example/v1/rerank",
    ],
    "llm": [
        "https://provider.example/v1",
        "https://provider.example/v1/chat/completions",
        "https://provider.example/v1/responses",
    ],
}

RETRIEVAL_PROVIDER_RUNTIME_ENV_KEYS = (
    "ROUTER_API_BASE",
    "ROUTER_API_KEY",
    "ROUTER_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_API_BASE",
    "RETRIEVAL_EMBEDDING_API_KEY",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_DIM",
    "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_API_KEY",
    "RETRIEVAL_RERANKER_MODEL",
    "RETRIEVAL_RERANKER_WEIGHT",
    "RETRIEVAL_RERANKER_FALLBACK_API_BASE",
    "RETRIEVAL_RERANKER_FALLBACK_API_KEY",
    "RETRIEVAL_RERANKER_FALLBACK_MODEL",
    "RETRIEVAL_RERANKER_FALLBACK_PROVIDER",
    "RETRIEVAL_RERANKER_FALLBACK_TIMEOUT_SEC",
)

_PROFILE_PLACEHOLDER_MARKERS = (
    "replace-with-your-",
    "replace-with-your-key",
    "<your-",
    "127.0.0.1:port",
    "host.docker.internal:port",
    "https://<",
    "http://<",
)

# ---------------------------------------------------------------------------
# OpenClaw host configuration search paths (IMP-1)
# ---------------------------------------------------------------------------
OPENCLAW_CONFIG_SEARCH_PATHS = (
    Path.home() / ".openclaw" / "config.json",
    Path.home() / ".config" / "openclaw" / "config.json",
)

# Canonical provider type names used by ProviderSeed
PROVIDER_TYPES = ("embedding", "reranker", "llm")

# Source labels (descending priority)
SEED_SOURCE_EXPLICIT = "explicit"
SEED_SOURCE_CHAT_INPUT = "chat_input"
SEED_SOURCE_RUNTIME_ENV = "runtime_env"
SEED_SOURCE_HOST_CONFIG = "host_config"
SEED_SOURCE_PROCESS_ENV = "process_env"

SEED_SOURCE_LOCAL_DISCOVERY = "local_discovery"

SEED_SOURCES_PRIORITY = (
    SEED_SOURCE_EXPLICIT,
    SEED_SOURCE_CHAT_INPUT,
    SEED_SOURCE_RUNTIME_ENV,
    SEED_SOURCE_HOST_CONFIG,
    SEED_SOURCE_PROCESS_ENV,
    SEED_SOURCE_LOCAL_DISCOVERY,
)

# Local provider discovery constants
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
LOCAL_DISCOVERY_TIMEOUT_SEC = 3

# Confidence levels
SEED_CONFIDENCE_HIGH = "high"
SEED_CONFIDENCE_MEDIUM = "medium"
SEED_CONFIDENCE_LOW = "low"

# Known embedding model name patterns (substring match, case-insensitive)
EMBEDDING_MODEL_INDICATORS = (
    "embed",
    "bge-",
    "gte-",
    "e5-",
    "jina-embedding",
    "text-embedding",
)

# Known reranker model name patterns
RERANKER_MODEL_INDICATORS = (
    "rerank",
    "bge-reranker",
    "jina-reranker",
)

# Field names that ProviderSeed.field can hold
SEED_FIELD_API_BASE = "api_base"
SEED_FIELD_API_KEY = "api_key"
SEED_FIELD_MODEL = "model"
SEED_FIELD_DIM = "dim"
