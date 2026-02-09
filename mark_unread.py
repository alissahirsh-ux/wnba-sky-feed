#!/usr/bin/env python3
"""
Mark all emails in a mailbox as unread (unseen) via IMAP.

Usage:
  Set environment variables, then run:

    export EMAIL_HOST="imap.gmail.com"
    export EMAIL_USER="you@gmail.com"
    export EMAIL_PASSWORD="your-app-password"
    python mark_unread.py

  Optional:
    EMAIL_PORT      - IMAP SSL port (default: 993)
    EMAIL_FOLDER    - Mailbox folder to process (default: INBOX)

  For Gmail, you must use an App Password (not your regular password).
  Generate one at: https://myaccount.google.com/apppasswords
"""

import imaplib
import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def mark_all_unread(host, port, user, password, folder):
    """Connect to the IMAP server and remove the \\Seen flag from all emails."""
    log.info("Connecting to %s:%s as %s ...", host, port, user)
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    log.info("Logged in successfully.")

    status, data = conn.select(folder)
    if status != "OK":
        log.error("Could not select folder %r: %s", folder, data)
        conn.logout()
        sys.exit(1)

    total = int(data[0])
    log.info("Folder %r contains %d messages.", folder, total)

    if total == 0:
        log.info("No messages to update.")
        conn.close()
        conn.logout()
        return

    # Search for all messages that are currently SEEN (read)
    status, data = conn.search(None, "SEEN")
    if status != "OK":
        log.error("Search failed: %s", data)
        conn.close()
        conn.logout()
        sys.exit(1)

    seen_ids = data[0].split()
    if not seen_ids:
        log.info("All messages are already unread.")
        conn.close()
        conn.logout()
        return

    log.info("Found %d read messages. Marking them as unread ...", len(seen_ids))

    # Build a comma-separated UID-style message set for a single STORE call
    msg_set = b",".join(seen_ids)
    status, _ = conn.store(msg_set, "-FLAGS", "\\Seen")
    if status != "OK":
        log.error("Failed to remove \\Seen flag.")
        conn.close()
        conn.logout()
        sys.exit(1)

    log.info("Done. %d messages marked as unread.", len(seen_ids))
    conn.close()
    conn.logout()


def main():
    host = os.environ.get("EMAIL_HOST", "")
    user = os.environ.get("EMAIL_USER", "")
    password = os.environ.get("EMAIL_PASSWORD", "")
    port = int(os.environ.get("EMAIL_PORT", "993"))
    folder = os.environ.get("EMAIL_FOLDER", "INBOX")

    if not host or not user or not password:
        print(
            "Error: EMAIL_HOST, EMAIL_USER, and EMAIL_PASSWORD "
            "environment variables are required.\n"
        )
        print(__doc__.strip())
        sys.exit(1)

    mark_all_unread(host, user, password, folder=folder, port=port)


if __name__ == "__main__":
    main()
