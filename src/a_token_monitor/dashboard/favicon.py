"""站点图标：按当前风格生成 SVG / PNG / ICO，并提供内容寻址的图标路由。"""

from __future__ import annotations

import hashlib
import math
import struct
import zlib



# 中文注释：浏览器标签页图标（favicon）与页面内品牌图形。图形由一组几何原语描述，
# 同一份原语既生成矢量 SVG（现代浏览器走 /favicon.svg），也用 zlib/struct 光栅化成
# PNG / ICO（老浏览器与 Safari 走 /favicon.ico），所以不需要图像库、不写外部文件、
# 也不发起站外请求。候选方案来自 Stitch MCP 的设计稿，换方案只改 _FAVICON_STYLE。
_FAVICON_SIZE = 64.0


_FAVICON_RADIUS = 14.0


# 中文注释：徽章渐变取页面自己的调色板——左上 violet（页面主色 #8b5cf6），
# 右下 cyan（状态色 #06b6d4），这样标签页图标、侧栏 logo 和页面是同一套颜色。
_FAVICON_BACKGROUND = ((0x8B, 0x5C, 0xF6), (0x06, 0xB6, 0xD4))


_FAVICON_FOREGROUND = (0xFF, 0xFF, 0xFF)


_FAVICON_ICO_SIZES = (16, 32)


_FAVICON_SVG_ROUTE = "/favicon.svg"


_FAVICON_ICO_ROUTE = "/favicon.ico"


_FAVICON_SVG_MIME = "image/svg+xml"


_FAVICON_ICO_MIME = "image/x-icon"


# 中文注释：图标是内容寻址的（URL 带几何指纹）且可以长期缓存。浏览器会把
# 「这个页面没有图标」也记进 favicon 数据库，改图标时指纹变化等于换 URL，
# 于是旧浏览器一定会重新拉取，不会一直顶着空白标签。
_FAVICON_CACHE_SECONDS = 604800


_FAVICON_ICO_CACHE: bytes | None = None


# 中文注释：几何原语。坐标都在 64×64 画布上，形状列表顺序即绘制顺序，
# mode="cut" 的原语从已画好的白色图形里挖洞（露出渐变底）。
#   ("rect", x, y, w, h, rx)                  填充圆角矩形
#   ("circle", cx, cy, r)                     填充圆
#   ("ring", cx, cy, r, width)                圆环
#   ("capsule", x1, y1, x2, y2, width)        圆头线段
#   ("arc", cx, cy, r, start, end, width)     圆弧（角度制，顺时针，0° 指右）
#   ("polyline", points, width)               折线（圆角连接）
#   ("polygon", points)                       填充多边形
_FAVICON_GLYPHS: dict[str, tuple[tuple, ...]] = {
    # A. 用量柱 + 高水位刻度孔（Stitch 方案 A）。
    "bars": (
        ("rect", 14.0, 34.0, 8.0, 18.0, 4.0),
        ("rect", 28.0, 24.0, 8.0, 28.0, 4.0),
        ("rect", 42.0, 14.0, 8.0, 38.0, 4.0),
        {"mode": "cut", "shape": ("circle", 46.0, 18.5, 2.8)},
    ),
    # B. 代币 + 脉搏线（监视 + token）。
    "token": (
        ("ring", 32.0, 32.0, 17.0, 5.0),
        (
            "polyline",
            (
                (12.0, 33.0),
                (24.0, 33.0),
                (28.5, 21.0),
                (35.5, 43.0),
                (40.0, 29.0),
                (52.0, 29.0),
            ),
            4.2,
        ),
    ),
    # C. 额度表盘：弧 + 指针 + 轴心。
    "gauge": (
        ("arc", 32.0, 33.0, 16.0, 145.0, 395.0, 5.0),
        ("capsule", 32.0, 33.0, 42.5, 22.5, 4.4),
        ("circle", 32.0, 33.0, 3.8),
    ),
    # D. 成本盾牌 + 脉搏线（预算守护）。
    "guard": (
        (
            "polygon",
            (
                (32.0, 12.0),
                (49.0, 18.5),
                (49.0, 33.0),
                (47.0, 41.0),
                (41.0, 47.5),
                (32.0, 52.0),
                (23.0, 47.5),
                (17.0, 41.0),
                (15.0, 33.0),
                (15.0, 18.5),
            ),
        ),
        {"mode": "cut", "shape": (
            "polyline",
            (
                (19.0, 33.0),
                (26.0, 33.0),
                (29.5, 24.5),
                (34.5, 41.0),
                (38.0, 31.0),
                (45.0, 31.0),
            ),
            4.0,
        )},
    ),
    # E. 鲸鱼吉祥物（对应 DeepSeek 那种白色剪影，但为 16px 做了粗简化）。
    "whale": (
        (
            "polygon",
            (
                (13.0, 36.0),
                (14.5, 29.5),
                (19.0, 25.0),
                (26.0, 22.5),
                (34.0, 22.5),
                (41.0, 24.5),
                (45.5, 28.0),
                (47.5, 32.0),
                (47.0, 37.0),
                (43.0, 41.0),
                (36.0, 43.5),
                (27.0, 44.0),
                (19.0, 42.0),
            ),
        ),
        ("polygon", ((47.0, 28.0), (54.0, 21.0), (52.5, 33.0), (54.0, 45.0), (46.5, 37.5))),
        {"mode": "cut", "shape": ("circle", 21.5, 32.0, 2.6)},
    ),
}


# 中文注释：当前上线的方案（改这一个常量即可切换，测试会渲染全部方案）。
# "guard" 来自 Stitch MCP 设计稿的「成本盾牌 + 脉搏线」：16px 下依然能认出盾牌轮廓。
_FAVICON_STYLE = "guard"


def _favicon_shapes() -> tuple[tuple, ...]:
    """返回当前方案的徽章 + 图形原语列表。"""

    badge = {"mode": "fill", "shape": ("rect", 0.0, 0.0, _FAVICON_SIZE, _FAVICON_SIZE, _FAVICON_RADIUS)}
    try:
        glyph = _FAVICON_GLYPHS[_FAVICON_STYLE]
    except KeyError as error:  # pragma: no cover - 常量写错时立刻暴露
        raise ValueError(f"未知的图标方案: {_FAVICON_STYLE}") from error
    shapes: list[tuple] = [badge]
    for item in glyph:
        if isinstance(item, dict):
            shapes.append({"mode": item.get("mode", "cut"), "shape": item["shape"]})
        else:
            shapes.append({"mode": "draw", "shape": item})
    return tuple(shapes)


def _hex_color(color: tuple[int, int, int]) -> str:
    """把 RGB 三元组转成 SVG 用的 #rrggbb。"""

    return "#{:02x}{:02x}{:02x}".format(*color)


def _favicon_token() -> str:
    """返回图标几何的短指纹，用作 favicon URL 的版本参数。"""

    payload = repr(
        (
            _FAVICON_SIZE,
            _FAVICON_BACKGROUND,
            _FAVICON_FOREGROUND,
            _FAVICON_STYLE,
            _FAVICON_GLYPHS[_FAVICON_STYLE],
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


_FAVICON_LINK = (
    f'  <link rel="icon" type="{_FAVICON_ICO_MIME}" '
    f'href="{_FAVICON_ICO_ROUTE}?v={_favicon_token()}" sizes="16x16 32x32">\n'
    f'  <link rel="icon" type="{_FAVICON_SVG_MIME}" '
    f'href="{_FAVICON_SVG_ROUTE}?v={_favicon_token()}">\n'
)


def _fill_attribute(mode: str, gradient_id: str) -> str:
    """填充类原语的 fill：徽章用渐变，挖洞用黑，图形继承父级白色。"""

    if mode == "badge":
        return f' fill="url(#{gradient_id})"'
    if mode == "cut":
        return ' fill="black"'
    return ""


def _stroke_attribute(mode: str, width: float) -> str:
    """描边类原语的 stroke：图形白色，挖洞黑色。"""

    color = "black" if mode == "cut" else _hex_color(_FAVICON_FOREGROUND)
    return f' stroke="{color}" stroke-width="{width:g}"'


def _shape_svg(shape: tuple, gradient_id: str, mode: str) -> str:
    """把一个几何原语渲染成 SVG 元素。"""

    kind = shape[0]
    if kind == "rect":
        _, x, y, width, height, radius = shape
        return (
            f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" '
            f'rx="{radius:g}"{_fill_attribute(mode, gradient_id)}/>'
        )
    if kind == "circle":
        _, cx, cy, radius = shape
        return (
            f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}"'
            f'{_fill_attribute(mode, gradient_id)}/>'
        )
    if kind == "ring":
        _, cx, cy, radius, width = shape
        return (
            f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" fill="none"'
            f'{_stroke_attribute(mode, width)}/>'
        )
    if kind == "capsule":
        _, x1, y1, x2, y2, width = shape
        return (
            f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}"'
            f'{_stroke_attribute(mode, width)} stroke-linecap="round"/>'
        )
    if kind == "arc":
        _, cx, cy, radius, start, end, width = shape
        points = _arc_points(cx, cy, radius, start, end)
        return (
            f'<polyline points="{_svg_points(points)}" fill="none"'
            f'{_stroke_attribute(mode, width)} stroke-linecap="round" '
            'stroke-linejoin="round"/>'
        )
    if kind == "polyline":
        _, points, width = shape
        return (
            f'<polyline points="{_svg_points(points)}" fill="none"'
            f'{_stroke_attribute(mode, width)} stroke-linecap="round" '
            'stroke-linejoin="round"/>'
        )
    if kind == "polygon":
        _, points = shape
        return (
            f'<polygon points="{_svg_points(points)}"'
            f'{_fill_attribute(mode, gradient_id)}/>'
        )
    raise ValueError(f"未知的几何原语: {kind}")


def _svg_points(points: tuple[tuple[float, float], ...]) -> str:
    """把点序列格式化成 SVG 的 points 属性。"""

    return " ".join(f"{x:g},{y:g}" for x, y in points)


def _arc_points(
    cx: float,
    cy: float,
    radius: float,
    start: float,
    end: float,
    steps: int = 24,
) -> tuple[tuple[float, float], ...]:
    """把一段圆弧离散成折线点（SVG 与光栅化共用同一份采样）。"""

    total = end - start
    return tuple(
        (
            cx + radius * math.cos(math.radians(start + total * index / steps)),
            cy + radius * math.sin(math.radians(start + total * index / steps)),
        )
        for index in range(steps + 1)
    )


def _icon_svg(gradient_id: str, label: str | None = None) -> str:
    """按共享几何生成图标 SVG：标签页图标和页面内 logo 用的是同一份图形。

    ``gradient_id`` 让同一页面里多处引用也不会撞 id；``label`` 为空时按装饰性
    图形处理（外层已经有 aria-hidden）。
    """

    stops = "".join(
        f'<stop offset="{index}" stop-color="{_hex_color(color)}"/>'
        for index, color in enumerate(_FAVICON_BACKGROUND)
    )
    size = int(_FAVICON_SIZE)
    attributes = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    shapes = _favicon_shapes()
    badge = _shape_svg(shapes[0]["shape"], gradient_id, "badge")
    draws = [entry["shape"] for entry in shapes[1:] if entry["mode"] != "cut"]
    cuts = [entry["shape"] for entry in shapes[1:] if entry["mode"] == "cut"]
    parts = [
        f'<defs><linearGradient id="{gradient_id}" x1="0" y1="0" x2="1" y2="1">'
        f"{stops}</linearGradient></defs>",
        badge,
    ]
    if draws:
        glyph = "".join(
            _shape_svg(shape, gradient_id, "glyph") for shape in draws
        )
        if cuts:
            # 中文注释：白色图形统一走一个 mask 组，黑色原语即为挖掉的洞
            # （例如柱状图的高水位点、盾牌里的脉搏线），露出的就是渐变底。
            mask_id = f"{gradient_id}-glyph"
            holes = "".join(
                _shape_svg(shape, gradient_id, "cut") for shape in cuts
            )
            parts.append(
                f'<mask id="{mask_id}"><rect width="{size}" height="{size}" '
                f'fill="white"/>{holes}</mask>'
            )
            parts.append(
                f'<g mask="url(#{mask_id})" fill="{_hex_color(_FAVICON_FOREGROUND)}">'
                f"{glyph}</g>"
            )
        else:
            parts.append(
                f'<g fill="{_hex_color(_FAVICON_FOREGROUND)}">{glyph}</g>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f"{attributes}>" + "".join(parts) + "</svg>"
    )


def _favicon_svg() -> str:
    """浏览器标签页图标：带尺寸和可访问名称，可独立作为图片加载。"""

    svg = _icon_svg("badge", label="Token Monitor")
    return svg.replace(
        "<svg ",
        f'<svg width="{int(_FAVICON_SIZE)}" height="{int(_FAVICON_SIZE)}" ',
        1,
    )


def _brand_mark_svg() -> str:
    """页面内品牌图形：与标签页图标同一份几何，只换主题里的渐变 id。"""

    return _icon_svg("brand-badge")


def _inside_rounded_square(
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
) -> bool:
    """判断点是否落在圆角矩形内（用于光栅化时的覆盖率采样）。"""

    inner_x = min(max(x, radius), width - radius)
    inner_y = min(max(y, radius), height - radius)
    delta_x = x - inner_x
    delta_y = y - inner_y
    return delta_x * delta_x + delta_y * delta_y <= radius * radius


def _distance_to_segment(
    x: float,
    y: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """点到线段的距离。"""

    delta_x = x2 - x1
    delta_y = y2 - y1
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 0:
        return math.hypot(x - x1, y - y1)
    ratio = ((x - x1) * delta_x + (y - y1) * delta_y) / length_squared
    ratio = min(1.0, max(0.0, ratio))
    return math.hypot(x - (x1 + ratio * delta_x), y - (y1 + ratio * delta_y))


def _inside_polygon(x: float, y: float, points: tuple[tuple[float, float], ...]) -> bool:
    """射线法判断点是否在多边形内。"""

    inside = False
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def _inside_shape(x: float, y: float, shape: tuple) -> bool:
    """判断点是否落在某个几何原语内（描边类形状按半宽判定）。"""

    kind = shape[0]
    if kind == "rect":
        _, rect_x, rect_y, width, height, radius = shape
        return _inside_rounded_square(x - rect_x, y - rect_y, width, height, radius)
    if kind == "circle":
        _, cx, cy, radius = shape
        return math.hypot(x - cx, y - cy) <= radius
    if kind == "ring":
        _, cx, cy, radius, width = shape
        return abs(math.hypot(x - cx, y - cy) - radius) <= width / 2
    if kind == "capsule":
        _, x1, y1, x2, y2, width = shape
        return _distance_to_segment(x, y, x1, y1, x2, y2) <= width / 2
    if kind == "arc":
        _, cx, cy, radius, start, end, width = shape
        return _distance_to_polyline(
            x, y, _arc_points(cx, cy, radius, start, end)
        ) <= width / 2
    if kind == "polyline":
        _, points, width = shape
        return _distance_to_polyline(x, y, points) <= width / 2
    if kind == "polygon":
        _, points = shape
        return _inside_polygon(x, y, points)
    raise ValueError(f"未知的几何原语: {kind}")


def _distance_to_polyline(
    x: float,
    y: float,
    points: tuple[tuple[float, float], ...],
) -> float:
    """点到折线的最短距离（圆角连接靠逐段取最小自然成立）。"""

    if len(points) < 2:
        return math.hypot(x - points[0][0], y - points[0][1]) if points else math.inf
    return min(
        _distance_to_segment(x, y, x1, y1, x2, y2)
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


def _favicon_sample(x: float, y: float) -> tuple[int, int, int, int]:
    """返回 64×64 画布上某个采样点的 RGBA：图形纯白，徽章按对角渐变。"""

    shapes = _favicon_shapes()
    badge = shapes[0]["shape"]
    for entry in shapes[1:]:
        if entry["mode"] == "cut" and _inside_shape(x, y, entry["shape"]):
            return (*_gradient_color(x, y), 255)
    if not _inside_shape(x, y, badge):
        return (0, 0, 0, 0)
    for entry in shapes[1:]:
        if entry["mode"] != "cut" and _inside_shape(x, y, entry["shape"]):
            return (*_FAVICON_FOREGROUND, 255)
    return (*_gradient_color(x, y), 255)


def _gradient_color(x: float, y: float) -> tuple[int, int, int]:
    """按对角线位置在品牌渐变上取色。"""

    ratio = min(1.0, max(0.0, (x + y) / (2.0 * _FAVICON_SIZE)))
    start, end = _FAVICON_BACKGROUND
    return (
        round(start[0] + (end[0] - start[0]) * ratio),
        round(start[1] + (end[1] - start[1]) * ratio),
        round(start[2] + (end[2] - start[2]) * ratio),
    )


def _favicon_pixels(size: int) -> bytes:
    """把图标光栅化成 RGBA 扫描行；每个像素做 4×4 超采样抗锯齿。"""

    samples = 4
    total_samples = samples * samples
    rows = bytearray()
    for row in range(size):
        rows.append(0)  # PNG 每行前缀：过滤器类型 0（None）
        for column in range(size):
            red = green = blue = alpha_sum = 0.0
            for sub_row in range(samples):
                for sub_column in range(samples):
                    # 采样点从目标像素换算回 64×64 画布坐标。
                    x = (column + (sub_column + 0.5) / samples) * _FAVICON_SIZE / size
                    y = (row + (sub_row + 0.5) / samples) * _FAVICON_SIZE / size
                    sample = _favicon_sample(x, y)
                    weight = sample[3] / 255.0
                    red += sample[0] * weight
                    green += sample[1] * weight
                    blue += sample[2] * weight
                    alpha_sum += weight
            if alpha_sum <= 0:
                rows.extend((0, 0, 0, 0))
                continue
            # 先按预乘 alpha 平均，再还原颜色，避免边缘出现黑边。
            rows.extend(
                (
                    round(red / alpha_sum),
                    round(green / alpha_sum),
                    round(blue / alpha_sum),
                    round(alpha_sum / total_samples * 255),
                )
            )
    return bytes(rows)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """组装一个 PNG 数据块（长度 + 类型 + 内容 + CRC32）。"""

    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _favicon_png(size: int) -> bytes:
    """生成 8 位 RGBA 的 PNG 图标，只依赖 zlib。"""

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(_favicon_pixels(size), 9))
        + _png_chunk(b"IEND", b"")
    )


def _favicon_ico() -> bytes:
    """把多个尺寸的 PNG 打包成 ICO（Vista 起支持 PNG 负载，结果做进程内缓存）。"""

    global _FAVICON_ICO_CACHE
    if _FAVICON_ICO_CACHE is not None:
        return _FAVICON_ICO_CACHE
    images = [(size, _favicon_png(size)) for size in _FAVICON_ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    for size, image in images:
        entries.extend(
            struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset)
        )
        offset += len(image)
    _FAVICON_ICO_CACHE = b"".join([header, bytes(entries)] + [i for _, i in images])
    return _FAVICON_ICO_CACHE


def favicon_response(path: str) -> tuple[str, bytes] | None:
    """返回 favicon 路由对应的 MIME 与内容；不是 favicon 路由时返回 None。

    带 ``?v=<指纹>`` 的版本参数会被忽略，这样页面可以用内容寻址的 URL
    绕开浏览器里「这个页面没有图标」的旧缓存。
    """

    route = path.split("?", 1)[0]
    if route == _FAVICON_SVG_ROUTE:
        return _FAVICON_SVG_MIME, _favicon_svg().encode("utf-8")
    if route == _FAVICON_ICO_ROUTE:
        return _FAVICON_ICO_MIME, _favicon_ico()
    return None
