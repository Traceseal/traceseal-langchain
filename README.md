# traceseal-langchain

**Signed execution receipts for every LangChain model and tool call.** Drop in one callback. Hand the receipts to your auditor.

## What it does

Add a single callback handler to your LangChain chain, agent, or runnable. Every LLM invocation and every tool call produces a signed [Traceseal Execution Receipt](https://traceseal.io/spec): a record of what your agent ran, signed with your operator key and verifiable by any third party. It shows what you recorded; it does not by itself prove the provider returned it (see [What receipts prove](#what-receipts-prove-and-what-they-dont)).

- **No rewrite.** Works with existing LangChain code via the standard callback API.
- **No vendor lock-in.** Receipts verify with `pip install traceseal-verify` on any machine, no access to your infrastructure.
- **Privacy-preserving.** Hashes of inputs/outputs, not the values themselves. Safe to share with auditors.
- **Workflow chaining.** Optional: roll every child call into a single signed orchestration receipt.

## Install

```bash
pip install traceseal-langchain
```

## Quick start

```python
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from traceseal_observe import OperatorKey
from traceseal_langchain import TracesealCallbackHandler

key = OperatorKey.load_from_file("~/.traceseal/keys/my-operator.key")
handler = TracesealCallbackHandler(
    operator_key=key,
    workflow_name="research-assistant",
    workflow_version="1.0",
)

@tool
def search(query: str) -> str:
    """Search the web."""
    return f"results for {query}"

llm = ChatOpenAI(model="gpt-4o-mini").bind_tools([search])
result = llm.invoke("research AI safety", config={"callbacks": [handler]})

# One receipt per LLM call and per tool call:
for r in handler.receipts:
    print(r.execution["receipt_type"], r.receipt_hash)

# A single signed orchestration over all of them:
workflow = handler.workflow_receipt()
open("run.json", "w").write(workflow.to_json())
```

Verify it on any machine:

```bash
pip install traceseal-verify
traceseal-verify run.json
```

## Completeness, causality and context (traceseal-observe 1.5+)

```python
from traceseal_observe import OperatorKey, Principal, ReceiptChain
from traceseal_langchain import TracesealCallbackHandler

chain = ReceiptChain.open("/var/lib/agent/chain", OperatorKey.load_from_file("operator.key"),
                          "research-assistant")
handler = TracesealCallbackHandler(
    chain.signer,                                   # every receipt hash-chained
    workflow_name="research-assistant",
    context={"agent_id": "research-assistant", "agent_version": "2.1.0",
             "principal": Principal.user(user_id, pepper=PEPPER)},
)
```

- **Nothing missing**: with a `ReceiptChain` signer, every receipt is
  hash-chained. `traceseal-verify receipts/*.json` reports any gap.
- **Which model really answered**: model receipts record the served model,
  response id, stop reason, and every tool call the model requested.
- **Why each tool ran**: each tool receipt is linked to the model receipt
  that requested it, through LangChain's `tool_call_id`, and hashes the
  structured arguments. `traceseal-trace verify receipts/*.json` reports
  each tool call as `matched`, `arguments_differ` (your code changed what
  the model asked for) or `not_requested`, and lists requested calls that
  never ran.
- **Who and for whom**: `context=` signs agent identity, a
  privacy-preserving principal, authorisation and session into every
  receipt. It is passed explicitly because LangChain may run callbacks on
  other threads.
- **No leaked secrets**: error messages are signed with URL query strings
  (`?api_key=…`) redacted.

## What the receipts record

| Call type | Captured in the receipt |
|---|---|
| LLM | provider, requested and served model, response id, stop reason, requested tool calls, input hash, output hash, input/output tokens, wall time, ok/error |
| Tool | tool name, python transport, hash of the structured arguments, output hash, `tool_call_id` and the model receipt that requested it, wall time, ok/error |
| Workflow | ordered list of step names + child receipt hashes, start/end times, aggregate ok |

Every receipt is a JSON file signed with the operator's ed25519 key and conforms to [RECEIPT-SPEC.md v1.0](https://traceseal.io/spec).

## Works with

- `langchain-core >= 0.3`, `traceseal-observe >= 1.5`
- LangChain, LangGraph, LangChain Community — any runnable that accepts a `callbacks=` config.
- LangGraph agents — the handler captures each node's LLM/tool calls and emits a workflow receipt over the whole graph.

## What receipts prove, and what they don't

The operator attests:

> "I observed these N LLM calls and M tool calls, in this order, with these input and output hashes."

A third party can verify the signature, confirm the chain hasn't been tampered with, and confirm the operator's key signed it. They cannot confirm (from the receipt alone) that the provider actually returned what was recorded, or that the tool functions behaved honestly. The receipt is an *operator attestation*, not a zero-knowledge proof — same trust model as any audit log, but cryptographically portable.

See [RECEIPT-SPEC.md §6](https://traceseal.io/spec) for the full trust analysis.

## Why this matters

Compliance officers, auditors, and regulators are starting to ask: *"can you prove what your AI agent actually did?"* Today most answers are "trust our logs." Traceseal receipts make your answer portable and tamper-evident: third parties can check that your record hasn't been altered without access to your machines or your LangSmith account. They still rest on your key, not on an independent observer.

## License

Apache 2.0.
