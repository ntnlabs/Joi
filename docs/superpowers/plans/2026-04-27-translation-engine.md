# Translation Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add transparent per-conversation EN<->SK (and future languages) translation to Joi's message pipeline using TranslateGemma via Ollama.

**Architecture:** Translation wraps the existing `process_with_llm` queue handler — inbound messages are translated before the English pipeline, outbound LLM responses are translated after generation. Per-conversation toggle via `.translate` files (same pattern as `.model`/`.context`). Three model swaps per translated message with `keep_alive=0`. Translation failures halt the service (`os._exit(78)`).

**Tech Stack:** Python (server/config/memory), Go (TUI), Ollama API, SQLite

---

### Task 1: Config — Translation lookup in `config/prompts.py`

**Files:**
- Modify: `execution/joi/config/prompts.py:670-748` (after knowledge scope section)

- [ ] **Step 1: Add `_read_translate_file` and lookup functions**

Add at the end of `execution/joi/config/prompts.py`, after the knowledge scope functions (after line 748):

```python
# --- Translation Configuration ---

def _read_translate_file(path: Path) -> Optional[str]:
    """Read translation language code from file if it exists."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip().lower()
            if content and content.isalpha() and len(content) <= 5:
                return content
    except Exception as e:
        logger.warning("Failed to read translate config", extra={"path": str(path), "error": str(e)})
    return None


def get_user_translate_lang(user_id: str) -> Optional[str]:
    """Get translation language for a specific user."""
    safe_user_id = sanitize_scope(user_id)
    user_file = PROMPTS_DIR / "users" / f"{safe_user_id}.translate"
    lang = _read_translate_file(user_file)
    if lang:
        logger.debug("Using user-specific translation", extra={"user_id": user_id, "lang": lang})
    return lang


def get_group_translate_lang(group_id: str) -> Optional[str]:
    """Get translation language for a specific group."""
    safe_group_id = sanitize_scope(group_id)
    group_file = PROMPTS_DIR / "groups" / f"{safe_group_id}.translate"
    lang = _read_translate_file(group_file)
    if lang:
        logger.debug("Using group-specific translation", extra={"group_id": group_id, "lang": lang})
    return lang


def get_translate_lang_for_conversation(conversation_type: str, conversation_id: str, sender_id: str) -> Optional[str]:
    """
    Get the translation language for a conversation.

    Returns language code (e.g., 'sk') or None if translation is disabled.
    """
    if conversation_type == "group":
        return get_group_translate_lang(conversation_id)
    else:
        return get_user_translate_lang(sender_id)


def get_translate_lang_by_id(conversation_id: str) -> Optional[str]:
    """
    Get translation language using conversation_id directly.

    Used by scheduler (Wind/reminders) which runs per-conversation without sender context.
    """
    if not conversation_id:
        return None
    is_group = not conversation_id.startswith("+")
    if is_group:
        return get_group_translate_lang(conversation_id)
    else:
        return get_user_translate_lang(conversation_id)
```

- [ ] **Step 2: Verify syntax**

Run: `python3 -m py_compile execution/joi/config/prompts.py`
Expected: No output (success)

- [ ] **Step 3: Export new functions from config package**

Add to `execution/joi/config/__init__.py` the imports for the new functions. Read the file first to see existing exports, then add:

```python
from config.prompts import get_translate_lang_for_conversation, get_translate_lang_by_id
```

alongside the existing imports.

- [ ] **Step 4: Verify syntax**

Run: `python3 -m py_compile execution/joi/config/__init__.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add execution/joi/config/prompts.py execution/joi/config/__init__.py
git commit -m "Add translation language config lookup for per-conversation .translate files"
```

---

### Task 2: Config — Env var and default file

**Files:**
- Modify: `execution/joi/config/settings.py:1-26`
- Modify: `execution/joi/systemd/joi-api.default:30-39`

- [ ] **Step 1: Add `translate_model_prefix` to Settings**

In `execution/joi/config/settings.py`, add to the `Settings` dataclass:

```python
translate_model_prefix: str = "translategemma"
```

And in `load_settings()`:

```python
translate_model_prefix=os.getenv("JOI_TRANSLATE_MODEL_PREFIX", "translategemma"),
```

- [ ] **Step 2: Add env var to default file**

In `execution/joi/systemd/joi-api.default`, after the `#JOI_OLLAMA_NUM_CTX=0` line (line 39) and before the `# --- Mesh ---` section, add:

```
# Translation model prefix - derives inbound/outbound model names as {prefix}-{lang}-en / {prefix}-en-{lang}
# Requires per-user/group .translate file with language code (e.g., "sk") to enable
#JOI_TRANSLATE_MODEL_PREFIX=translategemma
```

- [ ] **Step 3: Verify syntax**

Run: `python3 -m py_compile execution/joi/config/settings.py`
Expected: No output (success)

- [ ] **Step 4: Commit**

```bash
git add execution/joi/config/settings.py execution/joi/systemd/joi-api.default
git commit -m "Add JOI_TRANSLATE_MODEL_PREFIX env var to settings and default file"
```

---

### Task 3: Database — Add `translated_text` column

**Files:**
- Modify: `execution/joi/memory/store.py` — schema, migration, Message dataclass, `store_message`, `get_recent_messages`, `get_oldest_messages`

- [ ] **Step 1: Add `translated_text` to Message dataclass**

In `execution/joi/memory/store.py`, add to the `Message` dataclass (after `sender_name` field, around line 119):

```python
translated_text: Optional[str] = None  # English version when translation is active
```

- [ ] **Step 2: Add `translated_text` to SCHEMA_SQL**

In the `CREATE TABLE IF NOT EXISTS messages` block in `SCHEMA_SQL`, add after the `sender_name` column:

```sql
translated_text TEXT,
```

Note: This only affects new databases. Existing databases get the column via migration (next step).

- [ ] **Step 3: Add migration v20**

After the migration v19 block (around line 1092), add:

```python
        # Migration v20: translation support — add translated_text column to messages
        cursor = conn.execute("PRAGMA table_info(messages)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "translated_text" not in columns:
            logger.info("Migration v20: Adding 'translated_text' column to messages")
            conn.execute("ALTER TABLE messages ADD COLUMN translated_text TEXT")
            conn.commit()
```

- [ ] **Step 4: Add `update_translated_text` method**

Add a new method to the `MemoryStore` class, near the `store_message` method:

```python
    def update_translated_text(self, message_id: str, translated_text: str) -> None:
        """
        Set the translated_text for an already-stored message.

        Used by translation pipeline to store the English version after
        the original has been stored.
        """
        conn = self._connect()
        conn.execute(
            "UPDATE messages SET translated_text = ? WHERE message_id = ?",
            (translated_text, message_id),
        )
        conn.commit()
```

- [ ] **Step 5: Update `get_recent_messages` to include `translated_text`**

In all four SELECT branches of `get_recent_messages` (lines ~1663-1716), add `translated_text` to the column list. Each SELECT currently selects:

```sql
SELECT id, message_id, direction, channel, content_type,
       content_text, conversation_id, reply_to_id, timestamp, created_at,
       archived, sender_id, sender_name
```

Change to:

```sql
SELECT id, message_id, direction, channel, content_type,
       content_text, conversation_id, reply_to_id, timestamp, created_at,
       archived, sender_id, sender_name, translated_text
```

And in the Message construction (around line 1721-1737), add:

```python
translated_text=row["translated_text"],
```

- [ ] **Step 6: Update `get_oldest_messages` similarly**

Same pattern — add `translated_text` to SELECT columns and Message construction in `get_oldest_messages`.

- [ ] **Step 7: Verify syntax**

Run: `python3 -m py_compile execution/joi/memory/store.py`
Expected: No output (success)

- [ ] **Step 8: Commit**

```bash
git add execution/joi/memory/store.py
git commit -m "Add translated_text column to messages for translation pipeline"
```

---

### Task 4: Server — Translation helper function

**Files:**
- Modify: `execution/joi/api/server.py` — add `_translate_text` helper near top-level functions

- [ ] **Step 1: Add import for `get_translate_lang_for_conversation` and `get_translate_lang_by_id`**

In the imports block of `server.py` (around line 46-57), add to the existing `from config import (...)` block:

```python
get_translate_lang_for_conversation,
get_translate_lang_by_id,
```

- [ ] **Step 2: Read the translate model prefix from settings**

Near the existing `LLM_KEEP_ALIVE` line (around line 262), add:

```python
TRANSLATE_MODEL_PREFIX = os.getenv("JOI_TRANSLATE_MODEL_PREFIX", "translategemma")
```

- [ ] **Step 3: Add `_translate_text` helper function**

Add near the other helper functions (before `_build_chat_messages`, around line 2460):

```python
def _translate_text(text: str, lang: str, direction: str) -> Optional[str]:
    """
    Translate text using the translation model.

    Args:
        text: Text to translate
        lang: Language code (e.g., 'sk')
        direction: 'inbound' ({lang}-en) or 'outbound' (en-{lang})

    Returns:
        Translated text, or None on failure.
    """
    if direction == "inbound":
        model_name = f"{TRANSLATE_MODEL_PREFIX}-{lang}-en"
    else:
        model_name = f"{TRANSLATE_MODEL_PREFIX}-en-{lang}"

    logger.info("Translation request", extra={
        "direction": direction,
        "model": model_name,
        "text_length": len(text),
        "action": "translate_request",
    })

    response = llm.generate(
        prompt=text,
        model=model_name,
    )

    if response.error or not response.text.strip():
        logger.info("Translation failed", extra={
            "direction": direction,
            "model": model_name,
            "error": response.error or "empty_response",
            "action": "translate_fail",
        })
        return None

    translated = response.text.strip()
    logger.info("Translation complete", extra={
        "direction": direction,
        "model": model_name,
        "input_length": len(text),
        "output_length": len(translated),
        "action": "translate_complete",
    })
    return translated
```

Note: This function does NOT call `os._exit(78)` — the caller decides how to handle failure. The helper is reused by both the reactive path (server.py) and the scheduler path (scheduler.py via callback).

- [ ] **Step 4: Override `keep_alive` for translation calls**

The `OllamaClient.generate()` method currently uses the client's default `keep_alive`. We need translation calls to use `keep_alive=0`. Add a `keep_alive` parameter to `OllamaClient.generate()` in `execution/joi/llm/client.py`:

In the `generate` method signature (line 53), add `keep_alive: Optional[str] = None`:

```python
def generate(
    self,
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    keep_alive: Optional[str] = None,
) -> LLMResponse:
```

In the payload construction (around line 73), change:

```python
"keep_alive": keep_alive if keep_alive is not None else self.keep_alive,
```

Then update `_translate_text` to pass `keep_alive="0"`:

```python
    response = llm.generate(
        prompt=text,
        model=model_name,
        keep_alive="0",
    )
```

- [ ] **Step 5: Verify syntax**

Run: `python3 -m py_compile execution/joi/llm/client.py && python3 -m py_compile execution/joi/api/server.py`
Expected: No output (success)

- [ ] **Step 6: Commit**

```bash
git add execution/joi/llm/client.py execution/joi/api/server.py
git commit -m "Add translation helper function and keep_alive override for Ollama"
```

---

### Task 5: Server — Inbound translation in `process_with_llm`

**Files:**
- Modify: `execution/joi/api/server.py` — inside `process_with_llm` function

- [ ] **Step 1: Add inbound translation block**

Inside `process_with_llm` (the inner function starting at line 1823), after the typing indicator and first cancelled check (around line 1840), and BEFORE the mood jump detection (line 1843), add:

```python
        # --- Inbound translation ---
        translate_lang = get_translate_lang_for_conversation(
            conversation_type=msg.conversation.type,
            conversation_id=msg.conversation.id,
            sender_id=msg.sender.transport_id,
        )
        if translate_lang:
            translated_input = _translate_text(user_text, translate_lang, "inbound")
            if translated_input is None:
                # Translation failed — notify user and halt
                _send_to_mesh(
                    recipient_id=msg.sender.id,
                    recipient_transport_id=msg.sender.transport_id,
                    conversation=msg.conversation,
                    text="I'm sorry, I have problems translating this.",
                    reply_to=msg.message_id,
                )
                logger.info("Translation failure, halting", extra={
                    "conversation_id": msg.conversation.id,
                    "direction": "inbound",
                    "action": "translate_halt",
                })
                os._exit(78)
            # Store English translation on the already-stored inbound message
            memory.update_translated_text(msg.message_id, translated_input)
            # Replace user_text for all downstream processing (facts, reminders, LLM)
            user_text = translated_input
```

Important: `user_text` was already stored in the messages table earlier (line ~1704). This UPDATE adds the English version to `translated_text`. All downstream code uses the reassigned `user_text` (now English).

- [ ] **Step 2: Verify syntax**

Run: `python3 -m py_compile execution/joi/api/server.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add execution/joi/api/server.py
git commit -m "Add inbound translation in process_with_llm queue handler"
```

---

### Task 6: Server — Outbound translation in `process_with_llm`

**Files:**
- Modify: `execution/joi/api/server.py` — inside `process_with_llm` function

- [ ] **Step 1: Add outbound translation block**

After `format_for_signal()` (line ~2101) and before the response length logging (line ~2104), add:

```python
        # --- Outbound translation ---
        original_en_response = response_text  # Preserve English for translated_text storage
        if translate_lang:
            translated_output = _translate_text(response_text, translate_lang, "outbound")
            if translated_output is None:
                logger.info("Translation failure, halting", extra={
                    "conversation_id": msg.conversation.id,
                    "direction": "outbound",
                    "action": "translate_halt",
                })
                os._exit(78)
            response_text = translated_output
```

- [ ] **Step 2: Store both versions for outbound message**

After the outbound `memory.store_message` call (around line 2141-2149), add the translated_text update:

```python
        # Store English original for LLM context building
        if translate_lang:
            memory.update_translated_text(outbound_message_id, original_en_response)
```

Note: `store_message` stores `response_text` (the translated Slovak) as `content_text`. Then we update `translated_text` with the original English.

- [ ] **Step 3: Verify syntax**

Run: `python3 -m py_compile execution/joi/api/server.py`
Expected: No output (success)

- [ ] **Step 4: Commit**

```bash
git add execution/joi/api/server.py
git commit -m "Add outbound translation in process_with_llm queue handler"
```

---

### Task 7: Server — Update `_build_chat_messages` to prefer `translated_text`

**Files:**
- Modify: `execution/joi/api/server.py:2463-2490`

- [ ] **Step 1: Update message content selection**

In `_build_chat_messages`, change the content selection (line ~2476) from:

```python
            content = msg.content_text
```

To:

```python
            content = msg.translated_text if msg.translated_text else msg.content_text
```

This is unconditional — if `translated_text` exists, it's always the English version (correct for LLM context), regardless of whether translation is currently enabled.

- [ ] **Step 2: Verify syntax**

Run: `python3 -m py_compile execution/joi/api/server.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add execution/joi/api/server.py
git commit -m "Prefer translated_text in LLM chat context building"
```

---

### Task 8: Scheduler — Outbound translation for Wind and Reminders

**Files:**
- Modify: `execution/joi/api/scheduler.py`

The scheduler sends LLM-generated messages via three paths: Wind proactive, Wind wakeup, and Reminders. All three need outbound translation. The scheduler doesn't have direct access to `_translate_text`, so we pass it as a dependency callback (same pattern as `_generate_proactive_message`).

- [ ] **Step 1: Add `translate_outbound` dependency to Scheduler**

In `execution/joi/api/scheduler.py`, add to `__init__` (around line 55):

```python
self._translate_outbound: Optional[Callable] = None
```

Add to `set_dependencies` signature and body:

```python
# In signature, add parameter:
translate_outbound: Optional[Callable] = None,

# In body, add:
self._translate_outbound = translate_outbound
```

- [ ] **Step 2: Add outbound translation helper in Scheduler**

Add a private method to the Scheduler class:

```python
    def _translate_if_needed(self, text: str, conversation_id: str) -> str:
        """Translate outbound text if translation is enabled for this conversation.

        Returns translated text, or original text if no translation needed.
        On translation failure, calls os._exit(78).
        """
        if not self._translate_outbound:
            return text

        from config.prompts import get_translate_lang_by_id
        lang = get_translate_lang_by_id(conversation_id)
        if not lang:
            return text

        translated = self._translate_outbound(text, lang, "outbound")
        if translated is None:
            logger.info("Translation failure in scheduler, halting", extra={
                "conversation_id": conversation_id,
                "direction": "outbound",
                "action": "translate_halt",
            })
            os._exit(78)
        return translated
```

- [ ] **Step 3: Apply to Wind proactive send path**

In the Wind proactive send section (around line 867-889), after `message_text` is confirmed non-None and before `_send_to_mesh`, add translation and store both versions:

Change the block starting at line 867:

```python
                # Translate outbound if needed
                original_en = message_text
                message_text = self._translate_if_needed(message_text, conv_id)

                # Send the message
                success = self._send_to_mesh(
                    ...
                )

                if success:
                    message_id = str(uuid.uuid4())
                    self._memory.store_message(
                        message_id=message_id,
                        direction="outbound",
                        content_type="text",
                        content_text=f"[JOI-WIND] {message_text}",
                        timestamp=int(time.time() * 1000),
                        conversation_id=conv_id,
                    )
                    # Store English original for LLM context
                    if message_text != original_en:
                        self._memory.update_translated_text(message_id, f"[JOI-WIND] {original_en}")
```

- [ ] **Step 4: Apply to Wind wakeup send path**

Same pattern in the wakeup section (around line 700-717):

```python
        # Translate outbound if needed
        original_en = message_text
        message_text = self._translate_if_needed(message_text, conversation_id)

        success = self._send_to_mesh(
            ...
        )

        if success and self._memory:
            msg_id = str(uuid.uuid4())
            self._memory.store_message(
                message_id=msg_id,
                direction="outbound",
                content_type="text",
                content_text=f"[JOI-WAKEUP] {message_text}",
                timestamp=int(time.time() * 1000),
                conversation_id=conversation_id,
            )
            if message_text != original_en:
                self._memory.update_translated_text(msg_id, f"[JOI-WAKEUP] {original_en}")
```

- [ ] **Step 5: Apply to Reminder send path**

Same pattern in the reminder section (around line 972-991):

```python
                # Translate outbound if needed
                original_en = message_text
                message_text = self._translate_if_needed(message_text, reminder.conversation_id)

                success = self._send_to_mesh(
                    ...
                )

                if success:
                    message_id = str(uuid.uuid4())
                    self._memory.store_message(
                        message_id=message_id,
                        direction="outbound",
                        content_type="text",
                        content_text=f"[JOI-REMINDER] {message_text}",
                        timestamp=int(time.time() * 1000),
                        conversation_id=reminder.conversation_id,
                    )
                    if message_text != original_en:
                        self._memory.update_translated_text(message_id, f"[JOI-REMINDER] {original_en}")
```

- [ ] **Step 6: Wire the dependency in server.py**

In `server.py`, where `scheduler.set_dependencies(...)` is called (around line 1298-1317), add:

```python
translate_outbound=_translate_text,
```

- [ ] **Step 7: Verify syntax**

Run: `python3 -m py_compile execution/joi/api/scheduler.py && python3 -m py_compile execution/joi/api/server.py`
Expected: No output (success)

- [ ] **Step 8: Commit**

```bash
git add execution/joi/api/scheduler.py execution/joi/api/server.py
git commit -m "Add outbound translation for Wind proactive, wakeup, and reminder messages"
```

---

### Task 9: TUI — Add Translation to per-conversation settings

**Files:**
- Modify: `execution/joi/setup/model.go:55-107`
- Modify: `execution/joi/setup/config.go:210-290`
- Modify: `execution/joi/setup/ui.go:733-750`

- [ ] **Step 1: Add `Translate` field to `ConvOverride`**

In `execution/joi/setup/model.go`, add to the `ConvOverride` struct (after `Prompt`, around line 62):

```go
Translate *Setting
```

Update `AllSettings()` (line 84-86):

```go
func (c *ConvOverride) AllSettings() []*Setting {
	return []*Setting{c.Model, c.Context, c.Compact, c.Prompt, c.Translate}
}
```

Update `Tags()` (add after the Prompt tag check, around line 100):

```go
if c.Translate != nil && !c.Translate.Deleted {
    tags = append(tags, "translate")
}
```

- [ ] **Step 2: Add `.translate` to prompt extensions**

In `execution/joi/setup/config.go`, add to the `promptExts` slice (line 210-220):

```go
{".translate", "Translation", TypeString, "Translate"},
```

And update the `switch` block in `discoverConversations` (around line 268-279) to add:

```go
case "Translate":
    conv.Translate = s
```

- [ ] **Step 3: Add Translation row to convSettingRefs**

In `execution/joi/setup/ui.go`, add to `convSettingRefs()` (around line 744-749):

```go
{"Translation", &a.convRef.Translate, ".translate", TypeString, "disabled"},
```

- [ ] **Step 4: Add `JOI_TRANSLATE_MODEL_PREFIX` to hwDefs**

In `execution/joi/setup/model.go`, add to `hwDefs` (around line 143):

```go
{"Translate prefix", "JOI_TRANSLATE_MODEL_PREFIX", TypeString, "translategemma"},
```

- [ ] **Step 5: Build and verify**

Run: `cd execution/joi/setup && go build -o /dev/null .`
Expected: Build succeeds with no errors

- [ ] **Step 6: Commit**

```bash
git add execution/joi/setup/model.go execution/joi/setup/config.go execution/joi/setup/ui.go
git commit -m "Add translation setting to joi-setup TUI per-conversation screen"
```

---

### Task 10: Modelfile templates

**Files:**
- Create: `execution/joi/ollama/Modelfile_translate_sk_en`
- Create: `execution/joi/ollama/Modelfile_translate_en_sk`

- [ ] **Step 1: Create SK->EN Modelfile**

Create `execution/joi/ollama/Modelfile_translate_sk_en`:

```
FROM translategemma:12b

SYSTEM """You are a Slovak to English translator. Translate the following text from Slovak to English. Output ONLY the translation, nothing else. Do not add explanations, notes, or commentary. Preserve the original tone and style."""

PARAMETER temperature 0.1
PARAMETER num_ctx 2048
```

- [ ] **Step 2: Create EN->SK Modelfile**

Create `execution/joi/ollama/Modelfile_translate_en_sk`:

```
FROM translategemma:12b

SYSTEM """You are an English to Slovak translator. Translate the following text from English to Slovak. Output ONLY the translation, nothing else. Do not add explanations, notes, or commentary. Preserve the original tone and style."""

PARAMETER temperature 0.1
PARAMETER num_ctx 2048
```

- [ ] **Step 3: Commit**

```bash
git add execution/joi/ollama/Modelfile_translate_sk_en execution/joi/ollama/Modelfile_translate_en_sk
git commit -m "Add Modelfile templates for SK<->EN translation via TranslateGemma"
```

---

### Task 11: Sysprep — Update install scripts

**Files:**
- Check and update relevant `sysprep/` stage scripts if Modelfile deployment or translation config needs to be included.

- [ ] **Step 1: Check which sysprep stage handles Modelfiles and ollama model creation**

Search `sysprep/` for existing Modelfile or `ollama create` references to find the right stage to update.

- [ ] **Step 2: Add translation Modelfile deployment**

Add the two new Modelfiles to whichever stage handles Modelfile copying/building. Follow the exact pattern used for existing Modelfiles.

- [ ] **Step 3: Commit**

```bash
git add sysprep/
git commit -m "Add translation Modelfiles to sysprep deployment"
```

---

### Task 12: Final verification

- [ ] **Step 1: Full syntax check**

```bash
python3 -m py_compile execution/joi/config/prompts.py
python3 -m py_compile execution/joi/config/settings.py
python3 -m py_compile execution/joi/memory/store.py
python3 -m py_compile execution/joi/api/server.py
python3 -m py_compile execution/joi/api/scheduler.py
python3 -m py_compile execution/joi/llm/client.py
cd execution/joi/setup && go build -o /dev/null .
```

- [ ] **Step 2: Review all changes**

```bash
git diff main --stat
git log --oneline main..HEAD
```

Verify: no files outside the expected set were modified, no untracked files left behind.

- [ ] **Step 3: Commit any fixes**

If any issues found, fix and commit.
