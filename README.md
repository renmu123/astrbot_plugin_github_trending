# GitHub 日榜与 AI 摘要

AstrBot 插件：从 GitHub 官网获取 `since=daily` Trending，读取每个仓库默认分支的 README，使用 AstrBot 已配置的聊天模型生成中文摘要。

## 使用

在 AstrBot WebUI 的插件页加载或重载本插件，确保聊天模型已配置。支持 AstrBot **4.5.7 及以上**，依赖列于 `requirements.txt`，由 AstrBot 安装；手动环境可执行 `python -m pip install -r requirements.txt`。

| 命令 | 作用 |
| --- | --- |
| `/github日榜` | 默认返回全部语言日榜前 5 个项目及 README 摘要 |
| `/github日榜 python 3` | Python 日榜前 3 个项目 |
| `/github日榜 all 10` | 全部语言前 10 个项目，即使配置了默认语言 |
| `/github日榜 c++ 5` | C++ 日榜前 5 个项目 |
| `/github_trending typescript 3` | 英文命令别名 |
| `/github日榜帮助` | 查看帮助；英文别名 `/github_trending_help` |

语言和数量按顺序传入，数量为 1–25。榜单保留官网顺序，可能少于请求数量。先返回获取提示和榜单时间，再逐个发送项目，内容包含仓库链接、语言、总 Star、今日新增 Star、项目简介和 AI 摘要。

这里的 daily 是 **GitHub 当前日榜**，并非指定自然日的历史数据或插件自行计算的 Star 增量。插件通过命令触发获取。

## 配置

在 WebUI 的插件配置中修改，保存后重载插件：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `provider_id` | 留空 | 下拉选择摘要模型；留空使用当前会话模型 |
| `default_count` | 5 | 默认项目数量，1–25 |
| `default_language` | 留空 | 默认语言；留空表示全部语言 |
| `github_token` | 留空 | 可选，仅随官方 GitHub README API 请求发送 |
| `cache_minutes` | 60 | 榜单、README 和成功摘要的内存缓存；0 禁用 |
| `readme_max_chars` | 12000 | 输入给模型的 README 最大字符数，1000–50000 |
| `summary_max_chars` | 300 | 输出摘要最大字符数，100–800，不含状态标签 |
| `request_timeout` | 30 | GitHub 单次网络操作超时秒数，5–120 |
| `ai_timeout` | 90 | 每个项目 AI 摘要超时秒数，10–300 |
| `admin_only` | false | 开启后仅管理员可获取日榜 |
| `network_proxy.proxy_mode` | system | 环境代理、自定义代理或禁用环境代理 |
| `network_proxy.proxy_url` | 留空 | 自定义 HTTP/SOCKS5 代理地址 |

AI 接口使用 [AstrBot 官方 AI 文档](https://docs.astrbot.app/dev/star/guides/ai.html) 中的 `get_current_chat_provider_id(umo=...)` 和 `llm_generate(chat_provider_id=..., prompt=..., system_prompt=...)`，从 `completion_text` 取得摘要。摘要请求不附带聊天历史或工具，不写入会话历史。模型网络设置由 AstrBot 服务商配置控制。

README 通过 [GitHub 官方 README API](https://docs.github.com/en/rest/repos/contents#get-a-repository-readme) 获取，使用 raw 媒体类型，无需猜测 `main`/`master` 或 README 文件名。公开仓库无需 Token；遇到 API 额度限制时可配置 Token。

API 受限、Token 无效或 API 连接失败时，自动从 GitHub 仓库主页提取渲染后的 README 正文，不会把导航、登录提示或整个网页作为摘要资料。该回退不需要 Token；Token 不会发送给网页。若网页也无法读取，会分别说明 API 与网页的错误。

## 失败与缓存行为

- 每个 README 单独摘要。README 不存在、访问受限、AI 超时或失败时，保留该项目的官网信息并标明“摘要不可用”，继续处理后续项目。
- 未配置可用模型时仍返回日榜，明确提示选择模型。
- 网络连接错误和 5xx 最多尝试 3 次；API 401、403、429 自动回退网页，不立即重复请求 API。限流时遵守 `x-ratelimit-reset` 与 `retry-after`，冷却期间其他项目直接读取网页；无恢复时间的 403/429 冷却至少 60 秒，401 冷却 5 分钟。网页 429 最多尝试 3 次；404 不自动重试。错误信息不回显 Token、代理密码或响应正文。
- 日榜页面结构异常时明确报错，不把登录页、验证页当作空榜单。官网结构变化可能需要更新解析逻辑。
- 超长 README 取开头节选并注明；单份 README 超过 2 MB 时跳过。模型只根据 README 资料摘要，但仍可能出现错误，详情以原仓库为准。
- 缓存最多 256 项，重启或重载后清空。摘要按仓库、README 内容和模型区分；失败摘要不会缓存。榜单时间是实际获取时间，缓存命中时不会伪装为刚刚获取。
- 同一插件同时处理一条日榜请求，重复请求会提示稍后重试。卸载时取消进行中的请求并关闭 HTTP 客户端。

## 开发验证

在本插件目录运行：

```powershell
python -m unittest discover -s tests -v
```

测试使用 HTTP MockTransport 和 AstrBot 接口替身，不依赖 GitHub 实时榜单或付费模型。实际消息发送及真实模型摘要需在 AstrBot 中调用上述命令验证。
