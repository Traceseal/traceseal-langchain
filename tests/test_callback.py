"""Tests for TracesealCallbackHandler.

Uses FakeListLLM and a minimal tool so we exercise the callback API
without hitting any real model provider.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.language_models.fake import FakeListLLM
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


def test_receipt_is_round_trip_json_safe(operator_key):
    llm = FakeListLLM(responses=["ok"])
    handler = TracesealCallbackHandler(operator_key=operator_key)

    llm.invoke("x", config={"callbacks": [handler]})

    raw = handler.receipts[0].to_json()
    parsed = json.loads(raw)
    assert parsed["receipt_version"] == "1.0"
    assert parsed["attestation"]["signature"]
