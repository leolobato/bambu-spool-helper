# Web UI Tweaks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three focused UX improvements to the web UI — (1) expose unlinked Spoolman filaments on the profile-validation screen, (2) gate the Spoolman settings section content behind validation, and (3) add process-profile import via a unified import modal with a type toggle.

**Architecture:** Three independent slices. Slice 1 is a backend helper change plus template. Slice 2 is pure template reorganization. Slice 3 adds two thin methods to `OrcaSlicerClient`, a `kind` dispatch in the import handler, and a type toggle in the import modal template. No schema changes. No new routes.

**Tech Stack:** FastAPI + Jinja2 + HTMX + Tailwind. Pydantic models. httpx async client against orcaslicer-cli. Tests use stdlib `unittest` with `unittest.mock.AsyncMock`.

**Spec:** `docs/superpowers/specs/2026-04-20-webui-tweaks-design.md`

**Working directory:** `/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-spool-helper/.worktrees/webui-tweaks`

**Before starting:** every Python command below assumes the worktree's `.venv` is used. Prefix Python commands with `.venv/bin/python` or activate first. Baseline test count is 23 passing.

---

## File Structure

Files created or modified by this plan:

**Modified:**
- `app/routers/web.py` — widen `_build_linked_profile_validation`; add `kind` support to import handlers and `_render_import_profile_modal`.
- `app/services/orcaslicer.py` — add `resolve_import_process_profile` and `import_process_profile` methods.
- `app/templates/settings.html` — remove the always-on definition list + Create Missing Fields button from the Spoolman section; keep only the Validate button.
- `app/templates/partials/settings_validation_result.html` — 4-column count grid (add Unlinked); render unlinked filament list.
- `app/templates/partials/settings_spoolman_result.html` — render the expected-fields definition list and the Create Missing Fields button only when validation has run and reported problems.
- `app/templates/partials/import_profile.html` — add kind toggle; branch success rendering on kind; branch headings/labels on kind.

**Created:**
- `tests/test_orcaslicer_process_import.py` — unit tests for the two new `OrcaSlicerClient` methods.
- `tests/test_import_process_profile.py` — integration-ish tests for `POST /web/import-profile?kind=process` using FastAPI's `TestClient` with `app.state.orcaslicer` mocked.

Each slice below is self-contained: tests added, implementation added, commit.

---

## Task 1: Add unlinked-filament counters to profile validation

**Files:**
- Test: `tests/test_settings_profile_validation.py` (append a new test method)
- Modify: `app/routers/web.py:275-304` (function `_build_linked_profile_validation`)

- [ ] **Step 1.1: Add a failing test for unlinked counters**

Append a new test method to `tests/test_settings_profile_validation.py` inside `SettingsProfileValidationTests`:

```python
    def test_validation_includes_unlinked_filaments(self) -> None:
        profiles: list[FilamentProfileResponse] = []
        filaments = [
            SpoolmanFilament(
                id=1,
                name="Linked PLA",
                material="PLA",
                vendor=SpoolmanVendor(name="eSUN"),
                extra={
                    "ams_filament_id": '"PLA-001"',
                    "ams_filament_type": '"PLA"',
                },
            ),
            SpoolmanFilament(
                id=2,
                name="Never linked",
                material="PLA",
                vendor=SpoolmanVendor(name="eSUN"),
                extra={},
            ),
            SpoolmanFilament(
                id=3,
                name="Partially linked",
                material="PLA",
                vendor=SpoolmanVendor(name="eSUN"),
                extra={"ams_filament_id": '"PLA-777"'},
            ),
        ]

        validation = _build_linked_profile_validation(filaments, profiles)

        self.assertEqual(validation["linked_count"], 1)
        self.assertEqual(validation["unlinked_count"], 2)
        unlinked_ids = sorted(item["filament"].id for item in validation["unlinked"])
        self.assertEqual(unlinked_ids, [2, 3])
```

(Relies on the existing `is_linked` rule in `app/models.py:213-214` which requires *both* `ams_filament_id` and `ams_filament_type`, so filament #3 counts as unlinked.)

- [ ] **Step 1.2: Run the test and confirm it fails**

```bash
.venv/bin/python -m unittest tests.test_settings_profile_validation -v
```

Expected: the new test fails with `KeyError: 'unlinked_count'` (and/or `'unlinked'`). The pre-existing test continues to pass.

- [ ] **Step 1.3: Extend `_build_linked_profile_validation`**

In `app/routers/web.py`, replace the body of `_build_linked_profile_validation` (currently lines 275-304) with:

```python
def _build_linked_profile_validation(
    filaments: list[SpoolmanFilament],
    profiles: list[FilamentProfileResponse],
) -> dict[str, Any]:
    linked_filaments = [filament for filament in filaments if filament.is_linked]
    unlinked_filaments = [filament for filament in filaments if not filament.is_linked]
    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for filament in linked_filaments:
        profile = _find_linked_profile(profiles, filament)
        item = {
            "filament": filament,
            "linked_filament_id": (filament.ams_filament_id or "").strip(),
            "linked_filament_type": (filament.ams_filament_type or "").strip(),
        }
        if profile is None:
            missing.append(item)
            continue
        matched.append({
            **item,
            "profile": profile,
        })

    unlinked = [{"filament": filament} for filament in unlinked_filaments]

    return {
        "linked_count": len(linked_filaments),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "unlinked_count": len(unlinked_filaments),
        "matched": matched,
        "missing": missing,
        "unlinked": unlinked,
    }
```

- [ ] **Step 1.4: Run the full test suite**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: all tests pass (previous 23 + 1 new = 24).

- [ ] **Step 1.5: Commit**

```bash
git add app/routers/web.py tests/test_settings_profile_validation.py
git commit -m "Add unlinked filaments to profile validation result"
```

---

## Task 2: Render unlinked filaments in the validation partial

**Files:**
- Modify: `app/templates/partials/settings_validation_result.html`

No new tests — this is presentation-only. Correctness is verified by running the existing suite plus manual browser check.

- [ ] **Step 2.1: Update the count grid to 4 columns and add Unlinked card**

In `app/templates/partials/settings_validation_result.html`, replace the `grid` block (currently lines 16-29) with:

```html
    <div class="mt-2 grid gap-2 text-xs text-slate-200 sm:grid-cols-4">
      <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
        <div class="text-slate-400">Linked</div>
        <div class="mt-1 text-sm font-semibold">{{ validation.linked_count }}</div>
      </div>
      <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
        <div class="text-slate-400">Unlinked</div>
        <div class="mt-1 text-sm font-semibold">{{ validation.unlinked_count }}</div>
      </div>
      <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
        <div class="text-slate-400">Matched</div>
        <div class="mt-1 text-sm font-semibold">{{ validation.matched_count }}</div>
      </div>
      <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
        <div class="text-slate-400">Missing</div>
        <div class="mt-1 text-sm font-semibold">{{ validation.missing_count }}</div>
      </div>
    </div>
```

- [ ] **Step 2.2: Append an unlinked-filaments list section after the missing-list block**

Still in `settings_validation_result.html`, directly after the `{% if validation.missing %} … {% endif %}` block (currently ends at line 49) and still inside the outer `{% elif validation %}` branch, add:

```html
    {% if validation.unlinked %}
      <div class="mt-3">
        <div class="text-xs uppercase tracking-wide text-slate-400">Unlinked filaments</div>
        <div class="mt-2 space-y-2">
          {% for item in validation.unlinked %}
            <button
              class="block w-full rounded-md border border-slate-700/60 bg-slate-950/30 p-3 text-left text-sm text-slate-200 transition hover:bg-slate-900/60"
              hx-get="/web/filament/{{ item.filament.id }}?machine={{ validation.machine_id }}"
              hx-target="#filament-detail"
              hx-swap="innerHTML"
              type="button"
            >
              <div class="font-medium">{{ item.filament.display_name }}</div>
              <div class="mt-1 text-xs text-slate-400">Spoolman filament #{{ item.filament.id }}</div>
              <div class="mt-2 text-xs text-cyan-200">Open filament details</div>
            </button>
          {% endfor %}
        </div>
      </div>
    {% endif %}
```

Do **not** change the banner color logic (the outer `{% if validation.missing_count %}` amber/green check stays as-is — unlinked filaments do not flip the banner).

- [ ] **Step 2.3: Run the full test suite**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: all 24 tests pass.

- [ ] **Step 2.4: Commit**

```bash
git add app/templates/partials/settings_validation_result.html
git commit -m "Surface unlinked filaments in profile validation result"
```

---

## Task 3: Gate Spoolman settings content on validation

**Files:**
- Modify: `app/templates/settings.html` (Spoolman section only; lines 82-108)
- Modify: `app/templates/partials/settings_spoolman_result.html`

Pure template reorganization. No handler changes.

- [ ] **Step 3.1: Simplify the Spoolman section in `settings.html`**

In `app/templates/settings.html`, replace the whole Spoolman `<section>` block (lines 82-108) with:

```html
    <section class="rounded-xl border border-slate-800 bg-panel p-5">
      <h2 class="text-lg font-semibold tracking-tight">Spoolman</h2>
      <p class="mt-1 text-sm text-slate-400">Validate the required Spoolman filament custom fields.</p>

      <div class="mt-4 flex flex-wrap gap-2">
        <button
          class="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
          hx-post="/web/settings/spoolman/validate"
          hx-target="#settings-spoolman-result"
          hx-swap="innerHTML"
        >
          Validate Spoolman Fields
        </button>
      </div>

      <div id="settings-spoolman-result" class="mt-4">
        {% include "partials/settings_spoolman_result.html" %}
      </div>
    </section>
```

The Create Missing Fields button and the expected-fields reference now live inside the partial only.

- [ ] **Step 3.2: Move definition list + create button into the partial, gated on invalid state**

Replace the current contents of `app/templates/partials/settings_spoolman_result.html` with:

```html
{% if error_message %}
  <div class="rounded-md border border-rose-700/60 bg-rose-900/30 p-3 text-sm text-rose-100">
    {{ error_message }}
  </div>
{% elif validation %}
  <div class="space-y-3">
    {% if created_keys %}
      <div class="rounded-md border border-emerald-700/60 bg-emerald-900/20 p-3 text-sm text-emerald-100">
        Created fields: {{ created_keys|join(", ") }}
      </div>
    {% endif %}

    {% if errors %}
      <div class="rounded-md border border-rose-700/60 bg-rose-900/30 p-3 text-sm text-rose-100">
        {% for error in errors %}
          <div>{{ error }}</div>
        {% endfor %}
      </div>
    {% endif %}

    <div class="rounded-md border {% if validation.is_valid %}border-emerald-700/60 bg-emerald-900/20{% else %}border-amber-700/60 bg-amber-900/20{% endif %} p-3">
      <div class="text-sm {% if validation.is_valid %}text-emerald-100{% else %}text-amber-100{% endif %}">
        {% if validation.is_valid %}
          Spoolman filament custom fields are valid.
        {% else %}
          Spoolman filament custom fields are not valid yet.
        {% endif %}
      </div>
      <div class="mt-2 grid gap-2 text-xs text-slate-200 sm:grid-cols-3">
        <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
          <div class="text-slate-400">Required</div>
          <div class="mt-1 text-sm font-semibold">{{ validation.required_count }}</div>
        </div>
        <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
          <div class="text-slate-400">Valid</div>
          <div class="mt-1 text-sm font-semibold">{{ validation.valid_count }}</div>
        </div>
        <div class="rounded-md border border-slate-700/70 bg-slate-950/30 p-2">
          <div class="text-slate-400">Missing / Invalid</div>
          <div class="mt-1 text-sm font-semibold">{{ validation.missing_count + validation.invalid_count }}</div>
        </div>
      </div>

      {% if validation.missing %}
        <div class="mt-3">
          <div class="text-xs uppercase tracking-wide text-slate-400">Missing</div>
          <div class="mt-2 space-y-2">
            {% for field in validation.missing %}
              <div class="rounded-md border border-amber-700/40 bg-slate-950/30 p-2 text-xs text-slate-200">
                <span class="font-mono">{{ field.key }}</span> — {{ field.name }} — {{ field.field_type }}{% if field.unit %} — {{ field.unit }}{% endif %}
              </div>
            {% endfor %}
          </div>
        </div>
      {% endif %}

      {% if validation.invalid %}
        <div class="mt-3">
          <div class="text-xs uppercase tracking-wide text-slate-400">Invalid</div>
          <div class="mt-2 space-y-2">
            {% for field in validation.invalid %}
              <div class="rounded-md border border-amber-700/40 bg-slate-950/30 p-2 text-xs text-slate-200">
                <div><span class="font-mono">{{ field.expected.key }}</span> has mismatched settings.</div>
                <div class="mt-1 text-slate-400">Expected: {{ field.expected.name }} — {{ field.expected.field_type }}{% if field.expected.unit %} — {{ field.expected.unit }}{% endif %}</div>
                <div class="mt-1 text-slate-400">Actual: {{ field.actual.name or "-" }} — {{ field.actual.field_type or "-" }}{% if field.actual.unit is defined and field.actual.unit %} — {{ field.actual.unit }}{% endif %}</div>
                {% for mismatch in field.mismatches %}
                  <div class="mt-1">{{ mismatch }}</div>
                {% endfor %}
              </div>
            {% endfor %}
          </div>
        </div>
      {% endif %}
    </div>

    {% if not validation.is_valid %}
      <div class="rounded-md border border-slate-700 bg-slate-950/30 p-3">
        <div class="text-sm text-slate-200">Required filament custom fields</div>
        <div class="mt-2 space-y-2 text-xs text-slate-300">
          {% for field in expected_fields %}
            <div class="rounded-md border border-slate-700/70 p-2">
              <div><span class="text-slate-400">Key:</span> <span class="font-mono">{{ field.key }}</span></div>
              <div class="mt-1"><span class="text-slate-400">Name:</span> {{ field.name }}</div>
              <div class="mt-1"><span class="text-slate-400">Type:</span> {{ field.field_type }}</div>
              {% if field.unit %}
                <div class="mt-1"><span class="text-slate-400">Unit:</span> {{ field.unit }}</div>
              {% endif %}
            </div>
          {% endfor %}
        </div>
      </div>

      <div>
        <button
          class="rounded-md border border-cyan-700 px-3 py-1.5 text-sm text-cyan-200 hover:bg-cyan-900/30"
          hx-post="/web/settings/spoolman/ensure"
          hx-target="#settings-spoolman-result"
          hx-swap="innerHTML"
        >
          Create Missing Fields
        </button>
      </div>
    {% endif %}
  </div>
{% else %}
  <div class="rounded-md border border-dashed border-slate-700 p-3 text-sm text-slate-400">
    Validate Spoolman to confirm the required filament fields exist with the expected type and unit.
  </div>
{% endif %}
```

Key changes versus the old partial:
1. Outer `{% elif validation %}` replaces the old `{% else %}` to cover the initial-load case with the dashed placeholder (matching `settings_validation_result.html`'s pattern).
2. The expected-fields definition block is now wrapped in `{% if not validation.is_valid %}`.
3. The Create Missing Fields button lives inside the partial, also gated on `not validation.is_valid`.
4. The created/errors banners are rendered above the validation result (unchanged behavior, just relocated for readability).

- [ ] **Step 3.3: Run the full test suite**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: 24 tests pass.

- [ ] **Step 3.4: Commit**

```bash
git add app/templates/settings.html app/templates/partials/settings_spoolman_result.html
git commit -m "Gate Spoolman definition list and create button on validation"
```

---

## Task 4: Add process-profile methods to `OrcaSlicerClient`

**Files:**
- Create: `tests/test_orcaslicer_process_import.py`
- Modify: `app/services/orcaslicer.py`

- [ ] **Step 4.1: Write the failing test**

Create `tests/test_orcaslicer_process_import.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.orcaslicer import OrcaSlicerClient


def _make_client() -> OrcaSlicerClient:
    client = OrcaSlicerClient(base_url="http://orcaslicer.test", machine_id="GM014")
    return client


def _mock_response(json_body: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.json.return_value = json_body
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    return response


class ResolveImportProcessProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_to_resolve_endpoint_and_returns_payload(self) -> None:
        client = _make_client()
        expected_payload = {
            "setting_id": "CUSTOM001",
            "name": "Custom Process",
            "inherits_resolved": "0.20mm Standard @BBL P1S",
            "resolved_payload": {"layer_height": "0.2"},
        }
        client._client.post = AsyncMock(return_value=_mock_response(expected_payload))

        result = await client.resolve_import_process_profile({"name": "Custom Process"})

        client._client.post.assert_awaited_once_with(
            "/profiles/processes/resolve-import",
            json={"name": "Custom Process"},
        )
        self.assertEqual(result, expected_payload)


class ImportProcessProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_to_processes_endpoint_without_replace(self) -> None:
        client = _make_client()
        expected_payload = {
            "setting_id": "CUSTOM001",
            "name": "Custom Process",
            "message": "Imported",
        }
        client._client.post = AsyncMock(return_value=_mock_response(expected_payload))

        result = await client.import_process_profile({"name": "Custom Process"})

        client._client.post.assert_awaited_once_with(
            "/profiles/processes",
            json={"name": "Custom Process"},
            params={},
        )
        self.assertEqual(result, expected_payload)

    async def test_posts_with_replace_true_query_param(self) -> None:
        client = _make_client()
        client._client.post = AsyncMock(return_value=_mock_response({"setting_id": "X", "name": "X"}))

        await client.import_process_profile({"name": "X"}, replace=True)

        client._client.post.assert_awaited_once_with(
            "/profiles/processes",
            json={"name": "X"},
            params={"replace": "true"},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4.2: Run and confirm it fails**

```bash
.venv/bin/python -m unittest tests.test_orcaslicer_process_import -v
```

Expected: failures with `AttributeError: 'OrcaSlicerClient' object has no attribute 'resolve_import_process_profile'` (and `import_process_profile` missing too).

- [ ] **Step 4.3: Add the two methods to `OrcaSlicerClient`**

In `app/services/orcaslicer.py`, immediately after the existing `resolve_import_profile` method (currently ends around line 42), add:

```python
    async def import_process_profile(
        self,
        data: dict[str, Any],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if replace:
            params["replace"] = "true"
        response = await self._client.post("/profiles/processes", json=data, params=params)
        response.raise_for_status()
        return response.json()

    async def resolve_import_process_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/profiles/processes/resolve-import", json=data)
        response.raise_for_status()
        return response.json()
```

These methods deliberately do **not** touch `self._profiles_by_machine` — the in-memory cache is for filament profiles only and process profiles aren't listed in the web UI.

- [ ] **Step 4.4: Run and confirm tests pass**

```bash
.venv/bin/python -m unittest tests.test_orcaslicer_process_import -v
```

Expected: 3/3 pass.

- [ ] **Step 4.5: Run the full suite**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: 27 tests pass (24 + 3 new).

- [ ] **Step 4.6: Commit**

```bash
git add app/services/orcaslicer.py tests/test_orcaslicer_process_import.py
git commit -m "Add process-profile resolve + import to OrcaSlicerClient"
```

---

## Task 5: Dispatch import handler on `kind` form field

**Files:**
- Create: `tests/test_import_process_profile.py`
- Modify: `app/routers/web.py` — `import_profile_modal` GET, `import_profile_upload` POST, `_render_import_profile_modal`

- [ ] **Step 5.1: Write the failing integration test**

Create `tests/test_import_process_profile.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app


def _install_mocks(orcaslicer_mock: MagicMock, spoolman_mock: MagicMock) -> None:
    app.state.orcaslicer = orcaslicer_mock
    app.state.spoolman = spoolman_mock


def _base_orcaslicer_mock() -> MagicMock:
    mock = MagicMock()
    mock.default_machine_id = "GM014"
    mock.get_machines = MagicMock(return_value=[])
    mock.load_machines = AsyncMock(return_value=[])
    mock.has_machine = MagicMock(return_value=True)
    return mock


def _base_spoolman_mock() -> MagicMock:
    mock = MagicMock()
    mock.REQUIRED_SETTINGS_FILAMENT_FIELDS = []
    return mock


class ImportProcessProfileRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orcaslicer = _base_orcaslicer_mock()
        self.spoolman = _base_spoolman_mock()
        _install_mocks(self.orcaslicer, self.spoolman)
        # IMPORTANT: do NOT use `with TestClient(app) as client:` — entering the
        # context manager starts the app lifespan, which tries to reach a real
        # orcaslicer service. Plain instantiation skips the lifespan, leaving
        # our injected mocks intact.
        self.client = TestClient(app)

    def test_kind_process_happy_path(self) -> None:
        self.orcaslicer.resolve_import_process_profile = AsyncMock(
            return_value={
                "setting_id": "CUSTOM001",
                "name": "Custom Process",
                "inherits_resolved": "0.20mm Standard",
                "resolved_payload": {
                    "name": "Custom Process",
                    "setting_id": "CUSTOM001",
                    "layer_height": "0.2",
                    "vendor": "BBL",
                },
            }
        )
        self.orcaslicer.import_process_profile = AsyncMock(
            return_value={
                "setting_id": "CUSTOM001",
                "name": "Custom Process",
                "message": "Imported",
            }
        )

        payload_bytes = b'{"name": "Custom Process", "setting_id": "CUSTOM001"}'
        response = self.client.post(
            "/web/import-profile",
            data={"machine": "GM014", "kind": "process"},
            files={"profile_file": ("custom.json", payload_bytes, "application/json")},
        )

        self.assertEqual(response.status_code, 200)
        self.orcaslicer.resolve_import_process_profile.assert_awaited_once()
        self.orcaslicer.import_process_profile.assert_awaited_once()
        body = response.text
        self.assertIn("Custom Process", body)
        self.assertIn("CUSTOM001", body)

    def test_kind_process_resolve_failure_renders_error(self) -> None:
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "bad payload"
        self.orcaslicer.resolve_import_process_profile = AsyncMock(
            side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=mock_response)
        )

        response = self.client.post(
            "/web/import-profile",
            data={"machine": "GM014", "kind": "process"},
            files={"profile_file": ("bad.json", b"{}", "application/json")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Profile resolution failed", response.text)
        self.assertIn("bad payload", response.text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5.2: Run and confirm it fails**

```bash
.venv/bin/python -m unittest tests.test_import_process_profile -v
```

Expected: failures — the handler doesn't accept `kind`, doesn't call the new methods, and the process branch isn't implemented.

- [ ] **Step 5.3: Add a `kind` param to `_render_import_profile_modal`**

In `app/routers/web.py`, update the signature and context of `_render_import_profile_modal` (currently lines 712-741):

```python
def _render_import_profile_modal(
    request: Request,
    *,
    machine_id: str = "",
    kind: str = "filament",
    error_message: str = "",
    success_message: str = "",
    import_result: dict[str, Any] | None = None,
    pending_import_payload: str = "",
    pending_profile_name: str = "",
    pending_filament_id: str = "",
    pending_filament_type: str = "",
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/import_profile.html",
        {
            "request": request,
            "machine_id": machine_id,
            "kind": kind if kind in {"filament", "process"} else "filament",
            "error_message": error_message,
            "success_message": success_message,
            "import_result": import_result or {},
            "pending_import_payload": pending_import_payload,
            "pending_profile_name": pending_profile_name,
            "pending_filament_id": pending_filament_id,
            "pending_filament_type": pending_filament_type,
            "valid_filament_types": sorted(VALID_TRAY_TYPES),
        },
        headers=headers,
    )
```

- [ ] **Step 5.4: Let the GET handler pre-select `kind` from query**

In `app/routers/web.py`, replace the body of `import_profile_modal` (currently lines 864-870) with:

```python
@router.get("/import-profile")
async def import_profile_modal(
    request: Request,
    machine: str = Query(default=""),
    kind: str = Query(default="filament"),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    return _render_import_profile_modal(request, machine_id=machine_id, kind=kind)
```

- [ ] **Step 5.5: Branch the POST handler on `kind`**

In `app/routers/web.py`, replace the entire `import_profile_upload` handler (currently lines 970-1102) with:

```python
@router.post("/import-profile")
async def import_profile_upload(
    request: Request,
    profile_file: UploadFile | None = File(default=None),
    machine: str = Form(default=""),
    payload_json: str = Form(default=""),
    filament_type: str = Form(default=""),
    kind: str = Form(default="filament"),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    normalized_kind = kind if kind in {"filament", "process"} else "filament"

    if normalized_kind == "process":
        return await _import_process_profile_flow(
            request,
            profile_file=profile_file,
            machine_id=machine_id,
        )

    # kind == "filament" — existing flow, unchanged
    if payload_json.strip():
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Pending import payload is invalid. Upload the JSON file again.",
            )

        if not isinstance(payload, dict):
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Pending import payload is invalid. Upload the JSON file again.",
            )

        normalized_filament_type = _normalize_valid_filament_type(filament_type)
        if not normalized_filament_type:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Choose a valid filament type before importing.",
                pending_import_payload=payload_json,
                pending_profile_name=str(payload.get("name", "")).strip(),
                pending_filament_id=str(payload.get("filament_id", "")).strip(),
                pending_filament_type=str(filament_type or "").strip(),
            )
        _set_payload_filament_type(payload, normalized_filament_type)
    else:
        filename = profile_file.filename if profile_file else ""
        if not filename:
            return _render_import_profile_modal(request, machine_id=machine_id, kind="filament", error_message="Please choose a JSON file.")
        if not filename.lower().endswith(".json"):
            return _render_import_profile_modal(request, machine_id=machine_id, kind="filament", error_message="Only .json profile files are supported.")

        try:
            raw = await profile_file.read()
        finally:
            await profile_file.close()

        if not raw:
            return _render_import_profile_modal(request, machine_id=machine_id, kind="filament", error_message="Uploaded file is empty.")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _render_import_profile_modal(request, machine_id=machine_id, kind="filament", error_message="Profile file must be UTF-8 encoded JSON.")
        except json.JSONDecodeError:
            return _render_import_profile_modal(request, machine_id=machine_id, kind="filament", error_message="Invalid JSON file.")

        if not isinstance(payload, dict):
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Profile JSON must be an object.",
            )

        try:
            resolved_preview = await request.app.state.orcaslicer.resolve_import_profile(payload)
        except httpx.HTTPStatusError as exc:
            error_detail = exc.response.text.strip() or str(exc)
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message=f"Profile resolution failed ({exc.response.status_code}): {error_detail}",
            )
        except httpx.HTTPError as exc:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message=f"Profile resolution request failed: {exc}",
            )

        resolved_payload = resolved_preview.get("resolved_payload")
        if not isinstance(resolved_payload, dict):
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Resolved profile payload is invalid.",
            )

        payload = dict(resolved_payload)
        normalized_filament_type = _normalize_valid_filament_type(_extract_payload_filament_type(payload))
        if not normalized_filament_type:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Resolved profile is missing a valid filament type. Choose one before importing.",
                pending_import_payload=json.dumps(payload),
                pending_profile_name=str(resolved_preview.get("name", "")).strip(),
                pending_filament_id=str(resolved_preview.get("filament_id", "")).strip(),
                pending_filament_type=_extract_payload_filament_type(payload),
            )

    try:
        result = await request.app.state.orcaslicer.import_profile(payload, machine_id)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip()
        if not error_detail:
            error_detail = str(exc)
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="filament",
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="filament",
            error_message=f"Import request failed: {exc}",
        )

    profile_name = str(result.get("name", "")).strip()
    profile_id = str(result.get("filament_id") or "").strip()
    success_message = f"Imported profile {profile_name or profile_id or 'successfully'}."
    headers = {"HX-Trigger": json.dumps({"profiles-imported": True})}
    return _render_import_profile_modal(
        request,
        machine_id=machine_id,
        kind="filament",
        success_message=success_message,
        import_result=result,
        headers=headers,
    )
```

(Only change versus today: the handler signature gains `kind`, every `_render_import_profile_modal` call passes `kind="filament"` to preserve the toggle through re-renders, and the `normalized_kind == "process"` fast-path delegates to a new helper.)

- [ ] **Step 5.6: Add the process-flow helper**

In `app/routers/web.py`, immediately before `import_profile_upload`, add:

```python
async def _import_process_profile_flow(
    request: Request,
    *,
    profile_file: UploadFile | None,
    machine_id: str,
) -> HTMLResponse:
    filename = profile_file.filename if profile_file else ""
    if not filename:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Please choose a JSON file.",
        )
    if not filename.lower().endswith(".json"):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Only .json profile files are supported.",
        )

    try:
        raw = await profile_file.read()
    finally:
        await profile_file.close()

    if not raw:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Uploaded file is empty.",
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Profile file must be UTF-8 encoded JSON.",
        )
    except json.JSONDecodeError:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Invalid JSON file.",
        )

    if not isinstance(payload, dict):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Profile JSON must be an object.",
        )

    try:
        resolved_preview = await request.app.state.orcaslicer.resolve_import_process_profile(payload)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip() or str(exc)
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Profile resolution failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Profile resolution request failed: {exc}",
        )

    resolved_payload = resolved_preview.get("resolved_payload")
    if not isinstance(resolved_payload, dict):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Resolved process payload is invalid.",
        )

    try:
        result = await request.app.state.orcaslicer.import_process_profile(dict(resolved_payload))
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip() or str(exc)
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Import request failed: {exc}",
        )

    profile_name = str(result.get("name", "")).strip()
    setting_id = str(result.get("setting_id") or "").strip()
    success_message = f"Imported process profile {profile_name or setting_id or 'successfully'}."
    return _render_import_profile_modal(
        request,
        machine_id=machine_id,
        kind="process",
        success_message=success_message,
        import_result=result,
    )
```

- [ ] **Step 5.7: Run the new tests**

```bash
.venv/bin/python -m unittest tests.test_import_process_profile -v
```

Expected: 2/2 pass.

- [ ] **Step 5.8: Run the full suite**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: 29 tests pass.

- [ ] **Step 5.9: Commit**

```bash
git add app/routers/web.py tests/test_import_process_profile.py
git commit -m "Dispatch import handler on profile kind (filament or process)"
```

---

## Task 6: Add kind toggle + process rendering to the import modal template

**Files:**
- Modify: `app/templates/partials/import_profile.html`

- [ ] **Step 6.1: Replace the template with kind-aware version**

Replace the full contents of `app/templates/partials/import_profile.html` with:

```html
<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-4"
  onclick="closeModal()"
>
  <div
    class="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl"
    onclick="event.stopPropagation()"
  >
    {% if success_message %}
      <h2 class="text-lg font-semibold">
        {% if kind == "process" %}Import Process Profile{% else %}Import Filament Profile{% endif %}
      </h2>
      <div class="mt-3 rounded-md border border-emerald-700/60 bg-emerald-900/30 p-3 text-sm text-emerald-100">
        {{ success_message }}
      </div>
      {% if import_result %}
        <dl class="mt-3 space-y-1 text-sm text-slate-200">
          <div class="flex gap-2">
            <dt class="w-28 text-slate-400">Name</dt>
            <dd>{{ import_result.get("name", "-") }}</dd>
          </div>
          {% if kind == "process" %}
            <div class="flex gap-2">
              <dt class="w-28 text-slate-400">Setting ID</dt>
              <dd class="font-mono">{{ import_result.get("setting_id", "-") }}</dd>
            </div>
            {% if import_result.get("layer_height") %}
              <div class="flex gap-2">
                <dt class="w-28 text-slate-400">Layer height</dt>
                <dd>{{ import_result.get("layer_height") }}</dd>
              </div>
            {% endif %}
            {% if import_result.get("vendor") %}
              <div class="flex gap-2">
                <dt class="w-28 text-slate-400">Vendor</dt>
                <dd>{{ import_result.get("vendor") }}</dd>
              </div>
            {% endif %}
          {% else %}
            <div class="flex gap-2">
              <dt class="w-28 text-slate-400">Type</dt>
              <dd>{{ import_result.get("filament_type", "-") }}</dd>
            </div>
            <div class="flex gap-2">
              <dt class="w-28 text-slate-400">Filament ID</dt>
              <dd class="font-mono">{{ import_result.get("filament_id", "-") }}</dd>
            </div>
          {% endif %}
        </dl>
      {% endif %}
      <div class="mt-5 flex justify-end">
        <button
          type="button"
          class="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
          onclick="closeModal()"
        >
          Close
        </button>
      </div>
    {% elif pending_import_payload %}
      <div class="flex items-start justify-between gap-3">
        <div>
          <h2 class="text-lg font-semibold">Choose Filament Type</h2>
          <p class="mt-1 text-xs text-slate-400">The resolved profile needs a valid AMS filament type before it can be saved.</p>
        </div>
        <button
          type="button"
          class="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          onclick="closeModal()"
        >
          Close
        </button>
      </div>

      {% if error_message %}
        <div class="mt-3 rounded-md border border-rose-700/60 bg-rose-900/30 p-2 text-sm text-rose-100">
          {{ error_message }}
        </div>
      {% endif %}

      <div class="mt-3 rounded-md border border-slate-700 bg-slate-950/40 p-3 text-xs text-slate-300">
        <div><span class="text-slate-400">Resolved profile:</span> {{ pending_profile_name or "-" }}</div>
        <div class="mt-1"><span class="text-slate-400">Filament ID:</span> <span class="font-mono">{{ pending_filament_id or "-" }}</span></div>
        <div class="mt-1"><span class="text-slate-400">Current filament type:</span> {{ pending_filament_type or "-" }}</div>
      </div>

      <form
        class="mt-4 space-y-4"
        hx-post="/web/import-profile"
        hx-target="#modal"
        hx-swap="innerHTML"
      >
        <input type="hidden" name="machine" value="{{ machine_id }}" />
        <input type="hidden" name="kind" value="filament" />
        <input type="hidden" name="payload_json" value="{{ pending_import_payload }}" />
        <div>
          <label for="resolved-filament-type" class="mb-1 block text-xs text-slate-300">Filament type</label>
          <select
            id="resolved-filament-type"
            name="filament_type"
            required
            class="w-full rounded-md border-slate-700 bg-slate-950 text-sm text-ink"
          >
            <option value="">Choose filament type</option>
            {% for option in valid_filament_types %}
              <option value="{{ option }}" {% if option == pending_filament_type|upper %}selected{% endif %}>{{ option }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="flex justify-end gap-2">
          <button
            type="button"
            class="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
            onclick="closeModal()"
          >
            Cancel
          </button>
          <button
            type="submit"
            class="rounded-md border border-cyan-700 px-3 py-1.5 text-sm text-cyan-200 hover:bg-cyan-900/30"
          >
            Import
          </button>
        </div>
      </form>
    {% else %}
      <div class="flex items-start justify-between gap-3">
        <div>
          <h2 class="text-lg font-semibold">Import Profile</h2>
          <p class="mt-1 text-xs text-slate-400">Upload an OrcaSlicer profile JSON file.</p>
        </div>
        <button
          type="button"
          class="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          onclick="closeModal()"
        >
          Close
        </button>
      </div>

      {% if error_message %}
        <div class="mt-3 rounded-md border border-rose-700/60 bg-rose-900/30 p-2 text-sm text-rose-100">
          {{ error_message }}
        </div>
      {% endif %}

      <form
        class="mt-4 space-y-4"
        hx-post="/web/import-profile"
        hx-target="#modal"
        hx-swap="innerHTML"
        hx-encoding="multipart/form-data"
      >
        <input type="hidden" name="machine" value="{{ machine_id }}" />
        <fieldset>
          <legend class="mb-1 block text-xs text-slate-300">Profile kind</legend>
          <div class="flex gap-4 text-sm text-slate-200">
            <label class="flex items-center gap-2">
              <input
                type="radio"
                name="kind"
                value="filament"
                {% if kind != "process" %}checked{% endif %}
                class="accent-cyan-500"
              />
              Filament
            </label>
            <label class="flex items-center gap-2">
              <input
                type="radio"
                name="kind"
                value="process"
                {% if kind == "process" %}checked{% endif %}
                class="accent-cyan-500"
              />
              Process
            </label>
          </div>
        </fieldset>
        <div>
          <label for="profile-file" class="mb-1 block text-xs text-slate-300">JSON profile file</label>
          <input
            id="profile-file"
            name="profile_file"
            type="file"
            accept=".json,application/json"
            required
            class="w-full rounded-md border-slate-700 bg-slate-950 text-sm text-ink file:mr-3 file:rounded file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-slate-200"
          />
        </div>
        <div class="flex justify-end gap-2">
          <button
            type="button"
            class="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
            onclick="closeModal()"
          >
            Cancel
          </button>
          <button
            type="submit"
            class="rounded-md border border-cyan-700 px-3 py-1.5 text-sm text-cyan-200 hover:bg-cyan-900/30"
          >
            Import
          </button>
        </div>
      </form>
    {% endif %}
  </div>
</div>
```

Key changes:
1. Success heading changes based on `kind`.
2. Success detail list branches on `kind` — process shows `setting_id`, `layer_height`, `vendor`; filament shows `filament_type`, `filament_id`.
3. Pending state is filament-only (processes never reach pending). The hidden `kind` input is pinned to `filament` there.
4. Initial state heading is generic ("Import Profile") and the form gains a radio fieldset for kind.

- [ ] **Step 6.2: Run the full suite**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: 29 tests pass.

- [ ] **Step 6.3: Commit**

```bash
git add app/templates/partials/import_profile.html
git commit -m "Add kind toggle and process rendering to import modal"
```

---

## Task 7: Manual browser smoke check

This task has no code changes. It's here because the test suite can't verify CSS/HTMX wiring.

- [ ] **Step 7.1: Start the dev server**

```bash
bash scripts/run-local.sh
```

It will create/refresh `.venv`, install deps, source `.env`, and start uvicorn on port 9817. If `.env` is missing and no printer/Spoolman/orcaslicer services are reachable, the page will still render but the validation/import actions will error — that's fine for visual checks.

- [ ] **Step 7.2: Walk the three flows**

Open `http://localhost:9817/web/settings`:

1. **Spoolman section** — confirm on initial load you see only the Validate button (no field definitions, no Create button). Click Validate: if fields are valid, you get a green banner and nothing else; if invalid, you additionally see the definition list and the Create Missing Fields button.
2. **Profile validation** — click Validate Profiles. Confirm the 4-card row (Linked / Unlinked / Matched / Missing). If any filaments lack `ams_filament_id` + `ams_filament_type`, they appear in the new "Unlinked filaments" list and clicking them opens the filament detail modal.
3. **Import modal** — click Import Profile. Confirm the radio toggle is present with Filament selected by default. Try a filament JSON with kind=filament (unchanged behavior). Try a process JSON with kind=process: success modal shows Setting ID, Layer height, Vendor.

- [ ] **Step 7.3: Stop the server**

Ctrl-C the uvicorn process.

- [ ] **Step 7.4: Final full test run**

```bash
.venv/bin/python -m unittest discover tests
```

Expected: 29 tests pass.

No commit — this task is verification only.

---

## Done criteria

- 29 tests pass (24 baseline + 1 validation + 3 client + 2 handler-integration).
- `git log feature/webui-tweaks ^main` shows 6 commits on top of the spec commit.
- Browser smoke check confirms all three UX changes visible.
- No changes to `app/main.py`, `app/routers/api.py`, `app/services/spoolman.py`, or `app/services/mqtt_printer.py`.
