"""QA of the File Exceptions pages and of what the run reads from them.

These need the database (a real user, the unique constraint, the soft removal),
so unlike `tests/` they run through Django's runner: `manage.py test` with the
environment `acumenauto.settings` reads (see README).
"""
import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from openpyxl import Workbook

from app.management.commands.download_report import _load_exceptions, _stamp_matches
from app.models import FileException, FileExceptionChange


def _xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(name: str, content: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name,
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


class FileExceptionsPagesTests(TestCase):
    def setUp(self):
        User.objects.create_user("paul", password="pw")
        self.client.login(username="paul", password="pw")

    def _add(self, report, **fields):
        return self.client.post(reverse("exception_add", args=[report]), fields, follow=True)

    def test_the_menu_lands_on_the_invoice_list(self):
        response = self.client.get(reverse("exceptions"))
        self.assertRedirects(response, reverse("exceptions_list", args=["invoices"]))

    def test_each_file_has_its_page_with_its_key_columns(self):
        for slug, columns in (
            ("invoices", ["Invoice #"]),
            ("auths", ["Client DDDID", "Authorization ID"]),
            ("accruals", ["Client DDDID", "PA Number"]),
        ):
            response = self.client.get(reverse("exceptions_list", args=[slug]))
            self.assertEqual(response.status_code, 200)
            for column in columns:
                self.assertContains(response, column)

    def test_an_unknown_file_is_404(self):
        response = self.client.get(reverse("exceptions_list", args=["other"]))
        self.assertEqual(response.status_code, 404)

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse("exceptions_list", args=["invoices"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_adding_one_normalises_the_key_and_keeps_one_entry_per_key(self):
        self._add("invoices", key_1=" 500 ")
        self.assertEqual(
            list(FileException.objects.values_list("report", "key_1", "key_2")),
            [("invoices", "500", "")],
        )
        response = self._add("invoices", key_1="500")
        self.assertContains(response, "already listed")
        self.assertEqual(FileException.objects.count(), 1)

    def test_the_same_key_in_different_capitals_is_one_entry(self):
        """The run matches folded, so the page has to add folded too, or the
        second entry looks live on the list and drops nothing."""
        self._add("invoices", key_1="TCG188a5359DR6")
        response = self._add("invoices", key_1="tcg188A5359dr6")
        self.assertContains(response, "already listed")
        self.assertEqual(
            list(FileException.objects.values_list("key_1", flat=True)),
            ["TCG188a5359DR6"],
        )

    def test_a_composite_key_needs_both_fields(self):
        response = self._add("auths", key_1="721253")
        self.assertContains(response, "required")
        self.assertEqual(FileException.objects.count(), 0)
        self._add("auths", key_1="721253", key_2="173066812")
        self.assertEqual(FileException.objects.get().key_2, "173066812")

    def test_a_rejected_add_keeps_what_was_typed_and_names_the_field(self):
        response = self._add("auths", key_1="721253", key_2="")
        self.assertContains(response, "Authorization ID: This field is required.")
        self.assertContains(response, 'value="721253"')
        self.assertEqual(FileException.objects.count(), 0)

    def test_removing_keeps_the_record_and_restore_brings_it_back(self):
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        entry.refresh_from_db()
        self.assertIsNotNone(entry.removed_at)
        self.assertEqual(entry.removed_by, "paul")

        response = self.client.get(reverse("exceptions_list", args=["invoices"]))
        self.assertContains(response, "Restore")

        self.client.post(reverse("exception_restore", args=["invoices", entry.pk]))
        entry.refresh_from_db()
        self.assertIsNone(entry.removed_at)

    def test_adding_a_removed_key_again_restores_it(self):
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        self._add("invoices", key_1="500")
        entry.refresh_from_db()
        self.assertIsNone(entry.removed_at)
        self.assertEqual(FileException.objects.count(), 1)

    def test_the_record_of_changes_keeps_every_change_not_just_the_last(self):
        """The promise to Rob (2026-08-31): "keep a record of those changes so
        they can be reverted if needed". Before the log, this exact sequence
        left no trace that the key had ever been removed: re-adding it
        overwrote created_at/created_by and the removal was gone."""
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        self._add("invoices", key_1="500")

        actions = list(
            FileExceptionChange.objects.filter(entry=entry)
            .order_by("at", "id")
            .values_list("action", flat=True)
        )
        self.assertEqual(actions, ["added", "removed", "restored"])
        self.assertEqual(FileException.objects.count(), 1)

    def test_restoring_does_not_rewrite_when_the_key_was_first_listed(self):
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        first_listed, first_by = entry.created_at, entry.created_by
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        self.client.post(reverse("exception_restore", args=["invoices", entry.pk]))
        entry.refresh_from_db()
        self.assertEqual((entry.created_at, entry.created_by),
                         (first_listed, first_by))
        self.assertIsNone(entry.removed_at)

    def test_every_change_says_who_made_it(self):
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        self.assertEqual(
            set(FileExceptionChange.objects.values_list("by", flat=True)), {"paul"}
        )

    def test_the_page_shows_both_the_removal_and_the_re_add(self):
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        self._add("invoices", key_1="500")
        response = self.client.get(reverse("exceptions_list", args=["invoices"]))
        body = response.content.decode()
        for word in ("Added", "Removed", "Restored"):
            self.assertIn(word, body)

    def test_a_change_log_row_belongs_to_the_report_it_was_made_on(self):
        self._add("invoices", key_1="500")
        self._add("accruals", key_1="306194", key_2="1553411994")
        self.assertEqual(
            FileExceptionChange.objects.filter(report="invoices").count(), 1
        )
        self.assertEqual(
            FileExceptionChange.objects.filter(report="accruals").count(), 1
        )

    def test_a_search_finds_a_removed_entry_so_it_can_be_restored(self):
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        restore = reverse("exception_restore", args=["invoices", entry.pk])

        listing = self.client.get(reverse("exceptions_list", args=["invoices"]))
        self.assertContains(listing, "No entries yet")
        self.assertContains(listing, restore, count=1)  # in Recent changes only

        found = self.client.get(reverse("exceptions_list", args=["invoices"]), {"q": "500"})
        self.assertContains(found, restore, count=2)  # and now in the table too

    def test_an_apostrophe_in_a_key_does_not_break_the_remove_confirm(self):
        """A free-text invoice value can be a person's name; unescaped it made
        the confirm throw and the form submit without asking."""
        self._add("invoices", key_1="O'Brien 42")
        response = self.client.get(reverse("exceptions_list", args=["invoices"]))
        self.assertContains(response, "confirm('Remove O\\u0027Brien 42?');")

    def test_upload_adds_new_keys_skips_listed_ones_and_counts_the_rest(self):
        self._add("invoices", key_1="2")
        content = _xlsx([
            ["Invoice #", "Note"],
            [1, "typed as a number"],
            [1.0, "same key again"],
            [None, "note without a key"],
            ["2", "already listed"],
        ])
        preview = self.client.post(
            reverse("exception_upload", args=["invoices"]),
            {"file": _upload("list.xlsx", content)},
        )
        # Nothing is written until the second click: uploading the report
        # itself instead of a list reads fine and would empty the file that
        # goes out, so the count has to be seen first.
        self.assertContains(preview, "Add these 2 entries")
        self.assertEqual(FileException.objects.count(), 1)

        response = self.client.post(
            reverse("exception_upload_confirm", args=["invoices"]),
            preview.context["confirm_form"].initial,
            follow=True,
        )
        # The repeat inside the file and the entry already on the list are two
        # different things and are counted apart; this used to read
        # "2 already listed".
        self.assertContains(
            response,
            "list.xlsx: 1 added, 1 already on your list, 1 repeated in the file, "
            "1 row skipped for a missing Invoice #.",
        )
        active = FileException.objects.filter(removed_at__isnull=True)
        self.assertEqual(sorted(active.values_list("key_1", flat=True)), ["1", "2"])

    def test_upload_without_the_expected_header_is_refused_with_the_names(self):
        content = _xlsx([["Invoice Number"], [1]])
        response = self.client.post(
            reverse("exception_upload", args=["auths"]),
            {"file": _upload("list.xlsx", content)},
            follow=True,
        )
        self.assertContains(
            response, escape("'Client DDDID' and 'Authorization ID'")
        )
        self.assertEqual(FileException.objects.count(), 0)

    def test_a_run_marks_which_entries_matched_and_which_did_not(self):
        """A key that is checked and never matches is a typo; until the run
        stamped them, nothing on the page told them apart."""
        self._add("invoices", key_1="500")
        self._add("invoices", key_1="typo")
        self._add("accruals", key_1="431798", key_2="1553308787")
        exceptions = _load_exceptions()
        # What a run that wrote the invoice file leaves behind: one key matched.
        exceptions["invoices"].stats["invoices.xlsx"] = (7, {("500",)})

        _stamp_matches(exceptions, timezone.now())

        matched = FileException.objects.get(key_1="500")
        missed = FileException.objects.get(key_1="typo")
        untouched = FileException.objects.get(report="accruals")
        self.assertIsNotNone(matched.last_matched_at)
        self.assertIsNotNone(missed.last_checked_at)
        self.assertIsNone(missed.last_matched_at)
        # The accruals file was not written by this run, so it is not "checked".
        self.assertIsNone(untouched.last_checked_at)

    def test_the_run_reads_only_the_active_entries(self):
        self._add("invoices", key_1="500")
        self._add("invoices", key_1="600")
        removed = FileException.objects.get(key_1="600")
        self.client.post(reverse("exception_remove", args=["invoices", removed.pk]))

        specs = _load_exceptions()
        self.assertEqual(specs["invoices"].keys, frozenset({("500",)}))
        self.assertIsNone(specs["auths"])
        self.assertIsNone(specs["accruals"])
