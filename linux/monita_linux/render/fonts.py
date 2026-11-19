"""找字体：命令行参数 → fc-match → 常见路径候选。代码里不写死 Mac 字体路径（DESIGN.md §3.7）。

Linux 默认 Liberation Sans / Liberation Mono（和 Helvetica / Courier New 等宽）+ Noto Sans CJK SC。
Pillow 没有字体回落，所以 Canvas 按字符选字体：CJK（以及拉丁字体里没有的字形）用 CJK 字体，其余用拉丁字体。
"""
import os
import shutil
import subprocess

# (角色, 粗体) -> fc-match 查询
QUERIES = {
    ("sans", False): "Liberation Sans", ("sans", True): "Liberation Sans:bold",
    ("mono", False): "Liberation Mono", ("mono", True): "Liberation Mono:bold",
    ("cjk", False): "Noto Sans CJK SC:lang=zh-cn", ("cjk", True): "Noto Sans CJK SC:lang=zh-cn:bold",
}
# 没有 fc-match 时按顺序找（Debian / Fedora / Arch 的常见位置）
CANDIDATES = {
    "sans": ["/usr/share/fonts/truetype/liberation/LiberationSans-%s.ttf", "/usr/share/fonts/liberation-sans/LiberationSans-%s.ttf",
             "/usr/share/fonts/liberation/LiberationSans-%s.ttf", "/usr/share/fonts/TTF/LiberationSans-%s.ttf"],
    "mono": ["/usr/share/fonts/truetype/liberation/LiberationMono-%s.ttf", "/usr/share/fonts/liberation-mono/LiberationMono-%s.ttf",
             "/usr/share/fonts/liberation/LiberationMono-%s.ttf", "/usr/share/fonts/TTF/LiberationMono-%s.ttf"],
    "cjk": ["/usr/share/fonts/opentype/noto/NotoSansCJK-%s.ttc", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-%s.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-%s.ttc", "/usr/share/fonts/OTF/NotoSansCJK-%s.ttc"],
}


class FontError(RuntimeError):
    pass


def fc_match(query):
    """-> (路径, ttc 里的序号) 或 None"""
    exe = shutil.which("fc-match")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-f", "%{file}|%{index}", query], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    path, _, idx = out.partition("|")
    return (path, int(idx or 0)) if path and os.path.exists(path) else None


def has_glyph(path, index, ch, px=24):
    """字体里有没有 ch 的字形：画出来和缺字形时的 .notdef 不一样就算有"""
    from PIL import ImageFont
    f = ImageFont.truetype(path, px, index=index)
    m, miss = f.getmask(ch), f.getmask("\U0010fffd")
    return (m.size, bytes(m)) != (miss.size, bytes(miss))


def find_fonts(sans=None, mono=None, cjk=None, match=fc_match, exists=os.path.exists):
    """-> {(角色, 粗体): (路径, 序号)}。参数给了路径就两种粗细都用它；缺哪个都报清楚的错"""
    given = {"sans": sans, "mono": mono, "cjk": cjk}
    out, missing = {}, []
    for (role, bold), q in QUERIES.items():
        if given[role]:
            out[(role, bold)] = (given[role], 0)
            continue
        hit = match(q)
        if hit is None:
            for pat in CANDIDATES[role]:
                p = pat % ("Bold" if bold else "Regular")
                if exists(p):
                    hit = (p, 0)
                    break
        if hit is None:
            missing.append(q)
        else:
            out[(role, bold)] = hit
    for bold in (False, True):                  # fc-match 总会给个"最接近的"：没装 CJK 字体时会落到纯拉丁字体，中文全成豆腐块
        hit = out.get(("cjk", bold))
        if hit and not has_glyph(hit[0], hit[1], "中"):
            missing.append("%s（%s 里没有中文字形）" % (QUERIES[("cjk", bold)], os.path.basename(hit[0])))
    if missing:
        raise FontError("找不到字体（CJK 要能显示中文）：%s。装 fonts-liberation + fonts-noto-cjk（或 liberation-fonts / noto-fonts-cjk），"
                        "或者用 --font-sans / --font-mono / --font-cjk 指定路径" % "、".join(missing))
    return out
