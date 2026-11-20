//! Interactive daemon controls and first-run progress.

use std::{
    io::{self, BufRead, BufReader, Stdout, Write},
    path::Path,
    process::{Command, ExitStatus, Stdio},
    sync::mpsc::{self, Sender},
    thread,
    time::{Duration, Instant},
};

use anyhow::{anyhow, Context, Result};
use crossterm::{
    cursor::{Hide, Show},
    event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers},
    execute,
    terminal::{self, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Clear, List, ListState, Paragraph, Row, Table, TableState, Wrap},
    Frame, Terminal,
};
use serde::Deserialize;
use serde_json::Value;

use crate::api::{Client, Profile, Status};

const GREEN: Color = Color::Rgb(44, 105, 67);
const MUTED: Color = Color::Rgb(183, 183, 183);
const EDGE: Color = Color::Rgb(38, 67, 49);
const SELECTED: Color = Color::Rgb(34, 90, 56);
const REFRESH: Duration = Duration::from_secs(3);
const INPUT_TICK: Duration = Duration::from_millis(100);

struct RestoreTerminal;

impl Drop for RestoreTerminal {
    fn drop(&mut self) {
        let _ = execute!(io::stdout(), LeaveAlternateScreen, Show);
        let _ = terminal::disable_raw_mode();
    }
}

type Screen = Terminal<CrosstermBackend<Stdout>>;

fn screen() -> Result<(RestoreTerminal, Screen)> {
    terminal::enable_raw_mode().context("enable terminal input")?;
    let restore = RestoreTerminal;
    execute!(io::stdout(), EnterAlternateScreen, Hide).context("enter alternate screen")?;
    let terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    Ok((restore, terminal))
}

fn read_key() -> Result<Option<KeyEvent>> {
    if event::poll(INPUT_TICK)? {
        if let Event::Key(key) = event::read()? {
            if key.kind == KeyEventKind::Press {
                return Ok(Some(key));
            }
        }
    }
    Ok(None)
}

fn ctrl_c(key: KeyEvent) -> bool {
    key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL)
}

fn accent() -> Style {
    Style::default()
        .fg(Color::White)
        .add_modifier(Modifier::BOLD)
}

fn muted() -> Style {
    Style::default().fg(MUTED)
}

fn selected() -> Style {
    Style::default().bg(SELECTED).fg(Color::White)
}

fn panel(title: impl Into<Line<'static>>) -> Block<'static> {
    Block::bordered()
        .border_type(BorderType::Rounded)
        .style(Style::default().bg(Color::Black).fg(Color::White))
        .border_style(Style::default().fg(EDGE))
        .title(title.into().style(accent()))
}

fn clean(text: &str) -> String {
    text.chars()
        .filter(|ch| !ch.is_control() || *ch == '\n')
        .collect()
}

fn state_style(state: &str) -> Style {
    match state {
        "running" | "ready" | "complete" | "completed" | "succeeded" | "ok" => accent(),
        "failed" | "error" | "unhealthy" => Style::default().fg(Color::LightRed),
        "pending" | "queued" | "starting" | "stopping" | "reconciling" => {
            Style::default().fg(Color::Yellow)
        }
        _ => muted(),
    }
}

#[derive(Clone, PartialEq, Eq, Debug)]
struct ProfileKey {
    model: String,
    profile: String,
}

impl ProfileKey {
    fn label(&self) -> String {
        clean(&format!("{}/{}", self.model, self.profile))
    }
}

#[derive(Clone, Copy)]
enum Tab {
    Models,
    Jobs,
    Stats,
}

impl Tab {
    fn cycle(self, reverse: bool) -> Self {
        match (self, reverse) {
            (Self::Models, false) | (Self::Stats, true) => Self::Jobs,
            (Self::Jobs, false) | (Self::Models, true) => Self::Stats,
            _ => Self::Models,
        }
    }
}

enum Overlay {
    Help,
    PickSource {
        target: ProfileKey,
        sources: Vec<ProfileKey>,
        cursor: ListState,
    },
    ConfirmSwitch {
        target: ProfileKey,
        source: ProfileKey,
    },
    Logs {
        target: ProfileKey,
        lines: Option<Result<Vec<String>>>,
        cursor: ListState,
    },
}

enum Reply {
    Status(Result<Box<Status>>),
    Stats(Option<String>, Result<Value>),
    Action {
        action: &'static str,
        target: ProfileKey,
        result: Result<Value>,
    },
    Logs {
        target: ProfileKey,
        result: Result<Vec<String>>,
    },
}

struct App {
    tab: Tab,
    status: Option<Box<Status>>,
    status_problem: Option<String>,
    profile_keys: Vec<ProfileKey>,
    profile_cursor: TableState,
    job_cursor: TableState,
    stats: Option<Result<Value>>,
    stats_model: Option<String>,
    stats_scroll: u16,
    polling_status: bool,
    polling_stats: bool,
    acting: bool,
    reading_logs: bool,
    overlay: Option<Overlay>,
    notice: String,
}

impl App {
    fn new() -> Self {
        Self {
            tab: Tab::Models,
            status: None,
            status_problem: None,
            profile_keys: Vec::new(),
            profile_cursor: TableState::default(),
            job_cursor: TableState::default(),
            stats: None,
            stats_model: None,
            stats_scroll: 0,
            polling_status: false,
            polling_stats: false,
            acting: false,
            reading_logs: false,
            overlay: None,
            notice: "Start adds a profile without stopping others.".into(),
        }
    }

    fn refresh(&mut self, client: &Client, tx: &Sender<Reply>) {
        if !self.polling_status {
            self.polling_status = self.launch(client, tx, |client| {
                Reply::Status(client.status().map(Box::new))
            });
        }
        self.refresh_stats(client, tx);
    }

    fn refresh_stats(&mut self, client: &Client, tx: &Sender<Reply>) {
        if matches!(self.tab, Tab::Stats) && !self.polling_stats {
            let model = self.stats_model.clone();
            self.polling_stats = self.launch(client, tx, move |client| {
                let stats = client.stats(model.as_deref());
                Reply::Stats(model, stats)
            });
        }
    }

    fn launch(
        &mut self,
        client: &Client,
        tx: &Sender<Reply>,
        request: impl FnOnce(Client) -> Reply + Send + 'static,
    ) -> bool {
        let client = client.clone();
        let tx = tx.clone();
        match thread::Builder::new().spawn(move || {
            let _ = tx.send(request(client));
        }) {
            Ok(_) => true,
            Err(error) => {
                self.notice = format!("Could not launch request: {error}");
                false
            }
        }
    }

    fn apply(&mut self, reply: Reply, client: &Client, tx: &Sender<Reply>) {
        match reply {
            Reply::Status(result) => {
                self.polling_status = false;
                match result {
                    Ok(status) => {
                        let current = self.current().cloned();
                        let current_job = self
                            .job_cursor
                            .selected()
                            .and_then(|index| self.status.as_ref()?.jobs.get(index))
                            .map(|job| job.id.as_str());
                        self.job_cursor.select(
                            current_job
                                .and_then(|id| status.jobs.iter().position(|job| job.id == id))
                                .or_else(|| (!status.jobs.is_empty()).then_some(0)),
                        );
                        self.profile_keys = status
                            .models
                            .iter()
                            .flat_map(|model| {
                                model.profiles.iter().map(|profile| ProfileKey {
                                    model: model.id.clone(),
                                    profile: profile.id.clone(),
                                })
                            })
                            .collect();
                        self.profile_cursor.select(
                            current
                                .and_then(|key| {
                                    self.profile_keys.iter().position(|row| *row == key)
                                })
                                .or_else(|| (!self.profile_keys.is_empty()).then_some(0)),
                        );
                        self.status = Some(status);
                        self.status_problem = None;
                    }
                    Err(error) => self.status_problem = Some(format!("{error:#}")),
                }
            }
            Reply::Stats(model, result) => {
                self.polling_stats = false;
                if self.stats_model == model {
                    self.stats = Some(result);
                } else {
                    self.refresh_stats(client, tx);
                }
            }
            Reply::Action {
                action,
                target,
                result,
            } => {
                self.acting = false;
                self.notice = match result {
                    Ok(response) => {
                        let job = response.get("job").unwrap_or(&response);
                        let suffix = job["id"]
                            .as_str()
                            .map(|id| format!(" • job {id}"))
                            .unwrap_or_default();
                        format!("{action} accepted for {}{suffix}", target.label())
                    }
                    Err(error) => format!("{action} failed: {error:#}"),
                };
                self.refresh(client, tx);
            }
            Reply::Logs { target, result } => {
                self.reading_logs = false;
                if let Some(Overlay::Logs {
                    target: open,
                    lines,
                    cursor,
                }) = &mut self.overlay
                {
                    if *open == target {
                        cursor.select(
                            result
                                .as_ref()
                                .ok()
                                .and_then(|logs| logs.len().checked_sub(1)),
                        );
                        *lines = Some(result);
                    }
                }
            }
        }
    }

    fn current(&self) -> Option<&ProfileKey> {
        self.profile_keys.get(self.profile_cursor.selected()?)
    }

    fn profile(&self, key: &ProfileKey) -> Option<&Profile> {
        self.status
            .as_ref()?
            .models
            .iter()
            .find(|model| model.id == key.model)?
            .profiles
            .iter()
            .find(|profile| profile.id == key.profile)
    }

    fn can_control(&self, key: &ProfileKey) -> bool {
        self.profile(key)
            .is_some_and(|profile| profile.controllable == Some(true))
    }

    fn can_source(&self, key: &ProfileKey) -> bool {
        self.profile(key).is_some_and(|profile| {
            profile.controllable == Some(true)
                && matches!(profile.state.as_str(), "running" | "ready" | "unhealthy")
        })
    }

    fn ready_for_request(&mut self) -> bool {
        if self.status_problem.is_some() || self.status.is_none() {
            self.notice = "Waiting for live daemon status; press r to retry.".into();
            false
        } else if self.acting {
            self.notice = "A lifecycle request is in progress.".into();
            false
        } else {
            true
        }
    }

    fn action(
        &mut self,
        client: &Client,
        tx: &Sender<Reply>,
        action: &'static str,
        target: ProfileKey,
        source: Option<ProfileKey>,
    ) {
        if !self.ready_for_request() {
            return;
        }
        if !self.can_control(&target) {
            self.notice = "The daemon does not allow lifecycle control for this profile.".into();
            return;
        }
        if action == "switch"
            && !source
                .as_ref()
                .is_some_and(|key| *key != target && self.can_source(key))
        {
            self.notice = "Switch cancelled: the selected source is no longer eligible.".into();
            return;
        }
        self.notice = format!("Requesting {action} for {}…", target.label());
        self.acting = self.launch(client, tx, move |client| {
            let source = source
                .as_ref()
                .map(|key| (key.model.as_str(), key.profile.as_str()));
            let result = client.action(action, &target.model, &target.profile, source);
            Reply::Action {
                action,
                target,
                result,
            }
        });
    }

    fn pick_source(&mut self, target: ProfileKey) {
        if !self.ready_for_request() {
            return;
        }
        if !self.can_control(&target) {
            self.notice = "The daemon does not allow lifecycle control for this profile.".into();
            return;
        }
        let sources: Vec<_> = self
            .profile_keys
            .iter()
            .filter(|key| **key != target && self.can_source(key))
            .cloned()
            .collect();
        if sources.is_empty() {
            self.notice =
                "No other running, controllable profile is available to switch from.".into();
        } else {
            self.overlay = Some(Overlay::PickSource {
                target,
                sources,
                cursor: ListState::default(),
            });
        }
    }

    fn open_logs(&mut self, client: &Client, tx: &Sender<Reply>, target: ProfileKey) {
        if self.reading_logs {
            self.notice = "A log request is already in progress.".into();
            return;
        }
        let request_target = target.clone();
        self.reading_logs = self.launch(client, tx, move |client| Reply::Logs {
            result: client.logs(&request_target.model, &request_target.profile),
            target: request_target,
        });
        if self.reading_logs {
            self.overlay = Some(Overlay::Logs {
                target,
                lines: None,
                cursor: ListState::default(),
            });
        }
    }

    fn key(&mut self, key: KeyEvent, client: &Client, tx: &Sender<Reply>) -> bool {
        if ctrl_c(key) {
            return false;
        }
        if let Some(mut overlay) = self.overlay.take() {
            if matches!(key.code, KeyCode::Esc | KeyCode::Char('q')) {
                return true;
            }
            match &mut overlay {
                Overlay::Help => {}
                Overlay::PickSource {
                    target,
                    sources,
                    cursor,
                } => {
                    if key.code == KeyCode::Enter {
                        if let Some(source) = cursor.selected().and_then(|i| sources.get(i)) {
                            self.overlay = Some(Overlay::ConfirmSwitch {
                                target: target.clone(),
                                source: source.clone(),
                            });
                            return true;
                        }
                    }
                    if let Some(index) = navigate(key.code, cursor.selected(), sources.len()) {
                        cursor.select(Some(index));
                    }
                }
                Overlay::ConfirmSwitch { target, source } => match key.code {
                    KeyCode::Char('y') | KeyCode::Char('Y') => {
                        self.action(client, tx, "switch", target.clone(), Some(source.clone()));
                        return true;
                    }
                    KeyCode::Char('n') | KeyCode::Char('N') => return true,
                    _ => {}
                },
                Overlay::Logs { lines, cursor, .. } => {
                    if let Some(Ok(lines)) = lines {
                        if let Some(index) = navigate(key.code, cursor.selected(), lines.len()) {
                            cursor.select(Some(index));
                        }
                    }
                }
            }
            self.overlay = Some(overlay);
            return true;
        }
        match key.code {
            KeyCode::Char('q') => return false,
            KeyCode::Char('?') | KeyCode::F(1) => self.overlay = Some(Overlay::Help),
            KeyCode::Char('r') => self.refresh(client, tx),
            KeyCode::Char('1') => self.tab = Tab::Models,
            KeyCode::Char('2') => self.tab = Tab::Jobs,
            KeyCode::Char('3') => {
                self.tab = Tab::Stats;
                self.refresh_stats(client, tx);
            }
            KeyCode::Tab | KeyCode::BackTab => {
                self.tab = self.tab.cycle(key.code == KeyCode::BackTab);
                self.refresh_stats(client, tx);
            }
            code => match self.tab {
                Tab::Models => {
                    if let Some(index) = navigate(
                        code,
                        self.profile_cursor.selected(),
                        self.profile_keys.len(),
                    ) {
                        self.profile_cursor.select(Some(index));
                    } else if let Some(target) = self.current().cloned() {
                        match code {
                            KeyCode::Char('s') => self.action(client, tx, "start", target, None),
                            KeyCode::Char('x') => self.action(client, tx, "stop", target, None),
                            KeyCode::Char('w') => self.pick_source(target),
                            KeyCode::Char('l') => self.open_logs(client, tx, target),
                            _ => {}
                        }
                    }
                }
                Tab::Jobs => {
                    let len = self.status.as_ref().map_or(0, |status| status.jobs.len());
                    if let Some(index) = navigate(code, self.job_cursor.selected(), len) {
                        self.job_cursor.select(Some(index));
                    }
                }
                Tab::Stats => {
                    if matches!(code, KeyCode::Left | KeyCode::Right) {
                        if let Some(status) = &self.status {
                            let index = self.stats_model.as_ref().and_then(|id| {
                                status.models.iter().position(|model| &model.id == id)
                            });
                            let count = status.models.len() + 1;
                            let current = index.map_or(0, |i| i + 1);
                            let next = if code == KeyCode::Right {
                                (current + 1) % count
                            } else {
                                (current + count - 1) % count
                            };
                            self.stats_model =
                                next.checked_sub(1).map(|i| status.models[i].id.clone());
                            self.stats = None;
                            self.stats_scroll = 0;
                            self.refresh_stats(client, tx);
                        }
                    }
                    self.stats_scroll = match code {
                        KeyCode::Up | KeyCode::Char('k') => self.stats_scroll.saturating_sub(1),
                        KeyCode::Down | KeyCode::Char('j') => self.stats_scroll.saturating_add(1),
                        KeyCode::PageUp => self.stats_scroll.saturating_sub(8),
                        KeyCode::PageDown => self.stats_scroll.saturating_add(8),
                        KeyCode::Home => 0,
                        _ => self.stats_scroll,
                    };
                }
            },
        }
        true
    }
}

fn navigate(code: KeyCode, current: Option<usize>, len: usize) -> Option<usize> {
    let last = len.checked_sub(1)?;
    match code {
        KeyCode::Down | KeyCode::Char('j') => Some(current.map_or(0, |i| (i + 1).min(last))),
        KeyCode::Up | KeyCode::Char('k') => Some(current.map_or(last, |i| i.saturating_sub(1))),
        KeyCode::PageDown => Some(current.map_or(0, |i| (i + 8).min(last))),
        KeyCode::PageUp => Some(current.map_or(last, |i| i.saturating_sub(8))),
        KeyCode::Home => Some(0),
        KeyCode::End => Some(last),
        _ => None,
    }
}

/// Open the interactive manager. Daemon requests never run on the rendering thread.
pub fn run(client: Client) -> Result<()> {
    let (_restore, mut screen) = screen()?;
    let (tx, rx) = mpsc::channel();
    let mut app = App::new();
    let mut next_poll = Instant::now();
    loop {
        while let Ok(reply) = rx.try_recv() {
            app.apply(reply, &client, &tx);
        }
        if Instant::now() >= next_poll {
            app.refresh(&client, &tx);
            next_poll = Instant::now() + REFRESH;
        }
        screen.draw(|frame| draw_manager(frame, &mut app))?;
        if let Some(key) = read_key()? {
            if !app.key(key, &client, &tx) {
                return Ok(());
            }
        }
    }
}

fn draw_manager(frame: &mut Frame<'_>, app: &mut App) {
    frame.render_widget(
        Block::default().style(Style::default().bg(Color::Black).fg(Color::White)),
        frame.area(),
    );
    let [header, nav, body, detail, footer] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(3),
        Constraint::Length(2),
    ])
    .areas(frame.area());

    let health = if let Some(error) = &app.status_problem {
        Line::styled(
            format!(" Offline • {}", clean(error)),
            state_style("failed"),
        )
    } else if let Some(status) = &app.status {
        let mut health = Line::from(Span::styled(
            format!(" Boot: {}", clean(&status.boot.state)),
            state_style(&status.boot.state),
        ));
        if let Some(message) = &status.boot.message {
            health
                .spans
                .push(Span::styled(format!(" • {}", clean(message)), muted()));
        }
        health
    } else {
        Line::styled(" Connecting to the daemon…", muted())
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(" LOCALMODELHUB", accent()),
                Span::styled("   /   INFERENCE MANAGER", muted()),
            ]),
            health,
        ]),
        header,
    );
    let tab = |label, active| {
        Span::styled(
            label,
            if active {
                selected().add_modifier(Modifier::BOLD)
            } else {
                muted()
            },
        )
    };
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            tab(" 1 Models ", matches!(app.tab, Tab::Models)),
            tab(" 2 Jobs ", matches!(app.tab, Tab::Jobs)),
            tab(" 3 Stats ", matches!(app.tab, Tab::Stats)),
        ])),
        nav,
    );
    match app.tab {
        Tab::Models => draw_models(frame, app, body, detail),
        Tab::Jobs => draw_jobs(frame, app, body, detail),
        Tab::Stats => draw_stats(frame, app, body),
    }
    let keys = if matches!(app.tab, Tab::Models) {
        " ↑↓ Move   s Start   x Stop   w Switch   l Logs   ? Help   q Quit"
    } else if matches!(app.tab, Tab::Stats) {
        " ←→ Filter model   ↑↓ Scroll   Tab Views   ? Help   q Quit"
    } else {
        " ↑↓ Move   Tab Views   r Refresh   ? Help   q Quit"
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::raw(format!(" {}", clean(&app.notice))),
            Line::styled(keys, muted()),
        ]),
        footer,
    );
    if let Some(overlay) = &mut app.overlay {
        draw_overlay(frame, overlay);
    }
}

fn empty(frame: &mut Frame<'_>, area: Rect, title: &'static str, text: &'static str) {
    frame.render_widget(
        Paragraph::new(text).style(muted()).block(panel(title)),
        area,
    );
}

fn draw_models(frame: &mut Frame<'_>, app: &mut App, body: Rect, detail: Rect) {
    let Some(status) = &app.status else {
        return empty(
            frame,
            body,
            " Models ",
            "Waiting for the daemon to report profiles.",
        );
    };
    if app.profile_keys.is_empty() {
        return empty(frame, body, " Models ", "No model profiles are configured.");
    }
    let text = app.current().and_then(|key| {
        let profile = app.profile(key)?;
        let io = status
            .models
            .iter()
            .find(|model| model.id == key.model)
            .into_iter()
            .flat_map(|model| &model.io)
            .map(|pair| format!("{} → {}", pair.input.join(" + "), pair.output))
            .collect::<Vec<_>>()
            .join(" · ");
        Some(format!(
            "{} • {} • {}{}{}",
            key.label(),
            clean(&profile.device),
            if profile.controllable == Some(true) {
                "Lifecycle available"
            } else {
                "Lifecycle blocked"
            },
            profile
                .message
                .as_ref()
                .map(|text| format!(" • {}", clean(text)))
                .unwrap_or_default(),
            if io.is_empty() {
                String::new()
            } else {
                format!("\n{}", clean(&io))
            },
        ))
    });
    let rows = status.models.iter().flat_map(|model| {
        model.profiles.iter().map(|profile| {
            let name = if model.name.is_empty() {
                &model.id
            } else {
                &model.name
            };
            let default = if model.default_profile.as_ref() == Some(&profile.id) {
                " *"
            } else {
                ""
            };
            Row::new(vec![
                Line::raw(clean(name)),
                Line::raw(format!("{}{default}", clean(&profile.id))),
                Line::styled(clean(&profile.state), state_style(&profile.state)),
                Line::raw(clean(&profile.device)),
                Line::raw(profile.port.map_or("—".into(), |port| port.to_string())),
            ])
        })
    });
    let table = Table::new(
        rows,
        [
            Constraint::Fill(3),
            Constraint::Fill(2),
            Constraint::Length(11),
            Constraint::Fill(2),
            Constraint::Length(5),
        ],
    )
    .header(Row::new(["Model", "Profile", "State", "Device", "Port"]).style(muted()))
    .block(panel(format!(" Profiles · {} ", app.profile_keys.len())))
    .row_highlight_style(selected())
    .highlight_symbol("› ");
    frame.render_stateful_widget(table, body, &mut app.profile_cursor);
    frame.render_widget(
        Paragraph::new(text.unwrap_or_default())
            .wrap(Wrap { trim: true })
            .block(panel(" Selection ")),
        detail,
    );
}

fn draw_jobs(frame: &mut Frame<'_>, app: &mut App, body: Rect, detail: Rect) {
    let Some(status) = &app.status else {
        return empty(
            frame,
            body,
            " Jobs ",
            "Waiting for the daemon to report jobs.",
        );
    };
    if status.jobs.is_empty() {
        return empty(frame, body, " Jobs ", "No lifecycle jobs reported.");
    }
    let rows = status.jobs.iter().map(|job| {
        Row::new(vec![
            Line::raw(clean(&job.action)),
            Line::raw(clean(&format!("{}/{}", job.model, job.profile))),
            Line::styled(clean(&job.state), state_style(&job.state)),
            Line::raw(clean(job.message.as_deref().unwrap_or("—"))),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Length(9),
            Constraint::Fill(2),
            Constraint::Length(10),
            Constraint::Fill(3),
        ],
    )
    .header(Row::new(["Action", "Model / Profile", "State", "Message"]).style(muted()))
    .block(panel(format!(" Lifecycle jobs · {} ", status.jobs.len())))
    .row_highlight_style(selected())
    .highlight_symbol("› ");
    frame.render_stateful_widget(table, body, &mut app.job_cursor);
    if let Some(job) = app.job_cursor.selected().and_then(|i| status.jobs.get(i)) {
        let source = job.source.as_deref().unwrap_or("—");
        frame.render_widget(
            Paragraph::new(clean(&format!("ID: {} • Source: {}", job.id, source)))
                .wrap(Wrap { trim: true })
                .block(panel(" Job ")),
            detail,
        );
    }
}

fn memory(bytes: Option<u64>) -> String {
    bytes.map_or_else(
        || "not reported".into(),
        |bytes| format!("{:.2} GiB", bytes as f64 / 1_073_741_824.0),
    )
}

fn array<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value[key].as_array().map(Vec::as_slice).unwrap_or_default()
}

fn stats_lines(stats: &Value, lines: &mut Vec<Line<'static>>) {
    lines.push(Line::styled("Latest memory sample", accent()));
    if let Some(latest) = array(stats, "samples").last() {
        lines.push(Line::raw(format!(
            "RAM: {}  •  VRAM: {}",
            memory(latest["ram_used_bytes"].as_u64()),
            memory(latest["vram_used_bytes"].as_u64())
        )));
    } else {
        lines.push(Line::styled("No memory samples reported.", muted()));
    }
    lines.push(Line::default());
    lines.push(Line::styled("Lifecycle jobs • last 24 hours", accent()));
    let jobs = array(stats, "jobs_by_model");
    if jobs.is_empty() {
        lines.push(Line::styled("No finished jobs reported.", muted()));
    }
    for job in jobs {
        let name = job["model"].as_str().unwrap_or("—");
        let finished = job["finished"]
            .as_u64()
            .map_or("—".into(), |n| n.to_string());
        let failed = job["failed"].as_u64().map_or("—".into(), |n| n.to_string());
        let start = job["start_seconds"]
            .as_f64()
            .map_or("—".into(), |n| format!("{n:.2} s"));
        lines.push(Line::raw(format!(
            "{}: {} finished  •  {} failed  •  avg start/switch {}",
            clean(name),
            finished,
            failed,
            start
        )));
    }
    lines.push(Line::default());
    lines.push(Line::styled("Inference counters", accent()));
    let mut reported = false;
    let fields = [
        ("requests", "requests"),
        ("requests_per_second", "requests/s"),
        ("tokens", "tokens"),
        ("prompt_tokens", "prompt tokens"),
        ("generation_tokens", "generated tokens"),
        ("cached_prompt_tokens", "cached prompt tokens"),
        ("prompt_cache_hit_rate", "prompt cache hit"),
        ("expert_cache_hit_rate", "expert cache hit"),
        ("inference_error_rate", "error rate"),
    ];
    let mut append = |label: String, counters: &Value| {
        let values: Vec<_> = fields
            .iter()
            .filter_map(|(field, name)| {
                let number = counters[*field].as_number()?;
                let value = if field.ends_with("_rate") {
                    format!("{:.1}%", number.as_f64()? * 100.0)
                } else if *field == "requests_per_second" {
                    format!("{:.2}", number.as_f64()?)
                } else {
                    number.to_string()
                };
                Some(format!("{value} {name}"))
            })
            .collect();
        if !values.is_empty() {
            reported = true;
            lines.push(Line::raw(format!("{label}: {}", values.join("  •  "))));
        }
    };
    append("Total".into(), &stats["metrics"]);
    for row in array(stats, "profiles") {
        let name = format!(
            "{}/{}",
            row["model"].as_str().unwrap_or("—"),
            row["profile"].as_str().unwrap_or("—")
        );
        append(clean(&name), row);
    }
    if !reported {
        lines.push(Line::styled("No inference counters reported.", muted()));
    }
    lines.push(Line::default());
    lines.push(Line::styled(
        "Language heuristic • approximate English matches",
        accent(),
    ));
    let language = crate::language_stats_lines(stats);
    if language.is_empty() {
        lines.push(Line::styled("No generated text scanned.", muted()));
    } else {
        lines.extend(language.into_iter().map(|line| Line::raw(clean(&line))));
    }
}

fn draw_stats(frame: &mut Frame<'_>, app: &mut App, body: Rect) {
    let mut lines = vec![Line::styled("Host", accent())];
    if let Some(status) = &app.status {
        let hardware = &status.hardware;
        lines.push(Line::raw(format!(
            "CPU: {}",
            clean(hardware.cpu.as_deref().unwrap_or("not reported"))
        )));
        lines.push(Line::raw(format!(
            "RAM: {} available / {} total",
            memory(hardware.ram_available_bytes),
            memory(hardware.ram_total_bytes)
        )));
        if hardware.gpus.is_empty() {
            lines.push(Line::styled("No GPUs reported.", muted()));
        }
        for gpu in &hardware.gpus {
            lines.push(Line::raw(format!(
                "GPU {}: {}  •  {} free / {} total",
                clean(&gpu.id),
                clean(&gpu.name),
                memory(gpu.free_bytes),
                memory(gpu.total_bytes)
            )));
        }
    } else {
        lines.push(Line::styled("Waiting for host information…", muted()));
    }
    lines.push(Line::default());
    match &app.stats {
        Some(Ok(stats)) => stats_lines(stats, &mut lines),
        Some(Err(error)) => lines.push(Line::styled(
            clean(&format!("Stats unavailable: {error:#}")),
            state_style("failed"),
        )),
        None => lines.push(Line::styled("Loading statistics…", muted())),
    }
    let block = panel(format!(
        " Statistics · {} ",
        clean(app.stats_model.as_deref().unwrap_or("All models"))
    ));
    let inner = block.inner(body);
    let lines = stats_wrap(lines, usize::from(inner.width));
    let limit = lines.len().saturating_sub(usize::from(inner.height));
    app.stats_scroll = app
        .stats_scroll
        .min(u16::try_from(limit).unwrap_or(u16::MAX));
    frame.render_widget(block, body);
    frame.render_widget(Paragraph::new(lines).scroll((app.stats_scroll, 0)), inner);
}

fn stats_wrap(lines: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    let mut wrapped = Vec::new();
    for line in lines {
        if line.width() <= width {
            wrapped.push(line);
            continue;
        }
        let mut row = String::new();
        let mut used = 0;
        for word in line.to_string().split_whitespace() {
            let word_width = Span::raw(word).width();
            if !row.is_empty() {
                if used + 1 + word_width > width {
                    wrapped.push(Line::styled(std::mem::take(&mut row), line.style));
                    used = 0;
                } else {
                    row.push(' ');
                    used += 1;
                }
            }
            row.push_str(word);
            used += word_width;
        }
        wrapped.push(Line::styled(row, line.style));
    }
    wrapped
}

#[derive(Deserialize)]
struct Progress {
    stage: String,
    state: String,
    message: String,
    dashboard: Option<String>,
    inference: Option<String>,
}

enum OnboardReply {
    Event(Progress),
    Finished(Result<(ExitStatus, String)>),
}

fn onboard_process(python: &Path, tx: &Sender<OnboardReply>) -> Result<(ExitStatus, String)> {
    let mut child = Command::new(python)
        .args(["-I", "-m", "lmh.onboarding", "--jsonl"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("launch onboarding with {}", python.display()))?;

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    thread::scope(|scope| {
        // Drain stderr as well as stdout so a noisy subprocess cannot block.
        let stderr = scope.spawn(move || {
            let mut tail = std::collections::VecDeque::with_capacity(6);
            if let Some(stderr) = stderr {
                for line in BufReader::new(stderr)
                    .lines()
                    .map_while(std::result::Result::ok)
                {
                    if tail.len() == 6 {
                        tail.pop_front();
                    }
                    tail.push_back(clean(&line));
                }
            }
            tail.into_iter().collect::<Vec<_>>().join("\n")
        });
        let mut read_error = None;
        if let Some(stdout) = stdout {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(line) => {
                        if let Ok(progress) = serde_json::from_str::<Progress>(&line) {
                            let _ = tx.send(OnboardReply::Event(progress));
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::InvalidData => continue,
                    Err(error) => {
                        read_error = Some(error);
                        break;
                    }
                }
            }
        }
        let status = child.wait().context("wait for onboarding")?;
        let diagnostics = stderr
            .join()
            .map_err(|_| anyhow!("onboarding stderr reader failed"))?;
        if let Some(error) = read_error {
            return Err(error).context("read onboarding progress");
        }
        Ok((status, diagnostics))
    })
}

fn stage_name(stage: &str) -> &str {
    match stage {
        "account" => "Account",
        "backend" => "Local panel",
        "devices" => "Inference devices",
        "catalog" => "Starter catalog",
        "host" => "Host checks",
        "runtime" => "CPU runtime",
        "start" => "Starter model",
        "smoke" => "Inference check",
        "done" => "Setup",
        other => other,
    }
}

fn draw_onboarding(frame: &mut Frame<'_>, stages: &[Progress], elapsed: Duration) {
    frame.render_widget(
        Block::default().style(Style::default().bg(Color::Black).fg(Color::White)),
        frame.area(),
    );
    let bounds = frame.area();
    let area = popup(bounds, 88, 22);
    let block = panel(" LOCALMODELHUB / FIRST RUN ").border_style(Style::default().fg(GREEN));
    let [title, progress, footer] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Fill(1),
        Constraint::Length(1),
    ])
    .areas(block.inner(area));
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled("Getting your local inference ready", accent()),
            Line::styled("Setup will not stop any running model.", muted()),
        ]),
        title,
    );
    let spinner = ["◐", "◓", "◑", "◒"][(elapsed.as_millis() / 150 % 4) as usize];
    if stages.is_empty() {
        frame.render_widget(
            Paragraph::new(format!("{spinner}  Launching setup…")).style(muted()),
            progress,
        );
    } else {
        let lines: Vec<_> = stages
            .iter()
            .flat_map(|stage| {
                let marker = match stage.state.as_str() {
                    "ok" => "✓",
                    "error" | "failed" => "×",
                    "working" => spinner,
                    _ => "•",
                };
                let mut message = stage.message.lines();
                let first = Line::from(vec![
                    Span::styled(
                        format!("{marker}  {}", clean(stage_name(&stage.stage))),
                        state_style(&stage.state),
                    ),
                    Span::raw(format!(" — {}", clean(message.next().unwrap_or("")))),
                ]);
                std::iter::once(first)
                    .chain(message.map(|line| Line::raw(format!("    {}", clean(line)))))
            })
            .collect();
        let max_rows = usize::from(progress.height);
        // Keep the current stage visible when the terminal shrinks.
        let start = lines.len().saturating_sub((max_rows / 2).max(1));
        frame.render_widget(
            Paragraph::new(lines.into_iter().skip(start).collect::<Vec<_>>())
                .wrap(Wrap { trim: true }),
            progress,
        );
    }
    frame.render_widget(
        Paragraph::new(format!(
            " Setup running  •  {:02}:{:02} elapsed  •  Closes automatically",
            elapsed.as_secs() / 60,
            elapsed.as_secs() % 60
        ))
        .style(muted()),
        footer,
    );
}

/// Show first-run progress, waiting for Python to exit before reporting readiness.
pub fn onboarding(python: &Path) -> Result<()> {
    // A broken terminal must not prevent first-run setup from completing.
    let mut terminal = screen().ok();
    let (tx, rx) = mpsc::channel();
    let python = python.to_owned();
    let worker = thread::Builder::new()
        .name("lmh-onboarding".into())
        .spawn(move || {
            let result = onboard_process(&python, &tx);
            let _ = tx.send(OnboardReply::Finished(result));
        })
        .context("launch onboarding progress reader")?;

    let started = Instant::now();
    let mut stages: Vec<Progress> = Vec::new();
    let finished = loop {
        if let Some((_, screen)) = &mut terminal {
            if screen
                .draw(|frame| draw_onboarding(frame, &stages, started.elapsed()))
                .is_err()
                || read_key().is_err()
            {
                terminal = None;
            }
        }
        let reply = if terminal.is_some() {
            match rx.try_recv() {
                Ok(reply) => reply,
                Err(mpsc::TryRecvError::Empty) => continue,
                Err(mpsc::TryRecvError::Disconnected) => {
                    break Err(anyhow!("onboarding worker stopped unexpectedly"))
                }
            }
        } else {
            match rx.recv() {
                Ok(reply) => reply,
                Err(error) => break Err(error).context("onboarding worker stopped unexpectedly"),
            }
        };
        match reply {
            OnboardReply::Event(event) => {
                if terminal.is_none() && event.stage != "done" {
                    let _ = writeln!(
                        io::stdout().lock(),
                        "[{}] {}",
                        clean(stage_name(&event.stage)),
                        clean(&event.message)
                    );
                }
                if let Some(stage) = stages.iter_mut().find(|stage| stage.stage == event.stage) {
                    *stage = event;
                } else {
                    stages.push(event);
                }
            }
            OnboardReply::Finished(result) => break result,
        }
    };
    drop(terminal);
    worker
        .join()
        .map_err(|_| anyhow!("onboarding worker panicked"))?;
    let (status, diagnostics) = finished?;
    if !status.success() {
        let detail = stages
            .iter()
            .rev()
            .find(|stage| matches!(stage.state.as_str(), "error" | "failed"))
            .map(|stage| {
                format!(
                    "{}: {}",
                    clean(stage_name(&stage.stage)),
                    clean(&stage.message)
                )
            });
        return match detail {
            Some(detail) => Err(anyhow!("{detail}")),
            None if !diagnostics.is_empty() => {
                Err(anyhow!("Onboarding exited with {status}:\n{diagnostics}"))
            }
            None => Err(anyhow!("Onboarding exited with {status}.")),
        };
    }
    let done = stages
        .iter()
        .find(|stage| stage.stage == "done" && stage.state == "ok")
        .context("Onboarding exited without reporting readiness.")?;
    let dashboard = done
        .dashboard
        .as_deref()
        .context("Onboarding did not report a dashboard address.")?;
    let mut out = io::stdout().lock();
    writeln!(out, "{}\nPanel: {}", clean(&done.message), clean(dashboard))?;
    if let Some(inference) = &done.inference {
        writeln!(out, "Inference API: {}", clean(inference))?;
    }
    Ok(())
}

fn popup(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width.saturating_sub(2));
    let height = height.min(area.height.saturating_sub(2));
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}

fn draw_overlay(frame: &mut Frame<'_>, overlay: &mut Overlay) {
    let bounds = frame.area();
    let (width, height, title) = match overlay {
        Overlay::Help => (68, 13, " Keyboard help "),
        Overlay::PickSource { .. } => (76, 14, " Switch · choose a source "),
        Overlay::ConfirmSwitch { .. } => (76, 10, " Switch · confirm "),
        Overlay::Logs { .. } => (
            bounds.width.saturating_sub(8),
            bounds.height.saturating_sub(4),
            " Profile logs ",
        ),
    };
    let area = popup(bounds, width, height);
    let block = panel(title).border_style(Style::default().fg(GREEN));
    let inner = block.inner(area);
    frame.render_widget(Clear, area);
    frame.render_widget(block, area);
    match overlay {
        Overlay::Help => frame.render_widget(
            Paragraph::new(vec![
                Line::raw("1 / 2 / 3, Tab       Models, Jobs, Stats"),
                Line::raw("↑ ↓ or j / k        Move or scroll"),
                Line::raw("s                   Start (never stops another profile)"),
                Line::raw("x                   Stop selected profile"),
                Line::raw("w                   Switch: pick a source, then confirm"),
                Line::raw("l                   Logs for selected profile"),
                Line::raw("r                   Refresh"),
                Line::raw("q / Esc             Dismiss dialog (q quits in main view)"),
            ]).wrap(Wrap { trim: false }), inner),
        Overlay::PickSource { target, sources, cursor } => {
            let [instructions, list, help] = Layout::vertical([
                Constraint::Length(2), Constraint::Fill(1), Constraint::Length(1),
            ]).areas(inner);
            frame.render_widget(
                Paragraph::new(format!("Target: {}\nChoose exactly one running profile to stop.", target.label())).wrap(Wrap { trim: true }),
                instructions,
            );
            let profiles = List::new(sources.iter().map(ProfileKey::label))
                .highlight_style(selected()).highlight_symbol("› ");
            frame.render_stateful_widget(profiles, list, cursor);
            frame.render_widget(Paragraph::new("↑↓ Choose   Enter Review   Esc Cancel").style(muted()), help);
        }
        Overlay::ConfirmSwitch { target, source } => frame.render_widget(
            Paragraph::new(vec![
                Line::from(vec![Span::styled("STOP   ", Style::default().fg(Color::Yellow)), Span::raw(source.label())]),
                Line::from(vec![Span::styled("START  ", accent()), Span::raw(target.label())]),
                Line::default(),
                Line::raw("Only this source will be stopped. It is not automatically restarted if the target fails."),
                Line::default(),
                Line::styled("y Confirm switch   n / Esc Cancel", muted()),
            ]).wrap(Wrap { trim: true }), inner),
        Overlay::Logs { target, lines, cursor } => {
            let [header, body, help] = Layout::vertical([
                Constraint::Length(1), Constraint::Fill(1), Constraint::Length(1),
            ]).areas(inner);
            frame.render_widget(Paragraph::new(target.label()).style(accent()), header);
            match lines {
                Some(Ok(lines)) if !lines.is_empty() => {
                    let logs = List::new(lines.iter().map(|line| clean(line).replace('\n', " "))).highlight_style(selected());
                    frame.render_stateful_widget(logs, body, cursor);
                }
                Some(Ok(_)) => frame.render_widget(Paragraph::new("No log lines returned."), body),
                Some(Err(error)) => frame.render_widget(Paragraph::new(clean(&format!("{error:#}"))).wrap(Wrap { trim: true }).style(state_style("failed")), body),
                None => frame.render_widget(Paragraph::new("Loading logs…"), body),
            }
            frame.render_widget(Paragraph::new("↑↓ Scroll   PgUp / PgDn   Esc Close").style(muted()), help);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::Model;

    fn switch_ready(app: &mut App, client: &Client, tx: &Sender<Reply>) {
        let profile = |id: &str, state: &str, controllable| Profile {
            id: id.into(),
            state: state.into(),
            controllable,
            ..Profile::default()
        };
        let status = Status {
            models: vec![Model {
                id: "chat".into(),
                profiles: vec![
                    profile("target", "stopped", Some(true)),
                    profile("source", "ready", Some(true)),
                    profile("external", "ready", Some(false)),
                ],
                ..Model::default()
            }],
            ..Status::default()
        };
        app.apply(Reply::Status(Ok(Box::new(status))), client, tx);
    }

    #[test]
    fn switch_starts_without_a_source_and_requires_separate_confirmation() -> Result<()> {
        let mut app = App::new();
        let client = Client::new("http://127.0.0.1:5189", None)?;
        let (tx, _rx) = mpsc::channel();
        switch_ready(&mut app, &client, &tx);

        let press = |app: &mut App, code| {
            app.key(KeyEvent::new(code, KeyModifiers::NONE), &client, &tx);
        };
        press(&mut app, KeyCode::Char('w'));
        let mut screen = Terminal::new(ratatui::backend::TestBackend::new(80, 24))?;
        screen.draw(|frame| draw_manager(frame, &mut app))?;
        press(&mut app, KeyCode::Enter);
        assert!(
            matches!(&app.overlay, Some(Overlay::PickSource { sources, cursor, .. })
            if cursor.selected().is_none() && sources.len() == 1)
        );

        press(&mut app, KeyCode::Down);
        press(&mut app, KeyCode::Enter);
        press(&mut app, KeyCode::Enter);
        assert!(
            matches!(&app.overlay, Some(Overlay::ConfirmSwitch { source, target })
            if source.profile == "source" && target.profile == "target" && !app.acting)
        );
        Ok(())
    }

    #[test]
    fn confirming_switch_does_not_submit_when_source_has_stopped() -> Result<()> {
        let mut app = App::new();
        let client = Client::new("http://127.0.0.1:5189", None)?;
        let (tx, _rx) = mpsc::channel();
        switch_ready(&mut app, &client, &tx);
        app.overlay = Some(Overlay::ConfirmSwitch {
            target: app.profile_keys[0].clone(),
            source: app.profile_keys[1].clone(),
        });
        if let Some(status) = &mut app.status {
            status.models[0].profiles[1].state = "stopped".into();
        }
        app.key(
            KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE),
            &client,
            &tx,
        );
        assert_eq!(
            app.notice,
            "Switch cancelled: the selected source is no longer eligible."
        );
        Ok(())
    }
}
