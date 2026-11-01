"""Integration with traceseal-observe 1.5: chains, context, causality, redaction."""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from traceseal_observe import OperatorKey, Principal, ReceiptChain, sha256_of, verify_chain
from traceseal_observe.trace import verify_trace

from traceseal_langchain import TracesealCallbackHandler

PEPPER = b"0123456789abcdef-pepper"


@tool
def refund(order: str, amount: int) -> str:
    """Refund an order."""
    return f"refunded {amount} on {order}"


@tool
def broken(url: str) -> str:
    """Always fails, leaking a credential in its message."""
    raise RuntimeError(f"GET {url}?api_key=sk-live-SECRET failed")


def _model(calls):
    message = AIMessage(content="", tool_calls=calls,
                        response_metadata={"model_name": "claude-sonnet-5-5-20260101",
                                           "stop_reason": "tool_use", "id": "msg_1"})
    return FakeMessagesListChatModel(responses=[message])


def _run(handler, args):
    llm = _model([{"name": "refund", "args": {"order": "o-1", "amount": 20}, "id": "call_1"}])
    message = llm.invoke("refund o-1", config={"callbacks": [handler]})
    call = dict(message.tool_calls[0], args=args, type="tool_call")
    refund.invoke(call, config={"callbacks": [handler]})
    return message


def test_model_metadata_and_requested_tool_calls():
    handler = TracesealCallbackHandler(OperatorKey.generate())
    _run(handler, {"order": "o-1", "amount": 20})
    model = handler.receipts[0].execution
    assert model["served_model"] == "claude-sonnet-5-5-20260101"
    assert model["stop_reason"] == "tool_use" and model["response_id"] == "msg_1"
    assert model["requested_tool_calls"] == [
        {"id": "call_1", "name": "refund", "input_hash": sha256_of({"order": "o-1", "amount": 20})}]


def test_tool_is_causally_linked_and_matches():
    handler = TracesealCallbackHandler(OperatorKey.generate())
    _run(handler, {"order": "o-1", "amount": 20})
    model, tool_receipt = handler.receipts
    ctx = tool_receipt.execution["context"]
    assert ctx["caused_by"] == [model.receipt_hash] and ctx["tool_call_id"] == "call_1"
    report = verify_trace(handler.receipts)
    assert report["ok"] and report["tool_calls"][0]["status"] == "matched"


def test_changed_arguments_are_detected():
    handler = TracesealCallbackHandler(OperatorKey.generate())
    _run(handler, {"order": "o-1", "amount": 9999})
    assert verify_trace(handler.receipts)["tool_calls"][0]["status"] == "arguments_differ"


def test_chained_signer_and_context():
    chain = ReceiptChain(OperatorKey.generate(), "langchain-agent")
    handler = TracesealCallbackHandler(
        chain.signer, workflow_name="refunds",
        context={"agent_id": "refund-bot", "principal": Principal.user("u-1", pepper=PEPPER)})
    _run(handler, {"order": "o-1", "amount": 20})
    receipts = handler.receipts + [handler.workflow_receipt()]
    assert [r.execution["chain_seq"] for r in receipts] == [1, 2, 3]
    assert all(r.execution["context"]["agent"]["id"] == "refund-bot" for r in receipts)
    assert verify_chain(receipts, checkpoint=chain.checkpoint())["ok"]


def test_errors_are_redacted():
    handler = TracesealCallbackHandler(OperatorKey.generate())
    with pytest.raises(RuntimeError):
        broken.invoke({"url": "https://api.example.com/x"}, config={"callbacks": [handler]})
    message = handler.receipts[0].execution["error_message"]
    assert "SECRET" not in message and "?<redacted>" in message


def test_invalid_context_fails_fast():
    with pytest.raises(ValueError):
        TracesealCallbackHandler(OperatorKey.generate(), context={"trace_id": "nope"})
