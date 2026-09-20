"""Real SDK objects and deterministic clients, without paid API calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/mcp-builder/scripts'
sys.path.insert(0, str(SCRIPTS))
from mcp.types import CallToolResult, TextContent, ImageContent, EmbeddedResource, TextResourceContents
from connections import MCPConnectionStdio
from evaluation import agent_loop, evaluate_single_task, EvaluationBudgetExceeded


def response(*blocks, stop='tool_use'):
    return NS(content=list(blocks), stop_reason=stop)


def call(id, name='lookup'):
    return NS(type='tool_use', id=id, name=name, input={'id': id})


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_content_serializes_and_preserves_errors(self):
        connection = MCPConnectionStdio('unused')
        payload = CallToolResult(content=[TextContent(type='text', text='original result'),
            ImageContent(type='image', data='YWJj', mimeType='image/png'),
            EmbeddedResource(type='resource', resource=TextResourceContents(
                uri='test://resource', text='embedded', mimeType='text/plain'))],
            isError=True, structuredContent={'count': 3})
        connection.session = NS(call_tool=AsyncMock(return_value=payload))
        result = await connection.call_tool('lookup', {})
        self.assertEqual('original result', json.loads(json.dumps(result))['content'][0]['text'])
        self.assertTrue(result['isError'])
        self.assertEqual({'count': 3}, result['structuredContent'])
        self.assertEqual('embedded', result['content'][2]['resource']['text'])

    async def test_all_ids_returned_together_and_text_blocks_joined(self):
        requests = []
        responses = iter([response(call('a'), call('b'), call('c')),
                          response(NS(type='text', text='<response>'),
                                   NS(type='text', text='ok</response>'), stop='end_turn')])
        async def create(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            return next(responses)
        connection = NS(call_tool=AsyncMock(side_effect=[
            {'content': [{'type': 'text', 'text': 'kept'}], 'isError': False},
            {'content': [{'type': 'future', 'value': 'preserved'}], 'isError': True},
            RuntimeError('tool failed')]))
        text, metrics = await agent_loop(NS(messages=NS(create=create)), 'explicit-model', 'q', [], connection)
        results = requests[1]['messages'][-1]['content']
        self.assertEqual(['a', 'b', 'c'], [r['tool_use_id'] for r in results])
        self.assertIn('kept', results[0]['content'])
        self.assertIn('preserved', results[1]['content'])
        self.assertEqual([False, True, True], [r['is_error'] for r in results])
        self.assertEqual(3, metrics['lookup']['count'])
        self.assertEqual('<response>\nok</response>', text)
        self.assertEqual('explicit-model', requests[0]['model'])

    async def test_repeating_model_stops_at_round_limit_with_trace(self):
        create = AsyncMock(return_value=response(call('a')))
        connection = NS(call_tool=AsyncMock(return_value={'content': []}))
        with self.assertRaises(EvaluationBudgetExceeded) as caught:
            await agent_loop(NS(messages=NS(create=create)), 'm', 'q', [], connection, max_rounds=3)
        self.assertEqual(3, create.await_count)
        self.assertEqual(3, caught.exception.tool_metrics['lookup']['count'])
        self.assertEqual('user', caught.exception.messages[-1]['role'])

    async def test_tool_batch_budget_rejects_before_side_effects(self):
        connection = NS(call_tool=AsyncMock())
        create = AsyncMock(return_value=response(call('a'), call('b')))
        with self.assertRaises(EvaluationBudgetExceeded):
            await agent_loop(NS(messages=NS(create=create)), 'm', 'q', [], connection, max_tool_calls=1)
        connection.call_tool.assert_not_awaited()

    async def test_total_deadline_cancels_model_and_tool(self):
        for hung in ('model', 'tool'):
            cancelled = asyncio.Event()
            async def hang(*args, **kwargs):
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
            create = hang if hung == 'model' else AsyncMock(return_value=response(call('a')))
            conn = NS(call_tool=hang)
            with self.subTest(hung=hung), self.assertRaises(EvaluationBudgetExceeded):
                await agent_loop(NS(messages=NS(create=create)), 'm', 'q', [], conn, max_seconds=.03)
            self.assertTrue(cancelled.is_set())

    async def test_external_cancellation_propagates(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def hang(**kwargs):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        task = asyncio.create_task(agent_loop(NS(messages=NS(create=hang)), 'm', 'q', [], None))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_budget_failure_is_explicit_failed_task_with_metrics(self):
        create = AsyncMock(return_value=response(call('a')))
        conn = NS(call_tool=AsyncMock(return_value='ok'))
        result = await evaluate_single_task(NS(messages=NS(create=create)), 'm',
            {'question': 'q', 'answer': 'a'}, [], conn, 0, max_rounds=1)
        self.assertEqual(0, result['score'])
        self.assertIn('budget', result['error'])
        self.assertEqual(1, result['num_tool_calls'])

    async def test_model_failure_is_not_reported_as_tool_success(self):
        client = NS(messages=NS(create=AsyncMock(side_effect=RuntimeError('model unavailable'))))
        with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
            await agent_loop(client, 'retired', 'q', [], None)

    async def test_invalid_budgets_rejected_before_requests(self):
        for kwargs in ({'max_rounds': 0}, {'max_seconds': float('nan')},
                       {'max_seconds': float('inf')}, {'max_tool_calls': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await agent_loop(None, 'm', 'q', [], None, **kwargs)

    def test_cli_requires_explicit_model_before_connecting(self):
        env = dict(os.environ)
        env.pop('ANTHROPIC_MODEL', None)
        result = subprocess.run([sys.executable, str(SCRIPTS / 'evaluation.py'), 'missing.xml'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('Specify --model', result.stderr)


class StdioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_server_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'server.py'
            script.write_text("from mcp.server.fastmcp import FastMCP\nm = FastMCP('regression')\n@m.tool()\ndef echo(text: str) -> str:\n    return text\nm.run()\n")
            async with MCPConnectionStdio(sys.executable, [str(script)]) as conn:
                tools = await conn.list_tools()
                self.assertIn('echo', [tool['name'] for tool in tools])
                result = await conn.call_tool('echo', {'text': 'real stdio result'})
                self.assertFalse(result['isError'])
                self.assertEqual('real stdio result', result['content'][0]['text'])
                json.dumps(result)
