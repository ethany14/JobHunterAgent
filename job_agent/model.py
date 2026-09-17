"""LangGraph-independent model configuration and structured invocation."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from dotenv import dotenv_values
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

Result = TypeVar("Result", bound=BaseModel)
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def optional_setting(settings: Mapping[str, Any], key: str) -> str | None:
    value = settings.get(key)
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def create_model(
    *,
    env_path: Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
    model_factory: Callable[..., Any] = ChatOpenAI,
) -> Any:
    """Create the configured chat model with environment values taking priority."""
    environment = os.environ if environ is None else environ
    settings = {**dotenv_values(env_path), **environment}
    model_id = optional_setting(settings, "LLM_MODEL_ID")
    if not model_id:
        raise ValueError("Set LLM_MODEL_ID in the environment or project's .env file.")
    options: dict[str, Any] = {
        "model": model_id,
        "temperature": 0,
        "max_retries": 0,
    }
    api_key = optional_setting(settings, "LLM_API_KEY")
    base_url = optional_setting(settings, "LLM_BASE_URL")
    timeout = optional_setting(settings, "LLM_TIMEOUT")
    max_retries = optional_setting(settings, "LLM_MAX_RETRIES")
    if api_key is not None:
        options["api_key"] = api_key
    if base_url is not None:
        options["base_url"] = base_url
    if timeout is not None:
        try:
            seconds = float(timeout)
        except ValueError as exc:
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.") from exc
        if not 0 < seconds < float("inf"):
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.")
        options["timeout"] = seconds
    if max_retries is not None:
        try:
            retries = int(max_retries)
        except ValueError as exc:
            raise ValueError("LLM_MAX_RETRIES must be a non-negative integer.") from exc
        if retries < 0:
            raise ValueError("LLM_MAX_RETRIES must be a non-negative integer.")
        options["max_retries"] = retries
    return model_factory(**options)


def invoke_structured(
    model: Any,
    schema: type[Result],
    system_message: str,
    human_message: str,
    config: Any = None,
) -> Result:
    """Invoke a Pydantic structured model using the baseline message layout."""
    structured_model = model.with_structured_output(schema)
    result = structured_model.invoke(
        [("system", system_message), ("human", human_message)], config=config
    )
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)
