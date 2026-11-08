import json
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import bridge


def status(identifier="100", **changes):
    value = {
        "id": identifier,
        "url": f"https://example.social/@alice/{identifier}",
        "uri": f"https://example.social/users/alice/statuses/{identifier}",
        "created_at": "2026-11-07T12:00:00Z",
        "account": {"id": "7", "username": "alice", "acct": "alice", "display_name": "Alice", "url": "https://example.social/@alice"},
        "visibility": "public",
        "content": "<p>Hello world</p>",
        "in_reply_to_account_id": None,
        "media_attachments": [],
    }
    value.update(changes)
    return value


def native_feed(*identifiers):
    items = "".join(f"<item><link>https://example.social/@alice/{identifier}</link></item>" for identifier in identifiers)
    return ET.fromstring(f"<rss><channel>{items}</channel></rss>")


class FakeClient:
    def __init__(self, pages=(), feed=None):
        self.pages = iter(pages)
        self.feed = native_feed() if feed is None else feed
        self.urls = []
        self.responses = {}

    def page(self, url):
        self.urls.append(url)
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result

    def xml(self, url):
        return self.feed

    def json(self, url):
        self.urls.append(url)
        return self.responses[url]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.account = bridge.Account("alice", "example.social", "7", "Alice")

    def test_media_only_post_keeps_image_description_and_readable_title(self):
        value = status(content="", media_attachments=[{"type": "image", "url": "https://media.test/a.png", "description": 'A <cat> & "dog"'}])
        page = bridge.render_page({"host": "example.social", "status": value}, "https://feeds.test/project")
        self.assertIn('src="https://media.test/a.png"', page)
        self.assertIn('alt="A &lt;cat&gt; &amp; &quot;dog&quot;"', page)
        self.assertIn("Post with media, poll or quote", page)

    def test_poll_only_post_keeps_poll_options(self):
        page = bridge.render_status(status(content="", poll={"options": [{"title": "Tea < coffee"}]}))
        self.assertIn("Tea &lt; coffee", page)

    def test_content_warning_and_nested_quote_are_readable(self):
        quoted = status("99", content="<p>Quoted words</p>")
        page = bridge.render_status(status(spoiler_text="CW <strong>", quote={"quoted_status": quoted}))
        self.assertIn("CW &lt;strong&gt;", page)
        self.assertIn("Quoted words", page)

    def test_boost_renderer_keeps_boosted_content_when_present(self):
        page = bridge.render_status(status(content="", reblog=status("99", content="<p>Shared words</p>")))
        self.assertIn("Shared words", page)

    def test_hosted_article_identity_survives_title_changes(self):
        before = {"host": "example.social", "status": status()}
        after = {"host": "example.social", "status": status(content="<p>Edited text</p>")}
        state = {"account": {"user": "alice", "host": "example.social", "name": "Alice"}}
        first = ET.fromstring(bridge.render_feed("alice", state, [before], "https://feeds.test/project"))
        second = ET.fromstring(bridge.render_feed("alice", state, [after], "https://feeds.test/project"))
        self.assertEqual(first.findtext("./channel/item/guid"), second.findtext("./channel/item/guid"))
        self.assertEqual(first.findtext("./channel/item/link"), "https://feeds.test/project/posts/example.social/100.html")
        self.assertEqual(bridge.post_path(before), bridge.post_path(after))

    def test_hosted_page_canonical_points_to_hosted_page(self):
        page = bridge.render_page({"host": "example.social", "status": status()}, "https://feeds.test/project")
        self.assertIn('rel="canonical" href="https://feeds.test/project/posts/example.social/100.html"', page)
        self.assertIn('href="https://example.social/@alice/100">Original Mastodon post', page)

    def test_native_selection_maps_both_public_status_url_formats(self):
        feed = ET.fromstring("<rss><channel><item><link>https://example.social/users/alice/statuses/100</link></item><item><link>https://example.social/@alice/101</link></item></channel></rss>")
        self.assertEqual(bridge.native_members(feed), [("example.social", "100"), ("example.social", "101")])

    def test_status_mapping_rejects_another_account(self):
        value = status()
        value["account"]["id"] = "8"
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_status(value, self.account, "100")

    def test_self_reply_is_retained_while_reply_to_another_account_is_rejected(self):
        bridge.validate_status(status(in_reply_to_account_id="7"), self.account, "100")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_status(status(in_reply_to_account_id="8"), self.account, "100")

    def test_pagination_retains_lower_bound_across_all_pages(self):
        link = '<https://example.social/api/v1/accounts/7/statuses?max_id=103>; rel="next"'
        client = FakeClient([([status("104"), status("103")], link), ([status("102"), status("101")], ""), ([], "")])
        posts = bridge.catch_up(client, self.account, "100")
        self.assertEqual(set(posts), {"101", "102", "103", "104"})
        for url in client.urls:
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["since_id"], ["100"])

    def test_failed_second_page_keeps_published_files_and_checkpoint(self):
        previous = {"sources": {self.account.key: "100"}, "posts": {"example.social/100": {"host": "example.social", "status": status()}}, "account": {"user": "alice", "host": "example.social", "name": "Alice"}}
        client = FakeClient([([status("102")], ""), OSError("connection lost")])
        client.responses["https://example.social/api/v1/accounts/lookup?acct=alice"] = dict(status()["account"], moved=None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge.generate(root, "https://feeds.test", {"alice": previous})
            before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            health = bridge.update(root, "https://feeds.test", client, (("alice", "example.social"),))
            after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            for name, data in before.items():
                self.assertEqual(data, after[name], name)
            self.assertFalse(health["sources"]["alice"]["ok"])
            self.assertEqual(json.loads(after["cache/alice.json"])["sources"], previous["sources"])

    def test_one_failed_source_does_not_block_a_healthy_source(self):
        client = FakeClient()
        client.responses["https://example.social/api/v1/accounts/lookup?acct=bob"] = dict(status()["account"], id="8", username="bob", url="https://example.social/@bob", moved=None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            health = bridge.update(root, "https://feeds.test", client, (("alice", "example.social"), ("bob", "example.social")))
            self.assertFalse(health["sources"]["alice"]["ok"])
            self.assertTrue(health["sources"]["bob"]["ok"])
            self.assertTrue((root / "docs/bob.xml").exists())
            self.assertTrue((root / "cache/bob.json").exists())

    def test_current_native_post_is_refreshed_without_changing_its_identity(self):
        previous = {"sources": {self.account.key: "100"}, "posts": {"example.social/100": {"host": "example.social", "status": status()}}}
        client = FakeClient([( [], "")], native_feed("100"))
        client.responses["https://example.social/api/v1/statuses/100"] = status(content="<p>Edited words</p>")
        state = bridge.collect(previous, self.account, client)
        self.assertEqual(state["posts"]["example.social/100"]["status"]["content"], "<p>Edited words</p>")
        self.assertEqual(state["sources"][self.account.key], "100")

    def test_feed_author_and_page_language_preserve_the_source_metadata(self):
        value = status(language="ja")
        value["account"]["display_name"] = "まつもと"
        post = {"host": "example.social", "status": value}
        state = {"account": {"user": "alice", "host": "example.social", "name": "まつもと"}}
        feed = ET.fromstring(bridge.render_feed("alice", state, [post], "https://feeds.test"))
        self.assertEqual(feed.findtext(f"./channel/item/{{{bridge.CREATOR_NAMESPACE}}}creator"), "まつもと")
        self.assertIn('<html lang="ja">', bridge.render_page(post, "https://feeds.test"))

    def test_bootstrap_imports_only_native_members_without_account_history(self):
        client = FakeClient(feed=native_feed("100"))
        client.responses["https://example.social/api/v1/statuses/100"] = status()
        previous = {"sources": {}, "posts": {}}
        state = bridge.collect(previous, self.account, client)
        self.assertEqual(state["sources"], {self.account.key: "100"})
        self.assertEqual(set(state["posts"]), {"example.social/100"})
        self.assertEqual(previous, {"sources": {}, "posts": {}})
        self.assertFalse(any("/accounts/" in url for url in client.urls))

    def test_empty_native_feed_is_a_valid_empty_publication(self):
        state = bridge.collect({"sources": {}, "posts": {}}, self.account, FakeClient())
        self.assertEqual(state["posts"], {})
        self.assertEqual(state["sources"], {})

    def test_verified_move_keeps_a_separate_source_checkpoint(self):
        client = FakeClient()
        origin = dict(status()["account"], moved={"url": "https://new.social/@alice"})
        target = dict(status()["account"], id="88", url="https://new.social/@alice", moved=None)
        client.responses["https://example.social/api/v1/accounts/lookup?acct=alice"] = origin
        client.responses["https://new.social/api/v1/accounts/lookup?acct=alice"] = target
        moved = bridge.resolve_account(client, "alice", "example.social")
        state = bridge.collect({"sources": {self.account.key: "100"}, "posts": {}}, moved, client)
        self.assertEqual(moved.key, "alice@new.social")
        self.assertEqual(state["sources"], {self.account.key: "100"})


if __name__ == "__main__":
    unittest.main()
