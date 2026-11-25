# Implementation Plan: Custom Notification Metadata Templates & Live Preview (#149)

## Goal
Allow users to optionally enable and customize notification **Subject** and **Body** templates per event type using OctoPrint metadata and a **generic** mechanism for pulling data from other installed plugins (with no first-class/hardcoded coupling to any specific third-party plugin), paired with a clickable variable reference and a live sample preview in the Settings UI. When custom templates are disabled—or when an event's template fields are left blank—OctoText preserves its exact built-in wording.

---

## Review Decisions (resolved)

The seven review answers are the authoritative behavior for this plan:

1. **Template default — bare.** The setting is opt-in and templates start blank; `{percent}` renders a bare integer (`50`), not `50%`, so users add `%` in the template when they want it.
2. **Multi-tool temperatures — list all.** `{tool_temp}` lists every detected tool (for example `T0 210°C / 210°C, T1 205°C / 205°C`), not just the first tool.
3. **One progress template — yes.** A single `print_progress` template applies to every progress notification mode, including percentage-based progress and time-based/fixed-interval progress. Users do not maintain separate templates for those modes.
4. **`{event}` — full built-in title, yes.** `{event}` is the complete built-in event title including dynamic details (for example `Print Fail after 120 seconds`), not a generic event label.
5. **Preview placeholder — exactly `???`.** Unresolved advanced lookups show three question marks with no surrounding quotation marks in the Settings preview. Actual notifications still resolve missing data to an empty string instead of failing or emitting a quoted placeholder.
6. **README documentation — yes.** Document the feature, every core placeholder, and the generic metadata/helper lookups in `README.md`.
7. **Settings test button — overrides when set.** The Settings test button sends the selected event's custom Subject/Body with sample data when templates are enabled and that event has an override; otherwise it sends the built-in test message. The navbar test button remains built-in only.

---

## 1. Supported Template Variables

### A. Core OctoPrint Variables
Read directly from OctoPrint's `self._printer.get_current_data()`, `self._printer.get_current_temperatures()`, and event payloads:

| Variable | Description | Sample Value (for Live Preview) |
|---|---|---|
| `{event}` | Full built-in event title, including dynamic details (e.g., `Print Progress 50% finished.`, `Print Fail after 120 seconds`) | `Print Progress 50% finished.` |
| `{message}` | Built-in event body/details | `benchy.gcode` |
| `{printer}` | Printer appearance name | `Prusa MK4` |
| `{file}` | Current or event filename | `benchy.gcode` |
| `{percent}` | Completion percentage as a bare integer, without a `%` sign (add `%` in the template) | `50` |
| `{elapsed}` | Elapsed print time (`H:MM:SS`) | `1:15:00` |
| `{remaining}` | Estimated time remaining (`H:MM:SS`, from OctoPrint's active estimator) | `1:15:00` |
| `{eta}` | Estimated local completion clock time | `3:45 PM` |
| `{tool_temp}` | Hotend actual / target temperatures — one entry per tool | `T0 210°C / 210°C, T1 205°C / 205°C` |
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
   - `"templates"` dict with the **9 event types**: `print_started`, `print_done`, `print_progress`, `print_paused`, `print_resumed`, `print_cancelled`, `print_failed`, `upload`, `error` — each storing `{"subject": "", "body": ""}` independently. `print_progress` is one shared template for both percentage-based and time-based/fixed-interval progress notifications.
2. **Template lookup (`_resolve_template(event_key, field)`):**
   - Returns `None` when custom templates are disabled or the selected field is blank; callers treat `None` as "no override" and keep the built-in wording for that event.
   - Otherwise renders the stored template. A rendered subject that ends up empty after substitution also falls back to the built-in event title.
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
   - **Send a test message** (`on_api_command("test")`) accepts an optional `event` key in the request data. The Settings page test button passes the currently selected template event. When `en_custom_templates` is `True` and that event has at least one non-empty override, the test renders that event's Subject/Body using the same sample values as the live preview (a blank field falls back to the event's built-in wording); otherwise it keeps today's built-in test subject + `smtp_message`. The navbar envelope button sends no `event` and always uses the built-in test message.

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
   - **Live Sample Preview (`OctoText.js` via KnockoutJS `ko.computed`):** always shows what the selected event's **Subject** and **Body** will look like with realistic sample data (reflecting `subject_prefix` and `include_appearance_prefix`). When blank, it displays OctoText's **built-in default** output for that event; as soon as you type a custom template, it updates live (with sample values for the documented `{meta:...}`/`{plugin:...}` examples; any other advanced lookup shows exactly `???`—three question marks, no surrounding quotation marks—in the preview only, since the backend still resolves missing data to `""`).
4. **Test button:** the Settings **Send a test message** button uses the selected event's overrides when custom templates are enabled and that event has at least one non-empty Subject/Body template. It renders with the same sample values as the live preview, so the received message matches the preview. With no event selected, or no overrides set, it sends the existing built-in test message. The navbar test button always sends the built-in test message.

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
  - `{percent}` renders a bare integer (no `%`), and `{tool_temp}` lists every tool when more than one is present.
  - The settings test command renders the selected event's override with sample data when enabled, falls back to the event's built-in wording for a blank field, and keeps the built-in test message when no event or override is supplied.
- **Manual UI verification:**
  - Verify progressive disclosure: checkbox unchecked hides dropdown/editor; checkbox checked shows dropdown starting on empty `-- Select an event to customize --`; choosing an event reveals Subject/Body inputs (with `Leave blank to use default` watermarks), variable reference toggle (including generic plugin data docs), and live preview showing built-in defaults when blank and live updates when edited. Unknown advanced lookups show `???` in the preview. The Settings test button sends the selected event's override (with sample data) when enabled, and the navbar test button still sends the built-in test message.

## 5. Documentation (`README.md`)

- Add a short **Custom notification templates** section: the master toggle, the 9 event types, and the blank/disabled fallback behavior.
- Document every core placeholder in a table, noting that `{percent}` is a bare integer (`50`, so write `{percent}%` for a percent sign) and that `{tool_temp}` lists each tool (`T0 …`, `T1 …`).
- Document the generic `{meta:dotted.path}` and `{plugin:plugin_id.helper_name}` (optionally `{plugin:plugin_id.helper_name.key}`) syntax, with PrintTimeGenius as the worked example, and note that missing plugins, helpers, keys or data render as an empty string rather than failing the notification.
