# Evaluation runtime

Install `requirements.txt` with Python 3.10 or newer. The connection adapter targets MCP 1.x; MCP 2.x changed the HTTP transport API and is intentionally excluded.

Set `ANTHROPIC_API_KEY` and explicitly select an available model with `--model MODEL_ID` or `ANTHROPIC_MODEL`. There is no implicit model that can silently become retired.

Each QA task defaults to at most 20 model requests, 100 tool calls and 120 seconds in total, including model and tool waits. Override using `--max-rounds`, `--max-tool-calls` and `--max-seconds`. Requests use the async SDK without automatic retries so cancellation does not leave a synchronous worker running. Budget exhaustion is reported as a failed task with completed call metrics. These limits are not a currency budget, and cancelling a remote tool does not undo side effects it already performed.

All tool calls in one response run sequentially and return their IDs together. MCP content and structured results are serialized as JSON text, preserving `isError`; images and resources are retained as data rather than converted into native visual input.

Run offline regressions from the repository root:

```sh
python -m unittest discover -s tests -v
```

Tests use real MCP SDK types and a local stdio server, with simulated model responses; no paid model requests are made.
