import copy
import html
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path


ACCOUNTS = (
    ("rms", "mastodon.xyz"),
    ("bagder", "mastodon.social"),
    ("matz", "ruby.social"),
    ("tenderlove", "mastodon.social"),
    ("mitchellh", "hachyderm.io"),
    ("robpike", "hachyderm.io"),
    ("rsc", "hachyderm.io"),
    ("dhh", "mastodon.social"),
)
CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
CREATOR_NAMESPACE = "http://purl.org/dc/elements/1.1/"
ET.register_namespace("content", CONTENT_NAMESPACE)
ET.register_namespace("dc", CREATOR_NAMESPACE)


class BridgeError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    user: str
    host: str
    identifier: str
    name: str

    @property
    def key(self):
        return f"{self.user}@{self.host}"

    @property
    def profile(self):
        return f"https://{self.host}/@{self.user}"


def main():
    root = Path(__file__).resolve().parent
    try:
        base_url = parse_base_url(os.environ.get("BASE_URL", ""))
        health = update(root, base_url, PublicClient())
        for source, result in health["sources"].items():
            if not result["ok"]:
                print(f'::warning title=Source refresh failed::{source}: {result["error"]}', file=sys.stderr)
    except (BridgeError, OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
        print(f"Feed generation failed; no checkpoint advanced: {error}", file=sys.stderr)
        return 1
    return 0


def parse_base_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise BridgeError("BASE_URL must be the public HTTPS site URL, without query or fragment")
    return value.rstrip("/")


def update(root, base_url, client, accounts=ACCOUNTS):
    states = {}
    checked_on = datetime.now(timezone.utc).date().isoformat()
    health = {"checked_on": checked_on, "sources": {}}
    for user, host in accounts:
        cache_path = root / "cache" / f"{user}.json"
        previous = json.loads(cache_path.read_text()) if cache_path.exists() else {"sources": {}, "posts": {}}
        try:
            state = collect(previous, resolve_account(client, user, host), client)
            state["last_success"] = checked_on
            states[user] = state
            health["sources"][user] = {"ok": True, "last_success": checked_on, "posts": len(state["posts"])}
            print(f"{user}: {len(state['posts'])} retained posts", flush=True)
        except (BridgeError, OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
            if "account" in previous:
                states[user] = previous
            health["sources"][user] = {"ok": False, "last_success": previous.get("last_success"), "error": str(error)}
            print(f"{user}: previous publication retained after {type(error).__name__}", flush=True)
    with tempfile.TemporaryDirectory(prefix=".bridge-", dir=root) as temporary:
        stage = Path(temporary)
        generate(stage, base_url, states)
        (stage / "docs/health.json").write_text(json.dumps(health, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        retained_paths = set()
        for slug, result in health["sources"].items():
            if result["ok"] or slug not in states:
                continue
            retained_paths.update((f"cache/{slug}.json", f"docs/{slug}.xml"))
            retained_paths.update("docs/" + post_path(post) for post in states[slug]["posts"].values())
        for directory in ("docs", "cache"):
            for source in sorted((stage / directory).rglob("*")):
                if not source.is_file():
                    continue
                destination = root / source.relative_to(stage)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.relative_to(stage).as_posix() in retained_paths and destination.exists():
                    continue
                if not destination.exists() or source.read_bytes() != destination.read_bytes():
                    os.replace(source, destination)
    return health


def resolve_account(client, user, host):
    visited = set()
    while (user, host) not in visited:
        visited.add((user, host))
        data = client.json(f"https://{host}/api/v1/accounts/lookup?" + urllib.parse.urlencode({"acct": user}))
        profile = urllib.parse.urlsplit(data["url"])
        if profile.hostname != host or data["username"].lower() != user.lower():
            raise BridgeError(f"Account lookup did not match {user}@{host}")
        moved = data.get("moved")
        if not moved:
            return Account(user, host, parse_id(data["id"]), data.get("display_name") or user)
        target = urllib.parse.urlsplit(moved["url"])
        match = re.fullmatch(r"/@([^/]+)/?", target.path)
        if target.scheme != "https" or not target.hostname or not match:
            raise BridgeError(f"Unsupported moved-account address: {moved['url']}")
        user, host = match.group(1), target.hostname
    raise BridgeError("Moved-account lookup contains a cycle")


def collect(previous, account, client):
    state = copy.deepcopy(previous)
    selected = native_members(client.xml(account.profile + ".rss"))[:20]
    checkpoint = state["sources"].get(account.key)
    statuses = {}
    if checkpoint:
        statuses.update(catch_up(client, account, checkpoint))
    for address in selected:
        host, identifier = address
        if host != account.host:
            raise BridgeError(f"Native RSS contains an unexpected host: {host}")
        if identifier in statuses:
            continue
        status = client.json(f"https://{host}/api/v1/statuses/{identifier}")
        validate_status(status, account, identifier)
        statuses[identifier] = status
    for identifier, status in statuses.items():
        state["posts"][post_key(account.host, identifier)] = {"host": account.host, "status": status}
    identifiers = list(statuses)
    if checkpoint:
        identifiers.append(checkpoint)
    if identifiers:
        state["sources"][account.key] = max(identifiers, key=int)
    state["account"] = {"user": account.user, "host": account.host, "name": account.name}
    return state


def native_members(feed):
    if feed.find("channel") is None:
        raise BridgeError("Native RSS response has no channel")
    addresses = []
    for item in feed.findall("./channel/item"):
        url = item.findtext("link")
        parsed = urllib.parse.urlsplit(url or "")
        match = re.fullmatch(r"/(?:@[^/]+/|users/[^/]+/statuses/)(\d+)/?", parsed.path)
        if parsed.scheme != "https" or not parsed.hostname or not match:
            raise BridgeError(f"Native RSS item has no supported status URL: {url}")
        addresses.append((parsed.hostname, parse_id(match.group(1))))
    return list(dict.fromkeys(addresses))


def catch_up(client, account, checkpoint):
    path = f"/api/v1/accounts/{account.identifier}/statuses"
    parameters = {"since_id": checkpoint, "limit": "40", "exclude_replies": "true", "exclude_reblogs": "true"}
    url = f"https://{account.host}{path}?" + urllib.parse.urlencode(parameters)
    visited = set()
    statuses = {}
    while url:
        if url in visited:
            raise BridgeError(f"Pagination repeated a page for {account.key}")
        visited.add(url)
        page, links = client.page(url)
        if not isinstance(page, list):
            raise BridgeError(f"Account statuses did not return a list for {account.key}")
        if not page:
            break
        for status in page:
            identifier = parse_id(status["id"])
            validate_status(status, account, identifier)
            if int(identifier) > int(checkpoint):
                statuses[identifier] = status
        oldest = min((parse_id(status["id"]) for status in page), key=int)
        if int(oldest) <= int(checkpoint):
            break
        next_url = next_link(links)
        if next_url:
            parsed = urllib.parse.urlsplit(next_url)
            if parsed.scheme != "https" or parsed.hostname != account.host or parsed.path != path:
                raise BridgeError(f"Unexpected pagination destination for {account.key}")
            query = dict(urllib.parse.parse_qsl(parsed.query))
            query.update(parameters)
            url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
        else:
            query = dict(parameters, max_id=oldest)
            url = f"https://{account.host}{path}?" + urllib.parse.urlencode(query)
    return statuses


def validate_status(status, account, identifier):
    if parse_id(status["id"]) != identifier or status["account"]["id"] != account.identifier:
        raise BridgeError(f"Status {identifier} did not match {account.key}")
    if status.get("visibility") not in ("public", "unlisted"):
        raise BridgeError(f"Status {identifier} is not public content")
    if status.get("reblog") or status.get("in_reply_to_account_id") not in (None, account.identifier):
        raise BridgeError(f"Status {identifier} does not match native RSS membership")
    datetime.fromisoformat(status["created_at"].replace("Z", "+00:00"))


def parse_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        raise BridgeError("Expected a numeric Mastodon identifier")
    return value


def post_key(host, identifier):
    return f"{host}/{parse_id(identifier)}"


def next_link(value):
    for part in value.split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


class PublicClient:
    def request(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "mastodon-reader-feeds/1.0", "Accept": "application/json, application/rss+xml"})
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read(), response.headers.get("Link", "")

    def json(self, url):
        return json.loads(self.request(url)[0])

    def xml(self, url):
        return ET.fromstring(self.request(url)[0])

    def page(self, url):
        data, links = self.request(url)
        return json.loads(data), links


def generate(root, base_url, states):
    docs = root / "docs"
    cache = root / "cache"
    docs.mkdir(parents=True)
    cache.mkdir()
    links = []
    for slug, state in states.items():
        posts = sorted(state["posts"].values(), key=lambda item: (item["status"]["created_at"], item["host"], int(item["status"]["id"])), reverse=True)
        for post in posts:
            relative = post_path(post)
            output = docs / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_page(post, base_url), encoding="utf-8")
        (docs / f"{slug}.xml").write_bytes(render_feed(slug, state, posts, base_url))
        account = state["account"]
        links.append(f'<li><a href="{html.escape(base_url)}/{slug}.xml">{html.escape(slug)} RSS</a> · {html.escape(account["user"])}@{html.escape(account["host"])} · {len(posts)} posts</li>')
        (cache / f"{slug}.json").write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    index = '<h1>Mastodon feeds for Reader</h1><p>Readable copies of public posts. Subscribe to an RSS link below.</p><ul>' + "".join(links) + "</ul>"
    (docs / "index.html").write_text(document("Mastodon feeds for Reader", index, base_url + "/"), encoding="utf-8")
    (docs / ".nojekyll").write_text("", encoding="utf-8")


def post_path(post):
    return f'posts/{post["host"]}/{parse_id(post["status"]["id"])}.html'


def render_page(post, base_url):
    status = post["status"]
    title = readable_title(status)
    body = f'<article class="h-entry"><h1 class="p-name">{html.escape(title)}</h1>{render_status(status)}</article>'
    return document(title, body, base_url + "/" + post_path(post), status)


def readable_title(status):
    name = status["account"].get("display_name") or status["account"]["acct"]
    content = status.get("content") or (status.get("reblog") or {}).get("content") or ""
    text = re.sub(r"https?://\S+", "", PlainText(content).text()).strip()
    if not text:
        text = status.get("spoiler_text") or (status.get("card") or {}).get("title") or "Post with media, poll or quote"
    if len(text) > 110:
        text = text[:107].rsplit(" ", 1)[0] + "…"
    return f'{name} · {status["created_at"][:10]} — {text}'


class PlainText(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.parts = []
        self.feed(markup)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("p", "br", "div"):
            self.parts.append(" ")

    def text(self):
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


def render_status(status, visited=None):
    visited = set() if visited is None else visited
    identity = status.get("url") or status["uri"]
    if identity in visited:
        return f'<p><a href="{html.escape(identity, quote=True)}">Original post</a></p>'
    visited.add(identity)
    account = status["account"]
    name = account.get("display_name") or account["acct"]
    date = status["created_at"]
    parts = [f'<p class="byline"><a class="p-author" href="{html.escape(account["url"], quote=True)}">{html.escape(name)}</a> · <time class="dt-published" datetime="{html.escape(date, quote=True)}">{html.escape(date)}</time></p>']
    if status.get("spoiler_text"):
        parts.append(f'<p><strong>Content warning: {html.escape(status["spoiler_text"])}</strong></p>')
    parts.append(status.get("content") or "")
    if status.get("sensitive"):
        parts.append("<p><em>The original marks its media as sensitive.</em></p>")
    for media in status.get("media_attachments", []):
        url = media.get("url") or media.get("remote_url")
        if not url:
            continue
        escaped_url = html.escape(url, quote=True)
        description = media.get("description") or media.get("type") or "Media"
        escaped_description = html.escape(description, quote=True)
        if media.get("type") == "image":
            parts.append(f'<figure><img src="{escaped_url}" alt="{escaped_description}"><figcaption>{html.escape(description)}</figcaption></figure>')
        elif media.get("preview_url"):
            preview = html.escape(media["preview_url"], quote=True)
            parts.append(f'<p><a href="{escaped_url}"><img src="{preview}" alt="{escaped_description}"></a></p>')
        parts.append(f'<p><a href="{escaped_url}">{html.escape(description)}</a></p>')
    poll = status.get("poll")
    if poll:
        options = "".join(f'<li>{html.escape(option["title"])}</li>' for option in poll.get("options", []))
        parts.append(f"<p>Poll options (vote on the original):</p><ul>{options}</ul>")
    if status.get("reblog"):
        parts.append("<h2>Boosted post</h2>" + render_status(status["reblog"], visited))
    quote = status.get("quote") or {}
    if quote.get("quoted_status"):
        parts.append("<h2>Quoted post</h2>" + render_status(quote["quoted_status"], visited))
    elif quote:
        parts.append("<p>Quoted content is unavailable. See the original post.</p>")
    card = status.get("card") or {}
    if card.get("url"):
        parts.append(f'<p><a href="{html.escape(card["url"], quote=True)}">{html.escape(card.get("title") or card["url"])}</a></p>')
    parts.append(f'<footer><p><a href="{html.escape(identity, quote=True)}">Original Mastodon post</a></p></footer>')
    return "\n".join(parts)


def document(title, body, canonical, status=None):
    metadata = ""
    language = html.escape((status or {}).get("language") or "en", quote=True)
    if status:
        author = status["account"].get("display_name") or status["account"]["acct"]
        metadata = f'<meta name="author" content="{html.escape(author, quote=True)}"><meta property="article:published_time" content="{html.escape(status["created_at"], quote=True)}">'
    return f'<!doctype html><html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title><link rel="canonical" href="{html.escape(canonical, quote=True)}"><meta property="og:type" content="article">{metadata}<style>body{{font:18px/1.6 system-ui,sans-serif;max-width:760px;margin:3rem auto;padding:0 1rem;color:#20252b}}h1{{font-size:1.6rem;line-height:1.3}}img,video{{max-width:100%;height:auto}}a{{overflow-wrap:anywhere}}figure{{margin:1.5rem 0}}figcaption,.byline,footer{{font-size:.9rem;color:#505966}}.invisible{{display:none}}blockquote{{border-left:3px solid #bbb;padding-left:1rem}}@media(prefers-color-scheme:dark){{body{{background:#171b20;color:#e9edf2}}a{{color:#8dbfff}}figcaption,.byline,footer{{color:#c3cbd4}}}}</style></head><body><main>{body}</main></body></html>\n'


def render_feed(slug, state, posts, base_url):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    account = state["account"]
    ET.SubElement(channel, "title").text = f'{account["name"]} · Mastodon readable feed'
    ET.SubElement(channel, "link").text = base_url + "/"
    ET.SubElement(channel, "description").text = f'Readable public posts by {account["user"]}@{account["host"]}'
    for post in posts:
        status = post["status"]
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = readable_title(status)
        ET.SubElement(item, f"{{{CREATOR_NAMESPACE}}}creator").text = status["account"].get("display_name") or status["account"]["acct"]
        ET.SubElement(item, "link").text = base_url + "/" + post_path(post)
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f'urn:mastodon:{post["host"]}:{status["id"]}'
        ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromisoformat(status["created_at"].replace("Z", "+00:00")))
        ET.SubElement(item, "description").text = render_status(status)
        ET.SubElement(item, f"{{{CONTENT_NAMESPACE}}}encoded").text = render_status(status)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


if __name__ == "__main__":
    sys.exit(main())
