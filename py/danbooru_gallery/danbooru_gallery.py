import requests
import json
import folder_paths
from server import PromptServer
from aiohttp import web
import time
import threading
import asyncio
import torch
import io
import urllib.request
import urllib.parse
import numpy as np
from PIL import Image
import os
import re
from requests.auth import HTTPBasicAuth
import urllib3
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..utils.logger import get_logger
from .site_adapters import get_site_adapter
from .site_clients import DanbooruHttpClient, GelbooruHttpClient
from .post_cache import get_gallery_post_cache
from .thumbnail_cache import get_thumbnail, save_thumbnail
from .offline_routes import OfflineGallery
from .local_favorites import LocalFavorites, LocalFavoritesStore, has_details
from ..shared.translation.translation_loader import get_translation_loader
from functools import partial

logger = get_logger(__name__)

# 导入数据库管理器
try:
    from ..shared.db.db_manager import get_db_manager
except ImportError as e:
    logger.warning(f"[Autocomplete] 无法导入数据库管理器，将仅使用远程API模式: {e}")
    get_db_manager = None

# 禁用 SSL 警告（如果需要禁用证书验证）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REDACTED_QUERY_KEYS = {"api_key", "key", "login", "password", "token", "user_id"}

HTTP_STATUS_HINTS = {
    400: ("请求参数不太对", "检查标签、页码或筛选条件是否包含站点不支持的写法。"),
    401: ("认证失败", "检查 User ID、用户名或 API Key 是否填错，必要时重新生成 API Key。"),
    403: ("访问被拒绝", "站点可能拦截了当前网络/IP、权限不足，或触发了 Cloudflare/防爬策略。"),
    404: ("资源不存在", "图片、收藏记录或接口路径可能已经不存在。"),
    408: ("请求超时", "网络太慢或站点没有及时响应，可以稍后重试。"),
    409: ("请求冲突", "资源状态可能已经改变，刷新后再试一次。"),
    422: ("站点拒绝了这次操作", "通常是重复收藏、标签参数无效，或站点返回了校验错误。"),
    423: ("资源被锁定", "站点暂时不允许修改该资源。"),
    425: ("请求过早", "稍等几秒再试，避免站点把请求判定为异常。"),
    429: ("请求过于频繁", "已经触发限流，请降低刷新/翻页速度，等一会儿再试。"),
    500: ("站点内部错误", "对方服务器出错了，通常只能稍后再试。"),
    502: ("网关错误", "站点或中间网关暂时不稳定，可以稍后重试。"),
    503: ("服务暂不可用", "站点可能在维护、限流，或当前 IP 被临时挡住。"),
    504: ("网关超时", "站点响应太慢，可能是网络链路或服务器拥堵。"),
}


def _redact_url(raw_url):
    if not raw_url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(str(raw_url))
        safe_query = []
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            safe_query.append((key, "***" if key.lower() in REDACTED_QUERY_KEYS else value))
        return urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(safe_query),
            parsed.fragment,
        ))
    except Exception:
        return str(raw_url)


def _response_excerpt(response, limit=500):
    if response is None:
        return ""
    try:
        text = response.text or ""
    except Exception:
        return ""
    return text[:limit]


def _classify_request_exception(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection"
    if isinstance(exc, requests.exceptions.HTTPError):
        return "http"
    return "request"


def build_gallery_error_payload(source, action, exc=None, response=None, message=None, retries_exhausted=False, retry_count=None):
    if response is None:
        response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    status_title, status_advice = HTTP_STATUS_HINTS.get(status_code, ("网络请求失败", "检查网络、代理、DNS 或稍后重试。"))
    site_name = "Gelbooru" if (source or "").lower() == "gelbooru" else "Danbooru"
    category = _classify_request_exception(exc) if exc else ("http" if status_code else "unknown")

    if category == "timeout":
        status_title = "请求超时"
        status_advice = "站点响应太慢，建议稍后重试；如果经常出现，可以检查代理或网络质量。"
    elif category == "connection":
        status_title = "连接失败"
        status_advice = "无法连到站点，可能是 DNS/代理/IP 质量、网络中断，或站点临时拦截。"
    elif category == "ssl":
        status_title = "SSL 连接失败"
        status_advice = "证书校验或 HTTPS 握手失败，常见于代理、抓包工具或系统证书异常。"

    if retries_exhausted:
        summary = f"{site_name} {action}失败：已达到最大尝试次数，最后一次结果是{status_title}。"
    else:
        summary = message or f"{site_name} {action}失败：{status_title}。"

    raw_exception = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    if not raw_exception and response is not None:
        raw_exception = f"HTTP {status_code}: {getattr(response, 'reason', '')}".strip()

    details = {
        "source": site_name,
        "action": action,
        "category": category,
        "status_code": status_code,
        "status_hint": status_title,
        "retries_exhausted": bool(retries_exhausted),
        "retry_count": retry_count,
        "url": _redact_url(getattr(getattr(response, "request", None), "url", "") or getattr(response, "url", "")),
        "raw_exception": raw_exception,
        "response_excerpt": _response_excerpt(response),
    }

    return {
        "error": summary,
        "error_info": {
            "title": f"{site_name} 连接详情",
            "summary": summary,
            "causes": [
                status_advice,
                "如果只有 Gelbooru 经常失败，可能与当前 IP 信誉、地区网络或站点反爬策略有关。",
                "如果 Danbooru/Gelbooru 都失败，优先检查本机网络、代理软件、DNS 和防火墙。",
            ],
            "suggestions": [
                "稍等 30-120 秒后重试，避免连续刷新触发限流。",
                "确认代理/网络可以直接打开对应站点页面。",
                "如果在线浏览受限，重新检查 User ID/API Key 是否正确。",
            ],
            "tip": "看不懂也没关系，把详情复制给 AI 问问。它很擅长把这些小刺猬一样的报错梳顺。",
            "details": details,
        },
    }

# Danbooru API文档链接 https://danbooru.donmai.us/wiki_pages/help:api

# Danbooru API的基础URL
BASE_URL = "https://danbooru.donmai.us"

_danbooru_client = DanbooruHttpClient()
_gelbooru_client = GelbooruHttpClient()

# Gelbooru 公开 HTML 页列表固定每页 42 张，pid 为该页首张图片的偏移量
GELBOORU_PUBLIC_PAGE_SIZE = 42

def _danbooru_request(method, url, **kwargs):
    """Compatibility wrapper for the isolated Danbooru transport."""
    return _danbooru_client.request(method, url, **kwargs)


def _gelbooru_request(method, url, request_kind="api", display_all=False, force_refresh=False, **kwargs):
    """Route Gelbooru traffic through its own sessions and throttles."""
    return _gelbooru_client.request(
        method,
        url,
        request_kind=request_kind,
        display_all=display_all,
        force_refresh=force_refresh,
        **kwargs,
    )


async def _run_http_request(request_func, *args, **kwargs):
    """Run a synchronous site client without blocking aiohttp's event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(request_func, *args, **kwargs))

# 获取插件目录路径
# 获取当前文件所在目录
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")

def load_settings():
    """从本地文件加载所有设置"""
    default_settings = {
        "language": "zh",
        "blacklist": [],
        "filter_tags": [
            "watermark", "sample_watermark", "weibo_username", "weibo", "weibo_logo",
            "weibo_watermark", "censored", "mosaic_censoring", "artist_name", "twitter_username"
        ],
        "filter_enabled": True,
        "danbooru_username": "",
        "danbooru_api_key": "",
        "gelbooru_user_id": "",
        "gelbooru_api_key": "",
        "gelbooru_display_all_site_content": False,
        "favorites": [],
        "debug_mode": False,
        "cache_enabled": True,
        "max_cache_age": 3600,
        "persistent_post_cache_age": 2592000,
        "default_page_size": 20,
        "autocomplete_enabled": True,
        "tooltip_enabled": True,
        "autocomplete_max_results": 20,
        "selected_categories": ["copyright", "character", "general"],
        "source_site": "danbooru"
    }

    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for key, value in default_settings.items():
                    if key not in data:
                        data[key] = value
                return data
    except Exception as e:
        logger.error(f"加载设置失败: {e}")

    return default_settings

def load_autocomplete_config():
    """加载自动补全配置（用于数据库优先+API fallback机制）"""
    # 默认配置
    default_config = {
        "offline_mode": {
            "enabled": True,
            "fallback_to_remote": True,
            "remote_timeout_ms": 2000  # 2秒超时
        },
        "cache": {
            "use_database_query": True
        }
    }

    # 尝试从多个位置加载配置
    config_paths = [
        Path(PLUGIN_DIR) / "config.json",
        Path(PLUGIN_DIR).parent / "config.json",
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    # 深度合并配置
                    if "offline_mode" in loaded:
                        default_config["offline_mode"].update(loaded["offline_mode"])
                    if "cache" in loaded:
                        default_config["cache"].update(loaded["cache"])
                    logger.info(f"[Autocomplete] 加载配置: {config_path}")
                    return default_config
            except Exception as e:
                logger.warning(f"[Autocomplete] 配置文件加载失败 {config_path}: {e}")

    logger.info("[Autocomplete] 使用默认配置")
    return default_config

def save_settings(settings):
    """保存所有设置到本地文件"""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存设置失败: {e}")
        return False

def load_user_auth():
    """从统一设置文件加载用户认证信息"""
    settings = load_settings()
    return settings.get("danbooru_username", ""), settings.get("danbooru_api_key", "")

def save_user_auth(username, api_key):
    """保存用户认证信息到统一设置文件"""
    settings = load_settings()
    settings["danbooru_username"] = username
    settings["danbooru_api_key"] = api_key
    return save_settings(settings)

def load_gelbooru_auth():
    """从统一设置文件加载Gelbooru API认证信息"""
    settings = load_settings()
    return settings.get("gelbooru_user_id", ""), settings.get("gelbooru_api_key", "")

def save_gelbooru_auth(user_id, api_key):
    """保存Gelbooru API认证信息到统一设置文件"""
    settings = load_settings()
    settings["gelbooru_user_id"] = user_id
    settings["gelbooru_api_key"] = api_key
    return save_settings(settings)

def get_site_credentials(adapter):
    """返回站点适配器需要的认证参数。Danbooru 使用 HTTP Basic Auth，Gelbooru 使用 query 参数。"""
    if adapter.key == "gelbooru":
        user_id, api_key = load_gelbooru_auth()
        return {"user_id": user_id, "api_key": api_key}
    return {}

def has_required_site_credentials(adapter, credentials):
    """检查当前站点适配器的必要凭据是否已配置。"""
    if adapter.key == "gelbooru":
        return bool(credentials.get("user_id") and credentials.get("api_key"))
    return True

def load_language():
    """从统一设置文件加载语言设置"""
    settings = load_settings()
    return settings.get("language", "zh")

def save_language(language):
    """保存语言设置到统一设置文件"""
    settings = load_settings()
    settings["language"] = language
    return save_settings(settings)

def load_blacklist():
    """从统一设置文件加载黑名单"""
    settings = load_settings()
    return settings.get("blacklist", [])

def save_blacklist(blacklist_items):
    """保存黑名单到统一设置文件"""
    settings = load_settings()
    settings["blacklist"] = blacklist_items
    return save_settings(settings)

def load_filter_tags():
    """从统一设置文件加载提示词过滤设置"""
    settings = load_settings()
    return settings.get("filter_tags", []), settings.get("filter_enabled", True)

def save_filter_tags(filter_tags, enabled):
    """保存提示词过滤设置到统一设置文件"""
    settings = load_settings()
    settings["filter_tags"] = filter_tags
    settings["filter_enabled"] = enabled
    return save_settings(settings)

def load_ui_settings():
    """从统一设置文件加载UI设置"""
    settings = load_settings()
    return {
        "autocomplete_enabled": settings.get("autocomplete_enabled", True),
        "tooltip_enabled": settings.get("tooltip_enabled", True),
        "autocomplete_max_results": settings.get("autocomplete_max_results", 20),
        "autocomplete_min_post_count": settings.get("autocomplete_min_post_count", 100),
        "selected_categories": settings.get("selected_categories", ["copyright", "character", "general"]),
        "multi_select_enabled": settings.get("multi_select_enabled", False),
        "source_site": settings.get("source_site", "danbooru"),
        "gelbooru_display_all_site_content": settings.get("gelbooru_display_all_site_content", False)
    }

def save_ui_settings(ui_settings):
    """保存UI设置到统一设置文件"""
    settings = load_settings()
    threshold = ui_settings.get("autocomplete_min_post_count", settings.get("autocomplete_min_post_count", 100))
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 10 <= threshold <= 2**53 - 1:
        raise ValueError("Minimum post count must be an integer of at least 10")
    settings["autocomplete_min_post_count"] = threshold
    settings["autocomplete_enabled"] = ui_settings.get("autocomplete_enabled", True)
    settings["tooltip_enabled"] = ui_settings.get("tooltip_enabled", True)
    settings["autocomplete_max_results"] = ui_settings.get("autocomplete_max_results", 20)
    settings["selected_categories"] = ui_settings.get("selected_categories", ["copyright", "character", "general"])
    settings["multi_select_enabled"] = ui_settings.get("multi_select_enabled", False)
    settings["source_site"] = ui_settings.get("source_site", "danbooru")
    settings["gelbooru_display_all_site_content"] = ui_settings.get("gelbooru_display_all_site_content", False)
    return save_settings(settings)

# ================================
# Tag翻译系统
# ================================

translation_system = get_translation_loader()

def check_network_connection(source="danbooru"):
    """Check network connectivity for the selected gallery source."""
    adapter = get_site_adapter(source)
    try:
        # 使用一个简单的公开API端点来检测连接
        if adapter.key == "gelbooru":
            ui_settings = load_ui_settings()
            display_all = ui_settings.get("gelbooru_display_all_site_content", False)
            response = _gelbooru_request(
                "GET",
                adapter.posts_url,
                request_kind="public",
                display_all=display_all,
                params=adapter.build_public_posts_params("", 1, 1, None),
                timeout=(8, 10),
            )
            if response.status_code == 200:
                return True, False, None
            return False, True, build_gallery_error_payload(
                adapter.key,
                "network check",
                response=response,
                retries_exhausted=True,
                retry_count=1,
            )

        test_url = f"{BASE_URL}/posts.json?limit=1"
        response = _danbooru_request("GET", test_url, timeout=10)
        if response.status_code == 200:
            return True, False, None
        return False, True, build_gallery_error_payload(
            adapter.key,
            "network check",
            response=response,
            retries_exhausted=True,
            retry_count=1,
        )
    except requests.exceptions.Timeout as e:
        logger.error("网络连接超时")
        return False, True, build_gallery_error_payload(adapter.key, "network check", exc=e, retries_exhausted=True, retry_count=1)
    except requests.exceptions.RequestException as e:
        logger.error(f"网络连接失败: {e}")
        return False, True, build_gallery_error_payload(adapter.key, "network check", exc=e, retries_exhausted=True, retry_count=1)
    except Exception as e:
        logger.error(f"网络检测发生未知错误: {e}")
        return False, True, build_gallery_error_payload(adapter.key, "network check", exc=e, retries_exhausted=True, retry_count=1)

def verify_danbooru_auth(username, api_key):
    """验证Danbooru用户认证"""
    if not username or not api_key:
        return False, False
    try:
        test_url = f"{BASE_URL}/profile.json?login={username}&api_key={api_key}"
        response = _danbooru_request("GET", test_url, auth=HTTPBasicAuth(username, api_key), timeout=15)
        is_valid = response.status_code == 200
        return is_valid, False
    except Exception as e:
        logger.error(f"验证用户认证失败: {e}")
        return False, True

@PromptServer.instance.routes.get("/danbooru_gallery/user_auth")
async def get_user_auth_route(request):
    """获取用户认证信息"""
    try:
        username, api_key = load_user_auth()
        gelbooru_user_id, gelbooru_api_key = load_gelbooru_auth()
        has_auth = bool(username and api_key)
        return web.json_response({
            "success": True,
            "username": username,
            "api_key": api_key,
            "has_auth": has_auth,
            "gelbooru_user_id": gelbooru_user_id,
            "gelbooru_api_key": gelbooru_api_key,
            "gelbooru_has_auth": bool(gelbooru_user_id and gelbooru_api_key)
        })
    except Exception as e:
        logger.error(f"获取用户认证接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/user_auth")
async def save_user_auth_route(request):
    """保存用户认证信息"""
    try:
        data = await request.json()
        username = data.get("username", "")
        api_key = data.get("api_key", "")
        gelbooru_user_id = data.get("gelbooru_user_id", "")
        gelbooru_api_key = data.get("gelbooru_api_key", "")
        if save_user_auth(username, api_key) and save_gelbooru_auth(gelbooru_user_id, gelbooru_api_key):
            return web.json_response({"success": True})
        else:
            return web.json_response({"success": False, "error": "无法保存用户认证信息"}, status=500)
    except Exception as e:
        logger.error(f"保存用户认证接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/check_network")
async def check_network(request):
    """检测网络连接状态"""
    try:
        source = request.query.get("source") or load_ui_settings().get("source_site", "danbooru")
        adapter = get_site_adapter(source)
        if adapter.key == "danbooru":
            scope, store = offline_gallery.scope(request)
            if await offline_gallery.is_local(scope, store):
                return web.json_response({"success": False, "connected": False, "favorites": [], "error": "仅本地浏览，需手动重试联网"})

        is_connected, is_network_error, error_payload = await _run_http_request(
            check_network_connection,
            adapter.key,
        )
        payload = {
            "success": True,
            "connected": is_connected,
            "network_error": is_network_error,
            "source": adapter.key
        }
        if error_payload:
            payload.update(error_payload)
        return web.json_response(payload)
    except Exception as e:
        logger.error(f"网络检测接口错误: {e}")
        payload = {"success": False, "network_error": True}
        payload.update(build_gallery_error_payload("danbooru", "network check", exc=e))
        return web.json_response(payload, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/verify_auth")
async def verify_auth(request):
    """验证用户认证"""
    try:
        data = await request.json()
        username = data.get("username", "")
        api_key = data.get("api_key", "")

        if not username or not api_key:
            return web.json_response({"success": False, "error": "缺少用户名或API Key"})

        is_valid, is_network_error = await _run_http_request(
            verify_danbooru_auth,
            username,
            api_key,
        )
        return web.json_response({"success": True, "valid": is_valid, "network_error": is_network_error})
    except Exception as e:
        logger.error(f"验证认证接口错误: {e}")
        return web.json_response({"success": False, "error": "网络错误", "network_error": True}, status=500)

# 图片代理并发上限：浏览器一次打开一页会 lazy-load 多张缩略图，没有上限会导致
# 后端同时发出十几个 CDN 请求，配合全局限流会形成长队列；限到 3 并发即可让缩略图
# 平滑流入，又避免瞬时流量把 CF 的 rate rule 触发。
_image_proxy_semaphore = None

def _get_image_proxy_semaphore():
    global _image_proxy_semaphore
    if _image_proxy_semaphore is None:
        _image_proxy_semaphore = asyncio.Semaphore(2)
    return _image_proxy_semaphore

def _gelbooru_browser_headers(accept="text/html,*/*"):
    return _gelbooru_client.browser_headers(accept)

def _gelbooru_public_page_requires_options(html_text):
    """Detect Gelbooru's anonymous page that says results are hidden by site options."""
    if not html_text:
        return False
    return (
        "Check your blacklist" in html_text
        and "Nobody here but us" in html_text
    )

def _image_proxy_headers_for_host(host):
    """Return browser-like headers for image CDNs that are sensitive to generic requests."""
    if host == "gelbooru.com" or host.endswith(".gelbooru.com"):
        return _gelbooru_browser_headers("image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8")
    return {}

def _fetch_supported_media(url):
    """Fetch image/video bytes using the same site-aware request path as the preview proxy."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("invalid media url scheme")

    host = (parsed.hostname or "").lower()
    allowed_suffixes = ("donmai.us", "gelbooru.com")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_suffixes):
        raise ValueError(f"unsupported media host: {host}")

    if host == "gelbooru.com" or host.endswith(".gelbooru.com"):
        display_all_site_content = load_ui_settings().get("gelbooru_display_all_site_content", False)
        response = _gelbooru_request(
            "GET",
            url,
            request_kind="image",
            display_all=display_all_site_content,
            headers=_image_proxy_headers_for_host(host),
            timeout=(8, 30),
        )
    else:
        response = _danbooru_request(
            "GET",
            url,
            headers=_image_proxy_headers_for_host(host),
            timeout=30,
        )

    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "application/octet-stream").lower()
    if content_type and not content_type.startswith(("image/", "application/octet-stream")):
        raise ValueError(f"upstream returned non-image content: {content_type}")
    return response.content

def _fetch_gelbooru_public_posts(
    adapter,
    tags,
    limit,
    page,
    rating_query,
    display_all_site_content=False,
    hydrate_details=False,
):
    """Fetch Gelbooru posts from public HTML pages when DAPI credentials are unavailable.
    Uses fixed page size GELBOORU_PUBLIC_PAGE_SIZE (42) so pid is decoupled from
    the frontend's limit.  Frontend chains multiple fetches via maybeLoadMore.
    On 429/503 retries once with Retry-After.  Returns error placeholders on failure.
    """
    id_match = re.search(r"(?:^|\s)id:(\d+)(?:\s|$)", tags or "")
    if id_match:
        refs = [{"id": id_match.group(1)}]
    else:
        def _get_list_page(_params, _pid, force_refresh=False):
            """Fetch a list page through the isolated Gelbooru transport."""
            try:
                response = _gelbooru_request(
                    "GET",
                    adapter.posts_url,
                    request_kind="public",
                    display_all=display_all_site_content,
                    force_refresh=force_refresh,
                    params=_params,
                    timeout=(8, 15),
                )
                if response.status_code in (429, 503):
                    _log_gelbooru_retry_warning(f"重试仍限流({response.status_code})，跳过本页", pid_value=_pid)
                    return None
                return response
            except requests.exceptions.RequestException as exc:
                _log_gelbooru_retry_warning(f"网络异常: {exc}", pid_value=_pid)
                return None

        # 一次请求多页：limit > 42 时依次抓取多页再合并，顺序请求不跨序
        pages_to_fetch = max(1, -(-limit // GELBOORU_PUBLIC_PAGE_SIZE))
        all_refs = []
        for pi in range(pages_to_fetch):
            cur_page = page + pi
            pid_value = max(cur_page - 1, 0) * GELBOORU_PUBLIC_PAGE_SIZE
            params = adapter.build_public_posts_params(tags, limit, cur_page, rating_query)
            params["pid"] = pid_value

            list_response = _get_list_page(params, pid_value)
            if list_response is None:
                payload = build_gallery_error_payload(
                    adapter.key,
                    "公开页抓取",
                    message="Gelbooru 公开页抓取失败：已达到最大尝试次数，可能是限流、站点拦截或当前 IP 质量不佳。",
                    retries_exhausted=True,
                    retry_count=2,
                )
                all_refs.append({**payload, "pid": pid_value, "page": cur_page})
                continue

            if display_all_site_content and _gelbooru_public_page_requires_options(list_response.text):
                logger.warning("[GelbooruPublic] Display all site content needed; rebuilding session")
                list_response = _get_list_page(params, pid_value, force_refresh=True)
                if list_response is None:
                    payload = build_gallery_error_payload(
                        adapter.key,
                        "公开页抓取",
                        message="Gelbooru 公开页抓取失败：已达到最大尝试次数，可能是限流、站点拦截或当前 IP 质量不佳。",
                        retries_exhausted=True,
                        retry_count=2,
                    )
                    all_refs.append({**payload, "pid": pid_value, "page": cur_page})
                    continue

            remaining = limit - len(all_refs)
            refs = adapter.extract_public_post_refs(list_response.text, remaining)
            all_refs.extend(refs)
            logger.info(f"[GelbooruPublic] page={cur_page} pid={pid_value} refs={len(refs)} 累计={len(all_refs)}")

            if len(refs) < GELBOORU_PUBLIC_PAGE_SIZE:
                break

        refs = all_refs
        logger.info(f"[GelbooruPublic] tags='{tags}' total_refs={len(refs)}")

    # Optional compatibility path for callers that explicitly request eager
    # categorized details. Normal gallery loads use local classification and
    # bounded viewport prefetch instead.
    # The list HTML already carries the complete, uncategorized tag string in
    # each thumbnail title. Avoid blocking the first render on up to 42
    # rate-limited detail requests; categorized details are hydrated on demand.
    if hydrate_details and not id_match and refs:
        def _hydrate_one(ref):
            """Fetch and hydrate a single post's detail page."""
            try:
                resp = _gelbooru_request(
                    "GET",
                    adapter.posts_url,
                    request_kind="hydrate",
                    display_all=display_all_site_content,
                    params=adapter.build_public_post_params(ref["id"]),
                    timeout=(8, 15),
                )
                resp.raise_for_status()
                hydrated = adapter.normalize_public_post_page(ref["id"], resp.text, ref)
                for key in ("tag_string", "tag_string_artist", "tag_string_copyright", "tag_string_character", "tag_string_general", "tag_string_meta", "image_width", "image_height", "rating"):
                    ref[key] = hydrated.get(key, ref.get(key, ""))
                ref["preview_file_url"] = hydrated.get("preview_file_url") or ref.get("preview_file_url", "")
                if hydrated.get("file_url"):
                    ref["file_url"] = hydrated["file_url"]
                    ref["_gelbooru_preview_only"] = False
            except Exception:
                pass
            return ref

        with ThreadPoolExecutor(max_workers=2) as executor:
            for i in range(0, len(refs), 2):
                pair = refs[i:i+2]
                futures = [executor.submit(_hydrate_one, ref) for ref in pair]
                for f in futures:
                    f.result()  # 按序等待两张完成

    if not id_match:
        return refs

    # id_match 模式：逐帖 hydrate
    posts = []
    for ref in refs:
        post_id = ref.get("id")
        if not post_id:
            continue

        try:
            post_response = _gelbooru_request(
                "GET",
                adapter.posts_url,
                request_kind="hydrate",
                display_all=display_all_site_content,
                params=adapter.build_public_post_params(post_id),
                timeout=(8, 15),
            )
            post_response.raise_for_status()
            post = adapter.normalize_public_post_page(post_id, post_response.text, ref)
            has_original = bool(post.get("file_url"))
            exact_tags = bool(post.get("_tag_categories_exact"))
            preview_only = bool(post.get("_gelbooru_preview_only"))
            tag_count = len(str(post.get("tag_string") or "").split())
            if not has_original or not exact_tags or preview_only:
                logger.warning(
                    "[GelbooruPublic] 详情不满足入队要求 id=%s status=%s "
                    "content_type=%s bytes=%s missing_original=%s "
                    "missing_exact_tags=%s preview_only=%s tag_count=%s",
                    post_id,
                    post_response.status_code,
                    post_response.headers.get("Content-Type", "unknown"),
                    len(post_response.content),
                    not has_original,
                    not exact_tags,
                    preview_only,
                    tag_count,
                )
            if post.get("preview_file_url") or post.get("file_url"):
                posts.append(post)
        except requests.exceptions.RequestException as e:
            status_code = e.response.status_code if e.response is not None else "none"
            logger.warning(
                "[GelbooruPublic] 详情请求失败 id=%s display_all=%s status=%s "
                "error=%s: %s",
                post_id,
                bool(display_all_site_content),
                status_code,
                type(e).__name__,
                e,
            )
        except Exception:
            logger.exception("[GelbooruPublic] 详情解析异常 id=%s", post_id)
            raise

    return posts


def _log_gelbooru_retry_warning(message, pid_value=None):
    """Short helper for logging gelbooru public fetch retry messages."""
    if pid_value is not None:
        logger.warning(f"[GelbooruPublic] {message} (pid={pid_value})")
    else:
        logger.warning(f"[GelbooruPublic] {message}")

@PromptServer.instance.routes.get("/danbooru_gallery/image_proxy")
async def image_proxy(request):
    # 浏览器直连 cdn.donmai.us 会被 Cloudflare 按 cross-site <img> 请求挑战并返回 403，
    # 而 Danbooru 客户端使用描述性 UA 能过 CF。转发一次即可让前端拿到缩略图。
    url = request.query.get("url", "")
    if not url:
        return web.Response(status=400, text="missing url")

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return web.Response(status=400, text="invalid url")

    if parsed.scheme not in ("http", "https"):
        return web.Response(status=400, text="invalid scheme")

    # SSRF 防护：只允许当前支持图站的图片域名
    host = (parsed.hostname or "").lower()
    allowed_suffixes = ("donmai.us", "gelbooru.com")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_suffixes):
        return web.Response(status=403, text="host not allowed")

    if host == "donmai.us" or host.endswith(".donmai.us"):
        scope, store = offline_gallery.scope(request)
        if await offline_gallery.is_local(scope, store):
            return web.Response(status=409, text="离线模式未缓存原图，请手动重试联网")

    thumbnail = request.query.get("thumbnail") == "1"
    if thumbnail:
        cached = await asyncio.to_thread(get_thumbnail, url)
        if cached is not None:
            return web.Response(body=cached, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    async with _get_image_proxy_semaphore():
        try:
            if host == "gelbooru.com" or host.endswith(".gelbooru.com"):
                display_all_site_content = load_ui_settings().get("gelbooru_display_all_site_content", False)
                resp = await asyncio.to_thread(
                    _gelbooru_request,
                    "GET",
                    url,
                    request_kind="image",
                    display_all=display_all_site_content,
                    headers=_image_proxy_headers_for_host(host),
                    timeout=(8, 15),
                )
            else:
                resp = await asyncio.to_thread(
                    _danbooru_request,
                    "GET",
                    url,
                    headers=_image_proxy_headers_for_host(host),
                    timeout=15,
                )
        except requests.exceptions.RequestException as e:
            logger.warning(f"[ImageProxy] 上游请求失败 {url}: {e}")
            return web.Response(status=502, text="upstream error")

    if resp.status_code != 200:
        logger.debug(f"[ImageProxy] 上游返回 {resp.status_code}: {url}")
        return web.Response(status=resp.status_code)

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    if not content_type.lower().startswith(("image/", "video/")):
        logger.warning(f"[ImageProxy] 上游返回非媒体内容 {content_type}: {url}")
        return web.Response(status=502, text="upstream returned non-media content")

    body = resp.content
    if thumbnail:
        try:
            body = await asyncio.to_thread(save_thumbnail, url, body)
            content_type = "image/jpeg"
        except (OSError, ValueError) as error:
            logger.warning(f"[Thumbnail] Decode failed: {error}")
            return web.Response(status=502, text="thumbnail unavailable")

    return web.Response(
        body=body,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        },
    )

# --- 保留文件中剩余的其他部分 ---
offline_gallery = OfflineGallery(PromptServer.instance, load_user_auth, _danbooru_request,
                                 _image_proxy_headers_for_host, translation_system)


async def _favorite_cached_thumbnail(request, source, post):
    url = post.get("preview_file_url") or ""
    if not url:
        return None
    data = await asyncio.to_thread(get_thumbnail, url)
    if data is None and post.get("md5"):
        data = await asyncio.to_thread(get_thumbnail, f"{url}?v={post['md5']}")
    if data is None and source == "danbooru":
        _, store = offline_gallery.scope(request)
        data = await asyncio.to_thread(store.thumbnail, url)
    return data


async def _complete_local_favorite(request, source, post):
    thumb = await asyncio.to_thread(local_favorites.store.thumbnail, source, post['id'])
    if thumb is None:
        thumb = await _favorite_cached_thumbnail(request, source, post)
    if source == "danbooru":
        scope, store = offline_gallery.scope(request)
        if await offline_gallery.is_local(scope, store):
            return post, thumb
    if not has_details(post):
        try:
            if source == "danbooru":
                response = await asyncio.to_thread(_danbooru_request, "GET",
                    f"{BASE_URL}/posts/{post['id']}.json", timeout=(8, 15))
                response.raise_for_status()
                details = response.json()
            else:
                raw, = await asyncio.to_thread(DanbooruGalleryNode.get_posts_internal,
                    tags=f"id:{post['id']}", limit=1, source=source, force_public_detail=True)
                details = next((item for item in json.loads(raw) if str(item.get('id')) == str(post['id']) and not item.get('error')), {})
            if isinstance(details, dict) and str(details.get('id')) == str(post['id']):
                post = {**post, **details}
        except (requests.RequestException, ValueError, OSError) as error:
            logger.debug("Favorite details unavailable: %s", type(error).__name__)
    if thumb is None and post.get('preview_file_url'):
        try:
            data = await asyncio.to_thread(_fetch_supported_media, post['preview_file_url'])
            thumb = await asyncio.to_thread(save_thumbnail, post['preview_file_url'], data)
        except (requests.RequestException, ValueError, OSError) as error:
            logger.debug("Favorite thumbnail unavailable: %s", type(error).__name__)
    return post, thumb


local_favorites = LocalFavorites(PromptServer.instance.routes, LocalFavoritesStore(),
    _favorite_cached_thumbnail, _complete_local_favorite, translation_system.get_english)


@PromptServer.instance.routes.get("/danbooru_gallery/posts")
async def get_posts_for_front(request):
    query = request.query
    tags = query.get("search[tags]", "")
    page = query.get("page", "1")
    limit = query.get("limit", "100")
    rating = query.get("search[rating]", "")
    source = query.get("source", "danbooru")
    gelbooru_display_all_site_content = query.get("gelbooru_display_all_site_content", "").lower() in ("1", "true", "yes", "on")
    force_public_detail = query.get("force_public_detail", "").lower() in ("1", "true", "yes", "on")
    before_id_raw = query.get("before_id", "")
    gelbooru_dedup_mode = query.get("gelbooru_dedup_mode", "off").strip().lower()
    if gelbooru_dedup_mode not in ("off", "on", "on_auth"):
        gelbooru_dedup_mode = "off"

    if source == "danbooru":
        async def load_online():
            adapter = get_site_adapter("danbooru")
            ratings = [r.strip() for r in rating.split(",") if r.strip() and r != "all"]
            rating_query = " ".join(("~" if len(ratings) > 1 else "") + "rating:" + r for r in ratings)
            params = adapter.build_posts_params(tags, max(1, min(200, int(limit))), int(page), rating_query)
            if before_id_raw:
                params["page"] = f"b{before_id_raw}"
            username, api_key = load_user_auth()
            auth = HTTPBasicAuth(username, api_key) if username and api_key else None
            response = await asyncio.to_thread(_danbooru_request, "GET", adapter.posts_url,
                                               params=params, auth=auth, timeout=(8, 15))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Danbooru returned a non-list response")
            return [{**post, "source_site": "danbooru"} for post in payload]
        return await offline_gallery.posts(request, load_online)

    # Network and HTML parsing are synchronous. Keep them off aiohttp's event
    # loop so settings/favorites calls and a newly selected source stay
    # responsive while an older upstream request is still finishing.
    loop = asyncio.get_running_loop()
    posts_json_str, = await loop.run_in_executor(
        None,
        partial(
            DanbooruGalleryNode.get_posts_internal,
            tags=tags,
            limit=int(limit),
            page=int(page),
            rating=rating,
            source=source,
            gelbooru_display_all_site_content=gelbooru_display_all_site_content,
            force_public_detail=force_public_detail,
            before_id=before_id_raw,
            gelbooru_dedup_mode=gelbooru_dedup_mode,
        ),
    )
    
    try:
        posts_list = json.loads(posts_json_str)
    except json.JSONDecodeError:
        posts_list = []

    return web.json_response(posts_list, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

@PromptServer.instance.routes.get("/danbooru_gallery/autocomplete")
async def get_autocomplete(request):
    """三层查询机制：数据库 → API → 空结果"""
    try:
        query = request.query.get("query", "")
        limit = int(request.query.get("limit", "20"))
        source = request.query.get("source", "danbooru")
        adapter = get_site_adapter(source)

        if not query:
            return web.json_response([])

        if adapter.key == "danbooru" and translation_system.load_all():
            threshold = load_ui_settings()["autocomplete_min_post_count"]
            results = await asyncio.to_thread(translation_system.search_english, query, limit, threshold)
            return web.json_response(results)

        if adapter.key == "danbooru":
            scope, store = offline_gallery.scope(request)
            if await offline_gallery.is_local(scope, store):
                return web.json_response([])

        # 加载配置
        config = load_autocomplete_config()

        # ✅ 第1层：查询本地SQLite数据库
        if adapter.key == "danbooru" and get_db_manager and config['cache'].get('use_database_query', True):
            try:
                db = get_db_manager()
                db_results = await db.search_tags_by_prefix(query, limit)

                if db_results:
                    translations = translation_system.translate_tags_batch(tag['tag'] for tag in db_results)
                    # 数据库有结果，转换格式并返回
                    formatted_results = [
                        {
                            'name': tag['tag'],
                            'category': tag['category'],
                            'post_count': tag['post_count'],
                            'translation': translations.get(tag['tag']),
                            'aliases': tag.get('aliases', [])
                        }
                        for tag in db_results
                    ]
                    logger.debug(f"[Autocomplete] 数据库查询成功: '{query}' -> {len(formatted_results)}条结果")
                    return web.json_response(formatted_results)
                else:
                    logger.debug(f"[Autocomplete] 数据库无结果: '{query}'")
            except Exception as e:
                logger.warning(f"[Autocomplete] 数据库查询失败: {e}，尝试API fallback")

        # ✅ 第2层：Fallback到Danbooru API
        if config['offline_mode'].get('fallback_to_remote', True):
            try:
                timeout = config['offline_mode'].get('remote_timeout_ms', 2000) / 1000.0

                tags_url = adapter.tags_url
                params = adapter.build_autocomplete_params(query, limit)
                credentials = get_site_credentials(adapter)
                if not has_required_site_credentials(adapter, credentials):
                    if adapter.key == "gelbooru":
                        logger.info("[Gelbooru] 未配置 User ID/API Key，改用公开 autocomplete2")
                        params = adapter.build_public_autocomplete_params(query, limit)
                        response = await _run_http_request(
                            _gelbooru_request,
                            "GET",
                            tags_url,
                            request_kind="api",
                            params=params,
                            timeout=timeout,
                        )
                        response.raise_for_status()
                        return web.json_response(adapter.normalize_public_autocomplete_response(response.json()))
                    logger.warning(f"[{adapter.key}] 缺少必要认证信息，已跳过自动补全请求")
                    return web.json_response([])
                params = adapter.apply_auth_params(params, credentials)

                username, api_key = load_user_auth()
                auth = HTTPBasicAuth(username, api_key) if adapter.requires_auth and username and api_key else None

                logger.debug(f"[Autocomplete] 调用远程API({adapter.key}): '{query}' (超时: {timeout}s)")
                if adapter.key == "gelbooru":
                    response = await _run_http_request(
                        _gelbooru_request,
                        "GET",
                        tags_url,
                        request_kind="api",
                        params=params,
                        timeout=timeout,
                    )
                else:
                    response = await _run_http_request(
                        _danbooru_request,
                        "GET",
                        tags_url,
                        params=params,
                        auth=auth,
                        timeout=timeout,
                    )
                response.raise_for_status()

                result = adapter.normalize_autocomplete_response(response.json())

                # 排序确保按热度排列
                if isinstance(result, list):
                    result.sort(key=lambda x: x.get('post_count', 0), reverse=True)
                    logger.info(f"[Autocomplete] API查询成功({adapter.key}): '{query}' -> {len(result)}条结果")

                return web.json_response(result)

            except requests.Timeout:
                logger.warning(f"[Autocomplete] 远程API超时 (>{timeout}s): '{query}'")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[Autocomplete] 远程API失败: {e}")
            except Exception as e:
                logger.error(f"[Autocomplete] API调用错误: {e}")

        # ✅ 第3层：返回空结果
        logger.debug(f"[Autocomplete] 所有查询方式均无结果: '{query}'")
        return web.json_response([])

    except Exception as e:
        logger.error(f"[Autocomplete] 处理请求时发生错误: {e}")
        return web.json_response([])

@PromptServer.instance.routes.get("/danbooru_gallery/blacklist")
async def get_blacklist(request):
    blacklist = load_blacklist()
    return web.json_response({"blacklist": blacklist})

@PromptServer.instance.routes.post("/danbooru_gallery/blacklist")
async def save_blacklist_route(request):
    try:
        data = await request.json()
        blacklist_items = data.get("blacklist", [])
        success = save_blacklist(blacklist_items)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存黑名单接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/language")
async def get_language(request):
    language = load_language()
    return web.json_response({"language": language})

@PromptServer.instance.routes.post("/danbooru_gallery/language")
async def save_language_route(request):
    try:
        data = await request.json()
        language = data.get("language", "zh")
        success = save_language(language)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存语言设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/filter_tags")
async def get_filter_tags(request):
    filter_tags, filter_enabled = load_filter_tags()
    return web.json_response({"filter_tags": filter_tags, "filter_enabled": filter_enabled})

@PromptServer.instance.routes.post("/danbooru_gallery/filter_tags")
async def save_filter_tags_route(request):
    try:
        data = await request.json()
        filter_tags = data.get("filter_tags", [])
        filter_enabled = data.get("filter_enabled", False)
        success = save_filter_tags(filter_tags, filter_enabled)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存提示词过滤设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/ui_settings")
async def get_ui_settings(request):
    try:
        ui_settings = load_ui_settings()
        return web.json_response({
            "success": True,
            "settings": ui_settings
        })
    except Exception as e:
        logger.error(f"[UI_SETTINGS] 获取UI设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/ui_settings")
async def save_ui_settings_route(request):
    try:
        data = await request.json()
        ui_settings = {
            "autocomplete_enabled": data.get("autocomplete_enabled", True),
            "tooltip_enabled": data.get("tooltip_enabled", True),
            "autocomplete_max_results": data.get("autocomplete_max_results", 20),
            "autocomplete_min_post_count": data.get("autocomplete_min_post_count", load_ui_settings()["autocomplete_min_post_count"]),
            "selected_categories": data.get("selected_categories", ["copyright", "character", "general"]),
            "multi_select_enabled": data.get("multi_select_enabled", False),
            "source_site": data.get("source_site", "danbooru"),
            "gelbooru_display_all_site_content": data.get("gelbooru_display_all_site_content", False)
        }
        success = save_ui_settings(ui_settings)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存UI设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/selection_queue_push")
async def selection_queue_push(request):
    """Deprecated compatibility endpoint; selection state now travels in the prompt."""
    try:
        if request.can_read_body:
            await request.json()
        return web.json_response({"success": True, "deprecated": True})
    except Exception as e:
        logger.error(f"[SelectionQueue] push error: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/selection_queue_pop")
async def selection_queue_pop(request):
    """Deprecated compatibility endpoint; no server-side queue is retained."""
    try:
        if request.can_read_body:
            await request.json()
        return web.json_response({"success": True, "item": None, "deprecated": True})
    except Exception as e:
        logger.error(f"[SelectionQueue] pop error: {e}")
        return web.json_response({"success": False, "error": str(e)})

# ================================
# JS 日志转发：前端 logger_client 通过 POST /danbooru/logs/batch
# 将浏览器日志发到后端，用 Python logger 打印出来，这样用户在
# ComfyUI 控制台（终端）也能看到画廊的日志。
# ================================

@PromptServer.instance.routes.post("/danbooru/logs/batch")
async def js_log_batch(request):
    """接收前端 JS 日志，用 Python logger 转发到后端终端"""
    try:
        data = await request.json()
        logs = data.get("logs", [])
        for log_entry in logs:
            level = (log_entry.get("level") or "INFO").upper()
            component = log_entry.get("component", "danbooru_gallery")
            message = log_entry.get("message", "")
            # 映射级别
            if level in ("ERROR", "CRITICAL"):
                logger.error(f"[JS/{component}] {message}")
            elif level in ("WARN", "WARNING"):
                logger.warning(f"[JS/{component}] {message}")
            elif level == "DEBUG":
                logger.debug(f"[JS/{component}] {message}")
            else:
                logger.info(f"[JS/{component}] {message}")
    except Exception as e:
        logger.warning(f"[JSEndpoint] 日志转发错误: {e}")
    return web.json_response({"success": True})

# ================================
# Tag翻译API接口
# ================================

@PromptServer.instance.routes.get("/danbooru_gallery/translate_tag")
async def translate_tag_route(request):
    """翻译单个tag"""
    try:
        tag = request.query.get("tag", "").strip()
        if not tag:
            return web.json_response({"success": False, "error": "缺少tag参数"})
        
        translation = translation_system.translate_tag(tag)
        return web.json_response({
            "success": True,
            "tag": tag,
            "translation": translation
        })
    except Exception as e:
        logger.error(f"翻译tag接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/translate_tags_batch")
async def translate_tags_batch_route(request):
    """批量翻译tags"""
    try:
        data = await request.json()
        tags = data.get("tags", [])
        
        if not isinstance(tags, list):
            return web.json_response({"success": False, "error": "tags必须是数组"})
        
        translations = translation_system.translate_tags_batch(tags)
        return web.json_response({
            "success": True,
            "translations": translations
        })
    except Exception as e:
        logger.error(f"批量翻译tags接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/search_chinese")
async def search_chinese_route(request):
    """中文搜索匹配 - 优先使用FTS5数据库搜索"""
    try:
        query = request.query.get("query", "").strip()
        limit = int(request.query.get("limit", "10"))
        source = request.query.get("source", "danbooru")
        adapter = get_site_adapter(source)

        if not query:
            return web.json_response({"success": True, "results": []})

        try:
            results = await asyncio.to_thread(translation_system.search_chinese_tags, query, limit,
                                              load_ui_settings()["autocomplete_min_post_count"])
            logger.debug(f"[SearchChinese] translation_system查询: '{query}' -> {len(results)}条结果")
            return web.json_response({
                "success": True,
                "query": query,
                "results": results
            })
        except Exception as e:
            logger.error(f"[SearchChinese] translation_system查询失败: {e}")
            return web.json_response({
                "success": False,
                "error": str(e)
            })

    except Exception as e:
        logger.error(f"中文搜索接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/autocomplete_with_translation")
async def get_autocomplete_with_translation(request):
    """带翻译的自动补全API - 三层查询机制：数据库 → API → 空结果"""
    try:
        query = request.query.get("query", "")
        limit = int(request.query.get("limit", "20"))
        source = request.query.get("source", "danbooru")
        adapter = get_site_adapter(source)

        if not query:
            return web.json_response([])

        if adapter.key == "danbooru" and translation_system.load_all():
            threshold = load_ui_settings()["autocomplete_min_post_count"]
            results = await asyncio.to_thread(translation_system.search_english, query, limit, threshold)
            return web.json_response(results)

        if adapter.key == "danbooru":
            scope, store = offline_gallery.scope(request)
            if await offline_gallery.is_local(scope, store):
                return web.json_response([])

        # 加载配置
        config = load_autocomplete_config()

        # ✅ 第1层：查询本地SQLite数据库（已包含翻译）
        if adapter.key == "danbooru" and get_db_manager and config['cache'].get('use_database_query', True):
            try:
                db = get_db_manager()
                db_results = await db.search_tags_by_prefix(query, limit)

                if db_results:
                    translations = translation_system.translate_tags_batch(tag['tag'] for tag in db_results)
                    # 数据库有结果，转换格式（已包含translation_cn）
                    formatted_results = [
                        {
                            'name': tag['tag'],
                            'category': tag['category'],
                            'post_count': tag['post_count'],
                            'translation': translations.get(tag['tag']),
                            'aliases': tag.get('aliases', [])
                        }
                        for tag in db_results
                    ]
                    logger.debug(f"[AutocompleteTranslation] 数据库查询成功: '{query}' -> {len(formatted_results)}条结果")
                    return web.json_response(formatted_results)
                else:
                    logger.debug(f"[AutocompleteTranslation] 数据库无结果: '{query}'")
            except Exception as e:
                logger.warning(f"[AutocompleteTranslation] 数据库查询失败: {e}，尝试API fallback")

        # ✅ 第2层：Fallback到Danbooru API（需要手动添加翻译）
        if config['offline_mode'].get('fallback_to_remote', True):
            try:
                timeout = config['offline_mode'].get('remote_timeout_ms', 2000) / 1000.0

                tags_url = adapter.tags_url
                params = adapter.build_autocomplete_params(query, limit)
                credentials = get_site_credentials(adapter)
                if not has_required_site_credentials(adapter, credentials):
                    if adapter.key == "gelbooru":
                        logger.info("[Gelbooru] 未配置 User ID/API Key，改用公开 autocomplete2")
                        params = adapter.build_public_autocomplete_params(query, limit)
                        response = await _run_http_request(
                            _gelbooru_request,
                            "GET",
                            tags_url,
                            request_kind="api",
                            params=params,
                            timeout=timeout,
                        )
                        response.raise_for_status()
                        result = adapter.normalize_public_autocomplete_response(response.json())
                        for tag_data in result:
                            tag_name = tag_data.get('name', '')
                            tag_data['translation'] = translation_system.translate_tag(tag_name)
                        return web.json_response(result)
                    logger.warning(f"[{adapter.key}] 缺少必要认证信息，已跳过带翻译自动补全请求")
                    return web.json_response([])
                params = adapter.apply_auth_params(params, credentials)

                username, api_key = load_user_auth()
                auth = HTTPBasicAuth(username, api_key) if adapter.requires_auth and username and api_key else None

                logger.debug(f"[AutocompleteTranslation] 调用远程API({adapter.key}): '{query}' (超时: {timeout}s)")
                if adapter.key == "gelbooru":
                    response = await _run_http_request(
                        _gelbooru_request,
                        "GET",
                        tags_url,
                        request_kind="api",
                        params=params,
                        timeout=timeout,
                    )
                else:
                    response = await _run_http_request(
                        _danbooru_request,
                        "GET",
                        tags_url,
                        params=params,
                        auth=auth,
                        timeout=timeout,
                    )
                response.raise_for_status()

                result = adapter.normalize_autocomplete_response(response.json())

                # 为每个tag添加翻译
                if isinstance(result, list):
                    for tag_data in result:
                        tag_name = tag_data.get('name', '')
                        translation = translation_system.translate_tag(tag_name)
                        tag_data['translation'] = translation

                    logger.info(f"[AutocompleteTranslation] API查询成功: '{query}' -> {len(result)}条结果")

                return web.json_response(result)

            except requests.Timeout:
                logger.warning(f"[AutocompleteTranslation] 远程API超时 (>{timeout}s): '{query}'")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[AutocompleteTranslation] 远程API失败: {e}")
            except Exception as e:
                logger.error(f"[AutocompleteTranslation] API调用错误: {e}")

        # ✅ 第3层：返回空结果
        logger.debug(f"[AutocompleteTranslation] 所有查询方式均无结果: '{query}'")
        return web.json_response([])

    except Exception as e:
        logger.error(f"[AutocompleteTranslation] 处理请求时发生错误: {e}")
        return web.json_response([])

class DanbooruGalleryNode:
    _post_cache = {}
    _post_cache_lock = threading.Lock()

    @classmethod
    def _get_cached_posts(cls, cache_key, max_age):
        with cls._post_cache_lock:
            cached = cls._post_cache.get(cache_key)
            if cached is None:
                return None
            cached_data, timestamp = cached
            if time.time() - timestamp < max_age:
                return cached_data
            cls._post_cache.pop(cache_key, None)
            return None

    @classmethod
    def _cache_posts(cls, cache_key, result_text, max_entries=200):
        with cls._post_cache_lock:
            cls._post_cache[cache_key] = (result_text, time.time())
            if len(cls._post_cache) > max_entries:
                oldest_key = min(cls._post_cache, key=lambda key: cls._post_cache[key][1])
                del cls._post_cache[oldest_key]

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
            "optional": {
                # 兼容前端 bypass 解析：
                # 该节点原本只有 hidden 输入，某些前端 bypass 路径会在无可见输入时抛出
                # "No input found for flattened id ... slot [0]"。
                # 增加可选透传槽位后，bypass 时不会因缺少输入槽而直接报错。
                "bypass_image": ("IMAGE", {"forceInput": True}),
                "bypass_prompts": ("STRING", {"forceInput": True}),
            },
            "hidden": {
                "selection_data": ("STRING", {"default": "{}", "multiline": True, "forceInput": True}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "prompts")
    OUTPUT_IS_LIST = (True, True)
    FUNCTION = "get_selected_data"
    CATEGORY = "danbooru"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, selection_data="{}", **kwargs):
        return selection_data or ""

    def get_selected_data(self, selection_data="{}", **kwargs):
        offline_gallery.check_image_output(selection_data)
        images = []
        prompts = []

        try:
            if not selection_data or selection_data == "{}":
                return ([torch.zeros(1, 1, 1, 3)], [""])
            data = json.loads(selection_data)
            selections = data.get("selections", [])

            if not selections:
                return ([torch.zeros(1, 1, 1, 3)], [""])

            for sel in selections:
                prompt = sel.get("prompt", "")
                image_url = sel.get("image_url")
                prompts.append(prompt)
                if image_url:
                    try:
                        img_data = _fetch_supported_media(image_url)
                        img = Image.open(io.BytesIO(img_data)).convert("RGB")
                        img_array = np.array(img).astype(np.float32) / 255.0
                        tensor = torch.from_numpy(img_array)[None,]
                        images.append(tensor)
                    except Exception as e:
                        logger.error(f"图片加载失败 {image_url}: {e}")
                        images.append(torch.zeros(1, 1, 1, 3))
                else:
                    images.append(torch.zeros(1, 1, 1, 3))

            if not images:
                return ([torch.zeros(1, 1, 1, 3)], [""])

        except Exception as e:
            logger.error(f"选择处理错误: {e}")
            return ([torch.zeros(1, 1, 1, 3)], [""])

        return (images, prompts)
    
    @staticmethod
    def get_posts_internal(tags: str, limit: int = 100, page: int = 1, rating: str = None, source: str = "danbooru", gelbooru_display_all_site_content: bool = None, force_public_detail: bool = False, before_id=None, gelbooru_dedup_mode: str = "off"):
        settings = load_settings()
        cache_enabled = settings.get("cache_enabled", True)
        max_cache_age = settings.get("max_cache_age", 3600)
        persistent_cache_age = settings.get("persistent_post_cache_age", 2592000)
        adapter = get_site_adapter(source)
        if gelbooru_display_all_site_content is None:
            gelbooru_display_all_site_content = settings.get("gelbooru_display_all_site_content", False)

        if gelbooru_dedup_mode not in ("off", "on", "on_auth"):
            gelbooru_dedup_mode = "off"

        # before_id 分页：数字校验，防注入
        before_id = str(before_id).strip() if before_id else ""
        if before_id and not before_id.isdigit():
            before_id = ""
        if before_id and re.search(r'(?:^|\s)(?:order|ordfav|sort):', tags or ''):
            before_id = ""
        if before_id and not (
            adapter.key == "danbooru"
            or (adapter.key == "gelbooru" and gelbooru_dedup_mode in ("on", "on_auth"))
        ):
            before_id = ""

        credentials = get_site_credentials(adapter)
        has_gelbooru_creds = has_required_site_credentials(adapter, credentials)
        persistent_cache = get_gallery_post_cache() if adapter.key == "gelbooru" and cache_enabled else None
        persistent_source = (
            "gelbooru:display_all" if gelbooru_display_all_site_content else "gelbooru:default"
        )

        def enrich_gelbooru_posts(posts):
            if not persistent_cache or not posts:
                return posts
            try:
                normal_posts = [post for post in posts if isinstance(post, dict) and not post.get("error")]
                # Preserve error slots and ordering while replacing normal posts by ID.
                cached_by_id = {
                    str(post.get("id")): post
                    for post in persistent_cache.merge_cached_posts(
                        persistent_source,
                        normal_posts,
                        persistent_cache_age,
                    )
                }
                enriched = [
                    cached_by_id.get(str(post.get("id")), post)
                    if isinstance(post, dict) and not post.get("error")
                    else post
                    for post in posts
                ]
                return persistent_cache.classify_posts(persistent_source, enriched)
            except Exception as exc:
                logger.warning(f"[GelbooruCache] 本地分类/缓存读取失败，回退原始列表: {exc}")
                return posts

        # 创建缓存键（rating一化排序，避免不同排序命中两条缓存）
        rating_key = ','.join(sorted(r.strip().lower() for r in (rating or '').split(',') if r.strip()))
        site_options_key = ""
        if adapter.key == "gelbooru":
            site_options_key = "display_all" if gelbooru_display_all_site_content else "default"
            site_options_key += f"_dedup_{gelbooru_dedup_mode}"
        response_shape_key = "detail" if force_public_detail else "list"
        cache_key = f"{adapter.key}:{site_options_key}:{response_shape_key}:{tags}:{limit}:{page}:{rating_key}:{before_id}"

        # 判断是否获取收藏列表，如果是清除缓存以避免相同的请求前端列表不更新
        match = re.search(r'\bordfav:([^\s]+)', tags)

        # 如果启用了缓存，则检查缓存
        if cache_enabled and not match:
            cached_data = DanbooruGalleryNode._get_cached_posts(cache_key, max_cache_age)
            if cached_data is not None:
                if adapter.key == "gelbooru" and not force_public_detail:
                    try:
                        cached_posts = json.loads(cached_data)
                        cached_data = json.dumps(enrich_gelbooru_posts(cached_posts), ensure_ascii=False)
                    except (TypeError, json.JSONDecodeError):
                        pass
                return (cached_data,)

        # 分离 date: 标签和其他标签
        date_tag = ''
        other_tags = []
        for tag in tags.split(' '):
            if tag.strip().startswith('date:'):
                date_tag = tag.strip()
            elif tag.strip():
                other_tags.append(tag.strip())

        # 动态 max_tags：Gelbooru cursor 模式下 id:<X 占用一个名额
        if adapter.key == "gelbooru" and gelbooru_dedup_mode in ("on", "on_auth"):
            if gelbooru_dedup_mode == "on_auth" and has_gelbooru_creds:
                max_tags = 10
            else:
                max_tags = 1
        else:
            max_tags = 2
        if len(other_tags) > max_tags:
            other_tags = other_tags[:max_tags]
        
        # 重新组合标签
        final_tags = ' '.join(other_tags)
        if date_tag:
            final_tags = f"{final_tags} {date_tag}".strip()

        rating_query = ""
        if adapter.key == "danbooru" and rating and rating.lower() != 'all':
            allowed = {'general', 'sensitive', 'questionable', 'explicit', 'g', 's', 'q', 'e'}
            rating_values = [r.strip().lower() for r in rating.split(',') if r.strip()]
            rating_values = [r for r in rating_values if r in allowed]
            if len(rating_values) == 1:
                rating_query = f"rating:{rating_values[0]}"
            elif len(rating_values) > 1:
                rating_query = ' '.join(f"~rating:{r}" for r in rating_values)
        elif adapter.key != "danbooru":
            rating_query = rating
        
        tags = final_tags
        # Gelbooru cursor（id:<X）要追加到 tags（cursor 在 tags 之后，不是之前）
        # D站游标 page=b<id> 在下面单独处理，不拼 tags
        if before_id and adapter.key == "gelbooru":
            tags = f"{tags} id:<{before_id}".strip()

        username, api_key = load_user_auth()
        auth = HTTPBasicAuth(username, api_key) if adapter.requires_auth and username and api_key else None
        if adapter.key == "gelbooru" and force_public_detail:
            def is_queue_ready_detail(post):
                return bool(
                    isinstance(post, dict)
                    and post.get("file_url")
                    and post.get("_tag_categories_exact")
                    and not post.get("_gelbooru_preview_only")
                )

            requested_id_match = re.search(r"(?:^|\s)id:(\d+)(?:\s|$)", tags or "")
            if persistent_cache and requested_id_match:
                try:
                    cached_details = persistent_cache.get_posts(
                        persistent_source,
                        [requested_id_match.group(1)],
                        persistent_cache_age,
                    )
                    cached_post = cached_details.get(requested_id_match.group(1))
                    if is_queue_ready_detail(cached_post):
                        result_text = json.dumps([cached_post], ensure_ascii=False)
                        DanbooruGalleryNode._cache_posts(cache_key, result_text)
                        return (result_text,)
                except Exception as exc:
                    logger.warning(f"[GelbooruCache] 持久缓存读取失败，继续请求详情: {exc}")
            posts = _fetch_gelbooru_public_posts(
                adapter,
                tags,
                limit,
                page,
                rating_query,
                gelbooru_display_all_site_content,
                hydrate_details=True,
            )
            if persistent_cache:
                try:
                    for post in posts:
                        if is_queue_ready_detail(post):
                            persistent_cache.put_post(persistent_source, post)
                except Exception as exc:
                    logger.warning(f"[GelbooruCache] 详情持久化失败: {exc}")
            result_text = json.dumps(posts, ensure_ascii=False)
            if cache_enabled and posts and all(is_queue_ready_detail(post) for post in posts):
                DanbooruGalleryNode._cache_posts(cache_key, result_text)
            return (result_text,)

        if not has_required_site_credentials(adapter, credentials):
            if adapter.key == "gelbooru":
                logger.info("[Gelbooru] 未配置 User ID/API Key，改用公开网页解析模式")
                posts = _fetch_gelbooru_public_posts(adapter, tags, limit, page, rating_query, gelbooru_display_all_site_content)
                posts = enrich_gelbooru_posts(posts)
                result_text = json.dumps(posts, ensure_ascii=False)

                if cache_enabled and not (adapter.key == "gelbooru" and gelbooru_display_all_site_content and not posts):
                    # 跳过 error 占位格缓存（否则一次限流会让接下来 TTL 内都看不到新图）
                    has_error_placeholder = posts and any(isinstance(p, dict) and p.get("error") for p in posts)
                    if not has_error_placeholder:
                        DanbooruGalleryNode._cache_posts(cache_key, result_text)

                return (result_text,)
            logger.warning(f"[{adapter.key}] 缺少必要认证信息，已跳过帖子请求")
            return ("[]",)
            
        params = adapter.build_posts_params(tags, limit, page, rating_query)
        params = adapter.apply_auth_params(params, credentials)
        # D站游标 page=b<id>，G站已通过 tags 注入
        if before_id and adapter.key == "danbooru":
            params["page"] = f"b{before_id}"

        try:
            if adapter.key == "gelbooru":
                response = _gelbooru_request("GET", adapter.posts_url, request_kind="api", params=params, timeout=15)
            else:
                response = _danbooru_request("GET", adapter.posts_url, params=params, auth=auth, timeout=15)
            if response.status_code == 401 and adapter.key == "gelbooru":
                logger.warning("[Gelbooru] DAPI 返回 401，改用公开网页解析模式")
                posts = _fetch_gelbooru_public_posts(adapter, tags, limit, page, rating_query, gelbooru_display_all_site_content)
            else:
                response.raise_for_status()
                posts = adapter.normalize_posts_response(response.json())

            if adapter.key == "gelbooru":
                posts = enrich_gelbooru_posts(posts)

            result_text = json.dumps(posts, ensure_ascii=False)

            if cache_enabled and not (adapter.key == "gelbooru" and gelbooru_display_all_site_content and not posts):
                has_error_placeholder = posts and any(isinstance(p, dict) and p.get("error") for p in posts)
                if not has_error_placeholder:
                    DanbooruGalleryNode._cache_posts(cache_key, result_text)

            return (result_text,)
        except requests.Timeout as e:
            logger.error(f"请求超时: {e}")
            payload = build_gallery_error_payload(adapter.key, "加载图片列表", exc=e, retries_exhausted=True, retry_count=1)
            return (json.dumps([payload], ensure_ascii=False),)
        except requests.RequestException as e:
            logger.error(f"请求异常: {e}")
            payload = build_gallery_error_payload(adapter.key, "加载图片列表", exc=e, retries_exhausted=True, retry_count=1)
            return (json.dumps([payload], ensure_ascii=False),)
        except Exception as e:
            logger.error(f"未知错误: {e}")
            payload = build_gallery_error_payload(adapter.key, "加载图片列表", exc=e, retries_exhausted=True, retry_count=1)
            return (json.dumps([payload], ensure_ascii=False),)

# ComfyUI 必须的字典
def get_node_class_mappings():
    return {
        "DanbooruGalleryNode": DanbooruGalleryNode
    }

def get_node_display_name_mappings():
    return {
        "DanbooruGalleryNode": "D站画廊 (Danbooru Gallery)"
    }

NODE_CLASS_MAPPINGS = get_node_class_mappings()
NODE_DISPLAY_NAME_MAPPINGS = get_node_display_name_mappings()

