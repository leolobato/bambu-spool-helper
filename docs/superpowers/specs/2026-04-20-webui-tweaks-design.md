# Web UI Tweaks — Design

Date: 2026-04-20
Branch: `feature/webui-tweaks`

Three independent UX tweaks to the settings / profile-management screens.

## 1. Profile validation — surface unlinked filaments

### Problem

`POST /settings/validate-profiles` currently reports only on *linked* Spoolman filaments: how many are linked, how many have a matching Orca profile, how many don't. A filament that has never been linked is invisible on this screen, so the user has no prompt to go link it.

### Behavior

The validation result gains a fourth count: **Unlinked** — Spoolman filaments whose `ams_filament_id` is empty or unset (equivalently, `filament.is_linked == False`).

Unlinked filaments are **informational, not a failure**. The overall result banner stays green iff every *linked* filament has a matching profile (unchanged rule). Unlinked filaments do not turn the banner amber.

Below the counts, the partial renders a list of unlinked filaments using the same clickable-card pattern already used for missing-match filaments. Each card is an HTMX button that opens the filament detail modal so the user can jump straight into linking it.

### Changes

- **`app/routers/web.py`** — `_build_linked_profile_validation(filaments, profiles)` gains:
  - Computes `unlinked_filaments = [f for f in filaments if not f.is_linked]`.
  - Returns additional keys: `unlinked_count: int`, `unlinked: list[{"filament": SpoolmanFilament}]`.
  - Existing keys (`linked_count`, `matched_count`, `missing_count`, `matched`, `missing`) unchanged.
- **`app/templates/partials/settings_validation_result.html`**:
  - Count grid becomes 4 columns on `sm:` (Linked | Unlinked | Matched | Missing). "Unlinked" card uses neutral slate styling.
  - New optional section below the missing-list with header `Unlinked filaments`, rendering each `unlinked` entry as a clickable card linking to `/web/filament/{id}?machine={machine_id}`.
- **Tests** — extend `tests/test_web_profile_selection.py` (or new test module) with cases for:
  - A mix of linked/unlinked filaments → correct counts.
  - No unlinked filaments → empty `unlinked` list, banner color unaffected by the new field.

## 2. Spoolman settings section — gate content on validation

### Problem

The "Spoolman" section of the settings page currently shows, on page load:
- The required-custom-fields definition list (always visible).
- Two buttons: **Validate Spoolman Fields** and **Create Missing Fields**.

This is noisy. Most users just want to confirm fields are correct. The definition list is only useful when something is wrong, and the Create button should only be a visible path when a real problem has been detected.

### Behavior

On initial page load, the Spoolman section shows only its heading + the **Validate Spoolman Fields** button. The required-fields definition list and the **Create Missing Fields** button are both hidden.

After the user clicks Validate, the result partial is swapped in:

- If the validation passes (all fields valid), show a green "all good" banner and the counts. Do not show the definition list or the Create button — there is nothing to create.
- If the validation fails (any missing or invalid), show the amber validation result with counts and the missing/invalid lists (as today), **plus** the required-fields definition list (so the user has a reference for what's expected), **plus** the Create Missing Fields button.

After the user clicks Create Missing Fields, the current success path is preserved: the result partial shows the validation result plus a "Created fields: …" banner.

### Changes

- **`app/templates/settings.html`** — remove the always-visible definition list and the always-visible Create Missing Fields button from this template. Keep only the section heading and the Validate button; the result container is where post-validation content lives.
- **`app/templates/partials/settings_spoolman_result.html`** — restructure:
  - Move the required-fields definition block inside the `{% if validation %}` branch, rendered only when `validation.is_valid == False`.
  - Move the Create Missing Fields button into this partial as well, rendered only when `validation.is_valid == False`. (The button issues `hx-post="/settings/spoolman/ensure"` targeting the same result container, same as today.)
  - The "all good" case renders counts + green banner only.
  - The post-create state (with `created_keys` set) renders as today.
- **Route handlers** in `app/routers/web.py` are unchanged — this is pure template reorganization. No new routes.
- **Tests** — no new behavioral routes, so no required router test additions; existing tests continue to pass.

## 3. Process-profile import — unified import modal with type toggle

### Problem

Profile management supports importing filament profiles but not process (print-settings) profiles. orcaslicer-cli already exposes `/profiles/processes/resolve-import` and `POST /profiles/processes` with payload shapes that mirror the filament endpoints, so the gap is purely on the web UI + client side.

### Scope

**In scope:** Import only (one button, unified modal with a type toggle at the top).
**Out of scope:** Listing, viewing, or deleting process profiles in the UI.

### Service layer

Add to `OrcaSlicerClient` (in `app/services/orcaslicer_client.py`) two new methods mirroring the existing filament ones:

```python
async def resolve_import_process_profile(
    self, payload: dict
) -> ResolveProcessImportResponse: ...

async def import_process_profile(
    self, payload: dict, *, replace: bool = False
) -> ImportProcessProfileResponse: ...
```

- `resolve_import_process_profile` → `POST /profiles/processes/resolve-import`
- `import_process_profile` → `POST /profiles/processes` (with `?replace=true` when `replace` is set)

New Pydantic models in the same module:

- `ResolveProcessImportResponse` — fields: `setting_id: str`, `name: str`, `inherits_resolved: str | None`, `resolved_payload: dict`.
- `ImportProcessProfileResponse` — fields: `setting_id: str`, `name: str`, `message: str | None`.

These models deliberately omit `filament_id` and `filament_type` — process profiles don't carry either.

### UI

One **Import Profile** button (existing) opens one modal. At the top of the modal body, a radio toggle selects the profile kind:

- **Filament** (default)
- **Process**

The selected kind is posted alongside the file as a form field `kind` (one of `filament` | `process`).

Flow per kind:

- **kind=filament** — unchanged from today. Upload → `resolve_import_profile` → if `filament_type` missing, render the type-picker step → `import_profile` → render success partial with filament-specific fields.
- **kind=process** — Upload → `resolve_import_process_profile`. If resolve fails, render an error partial. On success, immediately call `import_process_profile` (no type-picker step — processes don't have a filament type). Render success partial with process-specific fields: `name`, `setting_id`, `layer_height`, `vendor`.

### Routes

- `GET /import-profile` — unchanged URL. Accepts new optional query param `?kind=filament|process` to pre-select the toggle. Default is `filament`.
- `POST /import-profile` — reads the new `kind` form field (default `filament`) and dispatches to the filament or process branch inside the handler. No new route path; same HTMX target container.

### Templates

- **`app/templates/partials/import_profile.html`** — add the kind toggle at the top of the modal's initial state. Thread `kind` through the form submission. Branch the success/pending rendering on `kind`:
  - Pending state: only rendered for filament kind (process has no pending step).
  - Success state: split into filament-success and process-success sub-blocks by a `{% if kind == "process" %}` check, showing the appropriate fields.

### Tests

- **`tests/test_orcaslicer_client.py`** (new or extended) — mock httpx; assert `resolve_import_process_profile` hits the right URL and parses the response, and `import_process_profile` posts with/without `?replace=true`.
- **`tests/test_web_profile_selection.py`** or a new `tests/test_import_process_profile.py` — exercise `POST /import-profile` with `kind=process`:
  - Happy path: resolve succeeds, import succeeds, success partial renders with process fields.
  - Resolve error path: error partial renders.
- Existing `kind=filament` tests must continue to pass unchanged.

## Non-goals

- No process-profile listing, detail, or delete UI.
- No changes to the home-page filament list.
- No changes to the tray activation / MQTT path.
- No refactor of `web.py`'s overall structure (it stays ~1800 lines; we add a focused branch, not a reorg).
