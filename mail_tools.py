"""A mail draft in the owner's Outlook, ready for them to read and send (plan 27).

Umay never sends mail: there is no tool for it, on purpose. She opens a mailto: address, and the new Outlook (the
owner's account lives there, and it has no COM to drive) opens a compose window of its own with To, Subject
and Body filled in. The owner reads it and presses Send.

Why not the mouse: the live tests of 2026-11-06 filled a mail form right about half the time, in 2-5 minutes, once
putting the body in Subject. mailto took 4 s, with Turkish letters and line breaks exact and 2,200 characters of text.
The compose window's title is the subject, so window_tools.start_and_wait can tell it really opened.
When the owner is away from the PC, a Telegram message says the draft is waiting (_tell_if_away).
"""
import os
import re
from urllib.parse import quote

import i18n
import input_tools
import pc_tools
import system_tools
import window_tools

MAX_URL = 16000  # tried with Outlook 1.2026.1020 on 2026-11-06
ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def addresses(text):
    """The addresses in "a@x.com, b@y.com". Anything else raises: the 4b would put a person's name here."""
    found = [a.strip() for a in re.split(r"[,;]", text or "") if a.strip()]
    for address in found:
        if not ADDRESS.match(address):
            raise pc_tools.ToolError(f"'{address}' isn't a mail address. Ask the user for the address, don't guess it.")
    return found


def mail_url(to, subject, body, cc=()):
    """The mailto address. Line breaks go as CRLF, which is what the standard asks for and Outlook keeps."""
    fields = [("subject", subject), ("body", re.sub(r"\r?\n", "\r\n", body))] + ([("cc", ",".join(cc))] if cc else [])
    query = "&".join(f"{name}={quote(value, safe='')}" for name, value in fields if value)
    return f"mailto:{quote(','.join(to), safe='@,')}?{query}"


def draft_mail(to: str = "", subject: str = "", body: str = "", cc: str = "") -> str:
    def run():
        to_list, cc_list = addresses(to), addresses(cc)
        url = mail_url(to_list, subject, body, cc_list)
        if len(url) > MAX_URL:
            raise pc_tools.ToolError(f"That mail is too long for a draft ({len(body)} characters). Shorten the text.")
        stop = input_tools._allowed(step=f"open a mail draft to {to or 'nobody yet'}")  # asks only in strict mode
        if stop:
            return stop
        result = window_tools.start_and_wait(subject or "Outlook", lambda: os.startfile(url))
        if " is open: " not in result:
            return f"The draft isn't confirmed: {result}"
        told = " The user is away, so they were told on Telegram too." if _tell_if_away(subject) else ""
        return (f"The draft is open in Outlook, to {', '.join(to_list) or 'nobody yet'}, subject '{subject}'. It has "
                f"NOT been sent. Tell the user: I've prepared the mail, please review it and press Send.{told}")
    return pc_tools._run(run)


def _tell_if_away(subject):
    """The owner's rule (2026-11-07): at the PC the answer is enough; away from it, a Telegram message. Not a call: a
    draft can wait. A request from Telegram gets its answer there already (only that channel sends photos)."""
    import ai_assistant  # imports this module
    import companion
    import telegram_listener

    if pc_tools.current_photo_sender() or system_tools.idle_seconds() < companion.away_seconds():
        return False
    try:  # False when Telegram isn't set up or the message failed
        return bool(telegram_listener.send(i18n.get(ai_assistant.current_language(), "mail_ready",
                                                    subject=subject or "-")))
    except Exception as e:  # the draft is there either way
        print(f"Mail: couldn't tell the owner on Telegram: {e}")
        return False


TOOLS = [{
    "type": "function",
    "function": {
        "name": "draft_mail",
        "description": (
            "Opens a mail draft in Outlook with the address, subject and text filled in, for the user to read and "
            "send. It never sends, and nothing here can: tell the user it's waiting for them. Use this for any mail, "
            "never the mouse and keyboard. Write the text the user dictated word for word."),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Mail addresses, separated by commas. Never a guess: if the "
                                                        "user gave a name without an address, ask for it."},
                "subject": {"type": "string", "description": "The subject line."},
                "body": {"type": "string", "description": "The whole text of the mail."},
                "cc": {"type": "string", "description": "Optional addresses to copy."},
            },
            "required": ["to", "subject", "body"],
        },
    },
}]

TOOL_FUNCTIONS = {"draft_mail": draft_mail}
