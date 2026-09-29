__license__ = 'AGPL v3'
__copyright__ = '2026, Bilingify LLC'

"""Background push worker: one QThread, sequential per-book processing."""

import io
import os
import shutil
import tempfile
import time
import traceback
from collections import namedtuple

from qt.core import QThread, pyqtSignal

from calibre_plugins.readest.api import (
    AuthRequiredError,
    QuotaExceededError,
    partial_md5,
    partial_md5_bytes,
)
from calibre_plugins.readest.wire import (
    EXTS,
    PLAN_MARKS,
    book_file_name,
    build_wire_book,
    calibre_columns,
    cloud_book_hashes,
    cloud_cover_hashes,
    cover_file_name,
    index_rows_by_uuid,
    merge_for_push,
    ms_to_iso,
    pick_server_binding,
    plan_push,
    resolve_format,
    should_bulk_list,
    supported_formats,
    tombstone_record,
    tombstone_records,
)

try:
    load_translations()
except NameError:
    pass

STATUS_LABELS = {
    'uploaded': _('Uploaded'),
    'replaced': _('Replaced'),
    'updated': _('Updated'),
    'skipped': _('Up to date'),
    'failed': _('Failed'),
}
PUSH_ORDER = ('uploaded', 'replaced', 'updated', 'skipped', 'failed')

# The summary line gives each status its own plural message, so translators
# control agreement and word order.
PUSH_SUMMARY = {
    'uploaded': lambda n: ngettext('%d uploaded', '%d uploaded', n) % n,
    'replaced': lambda n: ngettext('%d replaced', '%d replaced', n) % n,
    'updated': lambda n: ngettext('%d updated', '%d updated', n) % n,
    'skipped': lambda n: ngettext('%d up to date', '%d up to date', n) % n,
    'failed': lambda n: ngettext('%d failed', '%d failed', n) % n,
}

# A status check reports plan_push's actions verbatim, so its keys are the
# plan names rather than the past-tense push outcomes.
CHECK_LABELS = {
    'skip': _('In Readest'),
    'update': _('Metadata differs'),
    'replace': _('Out of date'),
    'new': _('Not in Readest'),
    'failed': _('Failed'),
}
CHECK_ORDER = ('skip', 'update', 'replace', 'new', 'failed')

CHECK_SUMMARY = {
    'skip': lambda n: ngettext('%d in Readest', '%d in Readest', n) % n,
    'update': lambda n: ngettext('%d metadata differs', '%d metadata differs', n) % n,
    'replace': lambda n: ngettext('%d out of date', '%d out of date', n) % n,
    'new': lambda n: ngettext('%d not in Readest', '%d not in Readest', n) % n,
    'failed': PUSH_SUMMARY['failed'],
}

# Every push outcome except a failure leaves the book present in the cloud.
PUSHED_MARK = PLAN_MARKS['skip']

_BookPlan = namedtuple(
    '_BookPlan',
    'mi fmt path source_hash cover_bytes cover_hash now_ms wire uuid server_row duplicate_rows plan',
)


def _user_metadata(mi):
    # mi.get() evaluates composite columns, which the raw '#value#' may not hold.
    user_metadata = {}
    for key in mi.custom_field_keys():
        try:
            fm = dict(mi.get_user_metadata(key, make_copy=False) or {})
            fm['#value#'] = mi.get(key)
            fm['#extra#'] = mi.get_extra(key)
        except Exception:
            continue
        user_metadata[key] = fm
    return user_metadata


def _book_dict(mi, include_custom_columns, source_hash):
    pubdate = getattr(mi, 'pubdate', None)
    # calibre uses year-101 dates as "undefined".
    if pubdate is not None and getattr(pubdate, 'year', 0) < 1000:
        pubdate = None
    return {
        'title': mi.title,
        'authors': [a for a in (mi.authors or []) if a],
        'languages': list(mi.languages or []),
        'publisher': mi.publisher,
        'pubdate': pubdate.isoformat() if pubdate else None,
        'comments': mi.comments,
        'tags': sorted(mi.tags or []),
        'series': mi.series,
        'series_index': mi.series_index if mi.series else None,
        'uuid': getattr(mi, 'uuid', None),
        'isbn': (mi.get_identifiers() or {}).get('isbn'),
        'custom_columns': calibre_columns(_user_metadata(mi)) if include_custom_columns else None,
        'source_hash': source_hash,
    }


def _embed_metadata_copy(path, mi, fmt):
    """Copy the library file to a temp path and embed `mi` into it.

    The library file itself is never touched. Formats without a metadata
    writer (or a failing writer) fall back to the unmodified copy.
    """
    fd, tmp = tempfile.mkstemp(suffix='.' + EXTS[fmt], prefix='readest-')
    os.close(fd)
    shutil.copyfile(path, tmp)
    try:
        from calibre.ebooks.metadata.meta import set_metadata

        with open(tmp, 'r+b') as stream:
            set_metadata(stream, mi, EXTS[fmt])
    except Exception:
        traceback.print_exc()
    return tmp


class _CloudWorker(QThread):
    """Pulls the cloud state once, then classifies each selected book.

    `PushWorker` acts on the plan; `StatusWorker` only reports it.
    """

    progress = pyqtSignal(int, int)  # done, total
    book_status = pyqtSignal(int, str, str, str)  # book_id, status key, format, detail
    done = pyqtSignal(bool, str)  # ok, message

    def __init__(
        self,
        parent,
        db,
        book_ids,
        client,
        include_custom_columns,
        requested_format=None,
        do_not_embed_metadata=True,
    ):
        QThread.__init__(self, parent)
        self.db = db  # calibre new_api (thread-safe)
        self.book_ids = list(book_ids)
        self.client = client
        self.include_custom_columns = include_custom_columns
        self.requested_format = requested_format.upper() if requested_format else None
        self.do_not_embed_metadata = do_not_embed_metadata
        self.canceled = False
        self.marks = {}  # book_id -> calibre mark label
        self.cloud_hashes = None  # whole-account listing, when it is worth paging
        self.cloud_cover_hashes = None
        self._blob_cache = {}  # book_hash -> in storage? (per-book fallback)
        self._cover_cache = {}  # book_hash -> cover.png in storage? (per-book fallback)

    def cancel(self):
        self.canceled = True

    def _load_cloud_state(self):
        """Server rows + storage listing. Returns an error message, or None."""
        try:
            rows = [r for r in self.client.pull_books() if r.get('book_hash')]
        except AuthRequiredError as err:
            return _('Please log in to Readest first. (%s)') % err
        except Exception as err:
            return _('Could not reach Readest: %s') % err
        self.by_hash = {r['book_hash']: r for r in rows}
        self.by_uuid = index_rows_by_uuid(rows)
        self.cloud_hashes, self.cloud_cover_hashes = self._bulk_listing()
        return None

    def _bulk_listing(self):
        """Every book and cover hash in storage, or None to look them up per book.

        Paging the whole listing costs a request per page whatever the
        selection size, so for a handful of books it is far cheaper to ask
        about just those. Page 1 tells us how long the listing is, and we need
        it anyway when we do go on to page the rest.
        """
        try:
            files, total_pages = self.client.list_files_page(1)
            if not should_bulk_list(total_pages, len(self.book_ids)):
                return None, None
            for page in range(2, total_pages + 1):
                if self.canceled:
                    return None, None
                more, _ = self.client.list_files_page(page)
                files.extend(more)
            return cloud_book_hashes(files), cloud_cover_hashes(files)
        except Exception:
            # Fall back to per-book lookups, which degrade on their own.
            traceback.print_exc()
            return None, None

    def _blob_present(self, book_hash):
        """Whether this book's file is really in cloud storage."""
        if self.cloud_hashes is not None:
            return book_hash in self.cloud_hashes
        if book_hash not in self._blob_cache:
            try:
                self._blob_cache[book_hash] = book_hash in cloud_book_hashes(
                    self.client.list_files(book_hash)
                )
            except Exception:
                # Without an answer we can only trust uploaded_at, which is
                # what the plugin did before. The run goes on.
                traceback.print_exc()
                self._blob_cache[book_hash] = True
        return self._blob_cache[book_hash]

    def _cover_present(self, book_hash):
        """Whether this book's cover.png is really in cloud storage."""
        if self.cloud_cover_hashes is not None:
            return book_hash in self.cloud_cover_hashes
        if book_hash not in self._cover_cache:
            try:
                self._cover_cache[book_hash] = book_hash in cloud_cover_hashes(
                    self.client.list_files(book_hash)
                )
            except Exception:
                # If storage probing fails, avoid forcing a cover-only update
                # that may churn on every sync. A changed local cover hash still
                # uploads through plan_push without this probe.
                traceback.print_exc()
                self._cover_cache[book_hash] = True
        return self._cover_cache[book_hash]

    def _format_constraint(self, mi):
        """(format, source) to force for this book, or (None, None)."""
        if self.requested_format:
            return self.requested_format, 'requested'
        uuid = (getattr(mi, 'uuid', None) or '').lower()
        row = self.by_uuid.get(uuid) if uuid else None
        if row is not None and not row.get('deleted_at') and row.get('format'):
            return row['format'].upper(), 'bound'
        return None, None

    def _format_choice(self, book_id, fmt):
        path = self.db.format_abspath(book_id, fmt)
        if not path or not os.path.exists(path):
            return None
        return fmt, path, partial_md5(path)

    def _unbound_hash_match_choice(self, book_id, available_formats):
        for fmt in supported_formats(available_formats):
            choice = self._format_choice(book_id, fmt)
            if choice is None:
                continue
            row = self.by_hash.get(choice[2])
            if row is not None and not row.get('deleted_at'):
                return choice
        return None

    def _plan_for(self, book_id):
        """(_BookPlan, '') for one book, or (None, reason) when it cannot be pushed."""
        mi = self.db.get_metadata(book_id)
        available_formats = self.db.formats(book_id)
        requested_format, format_source = self._format_constraint(mi)
        choice = None
        if requested_format is None:
            choice = self._unbound_hash_match_choice(book_id, available_formats)
        fmt = choice[0] if choice else resolve_format(available_formats, requested_format)
        if fmt is None:
            if requested_format:
                label = _('Bound') if format_source == 'bound' else _('Requested')
                return (
                    None,
                    _('%s format %s is not available or supported for this book')
                    % (label, requested_format),
                )
            return None, _('No Readest-supported format (EPUB, PDF, ...)')
        if choice is None:
            choice = self._format_choice(book_id, fmt)
        if choice is None:
            return None, _('Book file is missing from the calibre library')
        fmt, path, source_hash = choice

        cover_bytes = self.db.cover(book_id)
        cover_hash = partial_md5_bytes(cover_bytes) if cover_bytes else None

        now_ms = int(time.time() * 1000)
        book = _book_dict(mi, self.include_custom_columns, source_hash)
        wire = build_wire_book(book, source_hash, fmt, now_ms)
        uuid = (book.get('uuid') or '').lower()
        server_row, duplicate_rows = pick_server_binding(
            self.by_hash.get(source_hash), self.by_uuid.get(uuid) if uuid else None
        )
        plan = plan_push(
            server_row,
            wire,
            cover_hash,
            source_hash,
            self._blob_present,
            self._cover_present,
            expected_blob_hash=source_hash if self.do_not_embed_metadata else None,
        )
        return (
            _BookPlan(
                mi, fmt, path, source_hash, cover_bytes, cover_hash,
                now_ms, wire, uuid, server_row, duplicate_rows, plan,
            ),
            '',
        )

    def _summary(self, counts, summary, order, empty):
        parts = ', '.join(summary[key](counts[key]) for key in order if key in counts)
        return parts or empty


class PushWorker(_CloudWorker):
    def run(self):
        error = self._load_cloud_state()
        if error:
            self.done.emit(False, error)
            return

        counts = {}
        for index, book_id in enumerate(self.book_ids):
            if self.canceled:
                self.done.emit(False, _('Canceled.'))
                return
            try:
                status, fmt, detail = self._push_one(book_id)
            except QuotaExceededError as err:
                self.book_status.emit(book_id, 'failed', '', str(err))
                self.done.emit(False, _('Readest storage quota exceeded — push stopped.'))
                return
            except AuthRequiredError as err:
                self.book_status.emit(book_id, 'failed', '', str(err))
                self.done.emit(False, _('Session expired — please log in again.'))
                return
            except Exception as err:
                traceback.print_exc()
                status, fmt, detail = 'failed', '', str(err)
            counts[status] = counts.get(status, 0) + 1
            if status != 'failed':
                # Whatever the outcome, the book is now in the cloud, so keep
                # any marks a previous status check left from going stale.
                self.marks[book_id] = PUSHED_MARK
            self.book_status.emit(book_id, status, fmt, detail)
            self.progress.emit(index + 1, len(self.book_ids))

        self.done.emit(
            'failed' not in counts,
            self._summary(counts, PUSH_SUMMARY, PUSH_ORDER, _('Nothing to push.')),
        )

    def _upload(self, file_name, fileobj, size, book_hash):
        upload = self.client.get_upload_url(file_name, size, book_hash)
        self.client.put_file(upload['uploadUrl'], fileobj, size)

    def _upload_cover(self, book_hash, cover_bytes):
        self._upload(cover_file_name(book_hash), io.BytesIO(cover_bytes), len(cover_bytes), book_hash)

    def _delete_cloud_files(self, book_hash):
        # Best-effort quota reclaim for a replaced book, mirroring the
        # koplugin's deleteCloudFiles: list to learn exact keys, then DELETE.
        try:
            for record in self.client.list_files(book_hash):
                if record.get('file_key'):
                    self.client.delete_file(record['file_key'])
        except Exception:
            traceback.print_exc()

    def _remember(self, record, uuid):
        # Keep the lookup maps current so a duplicate calibre entry later in
        # this run resolves to what we just pushed.
        row = {
            'book_hash': record['hash'],
            'format': record.get('format'),
            'title': record['title'],
            'author': record['author'],
            'tags': record.get('tags'),
            'metadata': record['metadata'],
            'updated_at': ms_to_iso(record.get('updatedAt')),
            'uploaded_at': ms_to_iso(record.get('uploadedAt')),
            'cover_hash': record.get('coverHash'),
            'cover_updated_at': ms_to_iso(record.get('coverUpdatedAt')),
        }
        self.by_hash[record['hash']] = row
        if uuid:
            self.by_uuid[uuid] = row
        # We just uploaded it, so don't go asking storage about it again.
        self._blob_cache[record['hash']] = True
        self._cover_cache[record['hash']] = bool(record.get('coverHash'))
        if self.cloud_hashes is not None:
            self.cloud_hashes.add(record['hash'])
        if self.cloud_cover_hashes is not None:
            if record.get('coverHash'):
                self.cloud_cover_hashes.add(record['hash'])
            else:
                self.cloud_cover_hashes.discard(record['hash'])

    def _remember_tombstones(self, records):
        for record in records:
            book_hash = record.get('bookHash') or record.get('hash')
            if not book_hash:
                continue
            if book_hash in self.by_hash:
                self.by_hash[book_hash]['deleted_at'] = ms_to_iso(record.get('deletedAt'))
            self._blob_cache[book_hash] = False
            self._cover_cache[book_hash] = False
            if self.cloud_hashes is not None:
                self.cloud_hashes.discard(book_hash)
            if self.cloud_cover_hashes is not None:
                self.cloud_cover_hashes.discard(book_hash)

    def _push_one(self, book_id):
        ctx, reason = self._plan_for(book_id)
        if ctx is None:
            return 'failed', '', reason

        mi, fmt, path = ctx.mi, ctx.fmt, ctx.path
        cover_bytes, cover_hash = ctx.cover_bytes, ctx.cover_hash
        now_ms, wire, uuid, server_row = ctx.now_ms, ctx.wire, ctx.uuid, ctx.server_row
        duplicate_rows = ctx.duplicate_rows
        plan = ctx.plan

        if plan['action'] == 'skip':
            tombstones = tombstone_records(
                duplicate_rows,
                now_ms,
                keep_hashes={server_row['book_hash']} if server_row is not None else set(),
            )
            if tombstones:
                self.client.push_books(tombstones)
                for record in tombstones:
                    self._delete_cloud_files(record['bookHash'])
                self._remember_tombstones(tombstones)
                return 'updated', fmt, ''
            return 'skipped', fmt, ''

        if plan['action'] in ('new', 'replace'):
            blob_path = path
            remove_blob_path = False
            if not self.do_not_embed_metadata:
                blob_path = _embed_metadata_copy(path, mi, fmt)
                remove_blob_path = True
            try:
                blob_hash = partial_md5(blob_path)
                wire['hash'] = wire['bookHash'] = blob_hash
                size = os.path.getsize(blob_path)
                with open(blob_path, 'rb') as f:
                    self._upload(book_file_name(blob_hash, fmt), f, size, blob_hash)
            finally:
                if remove_blob_path:
                    try:
                        os.unlink(blob_path)
                    except OSError:
                        pass
            if plan['upload_cover'] and cover_bytes:
                self._upload_cover(blob_hash, cover_bytes)
            preserve_server_cover = (
                server_row is not None and server_row['book_hash'] == blob_hash
            )
            records = [
                merge_for_push(
                    wire,
                    server_row,
                    now_ms,
                    uploaded_at_ms=now_ms,
                    cover_hash=cover_hash if plan['upload_cover'] and cover_bytes else None,
                    preserve_server_cover=preserve_server_cover,
                )
            ]
            tombstones = []
            if server_row is not None and server_row['book_hash'] != blob_hash:
                tombstones.append(tombstone_record(server_row, now_ms))
            tombstones.extend(
                tombstone_records(duplicate_rows, now_ms, keep_hashes={blob_hash})
            )
            records.extend(tombstones)
            self.client.push_books(records)
            for record in tombstones:
                self._delete_cloud_files(record['bookHash'])
            self._remember(records[0], uuid)
            self._remember_tombstones(tombstones)
            return ('replaced' if plan['action'] == 'replace' else 'uploaded'), fmt, ''

        # action == 'update': the cloud file is current, only the row (and
        # possibly the cover) changed. Keep the row's hash namespace.
        wire['hash'] = wire['bookHash'] = server_row['book_hash']
        pushed_cover = plan['upload_cover'] and cover_bytes
        if pushed_cover:
            self._upload_cover(server_row['book_hash'], cover_bytes)
        record = merge_for_push(
            wire, server_row, now_ms, cover_hash=cover_hash if pushed_cover else None
        )
        tombstones = tombstone_records(
            duplicate_rows, now_ms, keep_hashes={server_row['book_hash']}
        )
        self.client.push_books([record] + tombstones)
        self._remember(record, uuid)
        self._remember_tombstones(tombstones)
        for tombstone in tombstones:
            self._delete_cloud_files(tombstone['bookHash'])
        return 'updated', fmt, ''


class StatusWorker(_CloudWorker):
    """Plans every selected book without uploading, and marks the results."""

    def run(self):
        error = self._load_cloud_state()
        if error:
            self.done.emit(False, error)
            return

        counts = {}
        for index, book_id in enumerate(self.book_ids):
            if self.canceled:
                self.done.emit(False, _('Canceled.'))
                return
            try:
                ctx, reason = self._plan_for(book_id)
                if ctx:
                    status, fmt, detail = ctx.plan['action'], ctx.fmt, ''
                else:
                    status, fmt, detail = 'failed', '', reason
            except Exception as err:
                traceback.print_exc()
                status, fmt, detail = 'failed', '', str(err)
            if status in PLAN_MARKS:
                self.marks[book_id] = PLAN_MARKS[status]
            counts[status] = counts.get(status, 0) + 1
            self.book_status.emit(book_id, status, fmt, detail)
            self.progress.emit(index + 1, len(self.book_ids))

        self.done.emit(
            'failed' not in counts,
            self._summary(counts, CHECK_SUMMARY, CHECK_ORDER, _('Nothing to check.')),
        )
