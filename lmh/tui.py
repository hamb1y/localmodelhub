"""Interactive Textual frontend for the same local HTTP API as the CLI."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog, Select, Static, TabbedContent, TabPane

from .cli import Backend, BackendError, Profile, boot_line, hardware_lines, profiles, stats_lines


class SwitchScreen(ModalScreen[Profile | None]):
    """Select a source and explicitly confirm its replacement."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, target: Profile, sources: list[Profile]) -> None:
        super().__init__()
        self.target = target
        self.sources = {source.key: source for source in sources}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(Text(f"Switch to {self.target.label}"))
            yield Static("Choose the running profile to replace.")
            yield Select(
                [(Text(source.label), key) for key, source in self.sources.items()],
                prompt="Select source profile",
                id="switch-source",
            )
            yield Static("No changes are made until you confirm.", id="switch-summary")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel-switch")
                yield Button("Confirm switch", id="confirm-switch", variant="warning", disabled=True)

    def on_select_changed(self, event: Select.Changed) -> None:
        source = self.sources.get(event.value)
        self.query_one("#confirm-switch", Button).disabled = source is None
        summary = (
            f"Replace {source.label} with {self.target.label}? This may interrupt requests."
            if source else "No changes are made until you confirm."
        )
        self.query_one("#switch-summary", Static).update(Text(summary))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-switch":
            source = self.sources.get(self.query_one("#switch-source", Select).value)
            if source is not None:
                self.dismiss(source)
        elif event.button.id == "cancel-switch":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class LogsScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, profile: Profile, lines: list[Any]) -> None:
        super().__init__()
        self.profile = profile
        self.lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="logs-dialog"):
            yield Label(Text(f"Recent logs — {self.profile.label}"))
            yield RichLog(wrap=True)
            yield Button("Close", id="close-logs")

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        for line in self.lines or ["No log lines returned."]:
            log.write(Text(str(line)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-logs":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


class LocalModelHubApp(App[None]):
    TITLE = "LocalModelHub"
    BINDINGS = [("r", "refresh_status", "Refresh"), ("q", "quit", "Quit")]
    CSS = """
    #device { height: auto; max-height: 7; padding: 0 1; overflow-y: auto; }
    #backend-message { height: auto; max-height: 3; padding: 0 1; color: $warning; }
    TabbedContent, TabPane, DataTable { height: 1fr; }
    #profile-detail { height: auto; max-height: 3; padding: 0 1; }
    .buttons { height: auto; padding: 0 1; }
    .buttons Button { margin-right: 1; min-width: 8; }
    #activity { height: auto; max-height: 3; padding: 0 1; }
    #stats-body { padding: 0 1; height: auto; }
    ModalScreen { align: center middle; }
    .dialog { background: $surface; border: thick $accent; width: 64; max-width: 95%;
              height: auto; padding: 1 2; }
    .dialog Label, .dialog Static { height: auto; margin-bottom: 1; }
    .dialog Select { margin-bottom: 1; }
    #logs-dialog { width: 90%; height: 80%; }
    #logs-dialog RichLog { height: 1fr; margin-bottom: 1; }
    """

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend
        self._profile_by_key: dict[str, Profile] = {}
        self._job_rows: list[tuple[Text, ...]] = []
        self._fetching = self._fetching_stats = self._busy = self._online = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(f"Connecting to {self.backend.endpoint}…"), id="device")
        yield Static(id="backend-message")
        with TabbedContent(id="panels"):
            with TabPane("Profiles", id="profiles-tab"):
                yield DataTable(id="profiles", cursor_type="row")
                yield Static("No model profile selected.", id="profile-detail")
                with Horizontal(classes="buttons"):
                    yield Button("Start", id="start", disabled=True)
                    yield Button("Stop", id="stop", disabled=True)
                    yield Button("Switch…", id="switch", disabled=True)
                    yield Button("Logs", id="logs", disabled=True)
            with TabPane("Jobs", id="jobs-tab"):
                yield DataTable(id="jobs", cursor_type="row")
            with TabPane("Stats", id="stats-tab"):
                with VerticalScroll():
                    yield Static("Loading statistics…", id="stats-body")
        yield Static("Actions are sent to the local backend.", id="activity")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#profiles", DataTable).add_columns("Model", "Profile", "Device", "State", "Port", "Control")
        self.query_one("#jobs", DataTable).add_columns(
            "ID", "Action", "Model / Profile", "State", "Created", "Source", "Message",
        )
        self.action_refresh_status()
        self.set_interval(3, self.action_refresh_status)

    def action_refresh_status(self) -> None:
        if not self._fetching:
            self._fetching = True
            self._fetch_status()
        if self.query_one("#panels", TabbedContent).active == "stats-tab":
            self._request_stats()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "stats-tab":
            self._request_stats()

    def _request_stats(self) -> None:
        if not self._fetching_stats:
            self._fetching_stats = True
            self._fetch_stats()

    @work(group="stats")
    async def _fetch_stats(self) -> None:
        try:
            text = "\n".join(stats_lines(await asyncio.to_thread(self.backend.stats)))
        except BackendError as exc:
            text = f"Statistics unavailable: {exc}"
        finally:
            self._fetching_stats = False
        self.query_one("#stats-body", Static).update(Text(text))

    @work(group="status")
    async def _fetch_status(self) -> None:
        try:
            status = await asyncio.to_thread(self.backend.status)
        except BackendError as exc:
            self._online = False
            self.query_one("#backend-message", Static).update(Text(f"Backend unavailable: {exc}"))
            self._update_controls()
        else:
            self._online = True
            self.query_one("#backend-message", Static).update("")
            self._apply_status(status)
        finally:
            self._fetching = False

    def _apply_status(self, status: dict[str, Any]) -> None:
        device = [boot_line(status), *hardware_lines(status.get("hardware") or {})]
        self.query_one("#device", Static).update(Text("\n".join(device)))
        table = self.query_one("#profiles", DataTable)
        entries = {entry.key: entry for entry in profiles(status)}
        if entries != self._profile_by_key:
            current = self._selected_profile()
            self._profile_by_key = entries
            table.clear()
            for entry in entries.values():
                table.add_row(
                    Text(f"{entry.name} ({entry.model})"), Text(entry.id), Text(entry.device),
                    Text(entry.state), Text(str(entry.port) if entry.port is not None else "—"),
                    Text("Available" if entry.controllable else "Blocked"),
                    key=entry.key,
                )
            if current is not None and current.key in entries:
                table.move_cursor(row=table.get_row_index(current.key))
        job_rows = [
            tuple(Text(str(item)) for item in (
                job.get("id", "—"), job.get("action", "—"),
                f"{job.get('model', '—')}/{job.get('profile', '—')}", job.get("state", "—"),
                job.get("created_at", "—"), job.get("source", "—"), job.get("message") or "",
            ))
            for job in status.get("jobs", [])
        ]
        if job_rows != self._job_rows:
            self._job_rows = job_rows
            jobs = self.query_one("#jobs", DataTable)
            jobs.clear()
            jobs.add_rows(job_rows)
        self._update_controls()

    def _selected_profile(self) -> Profile | None:
        table = self.query_one("#profiles", DataTable)
        if table.row_count and table.is_valid_row_index(table.cursor_row):
            cell = table.coordinate_to_cell_key(table.cursor_coordinate)
            return self._profile_by_key.get(cell.row_key.value)
        return None

    def _switch_sources(self, target: Profile) -> list[Profile]:
        return [
            entry for entry in self._profile_by_key.values()
            if entry.key != target.key and entry.controllable
            and entry.state in {"running", "ready", "unhealthy"}
        ]

    def _update_controls(self) -> None:
        profile = self._selected_profile()
        detail = "No model profile selected."
        if profile is not None:
            detail = f"{profile.label} — {profile.device}, {profile.state}"
            if not profile.controllable:
                detail += " — lifecycle controls blocked by local policy"
            if profile.message:
                detail += f" — {profile.message}"
        self.query_one("#profile-detail", Static).update(Text(detail))
        for button_id in ("start", "stop", "switch", "logs"):
            disabled = not self._online or self._busy or profile is None
            if button_id != "logs" and profile is not None and not profile.controllable:
                disabled = True
            if button_id == "switch" and profile is not None:
                disabled = disabled or not self._switch_sources(profile)
            self.query_one(f"#{button_id}", Button).disabled = disabled

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "profiles":
            self._update_controls()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        profile = self._selected_profile()
        action = event.button.id
        if not self._online or self._busy or profile is None:
            return
        if action != "logs" and not profile.controllable:
            return
        if action == "switch":
            sources = self._switch_sources(profile)
            if sources:
                def confirmed(source: Profile | None) -> None:
                    if source is not None:
                        self._begin_action("switch", profile, source)

                self.push_screen(SwitchScreen(profile, sources), confirmed)
        elif action in {"start", "stop", "logs"}:
            self._begin_action(action, profile)

    def _begin_action(self, action: str, target: Profile, source: Profile | None = None) -> None:
        if action != "logs" and any(
            entry is None or not entry.controllable
            for entry in (self._profile_by_key.get(profile.key) for profile in (target, source) if profile)
        ):
            self.query_one("#activity", Static).update(Text("Action blocked: profile control is no longer available."))
            return
        self._busy = True
        self._update_controls()
        self.query_one("#activity", Static).update(Text(f"Requesting {action} for {target.label}…"))
        self._perform(action, target, source)

    @work(group="control")
    async def _perform(self, action: str, target: Profile, source: Profile | None) -> None:
        detail = f"{action.capitalize()} requested for {target.label}."
        try:
            if action == "logs":
                lines = await asyncio.to_thread(self.backend.logs, target.model, target.id)
                self.push_screen(LogsScreen(target, lines))
                detail = f"Fetched logs for {target.label}."
            else:
                response = await asyncio.to_thread(
                    self.backend.action, action, target.model, target.id, source=source,
                )
                job = response.get("job", response)
                if isinstance(job, dict) and job.get("id"):
                    detail += f" Job {job['id']} — see Jobs."
                self.action_refresh_status()
        except BackendError as exc:
            detail = f"{action.capitalize()} failed: {exc}"
        finally:
            self._busy = False
        self.query_one("#activity", Static).update(Text(detail))
        self._update_controls()
