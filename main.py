import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .service import (
    GitHubClient,
    TrendingError,
    TrendingService,
    format_repo,
    normalize_language,
)

HELP = """GitHub 每日 Trending + README 中文摘要
/github日榜 [语言] [数量]：默认全部语言、前 5 个项目
例如 /github日榜 python 3，/github日榜 all 10
英文别名：/github_trending
/github日榜帮助：显示帮助
数量范围 1–25。摘要使用指定模型，留空则使用当前会话模型。
日榜为 GitHub 当前 daily 榜单，按官网顺序返回；每个项目单独发送。
缓存默认 1 小时；修改配置后请重新加载插件。"""


def bounded_int(config, key, default, lower, upper):
    try:
        return max(lower, min(upper, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


class GitHubTrendingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        network = config.get("network_proxy", {})
        self.client = GitHubClient(
            token=config.get("github_token", "").strip(),
            proxy_mode=network.get("proxy_mode", "system"),
            proxy_url=network.get("proxy_url", "").strip(),
            timeout=bounded_int(config, "request_timeout", 30, 5, 120),
        )
        self.service = TrendingService(
            self.client,
            cache_seconds=bounded_int(config, "cache_minutes", 60, 0, 1440) * 60,
            readme_chars=bounded_int(config, "readme_max_chars", 12000, 1000, 50000),
            summary_chars=bounded_int(config, "summary_max_chars", 300, 100, 800),
            ai_timeout=bounded_int(config, "ai_timeout", 90, 10, 300),
        )
        self.lock = asyncio.Lock()
        self.active_task = None
        self.closing = False

    @filter.command("github日榜", alias={"github_trending"})
    async def daily(self, event: AstrMessageEvent, language: str = "", count: str = ""):
        """获取 GitHub daily Trending 并使用 AI 摘要各项目 README。"""
        if self.config.get("admin_only", False) and not event.is_admin():
            yield event.plain_result("此命令仅管理员可使用。")
            return
        if self.closing:
            yield event.plain_result("插件正在停止，请稍后重试。")
            return
        try:
            language = normalize_language(
                language or self.config.get("default_language", "")
            )
            try:
                count = (
                    bounded_int(self.config, "default_count", 5, 1, 25)
                    if count == ""
                    else int(count)
                )
            except (TypeError, ValueError):
                raise TrendingError(
                    "数量必须为 1–25，例如 /github日榜 python 5。"
                ) from None
            if not 1 <= count <= 25:
                raise TrendingError("数量必须为 1–25，例如 /github日榜 python 5。")
        except TrendingError as exc:
            yield event.plain_result(str(exc))
            return
        if self.lock.locked():
            yield event.plain_result("正在处理另一条日榜请求，请稍后重试。")
            return
        async with self.lock:
            self.active_task = asyncio.current_task()
            try:
                yield event.plain_result(
                    "正在获取 GitHub 日榜并生成 README 摘要，请稍候。"
                )
                repos, fetched_at = await self.service.ranking(language)
                if not repos:
                    yield event.plain_result("GitHub 当前日榜在此语言下没有项目。")
                    return
                provider_id = self.config.get("provider_id", "").strip()
                if not provider_id:
                    try:
                        provider_id = await self.context.get_current_chat_provider_id(
                            umo=event.unified_msg_origin
                        )
                    except Exception as exc:  # noqa: BLE001 - provider failures must preserve the ranking
                        logger.warning(
                            "GitHub Trending 获取会话模型失败 (%s)", type(exc).__name__
                        )
                repos = repos[:count]
                yield event.plain_result(
                    f"GitHub 每日 Trending | {language or '全部语言'} | {len(repos)} 个项目\n"
                    f"榜单获取时间：{fetched_at}\n"
                    "来源：GitHub Trending（since=daily）"
                )
                for index, repo in enumerate(repos, 1):
                    summary = (
                        await self.service.summarize(repo, self.context, provider_id)
                        if provider_id
                        else "摘要不可用：请在 AstrBot 配置聊天模型或在插件中选择摘要模型。"
                    )
                    yield event.plain_result(format_repo(index, repo, summary))
            except TrendingError as exc:
                yield event.plain_result(f"获取日榜失败：{exc}")
            except Exception as exc:  # noqa: BLE001 - do not expose provider or proxy credentials
                logger.error("GitHub Trending 请求异常 (%s)", type(exc).__name__)
                yield event.plain_result("获取日榜失败，请检查插件配置和网络连接。")
            finally:
                self.active_task = None

    @filter.command("github日榜帮助", alias={"github_trending_help"})
    async def help_command(self, event: AstrMessageEvent):
        """显示 GitHub 日榜插件帮助。"""
        yield event.plain_result(HELP)

    async def terminate(self):
        self.closing = True
        task = self.active_task
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.client.close()
