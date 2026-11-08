# Readable Mastodon feeds for Reader

This bridge publishes readable HTML copies of public Mastodon posts and RSS feeds that link to those copies. Reader receives a normal article page, with a readable title, author, date, post text and media, rather than Mastodon's application page. Each article links back to its original post.

The configured accounts are rms@mastodon.xyz, bagder@mastodon.social, matz@ruby.social, tenderlove@mastodon.social, mitchellh@hachyderm.io, robpike@hachyderm.io, rsc@hachyderm.io and dhh@mastodon.social. Verified account moves are followed automatically. Rob Pike currently resolves to robpike@cosocial.ca; his subscription remains `robpike.xml`.

## Subscribe

After deployment, open the site's index and subscribe in Reader to the desired `.xml` links. For this repository the expected index is [jeangnc.github.io/mastodon-reader-feeds](https://jeangnc.github.io/mastodon-reader-feeds/). Files are `rms.xml`, `bagder.xml`, `matz.xml`, `tenderlove.xml`, `mitchellh.xml`, `robpike.xml`, `rsc.xml` and `dhh.xml`.

This changes future imports. It does not change existing Reader documents, subscriptions, notes or highlights. Confirm a new hosted article imports correctly before removing a previous subscription in Reader.

## Publish

1. Put this project at the root of a public GitHub repository, on `main`.
2. In **Settings → Pages → Build and deployment**, choose **GitHub Actions**.
3. Run **Publish readable Mastodon feeds** manually, or wait for its schedule.

The workflow uses official GitHub Pages actions and GitHub's automatic repository token. It needs repository write access for generated cache commits. No Readwise token, Mastodon login, personal access token or stored secret is needed. The optional repository variable `BASE_URL` overrides the URL reported by GitHub Pages; set it when serving under another public HTTPS address.

To run locally, use Python 3.12 or later with only its standard library:

```sh
BASE_URL=https://jeangnc.github.io/mastodon-reader-feeds python bridge.py
python -m unittest discover -s tests -v
```

`BASE_URL` must be the deployed HTTPS site URL, including a repository subpath when applicable. It controls article links and canonical URLs. Run a local preview with `python -m http.server --directory docs` after generation.

## Retention and selection

On the first successful run for an account, only its current native RSS entries are imported, up to 20. Native Mastodon RSS excludes boosts and replies to other people, while retaining replies to the same account. The bridge uses native RSS for the current selection and the equivalent public account-status filters for pagination after its last successfully generated ID. Each source is fetched completely before updating its cache. A failed request or malformed response preserves that source's previous content and checkpoint; healthy sources can still publish. `/health.json` reports each source, and the workflow reports degraded source health after deployment.

Cached public post data lives in `cache/` and is committed alongside `docs/`. Later runs retain every published post and include every retained entry in RSS. Page paths and RSS GUIDs use the source host and status ID, so changing a title does not change an article's identity. An account move starts a new source checkpoint with its current native RSS; previously published posts remain available. A move does not trigger a historical backfill of its destination account.

The renderer includes content warnings, image descriptions, attachment links and previews, poll options, link cards, and quoted or boosted content when supplied in the status response. Status HTML comes from the public Mastodon API; plain metadata is escaped. Poll results and votes are not mirrored. Media remains hosted by its source server.

## Operating limits

The schedule requests a run at minutes 7, 27 and 47 of each hour. GitHub can delay or drop scheduled runs, and disables scheduled workflows in public repositories after 60 days without repository activity. This is a best-effort refresh, not a guaranteed delivery interval. Check the Actions page for errors. A daily health date produces a repository commit even when no posts change; RSS publication dates remain the post's original dates. This helps keep an enabled schedule active, but does not restart an already disabled workflow.

Catch-up pagination avoids the latest-20 RSS window when the public API remains accessible. Posts deleted or made inaccessible before a successful fetch cannot be recovered. If an instance blocks unauthenticated API access or a moved account cannot be verified, its feed retains its previous publication and the workflow reports failure after deploying healthy feeds. The current native RSS entries are refreshed to reflect edits; older cached copies remain archival snapshots, and deletions are not synchronized automatically. GitHub commits the complete generated site and its cache before deployment; a deployment failure leaves both available for the next deployment attempt. As the archive grows, monitor GitHub Pages and repository size limits. This project does not include an alerting service beyond workflow status.

Implementation references: [Mastodon account-status API](https://docs.joinmastodon.org/methods/accounts/#get-account-s-statuses), [native RSS selection](https://github.com/mastodon/mastodon/blob/main/app/controllers/accounts_controller.rb), [self-reply rule](https://github.com/mastodon/mastodon/blob/main/app/models/status.rb), [GitHub Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages), and [scheduled workflow limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
