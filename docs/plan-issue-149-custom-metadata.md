# Implementation Plan: Custom Notification Metadata Templates & Live Preview (#149)

## Goal
Allow users to customize notification **Subject** and **Body** templates using readily available OctoPrint metadata (which automatically reflects **PrintTimeGenius** estimates), paired with a clickable variable reference and a live sample preview in the Settings UI. When templates are left blank, OctoText preserves its exact existing behavior.

---

## 1. Supported Template Variables (Readily Available from OctoPrint Core)

All fields are read directly from `self._printer.get_current_data()`, `self._printer.get_current_temperatures()`, and event payloads—avoiding fragile third-party plugin coupling. Because PrintTimeGenius plugs into OctoPrint's core `octoprint.printer.estimation.factory`, its estimates automatically populate `{remaining}`, `{eta}`, and `{elapsed}`.

| Variable | Description | Sample Value (for Live Preview) |
|---|---|---|
| `{event}` | Default event title (e.g., `Print Progress 50 percent finished.`) | `Print Progress 50 percent finished.` |
| `{message}` | Default event body/details | `benchy.gcode` |
| `{printer}` | Printer appearance name | `Prusa MK4` |
| `{file}` | Current or event filename | `benchy.gcode` |
| `{percent}` | Completion percentage (integer) | `50%` |
| `{elapsed}` | Elapsed print time (`H:MM:SS`) | `1:15:00` |
| `{remaining}` | Estimated time remaining (`H:MM:SS`, uses PrintTimeGenius if installed) | `1:15:00` |
| `{eta}` | Estimated local completion clock time | `3:45 PM` |
| `{tool_temp}` | Hotend actual / target temperature | `210°C / 210°C` |
| `{bed_temp}` | Bed actual / target temperature | `60°C / 60°C` |
| `{state}` | Current printer state (`Printing`, `Paused`, `Operational`, etc.) | `Printing` |

### Out of Scope for Phase 1
- Data not natively tracked by OctoPrint core (e.g., *"time until next `M600` filament change"* or slicer layer counts from third-party plugins).

---

## 2. Backend Changes (`octoprint_octotext/__init__.py`)

1. **Settings defaults** (empty strings by default for 100% backwards compatibility):
   - `"custom_subject_template": ""`
   - `"custom_body_template": ""`
2. **Metadata collector (`_get_template_context(title, body, file_name=None)`):**
   - Safely queries `self._printer.get_current_data()` and `self._printer.get_current_temperatures()` inside `try/except` blocks so offline/missing data gracefully falls back to `""` / `"N/A"`.
   - Computes `{eta}` from `datetime.datetime.now() + datetime.timedelta(seconds=int(printTimeLeft))`.
3. **Safe template renderer (`_render_template(template, context)`):**
   - Uses `re.sub(r"\{(\w+)\}", ...)` rather than `str.format()` so:
     - Unknown variables (e.g. `{typo}`) remain literal instead of raising `KeyError`.
     - Stray or unmatched braces (`{`, `}`) never raise `ValueError`.
     - Format-string attribute/index traversal (e.g. `{printer.__class__}`) is never evaluated.
4. **Notification assembly (`_prepare_email_message_and_send()`):**
   - If `custom_subject_template` is non-empty, render it (with existing `subject_prefix` / `include_appearance_prefix` applied consistently).
   - If `custom_body_template` is non-empty, render it; otherwise keep `body + " Message sent from: " + sender`.

---

## 3. Settings UI Changes (`OctoText_settings.jinja2` & `OctoText.js`)

1. **Optional Template Fields** under a new **"Notification Templates (Optional)"** section:
   - **Custom Subject Template** (`<input type="text">`) — placeholder: `{file} - {percent} ({remaining} left)`
   - **Custom Body Template** (`<textarea>`) — placeholder showing a multi-line example with `{event}`, `{file}`, `{percent}`, `{remaining}`, `{eta}`, `{tool_temp}`, `{bed_temp}`.
   - Help text explaining that leaving either field blank keeps OctoText's default formatting.
2. **Clickable Variable Reference Toggle ("Show available variables & examples"):**
   - Expands a compact table listing every `{variable}`, description, sample value, and brief copy-paste examples (e.g., *"Searchable email subject"*, *"Compact SMS"*).
3. **Live Sample Preview (`OctoText.js` via KnockoutJS `ko.computed`):**
   - Renders the user's current `custom_subject_template` and `custom_body_template` live as they type using realistic sample values (`benchy.gcode`, `50%`, `1:15:00`, `3:45 PM`, `210°C / 210°C`, `60°C / 60°C`, etc.).
   - Displays both the rendered **Subject** and **Body** preview directly inside the Settings dialog so users can verify formatting without sending test emails.

---

## 4. Test Plan (`local-test/test_octotext.py` + Manual Local Verification)

- **Unit tests:**
  - Blank templates preserve exact default Subject and Body behavior.
  - All 11 supported placeholders render accurately when printer data is present.
  - Missing/offline printer data (`None` fields) falls back cleanly without raising exceptions.
  - Unknown variables (`{unknown}`), malformed braces (`{oops`), and attribute traversal (`{printer.__class__}`) are handled safely as literal text.
- **Manual UI verification (`.\local-test\start-test-env.ps1`):**
  - Verify the clickable variable guide expands/collapses cleanly under `is_template_autoescaped() -> True`.
  - Verify the live KnockoutJS preview updates in real time as templates are edited.
  - Verify a sent test message uses the rendered custom Subject and Body in the local inbox (`http://127.0.0.1:8089`).
