#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建静态书籍阅读网站。

流程：
1. 读取 books.json（书籍注册表）
2. 对每本书：按标题层级分章、去行首全角空格、给章内 H2/H3 注入锚点 id、
   按文件名匹配复制图片并改写引用、渲染 markdown（nl2br：单换行即换行）、
   生成 toc.json 目录数据（供前端 AJAX 渲染多级目录）
3. 生成书架页 / 书目录页 / 章节阅读页，压缩 CSS/JS，输出到 dist/
"""
import html as html_mod
import json
import os
import re
import shutil
import struct
import time
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
ASSETS_SRC = ROOT / "assets"
TEMPLATES = ROOT / "templates"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
IMG_TAG_RE = re.compile(r"<img\s+([^>]*?)/?>")

SITE_NAME = "云间书屋"
SITE_URL = "https://read.mtxh.fun"  # 站点域名（用于 canonical / sitemap / JSON-LD）

# 站点所有 URL（用于 sitemap.xml）
URLS = []


def text_excerpt(html, limit=100):
    """从渲染后的 HTML 提取纯文本摘要（去标签、压缩空白）。"""
    txt = re.sub(r"<[^>]+>", "", html)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit]

# 本次构建写出的文件（相对 dist），用于清理残留
WRITTEN = set()


def write_file(relpath, text, encoding="utf-8"):
    """写文本文件到 dist 并记录。覆盖写而非删目录重建，避免 Windows 目录锁。"""
    path = DIST / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    WRITTEN.add(relpath.replace("\\", "/"))


def copy_file(src, relpath):
    """复制文件到 dist 并记录。"""
    dst = DIST / relpath
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    WRITTEN.add(relpath.replace("\\", "/"))


def cleanup_dist():
    """删除本次未生成的多余文件/空目录（如书籍被移出 books.json），尽力而为。"""
    for p in DIST.rglob("*"):
        rel = p.relative_to(DIST).as_posix()
        if p.is_file() and rel not in WRITTEN:
            try:
                p.unlink()
            except OSError:
                pass
    for p in sorted(DIST.rglob("*"), key=lambda x: -len(x.parts)):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass


def render_template(name, **kwargs):
    """用 {{key}} 占位符渲染模板（避免与 CSS/JS 中的 $ 冲突）。"""
    kwargs.setdefault("SITE_NAME", SITE_NAME)
    kwargs.setdefault("page_class", "")
    kwargs.setdefault("meta_description", "")
    kwargs.setdefault("canonical", SITE_URL + "/")
    kwargs.setdefault("og_type", "website")
    kwargs.setdefault("og_title", SITE_NAME)
    kwargs.setdefault("og_description", "")
    kwargs.setdefault("json_ld_block", "")
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in kwargs.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def clean_html(html):
    """压缩 HTML：去注释、去标签间空白（htmlmin 会保留 script/noscript 内容）。"""
    try:
        import htmlmin
        return htmlmin.minify(
            html,
            remove_comments=True,
            remove_empty_space=True,
            remove_all_empty_space=False,
        )
    except ImportError:
        # 退回轻度清理（未安装 htmlmin 时）
        html = re.sub(r"[ \t]+\n", "\n", html)
        html = re.sub(r"\n\s*\n+", "\n", html)
        return html.strip() + "\n"


def gif_size(path):
    """从 GIF 头读取宽高（bytes 6-9, little-endian），用于防布局抖动。"""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
        if head[:6] not in (b"GIF87a", b"GIF89a") or len(head) < 10:
            return None
        w, h = struct.unpack("<HH", head[6:10])
        return (w, h)
    except OSError:
        return None


def minify_assets():
    """压缩 CSS/JS 到 dist/assets/*.min.*，缺库时退回未压缩版本。"""
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    try:
        from rcssmin import cssmin
        from rjsmin import jsmin
    except ImportError:
        print("[提示] 未安装 rcssmin/rjsmin，输出未压缩版本")
        copy_file(ASSETS_SRC / "style.css", "assets/style.min.css")
        copy_file(ASSETS_SRC / "reader.js", "assets/reader.min.js")
        return
    write_file("assets/style.min.css", cssmin((ASSETS_SRC / "style.css").read_text(encoding="utf-8")))
    write_file("assets/reader.min.js", jsmin((ASSETS_SRC / "reader.js").read_text(encoding="utf-8")))


def load_books():
    data = json.loads((ROOT / "books.json").read_text(encoding="utf-8"))
    return data["books"]


def detect_split_level(lines):
    """自动检测分章层级：全书仅 1 个 H1 且 H2 不少于 2 个 -> 按 H2 分章（如章回体）；
    否则按 H1 分章（如多篇文集、志书）。"""
    h1 = h2 = 0
    for ln in lines:
        m = HEADING_RE.match(ln)
        if not m:
            continue
        n = len(m.group(1))
        if n == 1:
            h1 += 1
        elif n == 2:
            h2 += 1
    if h1 == 1 and h2 >= 2:
        return 2
    return 1


def split_chapters(lines, split_level):
    """按指定标题层级切分章节，返回 [(标题, 正文行列表), ...]。"""
    chapters = []
    cur_title, cur_lines = None, None
    for ln in lines:
        m = HEADING_RE.match(ln)
        if m and len(m.group(1)) == split_level:
            if cur_title is not None:
                chapters.append((cur_title, cur_lines))
            cur_title = m.group(2).strip()
            cur_lines = []
        elif cur_title is not None:
            cur_lines.append(ln)
    if cur_title is not None:
        chapters.append((cur_title, cur_lines))
    return chapters


def find_image_dirs(book_dir):
    """源书目录下以 _images 结尾的图片目录。"""
    if not book_dir.is_dir():
        return []
    return [d for d in book_dir.iterdir() if d.is_dir() and d.name.endswith("_images")]


def process_chapter_lines(chap_lines):
    """去掉行首全角空格（已有缩进先去除，由 CSS text-indent 统一实现）；
    给章内 H2/H3 标题注入 {#sec-N} 锚点 id（中文标题默认 slug 为空，必须显式指定）。
    返回 (处理后的行, 小节列表)。"""
    subs = []
    n = 0
    out = []
    for ln in chap_lines:
        ln = ln.lstrip("\u3000")
        m = HEADING_RE.match(ln)
        if m and len(m.group(1)) in (2, 3):
            n += 1
            sec_id = f"sec-{n}"
            t = m.group(2).strip()
            subs.append({"level": len(m.group(1)), "id": sec_id, "title": t})
            out.append(f"{m.group(1)} {t} {{#{sec_id}}}")
        else:
            out.append(ln)
    return out, subs


def enhance_images(html, img_dims):
    """渲染后处理 <img>：加灯箱 class、懒加载、由 GIF 头注入宽高。"""

    def repl(m):
        attrs = m.group(1)
        sm = re.search(r'src="([^"]+)"', attrs)
        if not sm:
            return m.group(0)
        name = os.path.basename(sm.group(1))
        dim = img_dims.get(name)
        extra = ' class="lightbox-img" loading="lazy" decoding="async"'
        if dim:
            extra += f' width="{dim[0]}" height="{dim[1]}"'
        return f"<img{extra} {attrs}>"

    return IMG_TAG_RE.sub(repl, html)


def build_book(book):
    """构建一本书，返回 {id, title, author, chapter_count} 供书架页使用。"""
    book_id = book["id"]
    title = book["title"]
    author = book.get("author", "")

    src = ROOT / book["source"]
    if not src.exists():
        print(f"[跳过] 找不到源文件: {src}")
        return None
    lines = src.read_text(encoding="utf-8").splitlines()

    split_level = book.get("split_level") or detect_split_level(lines)
    chapters = split_chapters(lines, split_level)
    print(f"[{book_id}] {title}: 按 H{split_level} 分章 -> {len(chapters)} 章")

    book_dir = DIST / "books" / book_id
    img_out_dir = book_dir / "images"
    img_src_dirs = find_image_dirs(src.parent)
    copied_imgs = {}  # name -> (w, h) | None

    def rewrite_images(match):
        """将 md 中的图片引用改写为站点路径，并复制图片文件、记录尺寸。"""
        alt, raw = match.group(1), match.group(2)
        name = os.path.basename(raw.replace("\\", "/"))
        web = f"/books/{book_id}/images/{name}"
        if name not in copied_imgs:
            found = next((d / name for d in img_src_dirs if (d / name).exists()), None)
            if found is None:
                print(f"  [警告] 图片未找到: {raw} -> 保留原引用")
                copied_imgs[name] = None
                return match.group(0)
            img_out_dir.mkdir(parents=True, exist_ok=True)
            copy_file(found, f"books/{book_id}/images/{name}")
            copied_imgs[name] = gif_size(found)
        return f"![{alt}]({web})"

    md = markdown.Markdown(extensions=["extra", "nl2br"])

    toc_chapters = []
    toc_items = []
    prev_info = None
    for idx, (chap_title, chap_lines) in enumerate(chapters, start=1):
        proc_lines, subs = process_chapter_lines(chap_lines)
        chap_body = "\n".join(proc_lines)
        chap_body = IMG_RE.sub(rewrite_images, chap_body)
        md.reset()
        html = md.convert(chap_body)
        html = enhance_images(html, copied_imgs)
        slug = f"ch{idx:03d}.html"

        prev_link = ""
        next_link = ""
        if idx > 1:
            p_slug, p_title = prev_info
            prev_link = (f'<a class="pager-link prev" href="{p_slug}">'
                         f'<span class="pager-arrow">←</span>'
                         f'<span class="pager-text"><span class="pager-label">上一章</span>{p_title}</span></a>')
        if idx < len(chapters):
            n_slug = f"ch{idx + 1:03d}.html"
            n_title = chapters[idx][0]
            next_link = (f'<a class="pager-link next" href="{n_slug}">'
                         f'<span class="pager-text"><span class="pager-label">下一章</span>{n_title}</span>'
                         f'<span class="pager-arrow">→</span></a>')

        body = render_template(
            "chapter.html",
            book_title=title,
            book_url=f"/books/{book_id}/index.html",
            book_id=book_id,
            chapter_file=slug,
            chapter_title=chap_title,
            content=html,
            prev_link=prev_link,
            next_link=next_link,
        )

        # SEO：描述、canonical、OG、JSON-LD（面包屑 + 文章）
        canonical = f"{SITE_URL}/books/{book_id}/{slug}"
        URLS.append(canonical)
        excerpt = text_excerpt(html, 90)
        desc = (f"{chap_title} - {excerpt}" if excerpt else chap_title)[:150]
        desc = html_mod.escape(desc)
        json_ld = json.dumps([
            {
                "@context": "https://schema.org",
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "书架", "item": f"{SITE_URL}/index.html"},
                    {"@type": "ListItem", "position": 2, "name": title, "item": f"{SITE_URL}/books/{book_id}/index.html"},
                    {"@type": "ListItem", "position": 3, "name": chap_title, "item": canonical},
                ],
            },
            {
                "@context": "https://schema.org",
                "@type": "Article",
                "headline": chap_title,
                "description": excerpt,
                "inLanguage": "zh-CN",
                "mainEntityOfPage": canonical,
                "isPartOf": {"@type": "Book", "name": title},
            },
        ], ensure_ascii=False)
        page = render_template(
            "layout.html",
            page_title=f"{chap_title} - {title} - {SITE_NAME}",
            page_class="reading",
            content=body,
            meta_description=desc,
            canonical=canonical,
            og_type="article",
            og_title=chap_title,
            og_description=desc,
            json_ld_block=f'<script type="application/ld+json">{json_ld}</script>',
        )
        (book_dir).mkdir(parents=True, exist_ok=True)
        write_file(f"books/{book_id}/{slug}", clean_html(page))

        # 书目录页条目（含小节展开按钮）
        li = [f'<li class="toc-item" data-ch="{idx}">']
        if subs:
            li.append('<button class="toc-expand" type="button" aria-label="展开小节">▸</button>')
        li.append(f'<span class="toc-num">{idx}</span><a href="{slug}">{chap_title}</a>')
        if subs:
            li.append('<ul class="toc-subs" hidden></ul>')
        li.append("</li>")
        toc_items.append("".join(li))

        toc_chapters.append({"num": idx, "title": chap_title, "file": slug, "subs": subs})
        prev_info = (slug, chap_title)

    # 书目录页
    book_canonical = f"{SITE_URL}/books/{book_id}/index.html"
    URLS.append(book_canonical)
    author_json = {"@type": "Person", "name": author} if author else None
    book_ld = {
        "@context": "https://schema.org",
        "@type": "Book",
        "name": title,
        "inLanguage": "zh-CN",
        "numberOfPages": len(chapters),
    }
    if author_json:
        book_ld["author"] = author_json
    book_desc = (f"{title}，{author}，共 {len(chapters)} 章" if author else f"{title}，共 {len(chapters)} 章")
    body = render_template(
        "book.html",
        book_title=title,
        book_author=author,
        book_id=book_id,
        chapter_count=len(chapters),
        toc_items="\n".join(toc_items),
    )
    page = render_template(
        "layout.html",
        page_title=f"{title} - 目录 - {SITE_NAME}",
        content=body,
        meta_description=html_mod.escape(book_desc),
        canonical=book_canonical,
        og_type="book",
        og_title=title,
        og_description=html_mod.escape(book_desc),
        json_ld_block=f'<script type="application/ld+json">{json.dumps(book_ld, ensure_ascii=False)}</script>',
    )
    write_file(f"books/{book_id}/index.html", clean_html(page))

    # 目录数据（前端 AJAX 懒加载，渲染多级目录）
    toc = {"id": book_id, "title": title, "author": author, "chapters": toc_chapters}
    write_file(
        f"books/{book_id}/toc.json",
        json.dumps(toc, ensure_ascii=False, separators=(",", ":")),
    )

    return {
        "id": book_id,
        "title": title,
        "author": author,
        "chapter_count": len(chapters),
    }


def build_index(summaries):
    cards = []
    for s in summaries:
        cards.append(
            f'<a class="book-card" href="/books/{s["id"]}/index.html">'
            f'<h2 class="book-card-title">{s["title"]}</h2>'
            f'<p class="book-card-meta">{s["author"]} · 共 {s["chapter_count"]} 章</p>'
            f'<span class="book-card-cta">开始阅读 →</span></a>'
        )
    site_ld = {"@context": "https://schema.org", "@type": "WebSite", "name": SITE_NAME, "url": SITE_URL}
    URLS.append(f"{SITE_URL}/index.html")
    body = render_template("index.html", book_cards="\n".join(cards))
    page = render_template(
        "layout.html",
        page_title=f"书架 - {SITE_NAME}",
        content=body,
        meta_description=f"{SITE_NAME}：一个 Markdown 书籍在线阅读站（{', '.join(s['title'] for s in summaries)}）",
        canonical=f"{SITE_URL}/index.html",
        og_type="website",
        og_title=f"书架 - {SITE_NAME}",
        og_description=f"{SITE_NAME}：Markdown 书籍在线阅读",
        json_ld_block=f'<script type="application/ld+json">{json.dumps(site_ld, ensure_ascii=False)}</script>',
    )
    write_file("index.html", clean_html(page))


def build_sitemap():
    """生成 sitemap.xml 与 robots.txt。"""
    today = time.strftime("%Y-%m-%d")
    entries = []
    for u in sorted(set(URLS)):
        entries.append(
            f"  <url>\n    <loc>{html_mod.escape(u)}</loc>\n    <lastmod>{today}</lastmod>\n  </url>"
        )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    write_file("sitemap.xml", sitemap)
    robots = (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    write_file("robots.txt", robots)


def main():
    DIST.mkdir(parents=True, exist_ok=True)

    minify_assets()

    books = load_books()
    summaries = [s for s in (build_book(b) for b in books) if s]
    build_index(summaries)
    build_sitemap()
    cleanup_dist()

    file_count = sum(1 for p in DIST.rglob("*") if p.is_file())
    print(f"\n构建完成: {len(summaries)} 本书, 共 {file_count} 个文件")


if __name__ == "__main__":
    main()
