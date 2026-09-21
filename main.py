"""astrbot_plugin_music_chart - Billboard 音乐榜单查询与每日定时推送

指令（统一入口 /music）:
    /music                  当前默认榜单 Top N（JSON 主方案）
    /music hot-100          指定榜单 slug
    /music date 2026-09-12  查询指定日期榜单（仅 JSON 主方案 / hot-100）
    /music refresh          强制刷新（绕过缓存）
    /music help             帮助
    /music debug            诊断（连通性 / 解析 / 缓存 / 渲染 / 调度）
"""

import asyncio
import base64
import datetime
import re

# 兼容旧版 AstrBot：astrbot.api 不导出 AstrMessageEvent/Star，逐符号降级链
try:
    from astrbot.api import logger
except ImportError:
    from astrbot.core import logger

try:
    from astrbot.api import AstrMessageEvent
except ImportError:
    try:
        from astrbot.api.event import AstrMessageEvent
    except ImportError:
        from astrbot.core.platform.astr_message_event import AstrMessageEvent

try:
    from astrbot.api import Star
except ImportError:
    try:
        from astrbot.api.star import Star
    except ImportError:
        from astrbot.core.star import Star

try:
    from astrbot.api import Context, register
except ImportError:
    from astrbot.api.star import Context, register

from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain

try:
    from astrbot.api import filter
except ImportError:  # 兼容不同版本 AstrBot 的导出位置
    try:
        import astrbot.api.star.filter as filter
    except ImportError:
        from astrbot.core.star import filter

from .fetcher import (MusicChartFetcher, ChartFetchError, HAS_BILLBOARD_LIB,
                      SLUG_ALIASES, NETEASE_CHARTS)
from .renderer import (render_chart, fallback_text, chart_display_name,
                       t2i_render_chart, _load_font)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PUSH_POLL_INTERVAL = 30  # 定时轮询间隔（秒）

HELP_TEXT = """🎵 音乐榜单插件（astrbot_plugin_music_chart）

/music 或 /音乐            当前 Billboard Hot 100 Top {max_items}
/music <榜单>              指定榜单，如 /音乐 billboard-200

—— 华语中文歌系列 ——
/音乐 华语                 Billboard 台湾歌曲榜（国语）
/音乐 粤语                 Billboard 香港歌曲榜（粤语）
/音乐 内地                 华语内地热歌榜（网易云音乐）
/音乐 飙升                 华语飙升榜（网易云音乐）
/音乐 新歌                 华语新歌榜（网易云音乐）
（英文别名：huayu / cantonese / mainland / netrise / netnew）

其他：
/music 日期 2026-09-12    指定日期榜单（仅 hot-100）
/music 刷新               强制刷新（绕过缓存）
/music 帮助               显示本帮助
/music 诊断               诊断检查

说明：
· 华语台湾/香港榜需安装 billboard-charts 库；内地榜走网易云音乐接口
· Billboard 榜单每周更新（通常周二），内地榜每日更新
· 定时推送请在管理面板配置 push_time / push_target（chart_name 可填 华语 / 内地 等）
""".rstrip()


@register(
    "astrbot_plugin_music_chart",
    "Zxin-Pro",
    "Billboard 音乐榜单查询与每日定时推送（Hot 100 等）",
    "1.0.0",
)
class MusicChartPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.context = context
        self.config: dict = config or {}
        self.fetcher = MusicChartFetcher()
        self._push_task: asyncio.Task | None = None
        self._last_push_date: str | None = None
        self._push_ok_date: str | None = None

    # ---------- 配置 ----------

    def _cfg(self, key: str, default=None):
        try:
            val = self.config.get(key, default)
            return val if val not in ("", None) else default
        except Exception:
            return default

    @property
    def _max_items(self) -> int:
        try:
            return max(1, min(100, int(self._cfg("max_items", 10))))
        except (TypeError, ValueError):
            return 10

    @property
    def _chart_name(self) -> str:
        slug = str(self._cfg("chart_name", "hot-100") or "hot-100").strip().lower()
        slug = SLUG_ALIASES.get(slug, slug)
        return slug if slug else "hot-100"

    # ---------- 生命周期 ----------

    async def initialize(self):
        self._push_task = asyncio.create_task(self._push_loop())
        logger.info("[music_chart] loaded v1.0.0")

    async def terminate(self):
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except (asyncio.CancelledError, Exception):
                pass
            self._push_task = None
        await self.fetcher.close()
        logger.info("[music_chart] terminated")

    # ---------- 指令入口 ----------

    @filter.command("music", alias={"音乐", "音乐榜"})
    async def cmd_music(self, event: AstrMessageEvent):
        """音乐榜单查询主指令（/music /音乐 /音乐榜）"""
        args = (event.message_str or "").strip().split()
        if args and args[0].lower() in ("music", "/music", "音乐", "/音乐",
                                        "音乐榜", "/音乐榜"):
            args = args[1:]

        # 子命令分发（中英双语）
        if args and args[0].lower() in ("help", "帮助"):
            yield event.plain_result(HELP_TEXT.format(max_items=self._max_items))
            return
        if args and args[0].lower() in ("debug", "诊断"):
            async for r in self._debug_report():
                yield r
            return
        if args and args[0].lower() in ("refresh", "刷新"):
            async for r in self._query_and_reply(event, self._chart_name,
                                                 force=True):
                yield r
            return
        if args and args[0].lower() in ("date", "日期"):
            if len(args) < 2:
                yield event.plain_result("用法：/音乐 日期 2026-09-12（仅 hot-100 支持）")
                return
            async for r in self._query_and_reply(event, self._chart_name,
                                                 date=args[1]):
                yield r
            return
        if args and args[0].lower() in ("hot", "top"):
            # 容错：/music top 20 之类
            async for r in self._query_and_reply(event, self._chart_name):
                yield r
            return
        if args:
            # 视作榜单 slug
            async for r in self._query_and_reply(event, args[0].lower()):
                yield r
            return

        # /music 默认查询
        async for r in self._query_and_reply(event, self._chart_name):
            yield r

    # ---------- 查询与回复 ----------

    async def _query_and_reply(self, event: AstrMessageEvent, slug: str,
                               date: str = None, force: bool = False):
        """取数 → 渲染/降级文本 → 回复（async generator，yield 消息结果）"""
        slug = SLUG_ALIASES.get(slug, slug)   # 别名归一化，保证展示名正确
        use_json = bool(self._cfg("use_json_source", True))
        enable_img = bool(self._cfg("enable_image_render", True))
        max_items = self._max_items

        try:
            data = await self.fetcher.fetch(slug, date=date, use_json=use_json,
                                            force=force)
        except ChartFetchError as e:
            yield event.plain_result(f"❌ 榜单获取失败：{e}")
            return
        except Exception as e:
            logger.error(f"[music_chart] fetch unexpected error: {e}")
            yield event.plain_result(f"❌ 榜单获取异常：{type(e).__name__}: {e}")
            return

        title = chart_display_name(slug)
        items = data.get("items") or []

        if enable_img:
            png = None
            # 1) 烛之游播报 t2i 模板（优先）
            try:
                png = await t2i_render_chart(title, data.get("date"),
                                             items, max_items)
            except Exception as e:
                logger.warning(f"[music_chart] t2i 渲染异常：{e}")
            # 2) 本地 Pillow 深色榜单图
            if png is None:
                try:
                    png = await asyncio.to_thread(
                        render_chart, title, data.get("date"), items, max_items)
                except Exception as e:
                    logger.warning(f"[music_chart] Pillow 渲染失败，降级文本：{e}")
            if png is not None:
                b64 = base64.b64encode(png).decode()
                tag = "（缓存）" if data.get("cached") else ""
                yield event.chain_result([
                    Image.fromBase64(b64),
                    Plain(f"📊 {title} · {data.get('date') or ''} {tag}".strip()),
                ])
                return

        tag = "（缓存）" if data.get("cached") else ""
        yield event.plain_result(fallback_text(title, data.get("date"), items,
                                               max_items) + tag)

    # ---------- debug 诊断 ----------

    async def _debug_report(self):
        lines = ["🔧 music_chart 诊断", ""]
        # 1) JSON 数据源
        try:
            data = await self.fetcher.fetch_from_json("hot-100")
            lines.append(f"✅ JSON 数据源：连通，date={data['date']}，"
                         f"解析 {len(data['items'])} 条，首条={data['items'][0]['song']}")
        except ChartFetchError as e:
            lines.append(f"❌ JSON 数据源：{e}")
        except Exception as e:
            lines.append(f"❌ JSON 数据源：意外异常 {type(e).__name__}: {e}")
        # 2) 库方案可用性
        lines.append(("✅ billboard-charts 库：已安装"
                      if HAS_BILLBOARD_LIB else
                      "⚠️ billboard-charts 库：未安装（查询非 hot-100 榜单将失败）"))
        # 3) 缓存
        lines.append(f"ℹ️ {self.fetcher.cache_info()}")
        # 4) 渲染
        try:
            f = _load_font(44)
            png = await asyncio.to_thread(
                render_chart, "渲染自检",
                datetime.date.today().strftime("%Y-%m-%d"),
                [{"rank": 1, "song": "测试歌曲 Test Song", "artist": "测试歌手",
                  "last_week": 2, "peak_position": 1, "weeks_on_chart": 3}], 1)
            lines.append(f"✅ 图片渲染：成功（{len(png)} bytes，字体 {type(f).__name__}）")
        except Exception as e:
            lines.append(f"❌ 图片渲染：{e}")
        # 5) 调度
        push_time = self._cfg("push_time", "10:00")
        push_target = self._cfg("push_target", "") or ""
        n_targets = len([t for t in re.split(r"[,，]", str(push_target)) if t.strip()])
        lines.append(f"ℹ️ 定时推送：每日 {push_time}，目标 {n_targets} 个，"
                     f"默认榜单 {self._chart_name}，今日{'已' if self._push_ok_date == datetime.date.today().strftime('%Y-%m-%d') else '未'}推送成功")
        yield event.plain_result("\n".join(lines))

    # ---------- 定时推送 ----------

    def _resolve_push_targets(self) -> list:
        """push_target 解析：完整 UMO 直接用；纯数字 ID 尝试所有平台实例的群聊会话"""
        raw = str(self._cfg("push_target", "") or "")
        targets = []
        insts = []
        try:
            insts = self.context.platform_manager.get_insts()
        except Exception:
            pass
        for part in re.split(r"[,，]", raw):
            t = part.strip()
            if not t:
                continue
            if ":" in t:
                targets.append(t)
            elif insts:
                for p in insts:
                    try:
                        targets.append(f"{p.meta().name}:GroupMessage:{t}")
                    except Exception:
                        targets.append(f"aiocqhttp:GroupMessage:{t}")
            else:
                targets.append(f"aiocqhttp:GroupMessage:{t}")
        return targets

    async def _push_loop(self):
        """30s 轮询，HH:MM 匹配 + 日期防重"""
        while True:
            try:
                await asyncio.sleep(PUSH_POLL_INTERVAL)
                now = datetime.datetime.now()
                push_time = str(self._cfg("push_time", "10:00") or "10:00").strip()
                if not re.match(r"^\d{1,2}:\d{2}$", push_time):
                    continue
                try:
                    h, m = (int(x) for x in push_time.split(":"))
                except ValueError:
                    continue
                if (now.hour, now.minute) != (h, m):
                    continue
                today = now.strftime("%Y-%m-%d")
                if self._last_push_date == today:
                    continue
                self._last_push_date = today  # 先标记防重，失败明天再试
                await self._do_push()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[music_chart] push loop error: {e}")

    async def _do_push(self):
        slug = self._chart_name
        max_items = self._max_items
        use_json = bool(self._cfg("use_json_source", True))
        enable_img = bool(self._cfg("enable_image_render", True))
        title = chart_display_name(slug)
        try:
            data = await self.fetcher.fetch(slug, use_json=use_json, force=True)
        except ChartFetchError as e:
            logger.error(f"[music_chart] 定时推送取数失败：{e}")
            return
        items = data.get("items") or []

        chain = MessageChain(chain=[])
        if enable_img:
            png = None
            try:
                png = await t2i_render_chart(title, data.get("date"),
                                             items, max_items)
            except Exception as e:
                logger.warning(f"[music_chart] t2i 渲染异常：{e}")
            if png is None:
                try:
                    png = await asyncio.to_thread(
                        render_chart, title, data.get("date"), items, max_items)
                except Exception as e:
                    logger.warning(f"[music_chart] 推送渲染失败，降级文本：{e}")
            if png is not None:
                b64 = base64.b64encode(png).decode()
                chain.chain = [Image.fromBase64(b64),
                               Plain(f"📊 每日音乐榜单：{title} · {data.get('date') or ''}")]
        if not chain.chain:
            chain.chain = [Plain(fallback_text(title, data.get("date"),
                                               items, max_items))]

        targets = self._resolve_push_targets()
        if not targets:
            logger.info("[music_chart] 未配置 push_target，跳过推送")
            return
        for umo in targets:
            try:
                await self.context.send_message(umo, chain)
                logger.info(f"[music_chart] 已推送 {title} 至 {umo}")
            except Exception as e:
                logger.error(f"[music_chart] 推送至 {umo} 失败：{e}")
        self._push_ok_date = datetime.date.today().strftime("%Y-%m-%d")
