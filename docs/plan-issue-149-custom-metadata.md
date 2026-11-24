# Implementation Plan: Custom Notification Metadata Templates & Live Preview (#149)

## Goal
Allow users to optionally enable and customize notification **Subject** and **Body** templates per event type using OctoPrint metadata and a **generic** mechanism for pulling data from other installed plugins (with no first-class/hardcoded coupling to any specific third-party plugin), paired with a clickable variable reference and a live sample preview in the Settings UI. When custom templates are disabled—or when an event's template fields are left blank—OctoText preserves its exact built-in wording.

---

## 1. Supported Template Variables

### A. Core OctoPrint Variables
Read directly from OctoPrint's `self._printer.get_current_data()`, `self._printer.get_current_temperatures()`, and event payloads:

| Variable | Description | Sample Value (for Live Preview) |
|---|---|---|
| `{event}` | Built-in event title (e.g., `Print Progress 50% finished.`) | `Print Progress 50% finished.` |
| `{message}` | Built-in event body/details | `benchy.gcode` |
| `{printer}` | Printer appearance name | `Prusa MK4` |
| `{file}` | Current or event filename | `benchy.gcode` |
| `{percent}` | Completion percentage (integer) | `50%` |
| `{elapsed}` | Elapsed print time (`H:MM:SS`) | `1:15:00` |
| `{remaining}` | Estimated time remaining (`H:MM:SS`, from OctoPrint's active estimator) | `1:15:00` |
| `{eta}` | Estimated local completion clock time | `3:45 PM` |
| `{tool_temp}` | Hotend actual / target temperature | `210°C / 210°C` |
| `{bed_temp}` | Bed actual / target temperature | `60°C / 60°C` |
| `{state}` | Current printer state (`Printing`, `Paused`, `Operational`, etc.) | `Printing` |
| `{error}` | Error text (for `Printer error` events) | `Thermal runaway` |
| `{user}` | User who triggered pause/resume/cancel | `admin` |
| `{reason}` | Failure reason (for `Print failed` events) | `error` |

### B. Generic Third-Party Plugin Data (No Hardcoded Plugin Coupling)
Instead of hardcoding integrations for specific plugins, OctoText supports two generic, safe lookup syntaxes so users can pull data from *any* installed OctoPrint plugin:

1. **Metadata path lookup — `{meta:dotted.path}`**
   - Traverses dicts only (never Python object attributes, preventing `__class__` / format-string injection) across OctoPrint's current printer data (`self._printer.get_current_data()`) and the active file's OctoPrint metadata (`self._file_manager.get_metadata(origin, path)`), which is where estimator and analysis plugins store their data.
   - **Example (e.g., with PrintTimeGenius):** `{meta:progress.printTimeLeftOrigin}` (shows `"genius"` when PrintTimeGenius is active), `{meta:analysis.estimatedPrintTime}`, `{meta:analysis.filament.tool0.length}`.
2. **Plugin helper lookup — `{plugin:plugin_id.helper_name}` (or `{plugin:plugin_id.helper_name.key}`)**
   - Uses OctoPrint's standard `self._plugin_manager.get_helpers(plugin_id)`.
   - Safely invokes a zero-argument read-only helper function inside `try/except` (and if `.key` is specified on a dict return value, extracts that key).
   - Missing plugins, missing helpers, or exceptions fall back gracefully to `""` (never breaking notification delivery).

*(In the UI variable reference and README, we document `{meta:...}` and `{plugin:...}` generically and mention PrintTimeGenius only as an example of how to pull values explicitly.)*

---

## 2. Backend Changes (`octoprint_octotext/__init__.py`)

1. **Settings defaults** (disabled & empty by default for 100% backwards compatibility):
   - `"en_custom_templates": False` — master toggle; when `False`, OctoText always uses built-in wording without losing saved templates.
   - `"templates"` dict with the **9 event types**: `print_started`, `print_done`, `print_progress`, `print_paused`, `print_resumed`, `print_cancelled`, `print_failed`, `upload`, `error` — each storing `{"subject": "", "body": ""}` independently.
2. **Template lookup (`_resolve_template(event_key, field)`):**
   - Returns `""` immediately if `en_custom_templates` is `False`.
   - Otherwise, if the event has a non-empty custom template for `subject` or `body`, renders it; if blank, uses OctoText's built-in wording for that event.
3. **Metadata & generic plugin resolver (`_get_template_context(...)`, `_resolve_meta_path(...)`, `_resolve_plugin_helper(...)`):**
   - Safely queries `self._printer.get_current_data()`, `self._printer.get_current_temperatures()`, and `self._file_manager.get_metadata(...)` inside `try/except` blocks so offline/missing data gracefully falls back to `""`.
   - Computes `{eta}` from `datetime.datetime.now() + datetime.timedelta(seconds=int(printTimeLeft))` formatted with `strftime("%I:%M %p").lstrip("0")` for Linux/Windows compatibility.
   - Only evaluates `{meta:...}` and `{plugin:...}` expressions on demand when actually present in a template (zero overhead when unused).
4. **Safe template renderer (`_render_template(template, context)`):**
   - Uses regex substitution (`re.sub`) rather than `str.format()`:
     - `{var}` resolves from `context` (unknown variables remain literal instead of raising `KeyError`).
     - `{meta:a.b.c}` traverses dict keys only (never Python attributes).
     - `{plugin:id.helper}` / `{plugin:id.helper.key}` calls `self._plugin_manager.get_helpers(id)` safely inside `try/except`.
     - Stray braces (`{`, `}`) and attribute traversal (`{printer.__class__}`) never raise errors or evaluate attributes.
5. **Notification assembly (`_prepare_email_message_and_send()`):**
   - Accepts optional `event_key` and `template_extras`, rendering custom Subject/Body when enabled & configured for that event while preserving existing `subject_prefix` and `include_appearance_prefix` behavior.
   - **Send a test message** (`on_api_command("test")`) remains unchanged (always uses the built-in test subject + `smtp_message`).

---

## 3. Settings UI Changes (`OctoText_settings.jinja2` & `OctoText.js`)

1. **Master Checkbox (`en_custom_templates`):**
   - **"Enable custom notification templates."**
   - When unchecked, all template controls stay hidden so the Settings page remains uncluttered.
2. **Event Dropdown Selector** *(visible only when checkbox is checked)*:
   - Initial selection is an empty placeholder option (`-- Select an event to customize --`, value `""`).
   - Lists the **9 event types** (`Print started`, `Print finished`, `Print progress`, `Print paused`, `Print resumed`, `Print cancelled`, `Print failed`, `File uploaded`, `Printer error`).
   - Selecting an event edits that event's template independently—you can customize any subset (0 to all 9) and save them all together. Events with a custom template are marked with `★` in the dropdown.
3. **Per-Event Editor** *(visible only after an event is chosen in the dropdown)*:
   - **Subject template** input and **Body template** textarea using Bootstrap 2 `input-block-level` (fits cleanly inside OctoPrint's ~700px Settings modal without overflowing), each displaying a watermark/placeholder (`Leave blank to use default`).
   - **Reset** button clears the selected event's custom Subject and Body back to blank (built-in default).
   - **Clickable Variable Reference Toggle:** label defined via `gettext(...)` in `OctoText.js` (avoiding translated apostrophes inside Jinja/Knockout `data-bind` strings); expands a compact responsive table listing every core `{variable}`, description, quick insert buttons (`+Subj` / `+Body`), plus an **"Advanced: Data from other plugins"** note documenting `{meta:...}` and `{plugin:...}` with concrete examples (including PrintTimeGenius as an example).
   - **Live Sample Preview (`OctoText.js` via KnockoutJS `ko.computed`):** always shows what the selected event's **Subject** and **Body** will look like with realistic sample data (reflecting `subject_prefix` and `include_appearance_prefix`). When blank, it displays OctoText's **built-in default** output for that event; as soon as you type a custom template, it updates live (with sample values for common `{meta:...}`/`{plugin:...}` expressions as well).

---

## 4. Test Plan (`local-test/test_octotext.py` + Manual Local Verification)

- **Unit tests:**
  - When `en_custom_templates` is `False`, built-in Subject and Body are always used even if custom templates are saved.
  - Blank templates preserve exact built-in Subject and Body behavior for every event.
  - Per-event overrides apply only to their respective event while leaving other events on built-in defaults.
  - All 14 core placeholders render accurately when printer data is present.
  - `{meta:a.b.c}` resolves nested dict keys from `get_current_data()` and `get_metadata()`, and safely returns `""` on missing paths or non-dict values.
  - `{plugin:id.helper}` and `{plugin:id.helper.key}` resolve via `self._plugin_manager.get_helpers()`, and safely return `""` when the plugin/helper is missing or raises an exception.
  - Missing/offline printer data (`None` fields), unknown variables (`{unknown}`), malformed braces (`{oops`), and attribute traversal (`{printer.__class__}`) never raise exceptions.
- **Manual UI verification:**
  - Verify progressive disclosure: checkbox unchecked hides dropdown/editor; checkbox checked shows dropdown starting on empty `-- Select an event to customize --`; choosing an event reveals Subject/Body inputs (with `Leave blank to use default` watermarks), variable reference toggle (including generic plugin data docs), and live preview showing built-in defaults when blank and live updates when edited.
