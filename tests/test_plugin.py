import asyncio
import importlib
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_github_trending.service import (
    GitHubClient,
    Repository,
    TrendingError,
    TrendingService,
    TTLCache,
    normalize_language,
    parse_readme_page,
    parse_trending,
)

HTML = """<article class="Box-row">
<a href="/login">Star</a><h2><a href="/acme/tool"> acme / tool </a></h2>
<p>A useful &amp; fast tool.</p><span itemprop="programmingLanguage">Python</span>
<a href="/acme/tool/stargazers"> 1,234 </a><a href="/acme/tool/forks"> 55 </a>
<span class="float-sm-right"><svg></svg>123 stars today</span></article>"""

README_PAGE = """<html><nav>Sign in and navigation</nav>
<article class="markdown-body entry-content" itemprop="text">
<h1>Useful Tool</h1><p>Parse data &amp; generate reports.</p>
<pre>tool --help</pre><script>untrusted script</script><button>Copy</button>
</article><footer>Copyright</footer></html>"""


class ParsingTests(unittest.TestCase):
    def test_readme_page_extracts_only_readme(self):
        text = parse_readme_page(README_PAGE)
        self.assertIn("Useful Tool", text)
        self.assertIn("Parse data & generate reports.", text)
        self.assertIn("tool --help", text)
        for unwanted in ["navigation", "script", "Copy", "Copyright"]:
            self.assertNotIn(unwanted, text)
        self.assertEqual(
            parse_readme_page(
                '<div id="readme"><div class="markdown-body">Old layout</div></div>'
            ),
            "Old layout",
        )
        for html in ["<h1>Sign in</h1>", '<article class="markdown-body"></article>']:
            with self.subTest(html=html), self.assertRaises(TrendingError):
                parse_readme_page(html)

    def test_order_fields_duplicates_and_optional_fields(self):
        repos = parse_trending(
            HTML
            + HTML
            + '<article class="Box-row"><h2><a href="/next/repo">repo</a></h2></article>'
        )
        self.assertEqual([r.name for r in repos], ["acme/tool", "next/repo"])
        self.assertEqual(repos[0].description, "A useful & fast tool.")
        self.assertEqual(repos[0].stars, "1,234")
        self.assertEqual(repos[0].today_stars, "123")
        self.assertEqual(repos[1].language, "")

    def test_challenge_and_malformed_repo_are_errors(self):
        for html in [
            "<h1>Sign in</h1>",
            '<article class="Box-row"><h2><a href="https://evil.test/a/b">x</a></h2></article>',
        ]:
            with self.subTest(html=html), self.assertRaises(TrendingError):
                parse_trending(html)

    def test_real_empty_result(self):
        self.assertEqual(
            parse_trending(
                "We couldn’t find any trending repositories matching your criteria."
            ),
            [],
        )

    def test_language(self):
        self.assertEqual(normalize_language(" C++ "), "c++")
        self.assertEqual(normalize_language("all"), "")
        self.assertEqual(normalize_language("Jupyter Notebook"), "jupyter-notebook")
        with self.assertRaises(TrendingError):
            normalize_language("../login?x=1")

    def test_cache_expiry_capacity_and_disable(self):
        cache = TTLCache(10, capacity=2)
        with patch(
            "astrbot_plugin_github_trending.service.time.monotonic", return_value=100
        ):
            cache.put("a", 1)
            cache.put("b", 2)
            self.assertEqual(cache.get("a"), 1)
            cache.put("c", 3)
            self.assertIsNone(cache.get("b"))
        with patch(
            "astrbot_plugin_github_trending.service.time.monotonic", return_value=111
        ):
            self.assertIsNone(cache.get("a"))
        disabled = TTLCache(0)
        disabled.put("a", 1)
        self.assertIsNone(disabled.get("a"))


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler, **kwargs):
        client = GitHubClient(proxy_mode="none", **kwargs)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_daily_url_and_api_token_isolation(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200, text="# README" if request.url.host == "api.github.com" else HTML
            )

        client = await self.make_client(handler, token="secret-test-token")
        repos = await client.trending("c++")
        self.assertEqual(await client.readme(repos[0]), "# README")
        self.assertEqual(
            str(requests[0].url), "https://github.com/trending/c%2B%2B?since=daily"
        )
        self.assertNotIn("Authorization", requests[0].headers)
        self.assertEqual(
            requests[1].headers["Authorization"], "Bearer secret-test-token"
        )
        self.assertEqual(
            requests[1].headers["Accept"], "application/vnd.github.raw+json"
        )
        self.assertEqual(requests[1].url.path, "/repos/acme/tool/readme")

    async def test_retry_then_success(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503 if len(calls) < 3 else 200, text=HTML)

        client = await self.make_client(handler)
        with patch(
            "astrbot_plugin_github_trending.service.asyncio.sleep", new=AsyncMock()
        ):
            self.assertEqual(len(await client.trending("")), 1)
        self.assertEqual(len(calls), 3)

    async def test_api_rejection_falls_back_without_sending_token(self):
        for status in [401, 403, 429]:
            with self.subTest(status=status):
                requests = []

                def handler(request, requests=requests, status=status):
                    requests.append(request)
                    return (
                        httpx.Response(status, text="private response")
                        if request.url.host == "api.github.com"
                        else httpx.Response(200, text=README_PAGE)
                    )

                client = await self.make_client(handler, token="secret-test-token")
                with patch(
                    "astrbot_plugin_github_trending.service.asyncio.sleep",
                    new=AsyncMock(),
                ) as sleep:
                    text = await client.readme(Repository("acme/tool"))
                    self.assertIn("Useful Tool", text)
                    sleep.assert_not_awaited()
                self.assertEqual(len(requests), 2)
                self.assertEqual(str(requests[1].url), "https://github.com/acme/tool")
                self.assertNotIn("Authorization", requests[1].headers)
                await client.readme(Repository("other/tool"))
                self.assertEqual(len(requests), 3)
                self.assertEqual(requests[-1].url.host, "github.com")

    async def test_rate_limit_reset_and_retry_after_then_api_recovers(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "github.com":
                return httpx.Response(200, text=README_PAGE)
            if len(requests) == 1:
                return httpx.Response(
                    403,
                    headers={
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": "1200",
                        "retry-after": "300",
                    },
                )
            return httpx.Response(200, text="Recovered README")

        client = await self.make_client(handler)
        with (
            patch(
                "astrbot_plugin_github_trending.service.time.time", return_value=1000
            ),
            patch(
                "astrbot_plugin_github_trending.service.time.monotonic",
                return_value=100,
            ),
        ):
            await client.readme(Repository("a/one"))
            self.assertEqual(client.api_retry_at, 400)
            self.assertIn("额度受限", client.api_error)
        with patch(
            "astrbot_plugin_github_trending.service.time.monotonic", return_value=399
        ):
            await client.readme(Repository("a/two"))
            self.assertEqual(len(requests), 3)
        with patch(
            "astrbot_plugin_github_trending.service.time.monotonic", return_value=401
        ):
            self.assertEqual(
                await client.readme(Repository("a/three")), "Recovered README"
            )
            self.assertEqual(len(requests), 4)
            self.assertEqual(client.api_error, "")

    async def test_api_and_web_failures_are_both_reported(self):
        def handler(request):
            return (
                httpx.Response(401, text="private-token")
                if request.url.host == "api.github.com"
                else httpx.Response(403, text="private-proxy")
            )

        client = await self.make_client(handler)
        with self.assertRaises(TrendingError) as caught:
            await client.readme(Repository("a/b"))
        self.assertIn("Token 无效", str(caught.exception))
        self.assertIn("网页拒绝访问", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    async def test_fallback_readme_is_used_for_summary(self):
        def handler(request):
            return (
                httpx.Response(403)
                if request.url.host == "api.github.com"
                else httpx.Response(200, text=README_PAGE)
            )

        client = await self.make_client(handler)
        context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="README 中文摘要")
            )
        )
        result = await TrendingService(client).summarize(
            Repository("a/b"), context, "model"
        )
        self.assertIn("README 中文摘要", result)
        self.assertIn("Useful Tool", context.llm_generate.call_args.kwargs["prompt"])
        self.assertNotIn("navigation", context.llm_generate.call_args.kwargs["prompt"])

    async def test_api_errors_and_size_limit(self):
        for status in [401, 403, 404, 302]:
            with self.subTest(status=status):
                client = await self.make_client(
                    lambda req, status=status: httpx.Response(
                        status,
                        text="credential-leak",
                        headers={"Location": "https://evil.test"},
                    )
                )
                with self.assertRaises(TrendingError) as caught:
                    await client.readme(Repository("acme/tool"))
                self.assertNotIn("credential-leak", str(caught.exception))
        client = await self.make_client(lambda req: httpx.Response(200, text="12345"))
        with self.assertRaises(TrendingError):
            await client.get("https://github.com/trending", limit=4)

    async def test_network_failure_retry_exhausted(self):
        def handler(request):
            raise httpx.ConnectError("private proxy credential", request=request)

        client = await self.make_client(handler)
        with patch(
            "astrbot_plugin_github_trending.service.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            with self.assertRaises(TrendingError) as caught:
                await client.trending("")
            self.assertEqual(sleep.await_count, 2)
            self.assertNotIn("credential", str(caught.exception))


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, **kwargs):
        client = types.SimpleNamespace(
            readme=AsyncMock(return_value="README CONTENT"),
            trending=AsyncMock(return_value=[Repository("acme/tool")]),
        )
        return TrendingService(client, **kwargs), client

    async def test_readme_summary_cache_and_model_isolation(self):
        service, client = self.make_service()
        context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="中文摘要")
            )
        )
        repo = Repository("acme/tool")
        first = await service.summarize(repo, context, "model-a")
        self.assertEqual(await service.summarize(repo, context, "model-a"), first)
        self.assertEqual(context.llm_generate.await_count, 1)
        await service.summarize(repo, context, "model-b")
        self.assertEqual(context.llm_generate.await_count, 2)
        self.assertEqual(client.readme.await_count, 1)
        self.assertEqual(
            context.llm_generate.call_args.kwargs["chat_provider_id"], "model-b"
        )
        self.assertNotIn("contexts", context.llm_generate.call_args.kwargs)
        self.assertNotIn("tools", context.llm_generate.call_args.kwargs)

    async def test_changed_readme_refreshes_summary(self):
        service, client = self.make_service()
        context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="摘要")
            )
        )
        repo = Repository("acme/tool")
        await service.summarize(repo, context, "model")
        service.cache.entries.pop(("readme", repo.name))
        client.readme.return_value = "CHANGED CONTENT"
        await service.summarize(repo, context, "model")
        self.assertEqual(context.llm_generate.await_count, 2)

    async def test_truncation_empty_response_and_retry(self):
        service, _client = self.make_service(readme_chars=4, summary_chars=5)
        context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="123456789")
            )
        )
        result = await service.summarize(Repository("acme/tool"), context, "model")
        self.assertIn("1234…", result)
        self.assertIn("节选", result)
        self.assertTrue(
            context.llm_generate.call_args.kwargs["prompt"].endswith("READ")
        )
        repo = Repository("other/tool")
        context.llm_generate.return_value.completion_text = ""
        self.assertIn("摘要不可用", await service.summarize(repo, context, "model"))
        context.llm_generate.return_value.completion_text = "成功"
        self.assertIn("成功", await service.summarize(repo, context, "model"))

    async def test_missing_readme_timeout_and_cancellation(self):
        service, client = self.make_service(ai_timeout=0.01)
        context = types.SimpleNamespace(llm_generate=AsyncMock())
        client.readme.side_effect = TrendingError("README 不存在")
        self.assertIn(
            "README 不存在", await service.summarize(Repository("a/b"), context, "m")
        )
        context.llm_generate.assert_not_awaited()
        client.readme.side_effect = None

        async def slow(**kwargs):
            await asyncio.sleep(1)

        context.llm_generate.side_effect = slow
        self.assertIn("超时", await service.summarize(Repository("a/b"), context, "m"))
        client.readme.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await service.summarize(Repository("c/d"), context, "m")

    async def test_ranking_cache_retains_fetch_time(self):
        service, client = self.make_service()
        first = await service.ranking("python")
        self.assertEqual(await service.ranking("python"), first)
        self.assertEqual(client.trending.await_count, 1)
        await service.ranking("typescript")
        self.assertEqual(client.trending.await_count, 2)


def load_main_with_stubs():
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("astrbot-test")
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = types.SimpleNamespace(
        command=lambda *a, **kw: lambda fn: fn,
        llm_tool=lambda *a, **kw: lambda fn: fn,
    )
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star, star.Context = Star, object
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star,
    }
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_github_trending.main")


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        module = load_main_with_stubs()
        self.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="session-model"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="中文摘要")
            ),
        )
        self.plugin = module.GitHubTrendingPlugin(self.context, {})
        self.addAsyncCleanup(self.plugin.terminate)
        self.event = types.SimpleNamespace(
            unified_msg_origin="test:GroupMessage:1",
            plain_result=lambda text: text,
            is_admin=lambda: False,
        )
        self.plugin.client.trending = AsyncMock(
            return_value=[Repository("a/one"), Repository("a/two")]
        )
        self.plugin.client.readme = AsyncMock(
            side_effect=[TrendingError("README 不存在"), "README"]
        )

    async def test_partial_failure_preserves_following_projects(self):
        messages = [m async for m in self.plugin.daily(self.event, "python", 2)]
        self.assertIn("README 不存在", messages[-2])
        self.assertIn("中文摘要", messages[-1])
        self.context.get_current_chat_provider_id.assert_awaited_once_with(
            umo=self.event.unified_msg_origin
        )
        self.assertFalse(self.plugin.lock.locked())

    async def test_tool_returns_readme_without_nested_ai_or_messages(self):
        self.event.plain_result = types.SimpleNamespace()  # Calling this would fail.
        result = await self.plugin.daily_tool(self.event, "python", 2)
        self.assertIsInstance(result, str)
        self.assertIn("a/one", result)
        self.assertIn("README 不可用", result)
        self.assertIn("a/two", result)
        self.assertIn("README 资料（全文）：\nREADME", result)
        self.assertNotIn("请稍候", result)
        self.plugin.client.trending.assert_awaited_once_with("python")
        self.context.llm_generate.assert_not_awaited()
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertFalse(self.plugin.lock.locked())

    async def test_tool_uses_configured_defaults_and_accepts_integral_number(self):
        self.plugin.config.update(default_language="python", default_count=1)
        self.plugin.client.readme.side_effect = None
        self.plugin.client.readme.return_value = "Repository README"
        result = await self.plugin.daily_tool(self.event)
        self.assertIn("a/one", result)
        self.assertNotIn("a/two", result)
        self.plugin.client.trending.assert_awaited_once_with("python")
        result = await self.plugin.daily_tool(self.event, "all", 2.0)
        self.assertIn("全部语言", result)
        self.assertIn("a/two", result)
        self.plugin.client.trending.assert_awaited_with("")

    async def test_tool_invalid_parameters_do_not_fetch(self):
        for count in [True, 1.5, -1, 26, "3", None, float("inf"), float("nan")]:
            with self.subTest(count=count):
                self.assertIn(
                    "数量必须",
                    await self.plugin.daily_tool(self.event, "python", count),
                )
        self.assertIn("语言名称", await self.plugin.daily_tool(self.event, None, 1))
        self.plugin.client.trending.assert_not_awaited()
        self.plugin.client.readme.assert_not_awaited()

    async def test_tool_permission_busy_and_shutdown_share_command_guards(self):
        self.plugin.config["admin_only"] = True
        self.assertIn("仅管理员", await self.plugin.daily_tool(self.event))
        self.plugin.config["admin_only"] = False
        async with self.plugin.lock:
            self.assertIn("另一条", await self.plugin.daily_tool(self.event))
        await self.plugin.terminate()
        self.assertIn("正在停止", await self.plugin.daily_tool(self.event))
        self.plugin.client.trending.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()

    async def test_tool_limits_combined_readme_material(self):
        self.plugin.client.trending.return_value = [
            Repository(f"owner/repo{i}") for i in range(25)
        ]
        self.plugin.client.readme.side_effect = None
        self.plugin.client.readme.return_value = "x" * 20000
        result = await self.plugin.daily_tool(self.event, "all", 25)
        self.assertEqual(result.count("x"), 12000)
        self.assertEqual(result.count("节选，已截断"), 25)
        self.assertLess(len(result), 18000)
        self.context.llm_generate.assert_not_awaited()

    async def test_terminate_cancels_inflight_tool(self):
        started = asyncio.Event()

        async def slow(language):
            started.set()
            await asyncio.sleep(60)

        self.plugin.client.trending.side_effect = slow
        task = asyncio.create_task(self.plugin.daily_tool(self.event))
        await started.wait()
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin.lock.locked())

    async def test_provider_unavailable_still_returns_ranking(self):
        self.context.get_current_chat_provider_id.side_effect = RuntimeError(
            "private credentials"
        )
        messages = [m async for m in self.plugin.daily(self.event)]
        self.assertIn("a/two", messages[-1])
        self.assertIn("配置聊天模型", messages[-1])
        self.context.llm_generate.assert_not_awaited()
        self.plugin.client.readme.assert_not_awaited()

    async def test_permission_validation_busy_and_shutdown(self):
        self.plugin.config["admin_only"] = True
        self.assertIn(
            "仅管理员", "\n".join([m async for m in self.plugin.daily(self.event)])
        )
        self.plugin.config["admin_only"] = False
        for count in ["0", "-1", "26", "abc"]:
            self.assertIn(
                "数量必须",
                "\n".join(
                    [m async for m in self.plugin.daily(self.event, "all", count)]
                ),
            )
        async with self.plugin.lock:
            self.assertIn(
                "另一条", "\n".join([m async for m in self.plugin.daily(self.event)])
            )
        self.plugin.client.trending.assert_not_awaited()
        await self.plugin.terminate()
        self.assertTrue(self.plugin.client.client.is_closed)
        self.assertIn(
            "正在停止", "\n".join([m async for m in self.plugin.daily(self.event)])
        )

    async def test_terminate_cancels_inflight_work(self):
        started = asyncio.Event()

        async def slow(language):
            started.set()
            await asyncio.sleep(60)

        self.plugin.client.trending.side_effect = slow

        async def consume():
            return [m async for m in self.plugin.daily(self.event)]

        task = asyncio.create_task(consume())
        await started.wait()
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin.lock.locked())


if __name__ == "__main__":
    unittest.main()
