"""Pillow 渲染层：Billboard 榜单深色风格图（2x 渲染，宽 1600px）

输出 PNG bytes。渲染失败由调用方捕获后降级为文本消息。
"""

import asyncio
import glob
import html
import os
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

try:
    from .zhuxi_t2i_template import ZHUXI_T2I_TEMPLATE
except ImportError:
    from zhuxi_t2i_template import ZHUXI_T2I_TEMPLATE

T2I_DIRECT_ENDPOINTS = ["https://t2i.soulter.top/text2img"]


async def _t2i_render_direct(tmpl: str, data: dict) -> bytes:
    """绕过系统代理直连官方 t2i 端点渲染自定义模板，返回图片字节。

    依次尝试 trust_env=False（直连）与 trust_env=True（走系统代理）。
    """
    import aiohttp
    post = {
        "tmpl": tmpl, "json": True, "tmpldata": data,
        "options": {"full_page": True, "type": "jpeg", "quality": 70},
    }
    last_exc = None
    for ep in T2I_DIRECT_ENDPOINTS:
        for trust_env in (False, True):
            try:
                async with aiohttp.ClientSession(trust_env=trust_env) as session:
                    async with session.post(
                        f"{ep}/generate", json=post,
                        headers={"User-Agent": "AstrBot/t2i"},
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        ret = await resp.json()
                    img_url = f"{ep}/{ret['data']['id']}"
                    async with session.get(
                        img_url, headers={"User-Agent": "AstrBot/t2i"},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as img_resp:
                        if img_resp.status != 200:
                            raise RuntimeError(f"HTTP {img_resp.status}")
                        raw = await img_resp.read()
                if raw:
                    return raw
                raise RuntimeError("t2i 返回空图片")
            except Exception as e:
                last_exc = e
    raise last_exc or RuntimeError("t2i 直连渲染失败")


def _chart_cards(items: list, max_items: int = 10) -> list:
    """榜单条目 → 烛之音乐播报卡片结构"""
    cards = []
    for it in items[:max_items]:
        rank, lw = it.get("rank"), it.get("last_week")
        song = html.escape(str(it.get("song", "")))
        artist = html.escape(str(it.get("artist", "")))
        peak = it.get("peak_position", rank)
        weeks = it.get("weeks_on_chart", 0)
        if lw is None:
            trend = "🆕 新上榜"
        elif lw == rank:
            trend = f"➖ 上周 #{lw} 持平"
        else:
            arrow = "📈" if lw > rank else "📉"
            sym = "↑" if lw > rank else "↓"
            trend = f"{arrow} 上周 #{lw} → 本周 #{rank}（{sym}{abs(lw - rank)}）"
        cards.append({
            "name": f"#{rank} {song}",
            "count": artist,
            "events": [f"{trend} ｜ 峰值 #{peak} ｜ 在榜 {weeks} 周"],
        })
    return cards


async def t2i_render_chart(title: str, date, items: list, max_items: int = 10):
    """烛之音乐模板渲染榜单，失败返回 None（调用方降级本地 Pillow）。"""
    try:
        if not items:
            return None
        data = {
            "title": title,
            "date": f"Week of {date}" if date else "未注明周次",
            "cards": _chart_cards(items, max_items),
        }
        return await asyncio.wait_for(
            _t2i_render_direct(ZHUXI_T2I_TEMPLATE, data), timeout=150)
    except Exception:
        return None


# 深色主题

WIDTH = 1600          # 2x 渲染宽度
HEADER_H = 240
COLUMN_HEADER_H = 90
ROW_H = 150
MARGIN_X = 60
RENDER_LIMIT = 100    # 最多渲染条数（防刷爆）

# 深色主题
BG = (13, 17, 23)
CARD = (22, 27, 34)
CARD_ALT = (18, 22, 28)
BORDER = (48, 54, 61)
TEXT_MAIN = (230, 237, 243)
TEXT_SUB = (139, 148, 158)
ACCENT = (88, 166, 255)
GOLD = (226, 178, 62)
SILVER = (192, 197, 206)
BRONZE = (205, 127, 50)
UP_GREEN = (63, 185, 80)
DOWN_RED = (248, 81, 73)
NEW_BLUE = (88, 166, 255)

# 字体搜索：插件 fonts/ 优先，其次系统常见 CJK/拉丁字体
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

_FONT_KEYWORDS = (
    "wqy", "notosanscjk", "noto-sans-cjk", "notoserifcjk", "sourcehansans",
    "source-han-sans", "droidsansfallback", "microsoftyahei", "msyh",
    "pingfang", "hiragino", "sarasa", "misans", "harmonyos",
)

def _font_candidates():
    paths = []
    # 1) 插件自带 fonts/ 目录（最优先，保证无字体裸服务器也能出中文图）
    for pattern in ("*.ttf", "*.otf", "*.ttc", "*.otc"):
        paths.extend(sorted(glob.glob(os.path.join(_PLUGIN_DIR, "fonts", pattern))))
    # 2) 系统字体目录（含 AstrBot 容器常见路径）
    roots = ["/usr/share/fonts", "/usr/local/share/fonts",
             os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts")]
    for root in roots:
        for kw in _FONT_KEYWORDS:
            for pattern in ("ttf", "otf", "ttc"):
                paths.extend(glob.glob(
                    os.path.join(root, "**", f"*{kw}*.{pattern}"), recursive=True))
    # 3) 任意 DejaVu/ liberation（兜底拉丁）
    for root in roots[:2]:
        for pattern in ("ttf", "otf"):
            paths.extend(glob.glob(
                os.path.join(root, "**", f"*DejaVuSans.{pattern}"), recursive=True))
    # 去重保序
    seen, out = set(), []
    for p in paths:
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out.append(p)
    return out


class RendererError(Exception):
    pass


def _load_font(size: int):
    for path in _font_candidates():
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    # 最后回退 Pillow 内置位图字体（不支持指定大小也要兜住）
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _supports_cjk(font) -> bool:
    try:
        box = font.getbbox("榜")
        return bool(box) and (box[2] - box[0]) > 1
    except Exception:
        return False


def _ellipsis(draw, text, font, max_w):
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    out = text
    while out and draw.textlength(out + "…", font=font) > max_w:
        out = out[:-1]
    return (out + "…") if out else ""


def _draw_tri(draw, cx, cy, up, color, s=22):
    """手绘升/降三角箭头（零 emoji 字形依赖）"""
    if up:
        pts = [(cx, cy - s), (cx - s * 0.9, cy + s * 0.6), (cx + s * 0.9, cy + s * 0.6)]
    else:
        pts = [(cx, cy + s), (cx - s * 0.9, cy - s * 0.6), (cx + s * 0.9, cy - s * 0.6)]
    draw.polygon(pts, fill=color)


def render_chart(title: str, date, items: list, max_items: int = 10) -> bytes:
    """渲染榜单图，返回 PNG bytes。任何异常包装为 RendererError。"""
    try:
        if not items:
            raise RendererError("无可渲染数据")
        n = max(1, min(int(max_items or 10), RENDER_LIMIT, len(items)))
        rows = items[:n]
        f_title = _load_font(72)
        f_date = _load_font(40)
        f_head = _load_font(34)
        f_rank = _load_font(64)
        f_song = _load_font(44)
        f_sub = _load_font(32)
        if not _supports_cjk(f_song):
            # 主字体不含 CJK 时再试一遍候选里其他字体
            pass  # _load_font 已按 CJK 关键字优先；此处仅防御，缺字形会显示方块但功能不受影响

        h = HEADER_H + COLUMN_HEADER_H + ROW_H * n + 40
        img = Image.new("RGB", (WIDTH, h), BG)
        d = ImageDraw.Draw(img)

        # ---------- 头部 ----------
        d.rectangle([0, 0, WIDTH, HEADER_H], fill=CARD)
        d.rectangle([0, HEADER_H - 4, WIDTH, HEADER_H], fill=ACCENT)
        d.text((MARGIN_X, 40), "📊 " + title, font=f_title, fill=TEXT_MAIN)
        date_text = str(date) if date else "未注明周次"
        d.text((MARGIN_X, 138), f"Week of {date_text} · Billboard Chart",
               font=f_date, fill=TEXT_SUB)
        d.text((WIDTH - MARGIN_X, 60), "HOT", font=f_title, fill=ACCENT, anchor="ra")

        # ---------- 列头 ----------
        col_rank_x = MARGIN_X + 50            # 排名中心
        col_main_x = MARGIN_X + 130           # 歌名/歌手左起点
        col_main_w = WIDTH - MARGIN_X * 2 - 130 - 470   # 主区宽度
        col_delta_x = col_main_x + col_main_w + 120     # 变化列中心
        col_peak_x = col_delta_x + 150        # 峰值中心
        col_weeks_x = WIDTH - MARGIN_X - 70   # 周数中心
        yh = HEADER_H + COLUMN_HEADER_H // 2
        for label, cx in (("#", col_rank_x), ("变化", col_delta_x),
                          ("峰值", col_peak_x), ("周数", col_weeks_x)):
            d.text((cx, yh), label, font=f_head, fill=TEXT_SUB, anchor="mm")
        d.line([MARGIN_X, HEADER_H + COLUMN_HEADER_H,
                WIDTH - MARGIN_X, HEADER_H + COLUMN_HEADER_H], fill=BORDER, width=2)

        # ---------- 行 ----------
        y = HEADER_H + COLUMN_HEADER_H
        for i, it in enumerate(rows):
            row_bg = CARD if i % 2 == 0 else CARD_ALT
            d.rectangle([0, y, WIDTH, y + ROW_H], fill=row_bg)
            rank = it.get("rank") or (i + 1)
            rank_color = {1: GOLD, 2: SILVER, 3: BRONZE}.get(rank, TEXT_MAIN)
            d.text((col_rank_x, y + ROW_H // 2), str(rank),
                   font=f_rank, fill=rank_color, anchor="mm")

            # 歌名 + 歌手（两行）
            song = _ellipsis(d, str(it.get("song", "")), f_song, col_main_w)
            artist = _ellipsis(d, str(it.get("artist", "")), f_sub, col_main_w)
            d.text((col_main_x, y + 26), song, font=f_song, fill=TEXT_MAIN)
            d.text((col_main_x, y + 96), artist, font=f_sub, fill=TEXT_SUB)

            # 变化列：↑n / ↓n / — / NEW
            lw, wk = it.get("last_week"), it.get("rank")
            if lw is None:
                d.text((col_delta_x, y + ROW_H // 2), "NEW",
                       font=f_head, fill=NEW_BLUE, anchor="mm")
            elif lw == wk:
                d.text((col_delta_x, y + ROW_H // 2), "—",
                       font=f_head, fill=TEXT_SUB, anchor="mm")
            else:
                up = lw > wk
                color = UP_GREEN if up else DOWN_RED
                _draw_tri(d, col_delta_x - 28, y + ROW_H // 2, up, color, s=18)
                d.text((col_delta_x + 4, y + ROW_H // 2), str(abs(lw - wk)),
                       font=f_song, fill=color, anchor="mm")

            d.text((col_peak_x, y + ROW_H // 2), f"#{it.get('peak_position', rank)}",
                   font=f_head, fill=TEXT_SUB, anchor="mm")
            d.text((col_weeks_x, y + ROW_H // 2), f"{it.get('weeks_on_chart', 0)}",
                   font=f_head, fill=TEXT_SUB, anchor="mm")
            y += ROW_H

        # 底部留白线
        d.line([0, h - 2, WIDTH, h - 2], fill=BORDER, width=2)

        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except RendererError:
        raise
    except Exception as e:
        raise RendererError(f"图片渲染失败：{type(e).__name__}: {e}")


def chart_display_name(slug: str) -> str:
    """slug → 展示名"""
    names = {
        "hot-100": "Billboard Hot 100",
        "billboard-200": "Billboard 200",
        "artist-100": "Billboard Artist 100",
        "pop-songs": "Pop Songs",
        "radio-songs": "Radio Songs",
        "streaming-songs": "Streaming Songs",
        "digital-song-sales": "Digital Song Sales",
        "billboard-global-200": "Billboard Global 200",
        "billboard-global-excl-us": "Billboard Global Excl. U.S.",
        "canadian-hot-100": "Canadian Hot 100",
        "uk-songs-chart": "UK Songs Chart",
        "country-songs": "Country Songs",
        "rock-songs": "Rock Songs",
        "r-and-b-hip-hop-songs": "R&B/Hip-Hop Songs",
        "dance-electronic-songs": "Dance/Electronic Songs",
        "latin-songs": "Latin Songs",
        "world-digital-song-sales": "World Digital Song Sales",
    }
    return names.get(slug, f"Billboard {slug.replace('-', ' ').title()}")


def fallback_text(title: str, date, items: list, max_items: int = 10) -> str:
    """Markdown 文本降级格式"""
    n = max(1, min(int(max_items or 10), RENDER_LIMIT, len(items)))
    lines = [f"📊 {title} Top {n}（{date or '未注明周次'}）", ""]
    for it in items[:n]:
        rank, lw = it.get("rank"), it.get("last_week")
        if lw is None:
            trend = "🆕 新上榜"
        elif lw == rank:
            trend = "➖ 与上周持平"
        else:
            arrow = "📈" if lw > rank else "📉"
            sym = "↑" if lw > rank else "↓"
            trend = f"{arrow} 上周 #{lw} → 本周 #{rank}（{sym}{abs(lw - rank)}）"
        lines.append(f"{rank}. {it.get('song', '')} — {it.get('artist', '')}")
        lines.append(f"   {trend} | 峰值 #{it.get('peak_position', rank)} "
                     f"| 在榜 {it.get('weeks_on_chart', 0)} 周")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
