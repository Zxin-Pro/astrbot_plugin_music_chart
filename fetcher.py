"""数据获取层：Billboard 榜单双数据源（GitHub JSON 主方案 / billboard-charts 库备选方案）

统一输出结构:
    {
        "date": "2026-09-19" | None,
        "source": "json" | "library",
        "items": [
            {"rank": 1, "song": "歌名", "artist": "歌手",
             "last_week": 2 | None, "peak_position": 1, "weeks_on_chart": 12},
            ...
        ]
    }
"""

import asyncio
import datetime
import json
import re
import time
from typing import Optional

import aiohttp

# ---------- billboard-charts 可选依赖（同步库，调用必须经 asyncio.to_thread） ----------
try:
    from billboard_charts import fetch_chart as _lib_fetch_chart

    try:
        from billboard_charts import WeekNotPublished
    except ImportError:
        try:
            from billboard_charts.exceptions import WeekNotPublished
        except ImportError:
            WeekNotPublished = None
    HAS_BILLBOARD_LIB = True
except ImportError:
    _lib_fetch_chart = None
    WeekNotPublished = None
    HAS_BILLBOARD_LIB = False

JSON_BASE = "https://raw.githubusercontent.com/mhollingshead/billboard-hot-100/main"
CACHE_TTL = 300          # 内存缓存 5 分钟
REQUEST_TIMEOUT = 20     # 单请求超时（秒）
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 华语榜系列别名（含中文别名；均走 billboard-charts 库方案）
SLUG_ALIASES = {
    "huayu": "taiwan-songs",       # 华语榜 = 台湾歌曲榜（国语）
    "mandarin": "taiwan-songs",
    "guoyu": "taiwan-songs",
    "tw": "taiwan-songs",
    "华语": "taiwan-songs",
    "国语": "taiwan-songs",
    "台湾": "taiwan-songs",
    "cantonese": "hong-kong-songs",  # 粤语榜 = 香港歌曲榜
    "yueyu": "hong-kong-songs",
    "hk": "hong-kong-songs",
    "粤语": "hong-kong-songs",
    "香港": "hong-kong-songs",
}

# 内地华语系列：网易云音乐官方榜单（Billboard 无内地榜，V Chart 已停更）
# slug -> (网易云歌单 id, 展示名)
NETEASE_CHARTS = {
    "mainland": ("3778678", "华语内地热歌榜"),   # /music mainland 内地热歌
    "nethot": ("3778678", "华语内地热歌榜"),
    "netrise": ("19723756", "华语飙升榜"),       # 飙升榜
    "netnew": ("3779629", "华语新歌榜"),         # 新歌榜
    "内地": ("3778678", "华语内地热歌榜"),
    "热歌": ("3778678", "华语内地热歌榜"),
    "飙升": ("19723756", "华语飙升榜"),
    "新歌": ("3779629", "华语新歌榜"),
}


class ChartFetchError(Exception):
    """榜单获取失败（网络 / 解析 / 不支持的数据源组合 / 空数据）"""


def normalize_date(date: Optional[str]) -> Optional[str]:
    """校验并归一化日期字符串，非法直接抛 ChartFetchError"""
    if not date:
        return None
    date = date.strip()
    if not DATE_RE.match(date):
        raise ChartFetchError(f"日期格式不正确：{date!r}，应为 YYYY-MM-DD")
    try:
        datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ChartFetchError(f"日期不存在：{date}")
    return date


class MusicChartFetcher:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}   # key -> {"ts": float, "data": dict}
        self._lock = asyncio.Lock()

    # ---------- 基础设施 ----------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers={"User-Agent": "astrbot_plugin_music_chart/1.0"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._cache.clear()

    def cache_info(self) -> str:
        now = time.time()
        alive = sum(1 for v in self._cache.values() if now - v["ts"] < CACHE_TTL)
        return f"缓存条目 {len(self._cache)}（有效 {alive}，TTL {CACHE_TTL}s）"

    @staticmethod
    def _cache_key(slug: str, date: Optional[str], source: str) -> tuple:
        return (source, slug, date or "")

    # ---------- 统一数据清洗 ----------

    @staticmethod
    def _to_int(val) -> Optional[int]:
        """库方案的 Last Week 可能是 '-' 或空，JSON 方案可能是 None"""
        if val is None:
            return None
        try:
            s = str(val).strip()
            if not s or s in {"-", "—"}:
                return None
            return int(float(s))
        except (ValueError, TypeError):
            return None

    @classmethod
    def _normalize_items(cls, raw_items, source: str) -> list:
        """统一为 [{rank, song, artist, last_week, peak_position, weeks_on_chart}]"""
        items = []
        for row in raw_items:
            if source == "json":
                rank = cls._to_int(row.get("this_week"))
                song = str(row.get("song") or "").strip()
                artist = str(row.get("artist") or "").strip()
                last_week = cls._to_int(row.get("last_week"))
                peak = cls._to_int(row.get("peak_position"))
                weeks = cls._to_int(row.get("weeks_on_chart"))
            else:  # library: Date/Rank/Song/Artist/Last Week/Peak Position/Weeks on Chart
                rank = cls._to_int(row.get("Rank"))
                song = str(row.get("Song") or "").strip()
                artist = str(row.get("Artist") or "").strip()
                last_week = cls._to_int(row.get("Last Week"))
                peak = cls._to_int(row.get("Peak Position"))
                weeks = cls._to_int(row.get("Weeks on Chart"))
            if rank is None or not song:
                continue
            items.append({
                "rank": rank,
                "song": song,
                "artist": artist or "未知歌手",
                "last_week": last_week,
                "peak_position": peak if peak is not None else rank,
                "weeks_on_chart": weeks if weeks is not None else 0,
            })
        if not items:
            raise ChartFetchError("榜单数据为空（解析到 0 条有效条目）")
        items.sort(key=lambda x: x["rank"])
        return items

    # ---------- 主方案：GitHub JSON ----------

    async def fetch_from_json(self, chart_slug: str, date: Optional[str] = None) -> dict:
        """从 mhollingshead/billboard-hot-100 拉取 JSON（仅支持 hot-100）"""
        if chart_slug != "hot-100":
            raise ChartFetchError(
                f"JSON 数据源仅支持 hot-100 榜单，{chart_slug} 请使用 billboard-charts 库"
            )
        date = normalize_date(date)
        url = f"{JSON_BASE}/recent.json" if not date else f"{JSON_BASE}/date/{date}.json"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    if date and resp.status == 404:
                        raise ChartFetchError(
                            f"JSON 数据源中不存在 {date} 的榜单（该仓库覆盖 1958 年至今的每周榜单，"
                            f"请确认日期是有效周次）"
                        )
                    raise ChartFetchError(f"JSON 数据源 HTTP {resp.status}")
                raw = await resp.read()
        except asyncio.TimeoutError:
            raise ChartFetchError(f"JSON 数据源请求超时（{REQUEST_TIMEOUT}s）")
        except aiohttp.ClientError as e:
            raise ChartFetchError(f"JSON 数据源网络错误：{type(e).__name__}: {e}")
        try:
            payload = json.loads(raw.decode("utf-8"))
            chart_date = str(payload.get("date") or date or "").strip()
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                raise ValueError("data 字段缺失或为空")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, AttributeError) as e:
            raise ChartFetchError(f"JSON 解析失败：{e}")
        return {
            "date": chart_date or None,
            "source": "json",
            "items": self._normalize_items(data, "json"),
        }

    # ---------- 备选方案：billboard-charts 库 ----------

    async def fetch_from_library(self, chart_slug: str, date: Optional[str] = None) -> dict:
        """billboard-charts 库方案（同步库，to_thread 包装；指定日期不支持）"""
        if not HAS_BILLBOARD_LIB:
            raise ChartFetchError(
                "billboard-charts 库未安装，请在部署环境执行：pip install billboard-charts"
            )
        if date:
            raise ChartFetchError(
                "billboard-charts 库方案不支持指定日期查询（指定日期请使用 JSON 数据源，仅 hot-100）"
            )
        chart_date: Optional[str] = None
        try:
            rows = await asyncio.to_thread(_lib_fetch_chart, chart_slug)
        except WeekNotPublished:
            raise ChartFetchError(
                f"Billboard 尚未发布 {chart_slug} 的该周榜单（WeekNotPublished），请稍后再试"
            )
        except ChartFetchError:
            raise
        except Exception as e:
            raise ChartFetchError(
                f"billboard-charts 库获取失败：{type(e).__name__}: {e}"
            )
        if not rows:
            raise ChartFetchError("billboard-charts 返回空数据")
        # 从行数据里取权威校验过的榜单日期
        for row in rows:
            d = str(row.get("Date") or "").strip()
            if d:
                chart_date = d
                break
        return {
            "date": chart_date or None,
            "source": "library",
            "items": self._normalize_items(rows, "library"),
        }

    # ---------- 内地华语：网易云音乐 ----------

    async def fetch_from_netease(self, chart_slug: str) -> dict:
        """网易云音乐官方榜单（歌单接口），返回统一结构。

        该源无排名变化数据：last_week=None 且 no_trend=True，
        渲染层据此显示「本期在榜」而非「新上榜」。
        """
        pid, display = NETEASE_CHARTS[chart_slug]
        session = await self._get_session()
        url = f"https://music.163.com/api/playlist/detail?id={pid}"
        try:
            async with session.get(
                url, headers={"Referer": "https://music.163.com",
                              "User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    raise ChartFetchError(f"网易云接口 HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            raise ChartFetchError(f"网易云接口请求超时（{REQUEST_TIMEOUT}s）")
        except aiohttp.ClientError as e:
            raise ChartFetchError(f"网易云接口网络错误：{type(e).__name__}: {e}")
        try:
            tracks = payload["result"]["tracks"]
            if not tracks:
                raise ValueError("tracks 为空")
            items = []
            for i, t in enumerate(tracks):
                song = str(t.get("name") or "").strip()
                if not song:
                    continue
                artists = "/".join(
                    str(a.get("name") or "").strip()
                    for a in (t.get("artists") or []) if a.get("name")
                ) or "未知歌手"
                items.append({
                    "rank": i + 1, "song": song, "artist": artists,
                    "last_week": None, "peak_position": i + 1,
                    "weeks_on_chart": 0, "no_trend": True,
                })
        except (KeyError, TypeError, ValueError) as e:
            raise ChartFetchError(f"网易云数据解析失败：{e}")
        if not items:
            raise ChartFetchError("网易云榜单数据为空")
        return {
            "date": datetime.date.today().strftime("%Y-%m-%d"),
            "source": "netease",
            "items": items,
        }

    # ---------- 统一入口（带 TTL 缓存） ----------

    async def fetch(
        self,
        chart_slug: str = "hot-100",
        date: Optional[str] = None,
        use_json: bool = True,
        force: bool = False,
    ) -> dict:
        # 别名归一化（含中文别名，必须在 slug 白名单校验之前）
        chart_slug = SLUG_ALIASES.get(chart_slug, chart_slug)
        if chart_slug not in NETEASE_CHARTS and not SLUG_RE.match(chart_slug or ""):
            raise ChartFetchError(
                f"榜单 slug 不合法：{chart_slug!r}（示例：hot-100 / huayu / mainland）"
            )
        date = normalize_date(date)

        # 数据源调度：内地华语走网易云；JSON 仅 hot-100；其他走库方案
        if chart_slug in NETEASE_CHARTS:
            source = "netease"
        elif use_json and chart_slug == "hot-100":
            source = "json"
        elif use_json and date:
            raise ChartFetchError(
                "JSON 数据源仅支持 hot-100 的日期查询；其他榜单不支持指定日期"
            )
        else:
            source = "library"

        key = self._cache_key(chart_slug, date, source)
        async with self._lock:
            cached = self._cache.get(key)
            if cached and not force and time.time() - cached["ts"] < CACHE_TTL:
                data = dict(cached["data"])
                data["cached"] = True
                return data

            if source == "json":
                data = await self.fetch_from_json(chart_slug, date)
            elif source == "netease":
                data = await self.fetch_from_netease(chart_slug)
            else:
                data = await self.fetch_from_library(chart_slug, date)
            self._cache[key] = {"ts": time.time(), "data": data}
            result = dict(data)
            result["cached"] = False
            return result

    def invalidate(self, chart_slug: Optional[str] = None):
        if chart_slug:
            self._cache = {k: v for k, v in self._cache.items() if k[1] != chart_slug}
        else:
            self._cache.clear()
