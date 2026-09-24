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
from app.models import FileException, FileExceptionChange, SharedKey


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

    def test_re_adding_a_removed_key_does_not_rewrite_when_it_was_listed(self):
        """The common path: Paul re-adds through the form or an upload, not
        through the Restore button. This is the one that used to erase the
        removal, so it is the one that has to be pinned."""
        self._add("invoices", key_1="500")
        entry = FileException.objects.get()
        first_listed, first_by = entry.created_at, entry.created_by
        self.client.post(reverse("exception_remove", args=["invoices", entry.pk]))
        self._add("invoices", key_1="500")
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
        # Asserted on the context, not the HTML: "Added" is also a column header
        # on this page, so a text search passes even with an empty log.
        self.assertEqual(
            [c.action for c in response.context["recent"]],
            ["restored", "removed", "added"],
        )

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

    def _confirm(self, report: str, content: bytes, name: str = "list.xlsx"):
        preview = self.client.post(
            reverse("exception_upload", args=[report]),
            {"file": _upload(name, content)},
        )
        return preview, self.client.post(
            reverse("exception_upload_confirm", args=[report]),
            preview.context["confirm_form"].initial,
            follow=True,
        )

    def test_naming_a_client_takes_the_bare_number_off_the_list(self):
        """Paul's 66 were already listed as bare numbers when he asked for the
        client too. Left there, the wildcard drops every client's row and the
        entry he just added changes nothing at all."""
        self._add("invoices", key_1="163746")
        content = _xlsx([
            ["External Invoice Number", "Client Number"],
            ["163746", "NJ00001544"],
        ])
        preview, response = self._confirm("invoices", content)
        self.assertContains(preview, "applies to every Client Number")
        self.assertContains(
            response, "list.xlsx: 1 added, 1 entry narrowed to the Client Number"
        )
        live = FileException.objects.filter(removed_at__isnull=True)
        self.assertEqual(
            list(live.values_list("key_1", "key_2")), [("163746", "NJ00001544")]
        )
        # Removed, not deleted: the record of changes says so and Restore
        # brings the wildcard back, the same as any other removal.
        wildcard = FileException.objects.get(key_1="163746", key_2="")
        self.assertEqual(wildcard.removed_by, "paul")
        self.assertEqual(
            list(wildcard.changes.values_list("action", flat=True)),
            [FileExceptionChange.REMOVED, FileExceptionChange.ADDED],
        )

    def test_a_file_that_carries_both_keeps_the_bare_number(self):
        """A row with no client says "every client" in so many words. An upload
        does not get to argue with itself, whatever order the rows come in."""
        self._add("invoices", key_1="163746")
        content = _xlsx([
            ["External Invoice Number", "Client Number"],
            ["163746", "NJ00001544"],
            ["163746", ""],
        ])
        _, response = self._confirm("invoices", content)
        self.assertNotContains(response, "narrowed to the Client Number in the file")
        self.assertEqual(
            FileException.objects.filter(removed_at__isnull=True).count(), 2
        )

    def test_typing_the_client_in_narrows_the_number_already_typed(self):
        self._add("invoices", key_1="163746")
        response = self._add("invoices", key_1="163746", key_2="NJ00001544")
        self.assertContains(
            response,
            "163746 on its own was removed: it covered every Client Number.",
        )
        self.assertFalse(
            FileException.objects.get(key_1="163746", key_2="").active
        )

    def test_a_report_whose_key_has_no_optional_part_never_narrows(self):
        """Both parts are required for authorizations, so there is no wildcard
        to retire and nothing on the list may be touched by an upload."""
        self._add("auths", key_1="664508", key_2="A1")
        content = _xlsx([
            ["Client DDDID", "Authorization ID"],
            ["664508", "A2"],
        ])
        preview, response = self._confirm("auths", content)
        self.assertNotContains(preview, "applies to every")
        self.assertNotContains(response, "narrowed to the")
        self.assertEqual(
            FileException.objects.filter(removed_at__isnull=True).count(), 2
        )

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
        exceptions["invoices"].stats["invoices.xlsx"] = (7, {("500", "")})

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
        self.assertEqual(specs["invoices"].keys, frozenset({("500", "")}))
        self.assertIsNone(specs["auths"])
        self.assertIsNone(specs["accruals"])

    # --- invoice numbers on more than one client's lines ----------------------

    NOTICE = "on more than one client&rsquo;s lines, and all of those lines"
    BURKE = ("NJ00006730", "Burke, M.")
    BURKERT = ("NJ00006516", "Burkert, H.")

    def _run_saw(self, numbers):
        """Stamp as a run that wrote the invoice file would, its rows carrying
        each number under the (client, name) pairs given. Needs one entry on
        the invoices list, or the run applies no list at all."""
        exceptions = _load_exceptions()
        spec = exceptions["invoices"]
        spec.stats["invoices.xlsx"] = (0, set())
        for number, seen in numbers.items():
            for client, name in seen:
                owner = (client.lower(),)
                spec.owners.labels.setdefault(owner, ((client,), name))
                spec.owners.note((number.lower(),), owner)
        _stamp_matches(exceptions, timezone.now())

    def _page(self):
        return self.client.get(reverse("exceptions_list", args=["invoices"]))

    def _narrow(self, entry, *clients):
        return self.client.post(
            reverse("exception_narrow", args=["invoices", entry.pk]),
            {"client": list(clients)},
            follow=True,
        )

    def test_a_listed_number_on_two_clients_lines_is_on_top_with_a_button_each(self):
        self._add("invoices", key_1="119344")
        self._add("invoices", key_1="500")
        self._run_saw({
            "119344": [self.BURKE, self.BURKERT],
            "500": [("NJ00001544", "Moore, A.")],
        })

        response = self._page()
        self.assertContains(response, "1 invoice number on your list is " + self.NOTICE)
        self.assertContains(response, "Only NJ00006730 Burke, M.")
        self.assertContains(response, "Only NJ00006516 Burkert, H.")
        self.assertEqual(
            list(SharedKey.objects.values_list("key_1", flat=True)), ["119344"]
        )

    def test_the_notice_is_there_the_moment_the_number_is_added(self):
        """No run in between: Paul sees it while he is still on the page."""
        self._add("invoices", key_1="500")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})
        self.assertNotContains(self._page(), self.NOTICE)

        response = self._add("invoices", key_1="119344")
        self.assertContains(response, self.NOTICE)
        self.assertContains(response, "Only NJ00006730 Burke, M.")

    def test_an_entry_that_names_its_client_is_not_flagged(self):
        self._add("invoices", key_1="119344", key_2="NJ00006730")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})
        self.assertNotContains(self._page(), self.NOTICE)

    def test_one_click_keeps_that_client_and_takes_the_number_off(self):
        self._add("invoices", key_1="119344")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})
        wildcard = FileException.objects.get(key_1="119344", key_2="")

        response = self._narrow(wildcard, "NJ00006730")

        self.assertContains(
            response,
            "119344 now leaves out only NJ00006730 Burke, M. — the other "
            "clients&#x27; lines go back into the file.",
        )
        self.assertNotContains(response, self.NOTICE)
        wildcard.refresh_from_db()
        self.assertFalse(wildcard.active)
        self.assertEqual(wildcard.removed_by, "paul")
        narrowed = FileException.objects.get(key_1="119344", key_2="NJ00006730")
        self.assertTrue(narrowed.active)
        self.assertEqual(narrowed.created_by, "paul")
        self.assertEqual(
            sorted(FileExceptionChange.objects.values_list("entry__key_2", "action")),
            [("", "added"), ("", "removed"), ("NJ00006730", "added")],
        )

    def test_all_of_them_keeps_every_client_by_name(self):
        self._add("invoices", key_1="119344")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})
        wildcard = FileException.objects.get(key_1="119344", key_2="")

        response = self._narrow(wildcard, "NJ00006730", "NJ00006516")

        self.assertNotContains(response, "go back into the file")
        self.assertNotContains(response, self.NOTICE)
        self.assertEqual(
            sorted(
                FileException.objects.filter(removed_at__isnull=True)
                .values_list("key_1", "key_2")
            ),
            [("119344", "NJ00006516"), ("119344", "NJ00006730")],
        )

    def test_a_client_the_run_did_not_see_changes_nothing(self):
        """The buttons offer only what the last run found; a stale page or a
        hand-made request must not put anything else on the list."""
        self._add("invoices", key_1="119344")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})
        wildcard = FileException.objects.get(key_1="119344", key_2="")

        response = self._narrow(wildcard, "NJ99999999")

        self.assertContains(response, "Nothing changed")
        self.assertEqual(FileException.objects.count(), 1)
        wildcard.refresh_from_db()
        self.assertTrue(wildcard.active)

    def test_the_next_run_replaces_what_the_last_one_saw(self):
        self._add("invoices", key_1="119344")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})
        self._run_saw({"119344": [self.BURKE]})

        self.assertFalse(SharedKey.objects.exists())
        self.assertNotContains(self._page(), self.NOTICE)

    def test_a_run_that_did_not_write_the_invoice_file_leaves_it_alone(self):
        """The accruals run opens no invoice file, so it knows nothing new."""
        self._add("invoices", key_1="119344")
        self._run_saw({"119344": [self.BURKE, self.BURKERT]})

        _stamp_matches(_load_exceptions(), timezone.now())

        self.assertEqual(SharedKey.objects.count(), 1)
        self.assertContains(self._page(), self.NOTICE)
