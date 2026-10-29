# -*- coding: utf-8 -*-
"""X（Twitter）工具集插件：注册三个 LLM 工具读取和搜索 X（Twitter）内容。

支持查看指定账号的最新推文、用 X 高级搜索语法全站搜索推文、按链接读取单条推文
完整内容。搜索走 twitterapi.io（需要 API Key），其余走免费的 FxTwitter。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 没有 _conf_schema.json，WebUI 不会生成配置项，默认值由插件自己写到 data/plugin_data 下。
DEFAULT_CONFIG: dict[str, Any] = {
    "api_key": "",
    "proxy": "",
    "max_results": 20,
    "text_limit": 400,
    "read_text_limit": 3000,
    "search_daily_limit": 50,
    "block_sensitive": True,
    "request_timeout": 25,
}
FX_API = "https://api.fxtwitter.com"
SEARCH_API = "https://api.twitterapi.io/twitter/tweet/advanced_search"


def _as_int(value: Any, default: int) -> int:
    """把任意值转成 int，转不了就用默认值（配置文件可能被手写成字符串）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dumps(payload: dict[str, Any]) -> str:
    """统一输出不转义中文的 JSON，工具返回值会原样进入模型上下文。"""
    return json.dumps(payload, ensure_ascii=False)


def _truncate(text: str, limit: int) -> str:
    """按字符数截断文本（含省略号也不超过 limit 字），避免长推文撑爆模型上下文。"""
    return text[: limit - 1] + "…" if limit > 0 and len(text) > limit else text


def _iso_time(raw: dict[str, Any]) -> str:
    """把两个数据源各自的时间格式统一成 ISO8601 字符串。"""
    dt: datetime | None = None
    timestamp = raw.get("created_timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    if dt is None:
        numeric = raw.get("created_at") or raw.get("createdAt")
        if isinstance(numeric, (int, float)) and numeric > 0:
            dt = datetime.fromtimestamp(float(numeric), tz=timezone.utc)
        elif isinstance(numeric, str) and numeric.strip():
            try:
                # FxTwitter 是 "Tue Mar 10 07:00:30 +0000 2024" 这种 RFC 2822 变体
                dt = parsedate_to_datetime(numeric.strip())
            except (TypeError, ValueError):
                try:
                    dt = datetime.fromisoformat(numeric.strip().replace("Z", "+00:00"))
                except ValueError:
                    dt = None
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """把 FxTwitter（snake_case）和 twitterapi.io（camelCase）归一成同一套字段。

    两个源字段名差别很大（reposts/retweetCount、screen_name/userName、
    created_timestamp/createdAt），下游只认这个结构，免得每个工具各写兼容分支。
    """
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    handle = str(author.get("screen_name") or author.get("userName") or "")
    tweet_id = str(raw.get("id") or "")
    media_urls: list[str] = []
    media = raw.get("media")
    if isinstance(media, dict):  # FxTwitter: {"photos": [...], "videos": [...]}
        media_urls = [str(p["url"]) for p in media.get("photos") or [] if isinstance(p, dict) and p.get("url")]
        for video in media.get("videos") or []:
            video_url = (video.get("thumbnail_url") or video.get("url")) if isinstance(video, dict) else None
            if video_url:
                media_urls.append(str(video_url))
    elif isinstance(media, list):  # twitterapi.io 的 media 是数组
        for entry in media:
            if isinstance(entry, dict) and (entry.get("url") or entry.get("thumbnail_url")):
                media_urls.append(str(entry.get("url") or entry.get("thumbnail_url")))
            elif isinstance(entry, str):
                media_urls.append(entry)
    quote_raw = raw.get("quote") or raw.get("quoted_tweet")
    quote: dict[str, str] | None = None
    if isinstance(quote_raw, dict):
        quote_author = quote_raw.get("author") if isinstance(quote_raw.get("author"), dict) else {}
        quote_handle = str(quote_author.get("screen_name") or quote_author.get("userName") or "")
        # 引用推文只留作者和 200 字摘要，原文整坨 JSON 塞进去会翻倍占上下文。
        quote = {"author": f"@{quote_handle}" if quote_handle else "", "text": _truncate(str(quote_raw.get("text") or ""), 200)}
    translation = raw.get("translation")
    if isinstance(translation, dict):
        translation = translation.get("text") or translation.get("translated_text")
    replying_to_raw = raw.get("replying_to") or raw.get("in_reply_to_screen_name")
    replying_to = replying_to_raw if isinstance(replying_to_raw, str) else ""
    return {
        "id": tweet_id, "created_at": _iso_time(raw), "text": str(raw.get("text") or ""),
        "url": f"https://x.com/{handle}/status/{tweet_id}" if handle and tweet_id else str(raw.get("url") or ""),
        "author": str(author.get("name") or handle), "handle": handle, "lang": str(raw.get("lang") or ""),
        "likes": _as_int(raw.get("likes") or raw.get("likeCount"), 0),
        "reposts": _as_int(raw.get("reposts") or raw.get("retweets") or raw.get("retweetCount"), 0),
        "replies": _as_int(raw.get("replies") or raw.get("replyCount"), 0),
        "quotes": _as_int(raw.get("quotes") or raw.get("quoteCount"), 0),
        "views": _as_int(raw.get("views") or raw.get("viewCount"), 0),
        "is_reply": bool(replying_to or raw.get("is_reply")), "replying_to": replying_to,
        "media": media_urls, "quote": quote, "translation": translation if isinstance(translation, str) else "",
        "possibly_sensitive": bool(raw.get("possibly_sensitive") or raw.get("possiblySensitive")),
    }


def _summary(item: dict[str, Any], text_limit: int) -> dict[str, Any]:
    """列表接口只回裁剪后的字段：原始单条 JSON 约 8.5KB，20 条约 170KB 会撑爆上下文。"""
    return {"id": item["id"], "url": item["url"], "author": item["author"], "handle": item["handle"],
            "created_at": item["created_at"], "text": _truncate(item["text"], text_limit),
            "likes": item["likes"], "reposts": item["reposts"], "replies": item["replies"], "views": item["views"]}


class XToolsPlugin(Star):
    """X 工具集：账号时间线、全站搜索、单推文详情三个 LLM 工具。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # 没有 _conf_schema.json，框架传进来的 config 永远是 None/{}，所以配置自己读 JSON。
        self._config_path = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_xtools" / "config.json"
        self._config = self._load_config()
        self._client: httpx.AsyncClient | None = None
        # 搜索日限额只放内存：进程重启后清零，省掉持久化，也够约束一天的调用量。
        self._search_usage: dict[str, Any] = {"date": "", "count": 0}

    def _load_config(self) -> dict[str, Any]:
        """读取 config.json；文件缺失时写入默认值，损坏时回退默认值。"""
        raw: Any = None
        if self._config_path.exists():
            try:
                raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # 配置写坏了不能让插件加载失败，回退默认值并留下可排查的错误日志。
                logger.error(f"[xtools] bad config {self._config_path}: {exc}; using defaults")
        else:
            try:
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                self._config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.warning(f"[xtools] default config created at {self._config_path}; fill in api_key")
            except OSError as exc:
                logger.error(f"[xtools] cannot write config {self._config_path}: {exc}")
        data = raw if isinstance(raw, dict) else {}
        config = dict(DEFAULT_CONFIG)
        config["api_key"] = data["api_key"].strip() if isinstance(data.get("api_key"), str) else ""
        config["proxy"] = data["proxy"].strip() if isinstance(data.get("proxy"), str) else ""
        # int 字段统一过 _as_int，手写配置写成字符串也不会让插件崩。
        # 单次返回条数上限，默认 20 等于不额外限制（count 本身已夹到 1..20）
        config["max_results"] = max(1, _as_int(data.get("max_results"), 20))
        config["text_limit"] = max(1, _as_int(data.get("text_limit"), 400))
        config["read_text_limit"] = max(1, _as_int(data.get("read_text_limit"), 3000))
        config["search_daily_limit"] = max(0, _as_int(data.get("search_daily_limit"), 50))
        config["request_timeout"] = max(1, _as_int(data.get("request_timeout"), 25))
        block = data.get("block_sensitive", True)
        # 手写 "false"/"0" 也按布尔处理，别让字符串 "false" 变成 True。
        config["block_sensitive"] = block if isinstance(block, bool) else str(block).strip().lower() not in ("0", "false", "no", "off")
        return config

    async def initialize(self) -> None:
        """插件加载后创建 HTTP client。"""
        self._client = httpx.AsyncClient(
            trust_env=True,  # 容器里通常靠 HTTP_PROXY/HTTPS_PROXY 环境变量出网
            proxy=self._config["proxy"] or None, timeout=float(self._config["request_timeout"]),
            headers={"User-Agent": "astrbot-plugin-xtools/1.0.0"},
        )
        logger.info("[xtools] http client initialized")

    async def terminate(self) -> None:
        """插件卸载时关闭 client。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("[xtools] http client closed")

    async def _get_json(self, url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Any:
        """GET 并解析 JSON；失败时抛 httpx.HTTPError 或 ValueError，由调用方决定策略。"""
        if self._client is None:  # 正常流程里 initialize 先执行，这里兜住热重载时工具被提前调用
            self._client = httpx.AsyncClient(trust_env=True, proxy=self._config["proxy"] or None, timeout=float(self._config["request_timeout"]))
        response = await self._client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    @filter.llm_tool(name="get_x_user_tweets")
    async def tool_user_tweets(
        self,
        event: AstrMessageEvent,
        username: str,
        count: int = 5,
        with_replies: bool = False,
    ) -> str:
        """查看某个 X（Twitter）账号最近发布了哪些推文。

        当用户问「某人/某机构最近在 X 上发了什么」「看看 @nasa 的最新动态」
        「这个账号最近有什么更新」时使用。返回裁剪后的摘要列表，正文只保留前
        几百字；要完整正文、图片或引用内容时，再用 read_x_tweet 读单条。工具会
        跳过转推（reposted_by 非空的条目），with_replies 为 false 时还会跳过该
        账号对他人的回复。

        Args:
            username(string): X 账号用户名，不带 @ 前缀，例如 nasa、elonmusk。
            count(number): 返回的推文条数，1-20，默认 5。
            with_replies(boolean): 为 true 时包含该账号对他人的回复，默认 false。

        Returns:
            JSON 字符串，成功形如 {"success": true, "handle": "nasa", "count": 3,
            "tweets": [...]}，失败形如 {"success": false, "error": "中文说明"}。
        """
        handle = str(username or "").strip().lstrip("@")
        if not handle:
            return _dumps({"success": False, "error": "username 不能为空"})
        count = min(max(_as_int(count, 5), 1), 20, self._config["max_results"])
        text_limit = self._config["text_limit"]
        block_sensitive = self._config["block_sensitive"]
        results: list[Any] = []
        for attempt in range(2):
            try:
                data = await self._get_json(f"{FX_API}/2/profile/{handle}/statuses", params={"with_replies": "1" if with_replies else "0"})
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(f"[xtools] timeline @{handle} failed (attempt {attempt + 1}): {exc}")
                data = None
            if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
                results = data["results"]
                break
            if attempt == 0:  # 该端点实测偶发 HTTP 404 且 results 为空，1.5 秒后重试一次基本能恢复
                await asyncio.sleep(1.5)
        if not results:
            return _dumps({"success": False, "error": f"获取 @{handle} 的时间线失败，FxTwitter 接口暂时不可用，请稍后重试"})
        tweets: list[dict[str, Any]] = []
        for raw in results:
            if not isinstance(raw, dict) or raw.get("reposted_by"):
                continue  # 转推不是该账号自己的内容
            item = _normalize(raw)
            if not with_replies and item["text"].startswith("@"):
                continue  # 回复他人时正文以 @ 开头
            if block_sensitive and item["possibly_sensitive"]:
                continue
            tweets.append(_summary(item, text_limit))
            if len(tweets) >= count:
                break
        return _dumps({"success": True, "handle": handle, "count": len(tweets), "tweets": tweets})

    @filter.llm_tool(name="search_x_posts")
    async def tool_search(
        self,
        event: AstrMessageEvent,
        query: str,
        count: int = 10,
        sort: str = "Latest",
    ) -> str:
        """在 X 上全站搜索推文（twitterapi.io 高级搜索接口）。

        当用户要求查找某个话题、事件、人物、品牌在 X 上的相关推文时使用。
        query 支持 X 高级搜索语法，本工具原样透传给下游服务，不解析、不改写：
        from:nasa 限定账号；-词 排除该词；min_faves:100 只要点赞过百；
        since:2026-10-01 限定起始日期；filter:media 只要带图或视频；OR 连接关键词；
        #标签 按话题标签搜索。返回的仍是摘要（正文已截断），需要完整正文、图片、
        引用推文或译文时，请对结果里的 url 再调用 read_x_tweet。

        Args:
            query(string): X 高级搜索表达式，必填，例如 from:nasa filter:media。
            count(number): 返回的推文条数，1-20，默认 10。
            sort(string): Latest 按最新排序，Top 按热门排序；默认 Latest，
                其他取值一律按 Latest 处理。

        Returns:
            JSON 字符串，成功形如 {"success": true, "query": "from:nasa",
            "count": 2, "tweets": [...]}，失败形如 {"success": false,
            "error": "中文说明"}。
        """
        query = str(query or "").strip()
        if not query:
            return _dumps({"success": False, "error": "搜索关键词不能为空"})
        api_key = self._config["api_key"]
        if not api_key:
            return _dumps({"success": False, "error": "未配置 twitterapi.io API Key，搜索功能不可用"})

        # 日限额按自然日计数，只放内存，插件重启后清零。
        today = time.strftime("%Y-%m-%d")
        if self._search_usage["date"] != today:
            self._search_usage = {"date": today, "count": 0}
        daily_limit = self._config["search_daily_limit"]
        if daily_limit > 0 and self._search_usage["count"] >= daily_limit:
            return _dumps({"success": False, "error": f"今日搜索额度已用完（{daily_limit} 次/天）"})
        count = min(max(_as_int(count, 10), 1), 20, self._config["max_results"])
        text_limit = self._config["text_limit"]
        block_sensitive = self._config["block_sensitive"]
        query_type = "Top" if str(sort or "").strip().lower() == "top" else "Latest"
        self._search_usage["count"] += 1  # 先占额度再请求，避免失败重试把额度打穿
        try:
            data = await self._get_json(
                SEARCH_API, params={"query": query, "queryType": query_type, "cursor": ""}, headers={"x-api-key": api_key}
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"[xtools] advanced_search failed: {exc}")
            return _dumps({"success": False, "error": f"搜索请求失败：{exc}"})
        raw_tweets = data.get("tweets") if isinstance(data, dict) else None
        if not isinstance(raw_tweets, list):
            return _dumps({"success": False, "error": "搜索接口返回格式异常"})
        tweets: list[dict[str, Any]] = []
        for raw in raw_tweets:
            if not isinstance(raw, dict):
                continue
            item = _normalize(raw)
            if block_sensitive and item["possibly_sensitive"]:
                continue
            tweets.append(_summary(item, text_limit))
            if len(tweets) >= count:
                break
        return _dumps({"success": True, "query": query, "count": len(tweets), "tweets": tweets})

    @filter.llm_tool(name="read_x_tweet")
    async def tool_read_tweet(
        self,
        event: AstrMessageEvent,
        url: str,
    ) -> str:
        """读取单条 X 推文的完整内容（详情级读取）。

        当用户给出推文链接、要求「看看这条推文写了什么」「翻译/总结/核查这条推文」
        「这条推文里的图是什么」时使用。返回正文全文（最长 read_text_limit 字）、
        作者、时间、互动数据、图片链接、引用推文摘要和译文；列表工具返回的摘要
        不够用时也应该用本工具补齐细节。

        支持 https://x.com/nasa/status/123、https://twitter.com/i/status/123、
        https://x.com/i/web/status/123、https://fxtwitter.com/.../status/123、
        https://fixupx.com/.../status/123、https://vxtwitter.com/.../status/123、
        /statuses/ 复数形式，以及纯数字推文 ID。

        Args:
            url(string): 推文链接或纯数字推文 ID。

        Returns:
            JSON 字符串，成功时含 id、url、author、created_at、text、lang、
            likes、reposts、replies、quotes、views、is_reply、replying_to、
            media、quote、translation；quote 没有引用时为 null，translation
            没有译文时为空字符串。失败形如 {"success": false, "error": "中文说明"}。
        """
        target = str(url or "").strip()
        match = re.search(r"/status(?:es)?/(\d+)", target)
        tweet_id = match.group(1) if match else (target if target.isdigit() else "")
        if not tweet_id:
            return _dumps({"success": False, "error": "无法从链接中解析出推文 ID，请提供形如 https://x.com/nasa/status/123 的链接或纯数字 ID"})
        raw: dict[str, Any] | None = None
        try:
            data = await self._get_json(f"{FX_API}/2/status/{tweet_id}")
            if isinstance(data, dict) and isinstance(data.get("status"), dict):
                raw = data["status"]
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"[xtools] v2 status {tweet_id} failed: {exc}")
        if raw is None:
            # v2 拿不到时退回 v1：字段略少（转发数叫 retweets、没有 possibly_sensitive），
            # 但能保证 v2 抖动时详情页仍然可用。
            try:
                data = await self._get_json(f"{FX_API}/status/{tweet_id}")
                if isinstance(data, dict) and isinstance(data.get("tweet"), dict):
                    raw = data["tweet"]
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(f"[xtools] v1 status {tweet_id} failed: {exc}")
        if raw is None:
            return _dumps({"success": False, "error": "读取推文失败，推文可能已删除、账号受保护或 FxTwitter 暂时不可用"})
        item = _normalize(raw)
        if self._config["block_sensitive"] and item["possibly_sensitive"]:
            return _dumps({"success": False, "error": "这条推文被标记为敏感内容，已拒绝读取"})
        return _dumps({
            "success": True, "id": item["id"] or tweet_id, "url": item["url"] or f"https://x.com/i/status/{tweet_id}",
            "author": {"name": item["author"], "handle": item["handle"]}, "created_at": item["created_at"],
            "text": _truncate(item["text"], self._config["read_text_limit"]), "lang": item["lang"],
            "likes": item["likes"], "reposts": item["reposts"], "replies": item["replies"], "quotes": item["quotes"],
            "views": item["views"], "is_reply": item["is_reply"], "replying_to": item["replying_to"],
            "media": item["media"], "quote": item["quote"], "translation": item["translation"],
        })
