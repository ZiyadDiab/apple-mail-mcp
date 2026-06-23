# Lessons — apple-mail-mcp

Hard-won patterns from real debugging. Read this before touching the compose/reply
code paths. Each lesson includes the symptom that gave it away and the fix.

---

## 1. Mail.app's compose editor wraps every body in a "share template"

**Symptom:** Plain-text emails sent via `make new outgoing message with properties {content: "..."}` rendered as **purple quoted text** on iPhone Mail, but looked normal on macOS Mail. Same MIME bytes, different rendering.

**Cause:** Every AppleScript-created outgoing message inherits Mail.app's "Share via Mail" template body, which has two zones:

```html
<div class="Apple-Mail-URLShareUserContentTopClass">  <!-- "your text" zone -->
  ...
</div>
<div class="Apple-Mail-URLShareWrapperClass">          <!-- "shared content" zone -->
  <blockquote type="cite" style="border-left-style: none; color: inherit; ...">
    ...
  </blockquote>
</div>
```

Setting the `content` property (whether at creation time via `make new ... with properties {content:...}` or after via `set content of m to ...`) lands the text in the **blockquote zone**. macOS Mail honors the `color: inherit` reset; iOS Mail ignores it and forces its theme quote color. Result: every line of your "new" email looks like a citation on phone.

**Fix:** Never use the `content` property for body text. Either:
- Build MIME directly in Python and bypass Mail.app entirely (see `smtp_send.py` — the cleanest path).
- Or use `make new outgoing message` with empty content (`content:""`), then NSPasteboard clipboard-paste the body into the visible compose window. The pasted content lands in the top "user" zone instead. (See `_send_html_email` in `compose.py`.)

---

## 2. Mail.app's WebKit editor auto-injects `color: rgb(0,0,0)` on pasted HTML

**Symptom:** Even with `body_html` that contained **no color attributes anywhere**, the delivered HTML had `<div style="color: rgb(0, 0, 0); caret-color: rgb(0, 0, 0);">` wrapping the body. When that email later got quoted in a reply, iOS Mail kept the text black instead of applying its natural purple/turquoise quote color (explicit inline color wins the CSS cascade).

**Cause:** WebKit's "smart paste" normalization adds explicit colors based on the compose window's computed style. We can't prevent it from the HTML side — Mail.app does it regardless of what we put on the clipboard.

**Fix:** Send via SMTP, not via Mail.app. Anything routed through Mail's compose editor gets this injection. The MIME built directly by Python `EmailMessage` and sent via `smtplib` has none of these editor mutations.

---

## 3. iPhone Mail and macOS Mail disagree on inline style precedence

**Symptom:** "Looks fine on Mac, broken on phone" — for any rendering bug, expect divergence.

**Cause:** macOS Mail respects inline CSS overrides like `color: inherit` and `border-left-style: none` on blockquotes. iOS Mail aggressively applies its own theme styles (purple-level-1, turquoise-level-2 quote colors) and overrides those inline declarations.

**Rule:** Don't trust the Mac view when debugging email-rendering bugs. **Always check on iPhone too.** And when in doubt, inspect the raw MIME, not the rendered DOM — the bugs live in the bytes.

---

## 4. Never hard-code colors in HTML email helpers

**Symptom:** A diagnostic test (DIAG-B) accidentally **masked** the bug it was supposed to detect, because the helper-generated HTML included `color: #111111` (near-black). The hard-coded color overrode whatever weird thing Mail/iPhone was doing, so the test looked clean. Then when that email got quoted in a reply, the hard-coded color blocked the natural quote-coloring downstream.

**Cause:** Forcing colors prevents the recipient client from applying its native theming — including dark-mode inversion and quote-level styling.

**Rule:** HTML email bodies emitted by helpers (`_build_html_from_text` etc.) should have **structural tags only**: `<div>`, `<p>`, `<br>`, `<strong>`, `<em>`, `<h2>`. No `color:`, no `font-family:`, no `line-height:`, no `font-size:`. Let the client decide. The user's recipient sees the email in *their* theme, on *their* device — let it adapt.

---

## 5. EMLs with `X-Unsent: 1` no longer open as draft compose windows in newer Mail.app

**Symptom:** Existing code in `create_rich_email_draft` writes a `.eml` with `X-Unsent: 1`, then opens it with `open -a Mail file.eml`, expecting Mail to create an editable draft. The follow-up call to find it via `every outgoing message whose subject is "..."` returns nothing — the message never enters the outgoing-messages collection.

**Cause:** In recent Mail.app versions (verified on Mail 16.0 / 3826.700.81 on macOS), opening a `.eml` produces a **read-only viewer window**. The `X-Unsent` hack only worked in older Mail versions. AppleScript can see the window (via `windows`) but not as an `outgoing message`. There is no `import message` or `edit as draft` command in Mail's scripting dictionary — only `import Mail mailbox` (which imports folders, not single messages).

**Implication:** `_save_open_message_as_draft` in `compose.py` is silently broken — it always returns `False` after timing out. The SMTP/IMAP-direct path in `smtp_send.py` (using `IMAP APPEND` to the Drafts folder with the `\Draft` flag) is the working alternative.

---

## 6. Python `imaplib.append()` does NOT auto-quote mailbox names with spaces

**Symptom:** `IMAP APPEND to 'Sent Messages' returned BAD [b'Parse Error']`. iCloud rejected the command. "Drafts" worked fine. Both went through the same code path.

**Cause:** The wire trace showed:
```
> b'TAG APPEND Sent Messages (\\Seen) "..." {171}'
```
The mailbox name `Sent Messages` is unquoted. iCloud's parser treats `Sent` as the mailbox and `Messages` as a stray argument → BAD Parse Error. `imaplib.append()` (unlike `select`, `list`, etc.) does not call `_quote` on the mailbox name.

**Fix:** Wrap the mailbox name yourself before passing to `append()`:
```python
quoted = f'"{folder}"' if not (folder.startswith('"') and folder.endswith('"')) else folder
imap.append(quoted, flags, date, msg_bytes)
```
This affects any multi-word folder name: `"Sent Messages"`, `"Deleted Messages"`, `"Junk E-mail"`, etc.

---

## 7. SMTP direct-send is the only fully clean send path

**Hierarchy of cleanliness (least → most editor interference):**

1. **`smtplib.send_message(EmailMessage)`** — zero Mail.app involvement, exact bytes you constructed go on the wire. The only way to get truly natural email rendering and preserved nested-quote colors.
2. **AppleScript with NSPasteboard clipboard paste into compose window** (existing `_send_html_email`, `reply_to_email`) — content lands in the correct zone, but WebKit injects `color: rgb(0,0,0)`.
3. **AppleScript `make new outgoing message with properties {content:...}`** — content lands in the BLOCKQUOTE zone. iPhone renders the whole body purple. **Avoid.**

When credentials are available, prefer Path 1. Fall back to Path 2 for `mode="open"` (which inherently needs a compose UI). Never Path 3.

---

## 8. macOS Keychain credential storage via `security` CLI

**Convention used here:**

- **Service:** `apple-mail-mcp/<AccountName>` (e.g. `apple-mail-mcp/iCloud`)
- **Account:** the full email address (e.g. `ziyad.diab@me.com`)
- **Value:** account-specific app password (Apple: appleid.apple.com → App-Specific Passwords; Gmail: myaccount.google.com → App passwords)

**Store (interactive, password never in shell history):**
```bash
security add-generic-password -a 'ziyad.diab@me.com' -s 'apple-mail-mcp/iCloud' -w
```

**Retrieve (silent, returns password on stdout):**
```bash
security find-generic-password -s 'apple-mail-mcp/iCloud' -a 'ziyad.diab@me.com' -w
```

**Inspect metadata without revealing password:**
```bash
security find-generic-password -s 'apple-mail-mcp/iCloud' -a 'ziyad.diab@me.com'
```

Use `subprocess.run([...], capture_output=True, text=True)` and check returncode — `None` (rather than raise) when absent, so callers can fall through to AppleScript path.

---

## 9. Diagnose email-rendering bugs via raw MIME, not rendered views

**Rule:** When an email "looks wrong" in a client, the first move is **always** to inspect the raw source. Both clients render the same bytes differently; the bytes are the ground truth.

**Get raw MIME via AppleScript:**
```applescript
tell application "Mail"
    set msg to item 1 of (every message of inboxMailbox whose subject contains "DIAG")
    return source of msg
end tell
```

**Get raw MIME via IMAP:**
```python
imap.select("INBOX")
typ, data = imap.search(None, 'SUBJECT', '"DIAG"')
typ, msg_data = imap.fetch(data[0].split()[0], "(RFC822)")
print(msg_data[0][1])
```

When bisecting an HTML-rendering issue: send several variants with unique markers (DIAG-A, DIAG-B...), then diff their raw MIME side by side. That's how the Apple-Mail-URLShare wrapper (lesson 1) and the WebKit color injection (lesson 2) were both found.

---

## 10. iCloud SMTP/IMAP connection details (reference)

For `ACCOUNT_CONFIG["iCloud"]` in `smtp_send.py`:

| Setting | Value |
|---|---|
| SMTP host | `smtp.mail.me.com` |
| SMTP port | `587` (STARTTLS) — also `465` (implicit SSL) works |
| IMAP host | `imap.mail.me.com` |
| IMAP port | `993` (implicit SSL) |
| Auth | Username = full email (`*@me.com` / `*@icloud.com` / `*@mac.com`); password = app-specific password |
| Folder names | `INBOX`, `Sent Messages`, `Drafts`, `Deleted Messages`, `Junk`, `Archive`, `Notes` |
| `\Sent` SPECIAL-USE | `"Sent Messages"` |
| `\Trash` SPECIAL-USE | `"Deleted Messages"` |

The `(\Sent) "/" "Sent Messages"` line in an IMAP `LIST` response confirms the canonical name. iCloud reserves `Sent Items` and `Deleted Items` as **alias folders** that show up but aren't the primary ones — don't write to them.

---

## How to use this file

- **Before touching `compose.py` or `smtp_send.py`:** skim lessons 1, 2, 5, 6 — they describe the bugs the current code paths are specifically designed to avoid. Don't undo the fix without understanding what it's working around.
- **Adding a new account type (Gmail, Exchange, custom IMAP):** follow lesson 8 for credential storage, lesson 10's structure for the config entry. Watch out for lesson 6 — every multi-word mailbox name needs quoting.
- **Debugging "looks fine on Mac but broken on phone":** lesson 3 is the prompt to check iPhone immediately, and lesson 9 is the prompt to drop to raw MIME.
- **Writing HTML helpers:** lesson 4 — no inline colors, no font declarations.
