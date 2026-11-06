"""Entwürfe und Senden per IMAP/SMTP für alle IMAP-Konten in Mail: Ordner über SPECIAL-USE, kein doppeltes Gesendet."""

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



GMAIL_AUTO = {
    "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_use_ssl": True,
    "smtp_use_starttls": False, "imap_host": "imap.gmail.com", "imap_port": 993,
    "drafts_folder": None, "sent_folder": None, "server_saves_sent": True,
}


class AccountConfigTests(unittest.TestCase):
    def setUp(self):
        smtp_send._AUTO_CONFIG_CACHE.clear()

    def test_imap_konto_wird_aus_mail_gelesen(self):
        with patch("apple_mail_mcp.core.run_applescript",
                   return_value="imap.gmail.com|993|smtp.gmail.com|465"):
            cfg = smtp_send.account_config("Irgendein Gmail")
        self.assertEqual(cfg, GMAIL_AUTO)

    def test_starttls_bei_port_587_und_gesendet_selbst_ablegen(self):
        with patch("apple_mail_mcp.core.run_applescript",
                   return_value="imap.example.org|993|smtp.example.org|587"):
            cfg = smtp_send.account_config("Firma")
        self.assertTrue(cfg["smtp_use_starttls"])
        self.assertFalse(cfg["smtp_use_ssl"])
        self.assertFalse(cfg["server_saves_sent"])

    def test_exchange_konto_bleibt_beim_editor(self):
        with patch("apple_mail_mcp.core.run_applescript", return_value="kein-imap"):
            self.assertIsNone(smtp_send.account_config("Exchange"))
            self.assertFalse(smtp_send.has_smtp_credentials("Exchange", "x@y.z"))

    def test_icloud_bleibt_fest_eingetragen(self):
        with patch("apple_mail_mcp.core.run_applescript") as osa:
            cfg = smtp_send.account_config("iCloud")
        osa.assert_not_called()
        self.assertEqual(cfg["drafts_folder"], "Drafts")


class ImapAppendTests(unittest.TestCase):
    def _imap(self):
        imap = MagicMock()
        imap.__enter__.return_value = imap
        imap.list.return_value = ("OK", GMAIL_LIST)
        imap.append.return_value = ("OK", [b"done"])
        return imap

    def test_append_ohne_ordnernamen_nimmt_den_entwuerfe_ordner(self):
        imap = self._imap()
        with patch("apple_mail_mcp.smtp_send.imaplib.IMAP4_SSL", return_value=imap):
            smtp_send._imap_append(GMAIL_AUTO, "u", "p", None, "(\\Draft)", smtp_send.EmailMessage())
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
    def setUp(self):
        smtp_send._AUTO_CONFIG_CACHE.clear()
        smtp_send._AUTO_CONFIG_CACHE["Gmail"] = GMAIL_AUTO
        smtp_send._AUTO_CONFIG_CACHE["Firma"] = dict(
            GMAIL_AUTO, smtp_host="smtp.example.org", smtp_port=587,
            smtp_use_ssl=False, smtp_use_starttls=True, server_saves_sent=False)

    def test_gmail_bekommt_kein_zweites_gesendet(self):
        with (
            patch("apple_mail_mcp.smtp_send._get_keychain_password", return_value="pw"),
            patch("apple_mail_mcp.smtp_send._smtp_send") as smtp,
            patch("apple_mail_mcp.smtp_send._imap_append") as append,
        ):
            ok, _ = smtp_send.send_via_smtp("Gmail", "me@gmail.com", "a@b.c", "S", "Text")
        self.assertTrue(ok)
        smtp.assert_called_once()
        append.assert_not_called()

    def test_anderer_server_bekommt_gesendet_ueber_special_use(self):
        with (
            patch("apple_mail_mcp.smtp_send._get_keychain_password", return_value="pw"),
            patch("apple_mail_mcp.smtp_send._smtp_send"),
            patch("apple_mail_mcp.smtp_send._imap_append") as append,
        ):
            ok, _ = smtp_send.send_via_smtp("Firma", "me@example.org", "a@b.c", "S", "Text")
        self.assertTrue(ok)
        self.assertIsNone(append.call_args[0][3])
        self.assertEqual(append.call_args.kwargs["special_use"], "\\Sent")

    def test_port_465_nimmt_implizites_tls(self):
        with patch("apple_mail_mcp.smtp_send.smtplib.SMTP_SSL") as ssl, \
             patch("apple_mail_mcp.smtp_send.smtplib.SMTP") as plain:
            smtp_send._smtp_send(GMAIL_AUTO, "u", "p", smtp_send.EmailMessage())
        ssl.assert_called_once()
        plain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
