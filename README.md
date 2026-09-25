# Calcutta High Court Case Monitoring System

A general version of the cause list and display board monitor for advocates of the Calcutta High Court.
Anyone enters their own name once; every night the system finds their matters in the cause lists and
tells them on Telegram which court, item and heading each matter is in, and how likely it is to be
reached. During court hours it watches the live display board and alerts the phone when an item is near,
on, or when its heading closes before the item. It also answers questions about any court, judge, group
of matters or case, keeps the roster history, and understands early-rising notices.

> **Status: in preparation (private).** This repository currently holds the instruction manual for review.
> The program is being generalised from the working personal system and will be added here next.

## Instruction manual

Open [`docs/manual.html`](docs/manual.html) (download it and open in a browser) for:

- how the system works (diagram)
- installation at a glance (diagram) and detailed steps
- Android and iPhone alarm setup
- daily routine, how "likely" is worked out, questions you can ask
- courts rising early (bar notices)
- every Telegram and Terminal command, and a full feature checklist

## Requirements (planned)

- A Mac that is on during court hours (a Mac mini is ideal), with Google Chrome and Tampermonkey
- Telegram on your phone, and your own Telegram bot (made in two minutes with @BotFather)
- Optional loud alarms: ntfy (Android, free) or Pushover (iPhone)

## Notes

- It only reads the court's public cause lists; the display board CAPTCHA is always typed by the user.
- Chances are estimates from the cause list and the Bench's notes. Always check the determination yourself.
