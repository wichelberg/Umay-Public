"""Coding mode's loop: the local 35B (llama-server, the owner's one big model) works on a task in a Workspace, one
action per round, until it finishes. The window and the voice (coding_mode.py) only start it and show what it did.

- One JSON action per reply, enforced by the server's grammar (`response_format`, the way task_plan's enum of tool
  names is: pitfall "Free strings for tool names"). No native tool calling: it is not needed and not proven here.
- The actions are Workspace's: list_dir, read_file, search, edit_file (exact old -> new, once), create_file,
  run_tests; and "finish" with a short report in the owner's language. They are private to this module on purpose:
  nothing here goes into ai_assistant.TOOLS (d3's choices, the planner's catalog and the tool enum would change).
- The window is 24k tokens: results are capped by Workspace, and from COMPACT_AT tokens on, older rounds shrink to
  one line each (the last KEEP_ROUNDS stay whole).
- Never silent: no llama backend, a game holding the machine or a server that stops mid-task ends the task with a
  sentence, and whatever was already written stays in the journal for the owner to keep or undo.
- The report the owner reads is what the journal says really changed, never the model's own words alone.
"""
import json

import requests

import coding_workspace as cw
import llama_server
import local_llm

MAX_ROUNDS = 40
ROUND_TOKENS = 3000        # one action: an edit with a screenful of code fits
COMPACT_AT = 14000         # estimated tokens of the conversation before old rounds shrink (the window is 24k)
KEEP_ROUNDS = 4
CHARS_PER_TOKEN = 3.2
SAME_LIMIT = 3             # the same failing action this many times in a row ends the task

ACTIONS = ["list_dir", "read_file", "search", "edit_file", "create_file", "run_tests", "finish"]
SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ACTIONS},
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "query": {"type": "string"},
        "old": {"type": "string"},
        "new": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["thought", "action", "path", "start_line", "end_line", "query", "old", "new", "message"],
    "additionalProperties": False,
}

SYSTEM = """You are a careful coding agent working inside one project folder. You do the owner's task by taking one action per reply, as JSON.
Fields: thought (one short sentence), action, path (relative to the folder), start_line, end_line, query, old, new, message. Fill the fields an action does not use with "" or 0.
Actions:
- list_dir: path = a folder ("." for the top).
- read_file: path, start_line, end_line (at most 200 lines; 0 = from the start).
- search: query (text, or /regex/), path = where ("." = everywhere).
- edit_file: path, old, new. old is copied EXACTLY from read_file (without the line numbers), appears once in the file, and is replaced by new. Change as little as possible; keep the file's style and indentation.
- create_file: path, new = the whole content.
- run_tests: runs the folder's own tests. Use it after your changes when the folder has tests, and fix what fails.
- finish: message = a short report for the owner, in the language the owner wrote the task in: what you did and what you did not.
Paths are relative to the project root, which is "." (list_dir "." first; a path like "src/a.py", never starting with the folder's own name or a drive).
Rules: look before you change (read the code you edit); never guess a file's content. Work only on the task. If something is not clear or not possible, finish and say so; do not invent. One file at a time; small edits."""


def estimate(messages):
    return int(sum(len(m["content"]) for m in messages) / CHARS_PER_TOKEN)


def compact(messages):
    """Shrinks old rounds when the conversation nears the window: the system prompt and the task stay, the last
    KEEP_ROUNDS rounds stay whole, each older round becomes the action and a cut of its result."""
    if estimate(messages) <= COMPACT_AT:
        return messages
    head, rest = messages[:2], messages[2:]
    keep = KEEP_ROUNDS * 2
    old, recent = (rest[:-keep], rest[-keep:]) if len(rest) > keep else ([], rest)
    shrunk = []
    for m in old:
        if m["role"] == "assistant":
            try:
                a = json.loads(m["content"])
                text = json.dumps({"action": a.get("action"), "path": a.get("path")})
            except ValueError:
                text = m["content"][:120]
            shrunk.append({"role": "assistant", "content": text})
        else:
            text = m["content"]
            shrunk.append({"role": "user", "content": text[:160] + (" ...(cut)" if len(text) > 160 else "")})
    return head + shrunk + recent


class Agent:
    def __init__(self, workspace, task, post=None, on_step=None, stopped=None):
        """post(body) -> the decoded response (tests give one); on_step(text) is told each action in a line;
        stopped() -> True ends the task between two rounds."""
        self.ws, self.task = workspace, task
        self._post = post or self._post_llama
        self._on_step = on_step or (lambda text: None)
        self._stopped = stopped or (lambda: False)
        self.summary = ""
        self.rounds = 0

    @staticmethod
    def _post_llama(body):
        with local_llm.working():
            r = llama_server.post_chat(body, timeout=600)
        r.raise_for_status()
        return r.json()

    def run(self):
        """Works until finish, the round limit, a stop or a failure. Returns why it ended in a sentence; the
        journal holds what changed."""
        if not llama_server.available() and self._post == self._post_llama:
            return "The 35B is not set up (UMAY_LLAMA_EXE / UMAY_LLAMA_MODEL): I did nothing."
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Task: {self.task}"}]
        last, repeats, unreadable = None, 0, 0
        while self.rounds < MAX_ROUNDS:
            if self._stopped():
                return "Stopped by the owner."
            self.rounds += 1
            try:
                data = self._post({"messages": compact(messages), "max_tokens": ROUND_TOKENS, "temperature": 0.1,
                                   "response_format": {"type": "json_schema",
                                                       "json_schema": {"name": "action", "schema": SCHEMA}}})
                choice = data["choices"][0]
                text = choice["message"].get("content") or ""
                if choice.get("finish_reason") == "length":   # cut off mid-JSON: there is nothing to parse
                    messages += [{"role": "assistant", "content": text[:200]},
                                 {"role": "user", "content": "That was cut off. Make a smaller edit (fewer lines at once)."}]
                    continue
                action = json.loads(text[text.index("{"):text.rindex("}") + 1])
            except requests.RequestException as e:
                return f"The 35B stopped answering ({type(e).__name__}); the task is unfinished."
            except (ValueError, KeyError, IndexError):
                unreadable += 1
                if unreadable >= 3:
                    return "The 35B did not give a usable action; the task is unfinished."
                messages += [{"role": "assistant", "content": "{}"},
                             {"role": "user", "content": "Reply with one JSON action."}]
                continue
            unreadable = 0
            name = action.get("action")
            if name == "finish":
                self.summary = (action.get("message") or "").strip()
                self._on_step("finish")
                return self.summary or "Finished."
            result, failed = self._do(name, action)
            self._on_step(self._line(name, action, failed))
            signature = (name, action.get("path"), action.get("old"), action.get("query"))
            repeats = repeats + 1 if signature == last and failed else 1
            last = signature
            if repeats >= SAME_LIMIT:
                return f"The 35B kept failing at {name} {action.get('path', '')}; the task is unfinished."
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": ("Error: " if failed else "Result:\n") + result}]
        return f"The task took more than {MAX_ROUNDS} steps; it is unfinished."

    def _do(self, name, a):
        ws = self.ws
        try:
            if name == "list_dir":
                return ws.list_dir(a.get("path") or "."), False
            if name == "read_file":
                return ws.read_file(a.get("path"), a.get("start_line"), a.get("end_line")), False
            if name == "search":
                return ws.search(a.get("query"), a.get("path") or "."), False
            if name == "edit_file":
                return ws.edit_file(a.get("path"), a.get("old"), a.get("new") or ""), False
            if name == "create_file":
                return ws.create_file(a.get("path"), a.get("new") or ""), False
            if name == "run_tests":
                return ws.run_tests(), False
            return f"Unknown action {name!r}.", True
        except cw.WorkspaceError as e:
            return str(e), True
        except OSError as e:
            return f"{type(e).__name__}: {e}", True

    @staticmethod
    def _line(name, a, failed):
        where = a.get("path") or a.get("query") or ""
        if name == "read_file" and a.get("start_line"):
            where += f" {a.get('start_line')}-{a.get('end_line') or ''}"
        return f"{name} {where}".strip() + (" (failed)" if failed else "")
