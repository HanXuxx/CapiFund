"""只读网页：逻辑列表、record 列表、逻辑时间线与待办；不提供任何写入口。"""
from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from markdown_it import MarkdownIt
from starlette.responses import HTMLResponse, RedirectResponse

from ..domain.kinds import THESIS_STATUSES
from ..domain.records import TYPES
from ..errors import RecordError

MARKDOWN = MarkdownIt("commonmark", {"html": False})
PAGE_SIZE = 50
KIND_LABELS = {"checkpoint": "验证节点", "evidence": "证据"}
THESIS_LABELS = {"draft": "草稿", "active": "跟踪中", "closed": "已关闭"}


def layout(title, content, status=200):
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · CapiFund</title>
<link rel="stylesheet" href="/static/fund.css"></head><body>
<header><a class="brand" href="/">CapiFund</a><nav><a href="/">投资逻辑</a><a href="/agenda">待办</a><a href="/records">全部记录</a></nav></header>
<main>{content}</main></body></html>"""
    return HTMLResponse(page, status_code=status, headers={"Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:"})


def badge(text, css):
    return f'<span class="badge {escape(css)}">{escape(text)}</span>'


def badges(metadata):
    custom, parts = metadata.get("custom", {}), []
    if metadata["type"] == "thesis":
        status = custom.get("thesis_status", "")
        parts.append(badge(THESIS_LABELS.get(status, status), "thesis-" + status))
        if custom.get("close_reason"):
            parts.append(badge(custom["close_reason"], "muted"))
    elif custom.get("kind") in KIND_LABELS:
        kind = custom["kind"]
        parts.append(badge(KIND_LABELS[kind], kind))
        if kind == "checkpoint":
            parts.append(badge(f'{custom.get("status", "")} · {custom.get("due_date", "")}', "checkpoint-" + str(custom.get("status", ""))))
        else:
            parts.append(badge(str(custom.get("stance", "")), "stance-" + str(custom.get("stance", ""))))
    else:
        parts.append(badge(metadata["type"], "muted"))
    parts.extend(badge(tag, "tag") for tag in metadata.get("tags", []))
    return " ".join(parts)


def byline(metadata, revision):
    return (f'<p class="byline">{escape(metadata["author"])} · {escape(metadata.get("runtime", ""))} · '
            f'{escape(metadata["created_at"][:16].replace("T", " "))} · 修订 {revision}</p>')


def record_row(item):
    metadata = item["metadata"]
    target = metadata.get("topic_id") or metadata["id"]
    anchor = f'#floor-{metadata["floor"]}' if metadata.get("floor") else ""
    return (f'<li><a href="/records/{escape(target)}{anchor}">{escape(metadata["title"])}</a> {badges(metadata)}'
            f'{byline(metadata, item["revision"])}</li>')


def pager(path, arguments, page, total, page_size=PAGE_SIZE):
    links = []
    if page > 1:
        links.append(f'<a href="{path}?{escape(urlencode({**arguments, "page": page - 1}))}">上一页</a>')
    if page * page_size < total:
        links.append(f'<a href="{path}?{escape(urlencode({**arguments, "page": page + 1}))}">下一页</a>')
    return f'<p class="pager">共 {total} 条 {" ".join(links)}</p>'


def page_number(request):
    value = request.query_params.get("page", "1")
    if not value.isdigit() or int(value) < 1:
        raise RecordError("page must be a positive integer")
    return int(value)


class WebEndpoints:
    def __init__(self, access):
        self.access = access

    async def theses(self, request):
        status = request.query_params.get("status") or None
        try:
            page = page_number(request)
            custom = {"thesis_status": status} if status else None
            result = await self.access.call("query", {"type": "thesis", "custom": custom, "sort": "activity",
                                                      "page": page, "page_size": PAGE_SIZE})
        except RecordError as exc:
            return layout("错误", f"<p>{escape(str(exc))}</p>", getattr(exc, "status", 400))
        filters = " ".join(f'<a href="/?status={value}"{" class=active" if value == status else ""}>{THESIS_LABELS[value]}</a>'
                           for value in THESIS_STATUSES)
        items = "".join(record_row(item) for item in result["items"]) or "<li>暂无</li>"
        return layout("投资逻辑", f'<h1>投资逻辑</h1><p class="filters"><a href="/">全部</a> {filters}</p>'
                                 f'<ul class="records">{items}</ul>{pager("/", {"status": status} if status else {}, page, result["total"])}')

    async def records(self, request):
        arguments = {key: request.query_params[key] for key in ("type", "tag", "keyword") if request.query_params.get(key)}
        try:
            page = page_number(request)
            result = await self.access.call("query", {**arguments, "page": page, "page_size": PAGE_SIZE})
        except RecordError as exc:
            return layout("错误", f"<p>{escape(str(exc))}</p>", getattr(exc, "status", 400))
        types = " ".join(f'<a href="/records?type={value}">{value}</a>' for value in TYPES)
        form = (f'<form method="get" action="/records"><input name="keyword" value="{escape(arguments.get("keyword", ""))}" '
                f'placeholder="关键词"><button>搜索</button></form>')
        items = "".join(record_row(item) for item in result["items"]) or "<li>暂无</li>"
        return layout("全部记录", f'<h1>全部记录</h1><p class="filters"><a href="/records">全部</a> {types}</p>{form}'
                                 f'<ul class="records">{items}</ul>{pager("/records", arguments, page, result["total"])}')

    async def topic(self, request):
        record_id = request.path_params["record_id"]
        try:
            page = page_number(request)
            result = await self.access.call("topic", {"record_id": record_id, "page": page, "page_size": 200})
        except RecordError as exc:
            return layout("错误", f"<p>{escape(str(exc))}</p>", getattr(exc, "status", 400))
        topic = result["topic"]
        if topic["metadata"]["id"] != record_id:
            return RedirectResponse(f'/records/{topic["metadata"]["id"]}', status_code=303)
        metadata = topic["metadata"]
        sections = [f'<article class="topic"><h1>{escape(metadata["title"])}</h1><p>{badges(metadata)}</p>'
                    f'{byline(metadata, topic["revision"])}{self._references(metadata)}'
                    f'<div class="body">{MARKDOWN.render(topic["body"])}</div></article>']
        for reply in result["replies"]:
            reply_meta = reply["metadata"]
            target = "" if reply_meta["reply_to_id"] == metadata["id"] else " · 回复其他楼层"
            sections.append(f'<article class="reply" id="floor-{reply_meta["floor"]}"><h2>#{reply_meta["floor"]} '
                            f'{escape(reply_meta["title"])}</h2><p>{badges(reply_meta)}{target}</p>'
                            f'{byline(reply_meta, reply["revision"])}{self._references(reply_meta)}'
                            f'<div class="body">{MARKDOWN.render(reply["body"])}</div></article>')
        if result["total"] > 200:
            sections.append(pager(f'/records/{escape(record_id)}', {}, page, result["total"], 200))
        return layout(metadata["title"], "".join(sections))

    @staticmethod
    def _references(metadata):
        if not metadata.get("reference"):
            return ""
        links = ", ".join(f'<a href="/records/{escape(value)}">{escape(value)}</a>' for value in metadata["reference"])
        return f'<p class="references">引用：{links}</p>'

    async def agenda(self, request):
        try:
            result = await self.access.call("agenda", {"as_of": request.query_params.get("as_of") or None})
        except RecordError as exc:
            return layout("错误", f"<p>{escape(str(exc))}</p>", getattr(exc, "status", 400))
        due = "".join(
            f'<li><a href="/records/{escape(item["metadata"]["topic_id"])}#floor-{item["metadata"]["floor"]}">'
            f'{escape(item["metadata"]["title"])}</a> {badges(item["metadata"])}'
            f'<p class="byline">所属逻辑：{escape(item["thesis"]["title"])}</p></li>'
            for item in result["due_checkpoints"]["items"]) or "<li>暂无</li>"
        review = "".join(
            f'<li><a href="/records/{escape(item["metadata"]["id"])}">{escape(item["metadata"]["title"])}</a> '
            f'{badges(item["metadata"])}<p class="byline">新变动 {item["new_evidence"]} 条，最近 '
            f'{escape(item["latest_evidence_at"][:16].replace("T", " "))}</p></li>'
            for item in result["theses_with_new_evidence"]["items"]) or "<li>暂无</li>"
        unmonitored = "".join(
            record_row(item) for item in result["theses_without_pending_checkpoints"]["items"]) or "<li>暂无</li>"
        return layout("待办", f'<h1>待办（截至 {escape(result["cutoff"])}）</h1>'
                             f'<h2>到期验证节点（{result["due_checkpoints"]["total"]}）</h2><ul class="records">{due}</ul>'
                             f'<h2>有新证据或已判定节点待复核的逻辑（{result["theses_with_new_evidence"]["total"]}）</h2>'
                             f'<ul class="records">{review}</ul>'
                             f'<h2>缺少待验证节点的跟踪中逻辑（{result["theses_without_pending_checkpoints"]["total"]}）</h2>'
                             f'<ul class="records">{unmonitored}</ul>')
