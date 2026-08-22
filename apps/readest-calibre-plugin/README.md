# Readest calibre plugin

A [calibre](https://calibre-ebook.com) GUI plugin that pushes selected books —
with their metadata — into your [Readest](https://readest.com) cloud library.
Re-pushing a book updates its existing entry instead of creating a duplicate,
so you can edit metadata in calibre and re-send it any time.

Implements [readest/readest#4863](https://github.com/readest/readest/issues/4863).

## Rationale

calibre and Readest can both contain more than one file version of the same
book. A calibre book may have EPUB and PDF formats at the same time, and the
same title may already exist in Readest because it was uploaded directly.
Choosing one hard-coded calibre format by priority is not enough in that
environment: it can make PDF impossible to push when EPUB is present, and it
can overwrite a reader's active PDF progress by rebinding the book to EPUB.

The plugin therefore treats a calibre book as one Readest peer with one bound
content format. Before a book is bound, default push and status checks look
for a live Readest row matching any supported calibre format's content hash,
in priority order. If none exists, the first push chooses a format by priority
unless you choose one explicitly. After a Readest row is bound, default push
and status checks use that bound format. Explicit format selection is reserved
for intentional format switches, and the row for the selected live content is
used as the source of Readest-owned state such as progress, reading status and
grouping.

## Features

- **Selective, manual push**: select books in calibre, click *Readest* in the
  toolbar. Nothing syncs in the background.
- **Format choice without duplicates**: for an unbound book, default push and
  status checks first scan supported calibre formats in
  `EPUB > PDF > AZW3 > MOBI > AZW > FB2 > FBZ > CBZ > TXT > MD` order for an
  existing live Readest hash match, then fall back to the first format in that
  order. Once a book is bound to a Readest row, default push and status checks
  use the bound format. To switch formats, use *Push selected books as...* and
  choose one of the common supported formats for the current selection.
- **Metadata included and embedded**: title, authors, series, tags,
  description, publisher, language, identifiers — and optionally calibre
  custom columns. Metadata is written both to the Readest library entry
  (custom columns under `calibreColumns`) and into the uploaded file's OPF
  (calibre's own embedding, so custom columns travel as
  `calibre:user_metadata`). Your calibre library files are never modified —
  embedding happens on a temporary copy.
- **Update on re-push**: books already in your Readest library are recognized
  by their calibre uuid and only changed entries are rewritten; unchanged
  books are skipped. A changed file or an intentional format switch replaces
  the old entry instead of duplicating it. Reading progress, grouping and
  reading status in Readest are preserved from the row that represents the
  selected live content format.
- **Per-book status report**: uploaded / updated / up to date / failed, with
  the checked format shown for each book and a storage-quota check (the push
  stops cleanly when your quota is exhausted).
- **Login like the apps**: email + password, or browser sign-in with Google,
  Apple, GitHub or Discord (OAuth via a temporary localhost callback, the same
  flow the desktop app uses).

## Install

Download `Readest-<version>.calibre-plugin.zip` from the
[latest release](https://github.com/readest/readest/releases/latest), or build
it yourself:

```sh
make zip                 # builds dist/Readest-<version>.calibre-plugin.zip
calibre-customize -a dist/Readest-*.calibre-plugin.zip   # or: make install
```

Or in calibre: *Preferences → Plugins → Load plugin from file*, then restart
calibre and add the *Readest* button to the main toolbar if it is not visible.

Release and local zips are versioned from `apps/readest-app/package.json`.
`make zip` runs `make version` first, which stamps `PLUGIN_VERSION` in
`__init__.py` before zipping so calibre shows the same version.

## Usage

1. Click the *Readest* toolbar button menu → *Log in to Readest…*
2. Select the books to push (any number).
3. Click the *Readest* button (or menu → *Push selected books to Readest*).

For an unbound book, default push and *Check Readest status* first look for an
existing live Readest row whose content hash matches any supported calibre
format, checking formats in this order:

`EPUB > PDF > AZW3 > MOBI > AZW > FB2 > FBZ > CBZ > TXT > MD`

If no hash match is found, default push chooses the first available format in
that same order.

After a book is bound, default push and *Check Readest status* use the bound
format even if a higher-priority format is also present in calibre. This lets
you select a mixed batch of bound books and push them without accidentally
rebinding PDFs to EPUBs.

To choose or switch a format, open the toolbar menu and use *Push selected
books as...*. For a multi-book selection, the submenu lists only formats
available for every selected book. If there are no common supported formats,
the submenu shows a disabled explanatory item. If the requested or bound
format is missing locally, that book fails with an explicit message instead
of silently falling back to another format.

## How updates and duplicates work

At upload time the plugin embeds your current calibre metadata (including
custom columns) into a temporary copy of the book file, so the copy in your
Readest library is self-describing. Pushed books are then tracked by two keys:

- the **calibre book uuid**, carried in the entry's metadata
  (`urn:uuid:...`) — this recognizes "this calibre book is already in
  Readest" across pushes, even when the file bytes change;
- a **fingerprint of the raw calibre file** (`calibreSourceHash`), which
  detects whether the file itself changed since the last push — no local
  state, so it works from any machine.

Binding invariants:

- One calibre book maps to one live Readest book.
- A direct Readest upload whose content hash matches any supported calibre
  format is reused on first push; the calibre UUID metadata is written onto
  that row.
- Once a live row is bound, default push and status checks use that bound
  format. Priority selection applies only before binding.
- Explicit format selection is the intentional switch path.
- When switching formats, a live row matching the selected content hash is the
  preservation source. Its Readest-owned engagement state is carried forward,
  and other live peer rows for the same calibre UUID are tombstoned.

Re-push behavior:

- **Nothing changed** → skipped.
- **Metadata edited** → the Readest library entry is updated in place; the
  file is not re-uploaded (its embedded OPF keeps the metadata from its
  upload time).
- **File changed** (e.g. re-converted) → the new file is uploaded with fresh
  embedded metadata and replaces the old entry. Reading status, grouping,
  progress and the library date carry over; the old entry is tombstoned and
  its cloud files are deleted.
- **Format switched explicitly** → the selected format becomes the bound
  format. If a live Readest row already exists for that file hash, its
  progress, reading status and grouping are preserved while the calibre UUID
  metadata is wired onto it; the previous live peer is tombstoned.
- Books pushed by older plugin versions or dragged into Readest manually are
  recognized too: their entry hash can be used as the raw-file fingerprint
  for first binding.

## Development

Pure-logic modules (`api.py`, `wire.py`, `oauth.py`) have no calibre or Qt
dependencies and are covered by unit tests:

```sh
make test    # python3 -m unittest discover -s tests
```

### Translations

The plugin UI is localized with gettext: user-visible strings in the source
are wrapped in `_()` (singular) or `ngettext(singular, plural, n)` (plural),
and each module that uses them calls `load_translations()` at the top — that
is the hook calibre provides for plugins. calibre then picks the language
from *Preferences → Interface → Look & feel → Language* (or the
`CALIBRE_OVERRIDE_LANG` environment variable) and loads
`translations/<lang>.mo` from the plugin zip, falling back from e.g. `de_DE`
to `de`. English is the source language, so there is no `en` catalog.

Translations live in `translations/` as standard `.po` files (`readest.pot`
is the template); the compiled `.mo` files are committed alongside them so
`make zip` needs nothing but `zip`. After changing strings in the source:

```sh
make pot    # refresh translations/readest.pot from the source
# edit translations/*.po (or: python3 scripts/i18n.py new LANG for a new one)
make mo     # validate and compile every .po to its .mo
make test
```

`translations/` holds catalogs for German, Spanish, French, Italian, Japanese,
Korean, Polish, Portuguese (Portugal and Brazil), Russian, and Chinese
(Simplified, Traditional, and Hong Kong); other languages show the English
text. To try one without changing calibre's UI language:

```sh
CALIBRE_OVERRIDE_LANG=ja calibre
```

The wire protocol mirrors what the Readest apps and `readest.koplugin` use:
Supabase auth (`/auth/v1`), `GET/POST /api/sync` for library rows, and
`POST /api/storage/upload` + presigned PUT for file blobs.
