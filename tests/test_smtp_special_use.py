"""Entwürfe per IMAP für Gmail-Konten: Ordner über SPECIAL-USE, kein doppeltes Gesendet."""

import unittest
from unittest.mock import MagicMock, patch

from apple_mail_mcp import smtp_send

GMAIL_LIST = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"',
    b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Entw&APw-rfe"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Gesendet"',
]


class SpecialUseTests(unittest.TestCase):
    def test_findet_entwuerfe_unabhaengig_von_der_sprache(self):
        self.assertEqual(
            smtp_send._parse_special_use(GMAIL_LIST, "\\Drafts"), "[Gmail]/Entw&APw-rfe"
        )

    def test_findet_gesendet_auch_wenn_die_flagge_hinten_steht(self):
        self.assertEqual(smtp_send._parse_special_use(GMAIL_LIST, "\\Sent"), "[Gmail]/Gesendet")

    def test_ohne_passende_flagge_kein_ordner(self):
        self.assertIsNone(smtp_send._parse_special_use(GMAIL_LIST, "\\Junk"))

    def test_gmail_konto_ist_eingetragen(self):
        cfg = smtp_send.ACCOUNT_CONFIG["zd@alpine-valley.com"]
        self.assertEqual(cfg["imap_host"], "imap.gmail.com")
        self.assertIsNone(cfg["drafts_folder"])
        self.assertIsNone(cfg["sent_folder"])


class ImapAppendTests(unittest.TestCase):
    def _imap(self):
        imap = MagicMock()
        imap.__enter__.return_value = imap
        imap.list.return_value = ("OK", GMAIL_LIST)
        imap.append.return_value = ("OK", [b"done"])
        return imap

    def test_append_ohne_ordnernamen_nimmt_den_entwuerfe_ordner(self):
        imap = self._imap()
        cfg = smtp_send.ACCOUNT_CONFIG["zd@alpine-valley.com"]
        with patch("apple_mail_mcp.smtp_send.imaplib.IMAP4_SSL", return_value=imap):
            smtp_send._imap_append(cfg, "u", "p", None, "(\\Draft)", smtp_send.EmailMessage())
        self.assertEqual(imap.append.call_args[0][0], '"[Gmail]/Entw&APw-rfe"')

    def test_append_mit_festem_ordner_fragt_nicht_nach(self):
        imap = self._imap()
        cfg = smtp_send.ACCOUNT_CONFIG["iCloud"]
        with patch("apple_mail_mcp.smtp_send.imaplib.IMAP4_SSL", return_value=imap):
            smtp_send._imap_append(cfg, "u", "p", "Drafts", "(\\Draft)", smtp_send.EmailMessage())
        imap.list.assert_not_called()
        self.assertEqual(imap.append.call_args[0][0], '"Drafts"')

    def test_entwurf_beginnt_ohne_leere_zeile(self):
        msg = smtp_send._build_message(
            "zd@alpine-valley.com", "a@b.c", "Betreff", "Hallo Bernhard,\n\nText", None, None, None, None
        )
        self.assertTrue(msg.get_content().startswith("Hallo Bernhard,"))


class SendTests(unittest.TestCase):
    def test_gmail_bekommt_kein_zweites_gesendet(self):
        with (
            patch("apple_mail_mcp.smtp_send._get_keychain_password", return_value="pw"),
            patch("apple_mail_mcp.smtp_send._smtp_send") as smtp,
            patch("apple_mail_mcp.smtp_send._imap_append") as append,
        ):
            ok, _ = smtp_send.send_via_smtp(
                "zd@alpine-valley.com", "zd@alpine-valley.com", "a@b.c", "S", "Text"
            )
        self.assertTrue(ok)
        smtp.assert_called_once()
        append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
