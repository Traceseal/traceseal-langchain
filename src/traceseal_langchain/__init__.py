"""Traceseal callback handler for LangChain.

Drops into any LangChain chain, agent, or runnable. Every LLM call and
every tool invocation produces a signed Traceseal receipt, verifiable
third-party with `pip install traceseal-verify`.

Usage:

    from traceseal_langchain import TracesealCallbackHandler
    from traceseal_observe import OperatorKey

    key = OperatorKey.load_from_file("~/.traceseal/keys/my-operator.key")
    handler = TracesealCallbackHandler(
        operator_key=key,
        workflow_name="research-agent",
        workflow_version="1.0",
    )

    result = agent.invoke({"input": "..."}, config={"callbacks": [handler]})

    # handler.receipts  -> list[Receipt] for each model + tool call
    # handler.workflow_receipt()  -> a signed orchestration receipt
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler
from traceseal_observe import (
    ModelCallRecord,
    OperatorKey,
    OrchestrationRecord,
    Receipt,
    ToolCallRecord,
    WorkflowStep,
    sha256_of,
    sign_model_call,
    sign_orchestration,
    sign_tool_call,
)

__all__ = ["TracesealCallbackHandler", "__version__"]
__version__ = "0.1.0"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_safe(obj: Any) -> Any:
    """Coerce arbitrary LangChain objects into something sha256_of can hash.

    LangChain frequently passes `BaseMessage`, `LLMResult`, Pydantic models,
    or nested dicts containing them. We fall back to `str(obj)` when we
    can't dict-ify — the receipt captures *some* deterministic fingerprint
    rather than crashing.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _json_safe(method())
            except Exception:
                pass
    for attr in ("content", "text"):
        val = getattr(obj, attr, None)
        if val is not None:
            return {"type": obj.__class__.__name__, attr: _json_safe(val)}
    return str(obj)


def _extract_provider(serialized: Any) -> str:
    """Best-effort provider extraction from LangChain's `serialized` dict.

    LangChain stores the class path in `serialized["id"]`, e.g.
    `["langchain", "chat_models", "anthropic", "ChatAnthropic"]`. We
    pull the third segment when available.
    """
    if isinstance(serialized, dict):
        ident = serialized.get("id")
        if isinstance(ident, list) and len(ident) >= 3:
            return str(ident[-2]).lower()
    return "unknown"


def _extract_model(serialized: Any, kwargs: dict[str, Any]) -> str:
    """Best-effort model-string extraction."""
    invocation_params = kwargs.get("invocation_params") or {}
    for key in ("model", "model_name", "model_id"):
        if isinstance(invocation_params, dict) and invocation_params.get(key):
            return str(invocation_params[key])
    if isinstance(serialized, dict):
        inner = serialized.get("kwargs")
        if isinstance(inner, dict):
            for key in ("model", "model_name", "model_id"):
                if inner.get(key):
                    return str(inner[key])
    return "unknown"


def _extract_token_usage(llm_output: Any) -> tuple[int | None, int | None]:
    if not isinstance(llm_output, dict):
        return None, None
    usage = (
        llm_output.get("token_usage")
        or llm_output.get("usage")
        or llm_output.get("usage_metadata")
        or {}
    )
    if not isinstance(usage, dict):
        return None, None
    input_tokens = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
    )
    output_tokens = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
    )
    return (
        int(input_tokens) if input_tokens is not None else None,
        int(output_tokens) if output_tokens is not None else None,
    )


class TracesealCallbackHandler(BaseCallbackHandler):
    """Sign every LLM and tool call made by a LangChain runnable.

    Args:
        operator_key: The ed25519 key that signs each receipt.
        workflow_name: If set, `workflow_receipt()` can be called after
            the run to produce a signed orchestration over all child
            receipts. Leave as None if you only want individual receipts.
        workflow_version: Version string for the orchestration receipt.
    """

    def __init__(
        self,
        operator_key: OperatorKey,
        *,
        workflow_name: str | None = None,
        workflow_version: str = "1.0",
    ) -> None:
        self.operator_key = operator_key
        self.workflow_name = workflow_name
        self.workflow_version = workflow_version
        self.receipts: list[Receipt] = []
        self._step_names: list[str] = []
        self._pending: dict[UUID, dict[str, Any]] = {}
        self._workflow_started_at = _now_iso()
        self._workflow_t_start = time.monotonic()
        self._workflow_input_hash = "sha256:empty"
        self._workflow_final_output_hash = "sha256:empty"
        self._root_run_id: UUID | None = None

    # ------------------------------------------------------------------
    # Chain callbacks — capture root-run input/output for workflow scope
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is None and self._root_run_id is None:
            self._root_run_id = run_id
            self._workflow_input_hash = sha256_of(_json_safe(inputs))

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if run_id == self._root_run_id:
            self._workflow_final_output_hash = sha256_of(_json_safe(outputs))

    # ------------------------------------------------------------------
    # LLM callbacks
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._pending[run_id] = {
            "kind": "model",
            "started_at": _now_iso(),
            "t_start": time.monotonic(),
            "provider": _extract_provider(serialized),
            "model": _extract_model(serialized, kwargs),
            "input_hash": sha256_of({"prompts": prompts}),
        }

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._pending[run_id] = {
            "kind": "model",
            "started_at": _now_iso(),
            "t_start": time.monotonic(),
            "provider": _extract_provider(serialized),
            "model": _extract_model(serialized, kwargs),
            "input_hash": sha256_of({"messages": _json_safe(messages)}),
        }

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        wall_time_ms = int((time.monotonic() - pending["t_start"]) * 1000)
        output_obj = _json_safe(response)
        input_tokens, output_tokens = _extract_token_usage(
            getattr(response, "llm_output", None)
        )
        record = ModelCallRecord(
            provider=pending["provider"],
            model=pending["model"],
            api_endpoint="",
            input_hash=pending["input_hash"],
            output_hash=sha256_of(output_obj),
            started_at=pending["started_at"],
            wall_time_ms=wall_time_ms,
            ok=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        receipt = sign_model_call(record, self.operator_key)
        self.receipts.append(receipt)
        self._step_names.append(f"llm:{pending['model']}")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        wall_time_ms = int((time.monotonic() - pending["t_start"]) * 1000)
        record = ModelCallRecord(
            provider=pending["provider"],
            model=pending["model"],
            api_endpoint="",
            input_hash=pending["input_hash"],
            output_hash="sha256:empty",
            started_at=pending["started_at"],
            wall_time_ms=wall_time_ms,
            ok=False,
            exit_code=1,
            error_message=str(error)[:200],
        )
        receipt = sign_model_call(record, self.operator_key)
        self.receipts.append(receipt)
        self._step_names.append(f"llm:{pending['model']}")

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        name = "unknown"
        if isinstance(serialized, dict):
            name = str(serialized.get("name") or serialized.get("id", ["unknown"])[-1])
        self._pending[run_id] = {
            "kind": "tool",
            "started_at": _now_iso(),
            "t_start": time.monotonic(),
            "tool_name": name,
            "input_hash": sha256_of({"input": input_str}),
        }

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        wall_time_ms = int((time.monotonic() - pending["t_start"]) * 1000)
        record = ToolCallRecord(
            tool_name=pending["tool_name"],
            transport="python",
            input_hash=pending["input_hash"],
            output_hash=sha256_of({"output": _json_safe(output)}),
            started_at=pending["started_at"],
            wall_time_ms=wall_time_ms,
            ok=True,
        )
        receipt = sign_tool_call(record, self.operator_key)
        self.receipts.append(receipt)
        self._step_names.append(f"tool:{pending['tool_name']}")

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        wall_time_ms = int((time.monotonic() - pending["t_start"]) * 1000)
        record = ToolCallRecord(
            tool_name=pending["tool_name"],
            transport="python",
            input_hash=pending["input_hash"],
            output_hash="sha256:empty",
            started_at=pending["started_at"],
            wall_time_ms=wall_time_ms,
            ok=False,
            exit_code=1,
            error_message=str(error)[:200],
        )
        receipt = sign_tool_call(record, self.operator_key)
        self.receipts.append(receipt)
        self._step_names.append(f"tool:{pending['tool_name']}")

    # ------------------------------------------------------------------
    # Workflow receipt
    # ------------------------------------------------------------------

    def workflow_receipt(self) -> Receipt:
        """Sign an orchestration receipt over every child receipt collected.

        Raises:
            ValueError: if `workflow_name` wasn't supplied at construction.
        """
        if self.workflow_name is None:
            raise ValueError(
                "workflow_name must be set at construction to build a workflow receipt."
            )
        wall_time_ms = int((time.monotonic() - self._workflow_t_start) * 1000)
        steps = [
            WorkflowStep(
                name=self._step_names[i],
                receipt_hash=r.receipt_hash,
                receipt_type=r.execution.get("receipt_type", "unknown"),
                ok=r.execution.get("ok") == "true",
            )
            for i, r in enumerate(self.receipts)
        ]
        record = OrchestrationRecord(
            workflow_name=self.workflow_name,
            workflow_version=self.workflow_version,
            steps=steps,
            started_at=self._workflow_started_at,
            wall_time_ms=wall_time_ms,
            ok=all(s.ok for s in steps) if steps else True,
            workflow_input_hash=self._workflow_input_hash,
            final_output_hash=self._workflow_final_output_hash,
        )
        return sign_orchestration(record, self.operator_key)
