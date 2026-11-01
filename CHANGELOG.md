# Changelog

## [0.2.0]: unreleased

Requires traceseal-observe >= 1.5.

### Added
- Model receipts record `served_model`, `response_id`, `stop_reason` and
  `requested_tool_calls`, read from LangChain's `AIMessage`.
- Each tool receipt is causally linked to the model receipt that requested
  it, through `tool_call_id`. `traceseal-trace verify` can match each
  call against the model's request.
- `context=` signs agent identity, principal, authorisation and session
  into every receipt.
- A `ReceiptChain` signer works as `operator_key`, so receipts are
  hash-chained.

### Changed
- When LangChain passes structured tool arguments (`inputs`), the tool
  receipt's `input_hash` is `sha256_of(arguments)`, matching how the
  model's requested tool call is hashed. It was `sha256_of({"input":
  input_str})`; that form is still used when no structured arguments are
  passed.

### Fixed
- Error messages in receipts are now redacted (URL query strings such as
  `?api_key=…` removed). Previously the raw exception text, truncated to
  200 characters, was signed into a shareable receipt.

## [0.1.0]

- Initial release: a callback handler that signs a receipt for every LLM
  and tool call, plus an optional workflow receipt.
