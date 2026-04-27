# Translation Engine Design

Transparent, per-conversation translation pipeline for Joi using TranslateGemma via Ollama.

## Problem

Joi's LLM pipeline operates in English. Users who communicate in other languages (starting with Slovak) need transparent bidirectional translation so the LLM always receives English and the user always receives their language.

## Constraints

- No VRAM headroom: translation model and main model cannot coexist. Three model swaps per translated message (translate-in, main LLM, translate-out).
- Per-conversation toggle (off by default) because of the hardware cost.
- Must be open for future languages beyond Slovak.
- English is always the LLM side.
- Translation failures are integrity failures: stop the service (`os._exit(78)`), don't degrade.

## Configuration

### Per-conversation toggle

File-based, same pattern as `.model`/`.context`/`.txt` files:

```
/var/lib/joi/prompts/users/<user_id>.translate    -> "sk"
/var/lib/joi/prompts/groups/<group_id>.translate   -> "sk"
```

- No file = translation disabled (default).
- File contains a language code (e.g., `sk`, `hu`). One word, no other content.
- Lookup: user-specific -> group-specific -> none. Same chain as other per-conversation config.

### Global model prefix

Environment variable in `joi-api.default`:

```
#JOI_TRANSLATE_MODEL_PREFIX=translategemma
```

Default: `translategemma`. The code derives both model names from prefix + language:

- Inbound (user's lang -> EN): `{prefix}-{lang}-en` (e.g., `translategemma-sk-en`)
- Outbound (EN -> user's lang): `{prefix}-en-{lang}` (e.g., `translategemma-en-sk`)

### Adding a new language

No code changes required:

1. Create two Modelfiles (e.g., `Modelfile_translate_hu_en`, `Modelfile_translate_en_hu`).
2. Build them in Ollama (`ollama create translategemma-hu-en -f Modelfile_translate_hu_en`).
3. Write `hu` to the user's/group's `.translate` file.

## Pipeline Flow

### Reactive path (user message -> Joi response)

Inside `process_with_llm`, the existing queue handler:

```
1. Inbound message arrives (user_text, possibly in Slovak)
2. Store ORIGINAL text in messages.content_text
3. Look up .translate for this conversation
4. If translation enabled:
   a. llm.generate(model="translategemma-sk-en", prompt=user_text, keep_alive="0")
   b. On failure: send "I'm sorry, I have problems translating this." to user, log, os._exit(78)
   c. Replace user_text with English translation for all downstream use
   d. Store English translation in messages.translated_text (UPDATE on the row from step 2)
5. [existing pipeline unchanged: fact extraction, reminders, notes, tasks, context build, etc.]
6. llm.chat(model=main_model, ...) -> English response
7. validate_output(), format_for_signal()
8. If translation enabled:
   a. llm.generate(model="translategemma-en-sk", prompt=response_text, keep_alive="0")
   b. On failure: log, os._exit(78)
   c. response_text = translated Slovak text
9. Send response_text to mesh
10. Store outbound: content_text = what user receives, translated_text = original English from LLM
```

### Proactive path (Wind messages)

In `_generate_proactive_message` and its callers:

```
1. Wind generates English proactive message via LLM
2. Look up .translate for conversation_id
3. If translation enabled:
   a. llm.generate(model="translategemma-en-sk", prompt=message, keep_alive="0")
   b. On failure: log, os._exit(78)
4. Send translated message to mesh
5. Store both versions
```

### System messages (NOT translated)

These pass through untranslated:

- Timezone confirmations
- Memory compact confirmations
- Snooze confirmations
- Reminder fire notifications
- Any other non-LLM-generated responses

## Database Changes

### Migration: add `translated_text` column

Add nullable `translated_text TEXT` column to the messages table.

```sql
ALTER TABLE messages ADD COLUMN translated_text TEXT;
```

### Storage semantics

| Direction | `content_text` | `translated_text` |
|-----------|---------------|-------------------|
| Inbound (translation on) | Original (Slovak) | English translation |
| Outbound (translation on) | Translated (Slovak) | Original English from LLM |
| Any (translation off) | Original text | NULL |

### LLM context building

`_build_chat_messages` prefers `translated_text` over `content_text` when not NULL. This is unconditional (not gated by current translation setting) so that:

- Enabling translation mid-conversation: old English messages have `translated_text = NULL`, fall back to `content_text` (already English). Works.
- Disabling translation mid-conversation: old messages keep their `translated_text` (English). LLM still gets English. Works.

## Error Handling

Translation is not optional when enabled. Failures indicate infrastructure problems (model not available, VRAM issues) that won't self-resolve.

**Inbound translation failure:**

1. Send "I'm sorry, I have problems translating this." to user via mesh.
2. Log at INFO: conversation_id, direction="inbound", error details.
3. `os._exit(78)` -- service stops, admin investigates.

**Outbound translation failure:**

1. Log at INFO: conversation_id, direction="outbound", error details.
2. `os._exit(78)` -- response never reaches user in wrong language.

## VRAM Management

- Translation model calls use `keep_alive="0"` to unload immediately after use.
- Three model swaps per translated message: translate-in -> main LLM -> translate-out.
- Non-translated conversations have zero overhead (no translation calls, no model swaps).

## File Changes

1. **`config/prompts.py`** -- `get_translate_lang_for_conversation()`: user -> group -> None lookup for `.translate` files.
2. **`config/settings.py`** -- `JOI_TRANSLATE_MODEL_PREFIX` env var.
3. **`memory/store.py`** -- DB migration: add `translated_text` column.
4. **`memory/store.py`** -- `get_recent_messages()` return type includes `translated_text`.
5. **`api/server.py`** -- `process_with_llm`: inbound translation before pipeline, outbound translation after LLM.
6. **`api/server.py`** -- `_build_chat_messages`: prefer `translated_text` when available.
7. **`api/server.py`** -- Wind proactive outbound: translate before `_send_to_mesh`.
8. **`systemd/joi-api.default`** -- `JOI_TRANSLATE_MODEL_PREFIX` commented out with default.
9. **`setup/ui.go`** -- "Translation" row in per-conversation settings screen.
10. **`ollama/Modelfile_translate_sk_en`** -- Modelfile for SK->EN translation (example/template).
11. **`ollama/Modelfile_translate_en_sk`** -- Modelfile for EN->SK translation (example/template).
