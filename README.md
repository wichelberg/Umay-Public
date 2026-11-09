# Umay

A Windows desktop assistant that can act on your PC: warm, calm and honest, like a trusted family member (her persona is [personality.md](personality.md)). Say **"Hey Umay"** to talk to her, click her on the desktop to write to her, or use **Telegram** from your phone. Every action that changes something on your PC asks for your confirmation first: aloud on the PC (she listens for yes or no), with buttons on Telegram when the request came from there.

Umay speaks English. Her replies come from a local model on this PC (Qwen 3.5 4B through [Ollama](https://ollama.com), free and offline); `UMAY_LLM=openrouter` sends them to OpenRouter instead (any model there can be used). Web search goes to Tavily. On the desktop she is a HUD: an orb whose color and motion show what she is doing, the clock, and panels for the weather, the PC's vitals, its storage, the coming alarms, the news and the markets.

## Features

| Area | What she can do |
|---|---|
| Files | List, search, read, create, write, move and delete (to the Recycle Bin) files in the allowed folders, open them with their default app; tidy Downloads into Images / Documents / Code / ... subfolders with one confirmation (nothing deleted or overwritten) |
| Apps & games | Find and launch installed apps and Steam games, or run an `.exe` by its path (inside the allowed folders), then wait for its window and say what really happened; if Windows asks for administrator rights she tells you, because only you can press Yes. Move an open app's window to another monitor, minimize, maximize, restore or bring it to the front ("move Opera GX to the other monitor", "Spotify'ı küçült"): she reads the window back, so "done" means it happened. List open apps and close them (normally or forced); list running processes and end one by PID, like Task Manager |
| Browser | Open sites in Opera GX (new tab, or starts Opera), find a site by name via web search, search YouTube and open a result by number |
| Web control | With the Umay Web Control extension: Opera GX tabs (list, open, close, switch, back / forward / reload, zoom, pin, mute, duplicate, reopen the closed one, a new window), read a page part by part (also inside web components, like Brightspace's), find text on it, scroll, hover, click, type, pick options, tick boxes, fill several fields with one question, download (with Opera's own logins) and upload a file from the PC; search your bookmarks, history and downloads. Reading and moving around aren't asked; typing, buttons, downloads, uploads and closing a tab are asked where you asked from; password / card fields, buttons that send, pay, buy or delete, and account / payment pages are asked with a warning and the whole text. After she reads a page (or search results, or a message) the rest of that request is in strict mode: every question carries a warning, every mouse / keyboard step on the screen asks, and an address she made up herself is asked before it opens. "Find my Sample Data Analysis course on Brightspace": she walks the site herself in your Opera (links, menus, its own search; at most 30 pages / 5 minutes), leaves the page in front (and sends a screenshot on Telegram), and stops at a login, a CAPTCHA or a payment page. The video in a tab (loop on/off, its own volume in percent, mute) goes through it too, without asking, and the answer says the video's state; fullscreen is YouTube's own `f` key. If Opera can't run the extension (not loaded, its folder moved, an old copy, not connecting), she tells you on the PC and on Telegram |
| Media | Play/pause/next via media keys (works for YouTube, Spotify, ...), set the system volume |
| Spotify | What's playing (song, artist, position), play/pause/next/previous checked, shuffle, repeat, seek, Spotify's own volume; opens Spotify to play (Windows' media session, no account). With SPOTIFY_CLIENT_ID (Web API): play a song / artist / album / playlist by name, your own playlists and liked songs, queue, like, add to a playlist, play on another device |
| Python & dev | Run a `.py` file in the background (uses the project's virtualenv if there is one) and report the output when it finishes, optionally in a visible window; open a terminal or a folder in VS Code |
| Power | Shut down / restart now, in N minutes or when a script finishes (always with a 60 s warning you can cancel); lock the PC; show CPU/RAM/disk status |
| Steam | List installed games by size or last played; uninstall one |
| Health | Watches GPU temperature, RAM, CPU load and free disk space; past a limit (see `.env.example`) she warns you with a popup and on Telegram, with buttons to end the program causing it or leave it. Telegram reminders every minute from minute 3; with no answer by minute 10 and the value still bad, she ends that program herself. A full disk is only reported |
| Reminders | "Remind me at 9 tomorrow…", repeating alarms, turn alarms on/off. A ringing alarm goes to Telegram with Turn off / Snooze 10 min buttons, at once if you're away from the PC (10 min without input), otherwise if it isn't turned off within 2 minutes; after 15 minutes unanswered it goes quiet by itself (the popup stays) |
| Price alarms | "BTC 80 bini geçerse haber ver", "tell me if Tesla drops below 200", "USD/TL %1 oynarsa söyle": any stock, index, currency, commodity or crypto Yahoo Finance knows, on the markets panel or not. Above / below a price fires once when the price crosses it ("reaches 100k" picks the side from today's price; one set while already past waits for a new cross), a daily move of some percent fires once a day while the market is open. Checked every minute; she tells you aloud or in writing at the PC, on Telegram when you're away. A move far bigger than expected (a % alarm at twice its limit, or the market moving a stock / crypto 15%, an index 6%, a currency 4.5%, a commodity 7.5% in a day) is urgent: away, she calls you on WhatsApp (not in the quiet hours, at most twice a day; then Telegram). "List my price alarms", "remove alarm 2" / "remove the Bitcoin alarm". Stored in `%LOCALAPPDATA%\Umay\price_alerts.json` |
| Mail | "Write a mail to ali@example.com about...": a draft opens in Outlook in its own window, with the address, subject and text filled in. She never sends: you read it and press Send. If you're away from the PC, a Telegram message says it's waiting. A name without an address gets a question, not a guess |
| Calendar | "Add these to my plans": the events go into one `.ics` file (each with its own phone alarm) sent on Telegram; tap it on the phone to add them to Outlook. Each one also gets a PC reminder. Only when you ask for your plans/calendar; a plain "set an alarm" stays on the PC. She can read a numbered item of the development plan ("6. maddedeki alarmları planlarıma ekle") and never guesses a time that isn't given |
| Screen | Take a screenshot to check the result of an action, or send it to you on Telegram. She sees the monitor with the window in front at its full resolution (or the other one, or each of them, when she asks), so small text like a tab's name stays readable |
| Memory | Every conversation (text box, Telegram, spoken) is kept in `chat_logs`, and `search_memory` searches it. When one is over (15 minutes without a change) the local model calls it important or unimportant ("remember this" is always important; unsure counts as important). Past `UMAY_MEMORY_LIMIT_MB` (100 MB) the unimportant ones are deleted, oldest first; if the important ones alone are still over it, the oldest are summarized one by one, and every date and time in them is kept (the ones a summary leaves out are added under it). Not during a game. Log: `%LOCALAPPDATA%\Umay\memory.log` |
| Memory Cortex | What Umay knows about you, as a tree of short facts (User > Location > Lives in Springfield > ...), in `%LOCALAPPDATA%\Umay\umay_memory.db`. Each conversation is read once when it is over (at once after "Umay, goodbye" or closing the box, else after 15 quiet minutes): the local model takes the facts **you** said (never ones only Umay said), files each next to the facts it relates to, and replaces a fact that stopped being true (the old one is kept, hidden). There is no "save this" command: learning is automatic. Correct her in a conversation ("I don't own a fold phone") and the wrong fact is hidden when it is learnt. On Telegram, /yeni ends the conversation (an hour's silence too). Someone is family only if you said so. Your own projects go under Projects, your job's under Work (a group when you name one), the things you have (games, devices) under Things; people, channels and brands you watch or follow are kept as interests. What the memory knows about you goes to no cloud: it is masked in whatever reaches the heavy model (what you said in that very request stays, and so do screenshots), web searches can't carry a private name you didn't say, and she makes no web searches from the memory's topics or names unless you set `UMAY_MEMORY_WEB=on` (then, while you are away, she looks each interest up once, at most 6 a day, and groups them). While you are away (no input for `UMAY_AWAY_MINUTES`) she reads all the logs again once (at most once a day; the logs from before the cortex too). After every message she searches the whole memory (a few milliseconds, without embeddings) and the reply gets your Identity (your name) and only what fits: a project, a person, a game by its name, every fact a name appears in, facts sharing a meaningful word with your message, or the matching part of a branch for a word like "projects" or "where do I live". A name she only hears close ("Sem and Creator") makes her ask "Did you mean Sam Creator?". When a tool opens or lists a folder, its PC path goes on the project named like it, so the next "open the Umay project" goes straight there; a file she reads that pairs names with links (like `YouTube_Channel_Links.txt`) puts each link on that person, and she opens it instead of searching. Umay's own research, only while you are away, on who the newsfeed follows (one Tavily search every 8 hours at most, two a day, none under 300 credits left), is listed in `%LOCALAPPDATA%\Umay\self_research.md` with what you said about Umay; her own branch (Self) stays empty until you start it (`UMAY_SELF_GROW=on`), and none of it goes into her persona. The HUD's MEMORY panel (a click), "open the memory editor" or `Umay.exe --memory` opens the editor: rename, drag to move, Delete hides, Shift+Delete deletes for good, "Research file" opens the research list. The primary monitor's wallpaper shows the tree, lighting up the facts she really uses (behind the desktop icons; drawing stops while a window covers the desktop). On that desktop Umay herself is the wallpaper's outer ring, in her state's color, with star dust inside, her name at the top and the clock at the bottom; the orb shows again when it docks to a window. A button in the primary monitor's bottom-right corner lists the HUD panels: a click turns one on or off (Show all / Hide all at the end). Once a day while you are away the memory tidies itself, without asking: a fact naming something goes under it, duplicates merge (only when the code's checks of names, numbers and negation and the local model asked both ways all agree), one-off events a week past go; what goes is hidden from Umay at once and deleted after 30 days. Never while she listens, answers or talks, nor during a game. Log: `cortex.log` |
| Eyes | "Umay, eyes on" turns the laptop camera on and your hands move the mouse. One hand works like a trackpad: moving an open hand moves the cursor (slowly: finely; fast: across both monitors; where the hand is in the picture doesn't matter), a fist holds the left button (grab and drag), opening the hand lets go; touching the thumb's tip to the index's (the middle finger out, like an OK sign) is a click, twice quickly a double click (the cursor stands still meanwhile). A window grabbed with a fist and thrown: left / right sends it to the monitor on that side, or closes it if there is none (as its ✕ would: unsaved work still asks; never the desktop, the taskbar or Umay); up maximizes it, down minimizes it. A push closes the active window: click it, then turn the hand side-on (its back leading) and push it slowly aside; no speed needed. Two hands, Tony Stark's way: both open and moving apart / together zoom in / out (Ctrl + the wheel, where the cursor is); both side-on and pushed outwards show every window (Win+Tab); two fists stretch the window under the cursor. A small preview at the bottom left shows what the camera sees, both hands and the mode. Of several cameras the fastest is used (never your phone's). Two hands cost ~10 ms a frame on the CPU. "Hey Umay" opens her eyes too (`UMAY_EYES_ON_WAKE=off`: not). "Eyes off", a ✌ held for a second, or 5 minutes without a hand closes the camera. Said any time (the wake listener hears it), in a conversation, or typed. MediaPipe on the CPU, offline, in its own `hands_env` (`python setup_voice_env.py --hands`) |
| Mouse and keyboard | For a task no other tool covers, she works the PC like a person, from screenshots: clicks, scrolls a web page down or up, types and presses keys ("ctrl+l", "enter"), one step at a time, checking the screenshot after each. Each click names its target ("the Download button") and the heavy model (Claude Code Haiku, `UMAY_HEAVY_MODEL`) finds the exact point on a fresh copy of the screenshot, ~5 s; the local model's own point is used only when it can't (offline, `off`), and if it says the target isn't on the screen nothing is clicked. Each click says what it hit (UI Automation) and the screenshot after it has a red ring where it went; a click on the background is reported as a miss. With the local model a short task takes a minute or two (a step every ~5-10 s) |
| Plans | A request that takes several actions in a row ("open Brunel's Brightspace in Opera and download the Sample Data Analysis assessment brief", "find jobs in the UK and Türkiye from the CV in Documents\Projects\CV") becomes a plan: the local model tells such a request from a simple one (~1 s), the heavy model writes 2-10 steps over Umay's real tools (~10 s), the plan is shown in the text box or on Telegram and starts at once. The local model does each step seeing only that step, what the earlier steps found and the step's own tools; a step that fails gets one new attempt rewritten by the heavy model, then the plan stops and says where. If the planner needs an answer first (at most 2 questions) it asks, and your next message is the answer. Everything a step does is asked as usual. Without the heavy model (offline, `off`) she answers the old way. Log: `%LOCALAPPDATA%\Umay\heavy_model.log` (each call, its list price and the plan usage Claude Code reported) |
| Sleep mode | "Finish this, I'm going to sleep": after one confirmation, Claude Code does the job alone in `Documents\Projects` / `Documents\Work` (if a first attempt fails it investigates and finds its own way). Every project folder is copied to `Documents\Failsafe` before its first change; at the end a detailed report (what changed, what was done and why) is sent on Telegram and saved in the changed folders, then the PC shuts down |
| WhatsApp | Everything goes through the owner's WhatsApp Desktop app (WhatsApp Beta beside it holds Umay's own account for calling you; your tasks never use it, and `whatsapp:` links open in your app by its package) (no browser, no WhatsApp Web): list your chats, read a chat's latest messages (who, when, links, which ones are photos/videos/documents), opening it by name first if needed; send a file to a phone number or to a chat by name ("send the CV in Downloads to +44 7700 900123 on WhatsApp", "Annem'e bu fotoğrafı at"). You see a screenshot of the ready chat and it's sent only after you confirm. Reading is asked first, where you asked from (aloud on the PC, on Telegram). No contact list is stored |
| On her own | She starts the talk herself (`companion.py`): in the morning, the first time the camera sees you at the PC (your face, once she learned it: "Umay, learn my face"; only numbers describing it are kept, no photo), she says good morning, today's weather, reminders, a holiday and a headline, and asks about your plans. During the day she brings up news her model rates very important, a market's big move, rain coming, a low battery, two hours without a break. Aloud, as a conversation you can answer, when the room is quiet; written in the text box (it opens without taking the keyboard) when the room is loud, another program uses the microphone (a meeting), in the quiet hours (23-08) or her voice can't run; nothing but urgent things during a game. Away from the PC (5 min without input), a ringing reminder, huge news or a market's crash (a stock or coin -15% in a day, an index -6%; not in the quiet hours, then Telegram) is a WhatsApp call; the rest waits until you're back ("while you were away..."). Log: `%LOCALAPPDATA%\Umay\companion.log` |
| Calls | Umay calls you on WhatsApp from her own account in WhatsApp Beta (`/ara` on Telegram, "call me", or when something urgent needs you) and answers your calls to it, to your number in `UMAY_CALL_NUMBER` only. She speaks first, then listens and answers until the call ends; her voice goes into the call through VB-CABLE (she plays into "CABLE Input", the call takes "CABLE Output") and she hears you from the speakers' loopback. Beta's call engine uses Windows' default *communication* devices whatever its settings say, so during her call those are the speakers and CABLE Output; yours come back when it ends (or when Umay starts, after a crash). Voice only: the camera stays off (WhatsApp shares a screen only in a video call; a camera photo only when you ask, `/kamera`). While a game runs she neither calls nor answers (the game keeps the VRAM) and says so on Telegram. Steps: `%LOCALAPPDATA%\Umay\calls.log`, what was said: `chat_logs/<time>_call.txt` |
| Web & memory | Web search (Tavily): news only from the last 24 hours, with its date, other questions unfiltered; search past conversations |
| Voice | Say "Hey Umay" and talk; she answers out loud and keeps listening until "Umay, goodbye", the orb showing whether she listens, thinks or speaks; send Telegram voice notes (she confirms what she understood before acting) |
| Claude Code | "Umay, open Claude Code and fix the news panel in Umay" (or "Claude Code'u aç ve ..."): a Windows Terminal window opens with an interactive [Claude Code](https://claude.com/claude-code) session already working on the task, in the project you named (a folder in Documents\Projects or Documents\Work), so you can watch it and talk to it. From Telegram, `/c` hands a job to a headless session and sends its answer back |
| Credits | See your remaining OpenRouter credit and roughly how many messages it covers; the HUD's credits panel shows the OpenRouter and Tavily credit and what today / 7 / 30 days cost, and a click on it opens the API credit log: every paid request (text box, Telegram, voice chat, cloud speech-to-text, disk scan) with what it was for, model, tokens, tools called, the cost OpenRouter reported, the balance before and after and their difference (OpenRouter shows a new balance about a minute late), plus totals for today / 7 / 30 days and by source. Stored in `%LOCALAPPDATA%\Umay\credit_log.sqlite` |
| Disk | A click on the HUD's storage panel shows every drive and what takes the space on C: as items (Steam games, programs, Pythons and venvs, caches, HuggingFace models, projects, your folders, unknown big folders) with size, last use and its source, what it is, whether it can be downloaded again and what depends on it; delete goes the way the kind is meant to (Steam, the uninstaller, `pip cache purge`, Recycle Bin…) after a confirm listing what breaks. Once a day an idle scan runs and a "Disk: new findings" line shows beside the orb past the `UMAY_DISK_*` limits (a click on it opens the storage view). At start she deletes the `_MEI` folders force-closed exes left in Temp. Memory and history live in `%LOCALAPPDATA%\Umay\disk` (`knowledge.json` can be edited by hand) |
| Desktop | A HUD (`hud_app.py`, Qt): in the middle of its monitor the UMAY orb with the clock; its color and motion show her state (standby cyan, listening green, thinking violet, researching amber, working magenta, speaking white with bars following her voice). The orb sits small in the bottom-right corner of the active window, on whichever monitor, and follows it; there it slips away from the cursor and comes back when the cursor leaves, and something fullscreen hides it. With the desktop active it goes back to the middle of its monitor (or that monitor's corner if windows are open there). While VS Code is open (not minimized) the orb always sits in the bottom-right corner of VS Code's window instead, whichever window is active, and shows a red WORKING (her other states keep their own colors). Seven panels: weather (now and the next 24 hours, Open-Meteo), vitals (CPU, GPU, temperature, RAM, VRAM), storage, the calendar with the coming alarms with Türkiye's and the UK's holidays (`holiday_days.py`), the API credits, the news (the latest about who you follow, `UMAY_NEWS_WATCH`: Anthropic, Elon Musk, Sam Altman, OpenAI, Türkiye, the UK; say or write "follow Nvidia in the news" / "unfollow Elon Musk" ("haberlerde Nvidia'yı takip et", "Elon Musk'ı takipten çıkar") to change it, or ask who you follow; then Google News' sections) and the markets (`UMAY_MARKET_SYMBOLS`: price, change and today's curve, read every 10 s from Yahoo Finance; "add Apple to the markets" / "remove Tesla from the markets" ("Aselsan'ı borsaya ekle", "Bitcoin'i borsadan çıkar") changes them; "how is Nvidia looking?" gives its trend, averages, RSI, recent news and one sentence on which way it leans and why, a rule on those numbers, not a forecast). A click on a panel grows it into a detailed view in the middle of its monitor (Esc or a click outside shrinks it back): weather with every value, 48 hours and 7 days; each CPU core, the GPU's power and clocks, the last 5 minutes and the busiest programs; the disk scan with delete; the month with every alarm; the full credit log; every article read, filtered by name or section (a click opens it in the browser); each symbol with a chart over 1 day to 5 years, its day and year range and volume. Every panel and the orb can be dragged to any monitor and is remembered there; a monitor that is gone sends its windows to their default spots on the primary, and back when it returns. Click the orb (or Ctrl+F1) to open the text box; Ctrl+F2 hides or shows the HUD |

## Requirements

- Windows 10/11
- Python 3.12+ (developed on 3.13)
- [Ollama](https://ollama.com) with `qwen3.5:4b` (~3 GB on disk, ~3.5 GB of VRAM) and `qwen3-embedding:0.6b` (the
  memory's embeddings, ~0.6 GB, run on the CPU), a [Tavily](https://app.tavily.com) API key
- Optional: an [OpenRouter](https://openrouter.ai/settings/keys) API key (`UMAY_LLM=openrouter`, or cloud speech-to-text as a fallback)
- Optional: Opera GX, Steam, VS Code, Claude Code, a Telegram bot (see below)
- Optional, for WhatsApp: [WhatsApp Desktop](https://www.whatsapp.com/download) from the Microsoft Store,
  logged in. Umay reads it through Windows UI Automation (`comtypes`); if a WhatsApp update
  breaks reading, `python whatsapp_tools.py --dump` writes what she sees of its window.
- Optional, for browser control and the video in a tab (loop, volume, mute): load the Umay Web
  Control extension (its own project, `Documents\Projects\Web_ControlExtention`) in Opera GX once
  (`opera://extensions` → Developer mode → Load unpacked). The extension connects to Umay on
  `127.0.0.1:47821`; she only accepts that extension, and asks you before typing, sending and downloads
  (see Web control above). Opera loads an unpacked extension from its folder every time: if the folder moves, remove
  the extension in `opera://extensions` and load it from the new place (Umay notices and tells you).
  After updating its files, press the reload button on its card (she tells you if Opera runs an old copy).

## Setup

```powershell
git clone https://github.com/<you>/Umay.git
cd Umay
python -m pip install -r requirements.txt

ollama pull qwen3.5:4b                       # Umay's local model (Umay makes her 16k-token copy of it)
ollama pull qwen3-embedding:0.6b             # the Memory Cortex's embeddings (CPU, no VRAM)
copy .env.example .env                       # then fill in TAVILY_API_KEY
copy i18n.example.py i18n.py                 # required: UI text (edit it there)
copy config.example.json config.json         # optional: example alarms (reminders create it anyway)
```

Optionally put your own picture at `assets/user.jpg` to show it next to your messages.

## Run

```powershell
python main.py
```

Umay runs in the background, on the desktop as the HUD. A click on the orb opens or closes the
text box beside her, and so does **Ctrl+F1**; **Ctrl+F2** hides or shows the HUD; a click on a HUD panel
opens its detailed view; "Hey Umay" starts a spoken conversation.

### Build the exe

```powershell
python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File build.ps1
```

This creates `dist\Umay.exe`. Put your `.env` (and optionally `config.json`)
next to the exe; `personality.md` is built into it; chat logs and script logs are written there too. To start
Umay with Windows, put a shortcut to the exe in `shell:startup`. Only one Umay runs at a time: started again
while one is running, the new one quits at once.

## Commands

Most things are asked in plain language, in Turkish or English, for example:

```
Opera GX'te YouTube'u aç
lofi hip hop ara        →  3'ü aç  →  tam ekran yap
Masaüstündeki rapor.pdf'i İndirilenler'e taşı
C:\Users\me\Documents\Projects\demo\train.py'yi çalıştır, bitince PC'yi kapat
2 saat sonra PC'yi kapat
Yarın 9'da annemi aramamı hatırlat
En çok yer kaplayan Steam oyunlarım?
Ekranda ne var, video durdu mu?
```

### On the PC

| Input | What it does |
|---|---|
| "Hey Umay" | A spoken conversation: the light goes up around the screen, she listens, answers out loud and listens again. Only on mains power with no game running |
| "Umay, goodbye" | Ends it at once; nothing is sent to a model. 30 s with nothing said ends it too |
| Click on the orb | Opens / closes the text box beside it (Esc also closes it). Her answer shows in the box, silently, while the orb thinks and then talks |
| **Ctrl+F1** | Opens / closes the text box (beside the orb, or bottom right with the HUD hidden), in the HUD's look |
| **Ctrl+F2** | Hides / shows the HUD |
| **Ctrl+F3** | She stops listening, or listens again: the microphone closes, a spoken conversation ends, and a muted microphone shows above the bottom-right button. Back on after a restart |
| Click on a HUD panel | Grows it into its detailed view in the middle of its monitor; Esc, a click outside it or its ✕ shrinks it back. Dragging a panel still moves it |

Before she changes something on the PC, a small yes/no box in the same look asks first
(beside the orb). Every conversation is written to `chat_logs\` (spoken ones as `*_voice.txt`).

### Telegram

| Command | What it does |
|---|---|
| any message | Goes to Umay when no Claude Code session is waiting for an answer |
| `/z <message>` | Always goes to Umay (`/h` also works) |
| reply to her message | Goes to Umay |
| voice note | Transcribed; she asks "is this what you meant?" with Yes/No buttons before acting (a Yes also keeps the clip for voice training) |
| `/c <task>` | Hands the task to Claude Code and sends back its result |
| `/yeni` (`/new`) | Starts a fresh conversation (also resets the Claude Code session) |
| `/iptal` (`/cancel`) | Cancels a planned or counting-down shutdown immediately |
| `/ss` (`/ekran`) | Sends a screenshot |
| `/kamera` (`/camera`) | Sends a photo from the laptop's camera (only that camera, never a phone's or a virtual one) |
| `/kredi` (`/credits`) | Remaining OpenRouter credit |
| `/status` | Shows waiting Claude Code sessions and whether Umay is listening |

Confirmations arrive as ✅ Yes / ❌ No buttons; no answer within 10 minutes counts as No.

## Telegram

Umay doesn't talk to Telegram directly: she shares a bot with **claude_comm**, a small
standard-library tool that lets Claude Code sessions ask you questions on Telegram. Only one
program may poll a bot at a time, so Umay polls through claude_comm's router: messages
that belong to a waiting Claude Code session go there, everything else goes to her. Set
`CLAUDE_COMM_PATH` in `.env` to your `claude_comm.py`; without it the Telegram features are
simply off.

## Local speech-to-text

What you say (in a spoken conversation and in Telegram voice notes) is transcribed on this PC by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), free and in well under a second
on the GPU. It runs in its own small Python (`voice_env`) next to Umay, so the exe stays
small: voice_env only holds faster-whisper and ctranslate2 and borrows the rest (numpy,
onnxruntime, tokenizers, and the CUDA libraries that ship with torch) from an existing venv
that has torch, by default Wichelberg's training venv, which it never changes. Set it up once:

```powershell
python setup_voice_env.py            # creates voice_env, downloads the model (~1.6 GB), checks it loads
python setup_voice_env.py --test some_voice_note.ogg
```

Everything goes to `%LOCALAPPDATA%\Umay\voice` (`UMAY_VOICE_HOME`). Without voice_env,
or if the local model fails or hears nothing, transcription falls back to OpenRouter as before.
Audio longer than 30 s (long voice notes) is first cut at pauses and the pieces are
transcribed separately, so no words get lost where Whisper's 30 s windows meet. The model is
loaded on first use (or ahead by voice_guard) and unloaded after 10 idle minutes.

The default model is [RsGoksel_ITU_Mainframe-ct2](https://huggingface.co/RsGoksel/RsGoksel_ITU_Mainframe-ct2),
Whisper large-v3-turbo fine-tuned for Turkish (MIT; it often writes lowercase without punctuation).
Measured on an RTX 5070 Laptop with 71 Common Voice Turkish test clips (sentences without
numbers, since Whisper writes numbers as digits):

| Model | Word error rate | Time for a ~4 s clip | VRAM |
|---|---|---|---|
| Turkish turbo (default) | 5.0% | 0.36 s | 1.2 GB |
| `large-v3-turbo` | 6.8% | 0.35 s | 1.2 GB |
| `large-v3` | 6.2% | 0.82 s | 2.1 GB |
| Gemini Flash on OpenRouter (before) | 3.8% | 4.5 s | – |

### Training data for your voice

To later fine-tune Whisper on your own voice, Umay keeps clips whose text you confirmed:
a Telegram voice note you answered **Yes** to. To collect more at once, read sentences aloud:

```powershell
python voice_recorder.py             # Space: record/stop · D: play back · Enter: keep, next · S: skip
```

It shows ~230 Turkish sentences (`voice_sentences_tr.txt`: commands, everyday talk,
numbers and dates, English tech words), skips the ones you already recorded and warns
when a take is too quiet or clips. Clips go to `%LOCALAPPDATA%\Umay\voice\data`
(`clips\*.flac` + `samples.jsonl`), never into the repo. `UMAY_VOICE_DATA=off` keeps nothing.

### Fine-tuning on your voice

`train_stt.py` trains a LoRA on top of the Turkish model (its trainable
[Hugging Face weights](https://huggingface.co/RsGoksel/RsGoksel_ITU_Mainframe), downloaded
once, ~1.6 GB) with those clips. It runs in `train_env`, made the same way as voice_env
(it adds only peft, accelerate and ctranslate2's converter):

```powershell
python setup_voice_env.py --training
%LOCALAPPDATA%\Umay\voice\train_env\Scripts\python.exe train_stt.py --cv 200 --cv-test 60
```

Longer recordings work too: read the sections of `voice_reading_tr.txt` aloud (~2 minutes
each) with any recorder, on the PC and on the phone, and drop the files into `UserVoice\`
named `<section>_<device>.<ext>` (`1_pc.m4a`, `1_telefon.m4a`, ...; m4a and other formats are
decoded with ffmpeg). The last section is never trained on: it scores each device separately.

It holds back ~15% of your sentences as a test, scores the model on them before and after
training, then merges and converts the tuned model into `voice\models\<name>` and prints the
`UMAY_STT_MODEL=<name>` line that switches Umay to it (nothing changes until you add it
to `.env`). Clips longer than Whisper's 30 s are cut at quiet moments and their text is shared
out over the pieces. `--cv N` mixes Common Voice clips in so it doesn't unlearn other voices,
`--cv-test N` checks that. On the RTX 5070 Laptop, 200 clips take ~1.5 minutes per epoch and
~2.7 GB of VRAM. Every number and test transcript lands in `voice\training\<name>\report.json`.

First run (2026-10-18, `--cv 200 --cv-test 60 --epochs 6 --accumulate 2`, 19.5 minutes of
reading on two devices, 11 minutes of training), scored through Umay's own runtime on
section 6, which it never trained on:

| | Base | Tuned |
|---|---|---|
| Your voice, PC recorder | 8.0% | 7.1% |
| Your voice, phone recorder | 15.8% | 12.8% |
| Other voices (Common Voice, short clips, numbers aside) | 4.8% | 6.0% |

## Spoken replies

Spoken replies are read by Chatterbox Multilingual (`tts_worker.py` in `voice\tts_env`) and turned
into Umay's voice by the RVC voice pack (`voice_worker.py` in Wichelberg's Python). Both run as
a pipeline: while one sentence goes through the voice pack, Chatterbox already reads the next.
The first piece is cut short (at its first comma, or after ~7 words) so the first sound comes
early, and no piece is more than 1.5x as long as the one before (cut at commas), so each is ready
before the one before has been played. Both workers make one warm-up run before they say they're
ready (the first sentence after loading was 1-5 s slower). The voice pack runs on the GPU through
DirectML, but only because its output was measured to be the same as the CPU's by Wichelberg's
parity rule (`voice\rvc_parity.json`: checked once per GPU, ONNX Runtime version and voice pack; a
GPU that fails is never used). During a game (`voice_guard`) nothing is said and nothing local
runs: the reply stays as text, as it does when the GPU is busy; speech is never sent to a paid
service. The voice models leave the GPU at once; the local model only if the VRAM is still over
`UMAY_GAME_VRAM_LIMIT_MB` (7000) without them, so a light game keeps it (quick text replies) and a
heavier one gets all the VRAM. Audio longer than 8 s goes to the CPU, since DirectML's memory grows with the longest input (+1.8 GB at
6 s, +6.4 GB at 30 s). Chatterbox's token model (T3) runs in bfloat16: 1 GB less, same speed, Whisper heard it as well
(WER 5.4% vs 9.0% in fp32 on 20 sentences). With Whisper (1.2 GB), Chatterbox (2.7 GB) and the voice
pack (up to 2 GB) loaded the 8 GB card is ~81% full (93.7% before), so the voice pack loads only when a conversation starts, unloads after 3 idle minutes, and is the
first to move to the CPU (for 10 minutes) if the VRAM gets full. `voice\speech.log` gets one line
per spoken reply: how fast it started, the gaps, where the voice pack ran.

Measured on the RTX 5070 Laptop, GPU otherwise idle, models warm, 3 replies of 3-4 sentences × 2:

| | First sound (median) | Gaps between sentences (total, 12-16 joins) | Time / audio length |
|---|---|---|---|
| One sentence after another, voice pack on CPU | 6.65 s | 10.2 s | 1.58 |
| Overlapped | 6.49 s | 0.7 s | 1.47 |
| + short first piece | 4.93 s | 7.2 s | 1.39 |
| + voice pack on GPU | 2.9 s | 0.9-2.6 s | 1.2 |
| Installed app, one real 🎤 reply (before the 1.5x rule) | 4.35 s | 3.1 s (one long piece) | – |

What's left is Chatterbox itself: ~2.5 s for even 3-4 words (its token loop runs at ~19 ms per
token, and how long it makes a piece varies from run to run), so the piece after a short first one
is sometimes not ready in time (a pause of up to ~1.5 s after the first comma). With other GPU users
(e.g. Wallpaper Engine) the first sound was measured at 3.2-4.7 s.

## Wake phrase

Saying **"Hey Umay"** (or "Umay, wake up") starts a spoken conversation (`voice_session.py`): she listens until you've finished (1.2 s of quiet), answers out loud,
and listens again until **"Umay, goodbye"** (recognized locally: nothing is sent) or 30 s with
nothing said. Turns with no speech in them, or sound that never pauses for 15 s (a video, not you),
are never sent to a model, and speech is transcribed only by the local Whisper here. The orb
listens, thinks while the answer comes (researching or working while her tools run) and speaks
with her voice.

With `UMAY_GLOW=on` a light also comes up around the screen during the conversation (off by default).
The light is one rounded frame round the whole screen (no seams at the corners), purple to cyan
plasma wisps that rise and fall with her voice and yours, drawn at full resolution and ~55-60 fps
(measured on the Ryzen AI 7 350: ~0.65-0.8 of one CPU core, only while a conversation is on; the
frame is worked out on an unrolled strip and mapped onto the four screen edges).

Her name is said the Turkish way, and English Whisper wrote it "you me", "u mai", "o mais": each bit is
heard with **"Hey Umay."** as Whisper's prompt (`UMAY_WAKE_PROMPT`), which turned every call on a recording of
the owner into "Hey Umay." and none of their other sentences into her name; those spellings (and "hei", "ey"
before it) count as her name too. "Umay, eyes on / off" is heard the same way (see Eyes).

`wake.py` reads the
microphone on the CPU (~4.5% of one core with Windows' audio engine, measured), cuts out short bits
of speech by loudness (0.4-3.5 s, clearly louder than the room: a video playing in the background
becomes the room's level), and hands only those to the local Whisper that `voice_guard` keeps
loaded (~0.3 s on the GPU each). Nothing goes to OpenRouter. It runs only in `voice_guard`'s
"ready" mode: on battery or as soon as a game shows up the microphone is closed. It doesn't listen
while Umay speaks or a conversation is on. `voice\wake.log` shows when it listened and every bit it
heard (what was said only if it sounded like the phrase).

On battery, measured on the RTX 5070 Laptop: idle 26.0 W, with Whisper loaded 26.6 W, with Whisper
and the microphone listening 26.2 W (±1.5 W): no measurable difference, because other apps keep the
NVIDIA GPU awake anyway. If the GPU could sleep, a loaded Whisper would keep it awake (it idles at
~10-13 W in P2), which is why it is off on battery.

## Configuration

All settings live in `.env`; see [.env.example](.env.example) for the full list with
comments. The most useful ones:

| Setting | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY`, `TAVILY_API_KEY` | – | Required keys |
| `UMAY_LANGUAGE` | `en` | Language of the text box and the yes/no box (`en` or `tr`) |
| `UMAY_USER_NAME` | `You` | Your name in the text box and the chat logs |
| `UMAY_LLM` | `local` | Where her replies come from: `local` (Ollama, `UMAY_LOCAL_BASE_MODEL` = `qwen3.5:4b`, `UMAY_LOCAL_CONTEXT` = 16384 tokens) or `openrouter` |
| `OPENROUTER_MODEL` | Gemini Flash | The model Umay talks with on OpenRouter |
| `UMAY_HEAVY_MODEL` | `claude-code:haiku:low` | The heavy model that plans multi-step requests and aims clicks: Claude Code (`claude-code:<model>:<effort>`) on your subscription, never paid per request; `off` = none. At most `UMAY_HEAVY_MAX_CALLS` (40) calls per request |
| `UMAY_ALLOWED_DIRS` | Desktop;Downloads;Documents | The only folders she may touch |
| `CLAUDE_COMM_PATH` | – | Enables Telegram |
| `UMAY_STT` | `local` | Speech-to-text: `local` (Whisper, falls back to OpenRouter) or `openrouter` |
| `UMAY_STT_MODEL`, `UMAY_STT_LANGUAGE` | `large-v3-turbo`, `en` | Local Whisper model and language (`auto` detects it); run `setup_voice_env.py` after changing the model |
| `UMAY_STT_IDLE_MINUTES` | `10` | Unload the local model after this long unused (0 = keep it) |
| `UMAY_VOICE_DATA` | `on` | Keep confirmed voice clips for training (`off` = keep nothing) |
| `UMAY_TTS` | `local` | Spoken replies, only on this PC, sentence by sentence; when it can't run (a game) the reply stays as text, nothing is ever paid for. `off` = no spoken replies |
| `UMAY_TTS_ENGINE`, `UMAY_PIPER_VOICE` | `piper`, `en_US-lessac-medium` | The reader: English Piper on the CPU (`voice\piper_env`), or `chatterbox` (Turkish, `voice\tts_env`, GPU only) |
| `UMAY_VOICE_PACK` | `umay` | RVC voice pack that turns the spoken reply into Umay's voice (a folder, or a name in `voice\rvc` or Wichelberg's library); `umay` is her own pack in `voice\rvc\umay`, empty = off |
| `UMAY_RVC_DEVICE` | – | `cpu` or `gpu` (DirectML) for the voice pack; unset, the CPU with the local model (it and Whisper hold the GPU), else the GPU; the CPU during a game |
| `UMAY_MEMORY` | `on` | Judging finished conversations and keeping `chat_logs` under its limit (`off` = keep everything as it is) |
| `UMAY_MEMORY_LIMIT_MB` | `100` | Past this, unimportant conversations are deleted, then important ones summarized (their dates kept) |
| `UMAY_CORTEX` | `on` | The Memory Cortex: learning facts from conversations and adding them to replies (`off` = none) |
| `UMAY_SELF_RESEARCH` | `on` | Umay's slow web research on who the newsfeed follows (`off` = no Tavily searches of her own) |
| `UMAY_MEMORY_WEB` | `off` | `on` = that research and looking up your interests may also use the memory's own topics and names (they go to Tavily) |
| `UMAY_SELF_GROW` | `off` | Umay's Self branch grows from her research and what you say about her (`on` when you start her personality) |
| `UMAY_EMBED_MODEL` | `qwen3-embedding:0.6b` | The Ollama model for the memory's embeddings |
| `UMAY_CORTEX_WALLPAPER` | `on` | The memory as the primary monitor's wallpaper (`off`; `window` = a bottom window if the desktop layer isn't found) |
| `UMAY_CORTEX_HUDPOS` | `bl` | Where the wallpaper's info box sits: `bl`, `br`, `tl`, `tr` |
| `UMAY_CORTEX_DB` | `%LOCALAPPDATA%\Umay\umay_memory.db` | The memory file |
| `UMAY_GAME_VRAM_LIMIT_MB` | `7000` | During a game the voice models leave the GPU at once; the local model too if the VRAM is still over this without them (`0` = always) |
| `UMAY_WAKE_PROMPT` | `Hey Umay.` | Whisper's prompt for what the wake listener hears (it keeps her name spelled right in a Turkish accent) |
| `UMAY_EYES_ON_WAKE` | `on` | "Hey Umay" turns the camera's hand control on too (`off` = only "eyes on") |
| `UMAY_EYES_PYTHON` | – | The Python of the camera's environment (default `voice\hands_env`) |
| `UMAY_WAKE` | `on` | "Hey Umay" starts a spoken conversation (only on mains power, no game); `off` = never |
| `UMAY_RVC_IDLE_MINUTES` | `3` | Unload the voice pack after this long unused (it reloads in ~2 s when a conversation starts) |
| `UMAY_HUD` | `on` | The HUD on the desktop (`off` = none) |
| `UMAY_GLOW` | – | `on` = the light around the screen during a spoken conversation |
| `UMAY_WEATHER_LOCATION` | – | The weather panel's place: a city or `lat,lon` (default: this PC's IP's city) |
| `UMAY_NEWS_WATCH` | `Anthropic,Elon Musk,Sam Altman,OpenAI,Türkiye\|Turkey\|Erdogan\|-Thanksgiving\|-recipe\|-recipes\|-roast\|-bingo,UK\|Britain\|British` | The news panel's followed people, companies and countries (a Google News search each, the last 3 days); after a `\|` come other spellings it is searched by too, and `-word`s that leave out articles with that word. Once you follow or unfollow someone by voice or text, her own list (`%LOCALAPPDATA%\Umay\news_watch.json`) is used instead |
| `UMAY_NEWS_TOPICS` | `TECHNOLOGY,BUSINESS,WORLD,SCIENCE` | Its other headlines: Google News sections (`TOP`, `WORLD`, `NATION`, `BUSINESS`, `TECHNOLOGY`, `SCIENCE`, `HEALTH`, `SPORTS`, `ENTERTAINMENT`) |
| `UMAY_NEWS_REGION` | `en-US` | The news' language and country (`tr-TR`...) |
| `UMAY_HOLIDAYS`, `UMAY_HOLIDAYS_UK_PART` | `TR,GB`, `ENG` | The calendar's holidays: countries (the `holidays` package's codes) and which UK bank holidays (`ENG`, `SCT`, `WLS`, `NIR`) |
| `UMAY_COMPANION` | `on` | `off` = she never starts a conversation herself (`companion.py`) |
| `UMAY_AWAY_MINUTES` | `5` | No input this long: you're away (urgent things become a WhatsApp call) |
| `UMAY_QUIET_HOURS` | `23-08` | No voice and no news calls in these hours (the morning greeting is spoken anyway) |
| `UMAY_NOISY_LEVEL` | `0.004` | The room's level above which she writes instead of speaking |
| `UMAY_NEWS_SCORE`, `UMAY_NEWS_CALL_SCORE` | `9`, `10` | Her rating (1-10) of a new headline from which she tells it, and calls you about it while you're away |
| `UMAY_BREAK_MINUTES` | `120` | At the PC this long without a break: she suggests one (`0` = never) |
| `UMAY_CLAUDE_PERMISSION_MODE` | `auto` | The permission mode of Claude Code opened in a terminal (`auto`, `acceptEdits`; empty = it asks each time) |
| `UMAY_MARKET_SYMBOLS` | `XU100.IS,^GSPC,^IXIC,NVDA,TSLA,GOOGL,MSFT,USDTRY=X,GBPTRY=X,BTC-USD,GC=F` | The markets panel's symbols as Yahoo Finance writes them (Borsa Istanbul's `.IS` come about 15 min late). Once you add or remove one by voice or text, her own list (`%LOCALAPPDATA%\Umay\markets.json`) is used instead |

## Safety

- File tools only work inside `UMAY_ALLOWED_DIRS`; deleted files go to the Recycle Bin
  (uninstalling a Steam game is permanent).- Creating, changing, moving, deleting, running, closing, uninstalling and shutting down are
  always confirmed (aloud on the PC, buttons on Telegram).
- Shutdowns are forced (unsaved work is lost) and always preceded by a 60-second warning that
  `/iptal` or "cancel the shutdown" stops.
- Screenshots are sent to the model only when she uses `look_at_screen` (or after a mouse or keyboard
  step), and dropped from the conversation right after; during a task only the newest one is kept.
- The heavy model (`UMAY_HEAVY_MODEL`, Claude Code): a click's screenshot goes to it (so it leaves the
  PC, to Anthropic), a plan sends it your request and the last few lines of the conversation, and the check
  after a plan step sends what the tools answered, plus a screenshot of all monitors if the step used the
  mouse, keyboard or screen. It runs
  with no tools (only Read, for that one screenshot, deleted after), no MCP servers, none of your
  Claude Code settings and no saved session. `off` keeps everything on the PC.
- Mouse and keyboard: she asks only before a step that sends, posts, buys, deletes or can't be undone
  (where you asked from: buttons on Telegram, aloud at the PC); other steps go ahead without asking,
  except after she read a web page, search results or a message in that request: then every click,
  typing and key press asks (strict mode).
  Moving the mouse yourself stops her for the rest of that request. She never types into her own
  text box and only types after clicking where the text goes; she is told never to type passwords.
- `/c` runs Claude Code in `acceptEdits` mode: it can edit files but can't run shell commands
  that aren't already allowed in your Claude Code settings.
- `/c` works in `Documents\Projects` (`CLAUDE_CODE_CWD`) and may also use `Documents\Work`
  (`CLAUDE_CODE_EXTRA_DIRS`).
- Ending a process asks first and never touches Windows, security software, other users'
  processes or Umay herself.
- The one thing she does without asking: when a health alarm got no answer for 10 minutes
  and the value is still past its limit, she ends the process using most of it (same
  protections as above). Set `UMAY_HEALTH=off` to turn this off.
- Sleep mode is the other exception to "she asks first": you confirm once when it starts. Claude
  Code then runs in `auto` permission mode (no questions, but a safety check still blocks
  destructive or out-of-scope actions; commands can't be strictly fenced into a folder on
  Windows). The `sleep_hook.ps1` hook copies each project folder to `Documents\Failsafe` before
  anything in it changes and blocks the change if the copy fails; `.venv`, `venv`,
  `node_modules` and `__pycache__` aren't copied (rebuild them from `requirements.txt`).
  Copies of folders that end up unchanged are deleted. The Telegram bridge folder and `.env`
  files are off-limits. To undo a night's work, copy the folder back from `Failsafe`. The health
  monitor always runs during sleep mode (even with `UMAY_HEALTH=off`): an overheating job is
  ended like any other program. Claude is told not to message you on Telegram while you sleep.
- WhatsApp: files go only to a number you give with its country code or a chat you name (no
  guessing, no stored contacts), only from the allowed folders, never secrets (`.env`, keys,
  `.ssh`...), and only after you confirm the chat WhatsApp opened and a screenshot of it. Reading
  messages is asked first (PC + Telegram), and message text is treated as untrusted. It drives the
  official WhatsApp Desktop app (no unofficial API that could get the number banned), so opening a
  chat and sending need an unlocked PC; keys are only pressed while WhatsApp is in front, and the
  file is attached through the clipboard.

## Project layout

How the code fits together, how to add a tool, build and deploy, and known traps: [docs/INDEX.md](docs/INDEX.md).

| File | Purpose |
|---|---|
| `main.py` | Entry point: reminder loop, HUD, text box hotkey, voice, Telegram listener (`main.py --hud` runs the HUD's own process) |
| `ai_assistant.py` | Modes, OpenRouter requests, tool loop, the text box's conversation |
| `chat_box.py` | The text box beside the orb (Ctrl+F1) and the yes/no box, in the HUD's look |
| `voice_session.py`, `glow.py` | The spoken conversation and the light around the screen |
| `pc_tools.py` | Files and app launching (+ confirmation channels) |
| `browser_tools.py` | Opera GX and YouTube |
| `system_tools.py` | Closing apps, Python runs, terminal, VS Code, shutdown, PC status |
| `mail_tools.py` | A mail draft in Outlook (mailto); it never sends |
| `window_tools.py` | Windows of open apps (move, minimize, maximize, restore, front), starting an app and waiting for its window |
| `health_monitor.py` | Background health checks, alarms and their escalation |
| `steam_tools.py` | Installed Steam games |
| `whatsapp_tools.py` | WhatsApp Desktop: chat list, reading messages (UI Automation), sending files |
| `web_control.py` | Opera GX through the Umay Web Control extension, its approval tiers |
| `web_find.py` | Finding a page inside a website by walking it in Opera GX |
| `price_alerts.py` | The owner's price alarms and the thread that checks them |
| `sleep_mode.py`, `sleep_hook.ps1` | Sleep mode: backups, the unattended Claude Code job, report, shutdown. `python sleep_mode.py start/finish` runs the same mode inside an interactive Claude Code session (with the global hook `~/.claude/hooks/umay_sleep_gate.py`) |
| `screen_tools.py`, `media_tools.py` | Screenshots; media keys and volume |
| `spotify_tools.py` | Spotify through Windows' media session (winrt) and its mixer volume (pycaw) |
| `spotify_web.py` | Spotify Web API (PKCE login, search, play, queue, library); token in %LOCALAPPDATA%\Umay |
| `conversation_memory.py` | Judging finished conversations, deleting / summarizing past the size limit |
| `memory_cortex.py` | The Memory Cortex: the fact tree (SQLite + embeddings), learning from conversations, the reply context, `remember`, Self research |
| `memory_editor.py` | The memory editor window (`Umay.exe --memory`) |
| `hud_cortex.py`, `cortex.html`, `cortex_graph.py` | The memory wallpaper in the HUD's process (`python hud_cortex.py --demo` for a look) |
| `eyes.py`, `hands_worker.py`, `hand_gesture.py` | Umay's eyes: starting / stopping the camera, the camera worker (MediaPipe, in hands_env) and what a hand means for the mouse |
| `input_tools.py` | The mouse and keyboard: click, scroll, type, keys (pointing at the latest screenshot; a click's target aimed by the heavy model) |
| `heavy_model.py` | The heavy model (`UMAY_HEAVY_MODEL`): Claude Code Haiku on your subscription, for plans, checking their steps and aiming clicks |
| `task_plan.py` | Plans for multi-step requests: when one is needed, the planner's tool list, each step's tools and prompt |
| `reminder_tools.py`, `config_loader.py`, `check_event.py`, `alert.py`, `alarm_ring.py` | Alarms and reminders; one ringing alarm on the PC and Telegram |
| `calendar_tools.py` | Plans to the phone's calendar (.ics on Telegram) + PC reminders |
| `telegram_listener.py` | Telegram via claude_comm |
| `claude_bridge.py` | `/c` → headless Claude Code |
| `claude_terminal.py` | "Open Claude Code and ..." → Claude Code in a Windows Terminal window, on the task |
| `news_follow.py` | "Follow / unfollow ... in the news" → who the newsfeed follows |
| `markets_follow.py`, `market_outlook.py` | "Add / remove ... in the markets" → the markets panel's symbols; "How is ... looking?" → its numbers, news and lean |
| `wake.py` | The "Hey Umay" wake phrase |
| `local_llm.py` | Umay's local model through Ollama: her 16k-token copy, loading/unloading, its VRAM |
| `voice.py`, `voice_worker.py` | Speech-to-text, text-to-speech, optional RVC voice conversion |
| `speech_worker.py`, `setup_voice_env.py` | Local speech-to-text (Whisper in voice_env) and its setup |
| `voice_data.py`, `voice_recorder.py`, `voice_sentences_tr.txt`, `voice_reading_tr.txt` | Your confirmed voice clips for training speech-to-text, the sentence recorder, and longer texts to read into `UserVoice\` |
| `piper_worker.py`, `tts_worker.py`, `tts_text.py` | Local text-to-speech (English Piper in piper_env; Turkish Chatterbox in tts_env) and its text prep (sentences; numbers and names for Turkish) |
| `train_stt.py`, `stt_eval.py` | Fine-tuning speech-to-text on your voice (LoRA), and its scoring / long-clip cutting |
| `i18n.py` | UI texts (English / Turkish) |
| `hud.py`, `hud_app.py` | The HUD: Umay's side (starts it, sends her state, answers its detail views) and the Qt process (orb, clock, panels) |
| `hud_detail.py`, `hud_views.py`, `hud_disk.py`, `hud_credits.py`, `hud_paint.py` | A panel's detailed view (the overlay, weather / vitals / calendar, storage and the disk scan, the credit log) and the HUD's drawing helpers |
| `panel_text.py` | What the disk and credit views say (pure: shared by Umay and the HUD) |
| `hud_layout.py`, `hud_windows.py`, `hud_data.py` | Where its windows go across monitors, what is on the orb's monitor, what the panels show |
| `companion.py` | Umay on her own: the morning greeting, news, market moves, rain, battery, breaks; aloud, written or a call by the room and whether you're there |
| `face.py`, `face_worker.py` | Your face: learning it (from the camera, or a folder of photos: "learn my face from C:\...\photos") and looking for it (OpenCV's YuNet and SFace, in hands_env) |
| `holiday_days.py` | Türkiye's and the UK's holidays and marked days |
| `development/` | The development plan: `planned.txt` (to do) and `done.txt`, numbered items ("item 8") |
| `hud_feeds.py`, `hud_news.py`, `hud_markets.py` | The news and markets panels: what they read (Google News RSS, Yahoo Finance) and their detail views |
| `layered.py` | Per-pixel alpha windows for the text box and the screen light |
| `paths.py` | App/resource folders and `.env` loading |
