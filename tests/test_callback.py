"""Tests for TracesealCallbackHandler.

Uses FakeListLLM and a minimal tool so we exercise the callback API
without hitting any real model provider.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import tool
from traceseal_observe import OperatorKey, canonical_dumps
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from traceseal_langchain import TracesealCallbackHandler


@pytest.fixture
def operator_key() -> OperatorKey:
    return OperatorKey.generate(name="test-operator")


def _verify_receipt(receipt) -> bool:
    """Independently verify a receipt — same algorithm as traceseal-verify."""
    pk_bytes = bytes.fromhex(receipt.attestation["operator_public_key"])
    pk = Ed25519PublicKey.from_public_bytes(pk_bytes)
    payload = canonical_dumps(
        {"execution": receipt.execution, "provenance": receipt.provenance}
    )
    sig = bytes.fromhex(receipt.attestation["signature"])
    try:
        pk.verify(sig, payload)
        return True
    except Exception:
        return False


def test_llm_call_produces_signed_receipt(operator_key):
    llm = FakeListLLM(responses=["hello world"])
    handler = TracesealCallbackHandler(operator_key=operator_key)

    result = llm.invoke("say hi", config={"callbacks": [handler]})

    assert result == "hello world"
    assert len(handler.receipts) == 1
    receipt = handler.receipts[0]
    assert receipt.execution["receipt_type"] == "model"
    assert receipt.execution["ok"] == "true"
    assert receipt.execution["input_hash"].startswith("sha256:")
    assert receipt.execution["output_hash"].startswith("sha256:")
    assert _verify_receipt(receipt), "signature must verify"


def test_tool_call_produces_signed_receipt(operator_key):
    handler = TracesealCallbackHandler(operator_key=operator_key)

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    result = add.invoke({"a": 2, "b": 3}, config={"callbacks": [handler]})

    assert result == 5
    assert len(handler.receipts) == 1
    receipt = handler.receipts[0]
    assert receipt.execution["receipt_type"] == "tool"
    assert receipt.execution["tool_name"] == "add"
    assert receipt.execution["transport"] == "python"
    assert receipt.execution["ok"] == "true"
    assert _verify_receipt(receipt)


def test_tool_error_is_recorded(operator_key):
    handler = TracesealCallbackHandler(operator_key=operator_key)

    @tool
    def boom(x: str) -> str:
        """Always raises."""
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        boom.invoke({"x": "anything"}, config={"callbacks": [handler]})

    assert len(handler.receipts) == 1
    receipt = handler.receipts[0]
    assert receipt.execution["receipt_type"] == "tool"
    assert receipt.execution["ok"] == "false"
    assert "kaboom" in receipt.execution["error_message"]
    assert _verify_receipt(receipt)


def test_workflow_receipt_chains_children(operator_key):
    llm = FakeListLLM(responses=["first", "second"])
    handler = TracesealCallbackHandler(
        operator_key=operator_key,
        workflow_name="two-step-test",
        workflow_version="1.0",
    )

    llm.invoke("prompt a", config={"callbacks": [handler]})
    llm.invoke("prompt b", config={"callbacks": [handler]})

    wf = handler.workflow_receipt()

    assert wf.execution["receipt_type"] == "orchestration"
    assert wf.execution["workflow_name"] == "two-step-test"
    assert wf.execution["step_count"] == 2
    assert wf.execution["ok"] == "true"

    child_hashes = {r.receipt_hash for r in handler.receipts}
    step_hashes = {s["receipt_hash"] for s in wf.execution["steps"]}
    assert child_hashes == step_hashes

    assert _verify_receipt(wf)


def test_workflow_receipt_requires_name(operator_key):
    handler = TracesealCallbackHandler(operator_key=operator_key)
    with pytest.raises(ValueError, match="workflow_name"):
        handler.workflow_receipt()


def test_token_usage_from_message_metadata(operator_key):
    """Modern chat models attach usage_metadata to AIMessage, not llm_output."""
    handler = TracesealCallbackHandler(operator_key=operator_key)
    run_id = uuid4()

    handler.on_chat_model_start(
        serialized={"id": ["langchain", "chat_models", "anthropic", "ChatAnthropic"]},
        messages=[[]],
        run_id=run_id,
    )
    msg = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
    )
    response = LLMResult(generations=[[ChatGeneration(message=msg)]])
    handler.on_llm_end(response, run_id=run_id)

    receipt = handler.receipts[0]
    assert int(receipt.execution["input_tokens"]) == 12
    assert int(receipt.execution["output_tokens"]) == 7


def test_token_usage_sums_across_batched_generations(operator_key):
    handler = TracesealCallbackHandler(operator_key=operator_key)
    run_id = uuid4()

    handler.on_chat_model_start(
        serialized={"id": ["langchain", "chat_models", "openai", "ChatOpenAI"]},
        messages=[[], []],
        run_id=run_id,
    )
    response = LLMResult(
        generations=[
            [ChatGeneration(message=AIMessage(
                content="a",
                usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            ))],
            [ChatGeneration(message=AIMessage(
                content="b",
                usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            ))],
        ],
    )
    handler.on_llm_end(response, run_id=run_id)

    receipt = handler.receipts[0]
    assert int(receipt.execution["input_tokens"]) == 15
    assert int(receipt.execution["output_tokens"]) == 5


def test_retriever_call_produces_signed_receipt(operator_key):
    handler = TracesealCallbackHandler(operator_key=operator_key)
    run_id = uuid4()

    handler.on_retriever_start(
        serialized={"id": ["langchain", "retrievers", "MyRetriever"]},
        query="what is X?",
        run_id=run_id,
    )
    handler.on_retriever_end(
        [Document(page_content="answer about X")],
        run_id=run_id,
    )

    assert len(handler.receipts) == 1
    receipt = handler.receipts[0]
    assert receipt.execution["receipt_type"] == "tool"
    assert receipt.execution["transport"] == "retriever"
    assert receipt.execution["tool_name"] == "MyRetriever"
    assert receipt.execution["ok"] == "true"
    assert _verify_receipt(receipt)


def test_retriever_error_is_recorded(operator_key):
    handler = TracesealCallbackHandler(operator_key=operator_key)
    run_id = uuid4()

    handler.on_retriever_start(
        serialized={"id": ["langchain", "retrievers", "MyRetriever"]},
        query="x",
        run_id=run_id,
    )
    handler.on_retriever_error(RuntimeError("vector store down"), run_id=run_id)

    receipt = handler.receipts[0]
    assert receipt.execution["transport"] == "retriever"
    assert receipt.execution["ok"] == "false"
    assert "vector store down" in receipt.execution["error_message"]
    assert _verify_receipt(receipt)


def test_chain_error_marks_workflow_failed(operator_key):
    """Root chain errors must flip workflow ok=false even when no child errored."""
    handler = TracesealCallbackHandler(
        operator_key=operator_key,
        workflow_name="failing-workflow",
    )

    root_id = uuid4()
    handler.on_chain_start(serialized={}, inputs={"q": "x"}, run_id=root_id)

    llm_id = uuid4()
    handler.on_llm_start(
        serialized={"id": ["langchain", "llms", "fake", "FakeListLLM"]},
        prompts=["hi"],
        run_id=llm_id,
        parent_run_id=root_id,
    )
    handler.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="ok"))]]),
        run_id=llm_id,
        parent_run_id=root_id,
    )

    handler.on_chain_error(RuntimeError("agent iteration limit"), run_id=root_id)

    wf = handler.workflow_receipt()
    assert wf.execution["ok"] == "false"
    assert "agent iteration limit" in wf.execution.get("error_step", "")
    assert _verify_receipt(wf)


def test_workflow_ok_when_chain_succeeds(operator_key):
    """Sanity: chain success path keeps ok=true."""
    handler = TracesealCallbackHandler(
        operator_key=operator_key,
        workflow_name="ok-workflow",
    )
    root_id = uuid4()
    handler.on_chain_start(serialized={}, inputs={"q": "x"}, run_id=root_id)
    handler.on_chain_end({"out": "y"}, run_id=root_id)

    wf = handler.workflow_receipt()
    assert wf.execution["ok"] == "true"


def test_receipt_is_round_trip_json_safe(operator_key):
    llm = FakeListLLM(responses=["ok"])
    handler = TracesealCallbackHandler(operator_key=operator_key)

    llm.invoke("x", config={"callbacks": [handler]})

    raw = handler.receipts[0].to_json()
    parsed = json.loads(raw)
    assert parsed["receipt_version"] == "1.0"
    assert parsed["attestation"]["signature"]
