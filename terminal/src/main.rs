mod api;
mod tui;

use std::env;
use std::fs::{self, File};
use std::io::{self, IsTerminal, Read};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command as Process, ExitCode};

use anyhow::{bail, ensure, Context, Result};
use api::{CatalogSource, Client, Hardware, Status, DEFAULT_ENDPOINT};
use clap::{ArgGroup, Args, Parser, Subcommand};
use nix::sys::statvfs::statvfs;
use nix::unistd::{geteuid, User};
use serde_json::value::RawValue;
use serde_json::Value;

#[derive(Parser)]
#[command(
    version,
    about = "Control and monitor LocalModelHub",
    after_help = "If the panel requires authentication, set LMH_API_TOKEN (not a webhook token)."
)]
struct Cli {
    /// Backend URL (loopback by default).
    #[arg(long, env = "LMH_ENDPOINT", default_value = DEFAULT_ENDPOINT, global = true)]
    endpoint: String,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Open the interactive terminal panel (also the default command).
    Tui,
    /// Walk through setup without requiring a Hugging Face login.
    Onboard,
    /// Optionally save a Hugging Face token for gated or private models.
    HfLogin,
    /// List registered model profiles.
    List,
    /// Request an additive start; does not stop other profiles.
    Start(ProfileArgs),
    /// Request that the backend stop a profile.
    Stop(ProfileArgs),
    /// Show backend lifecycle jobs.
    Jobs,
    /// Show logs for a profile.
    Logs(ProfileArgs),
    /// Show reported memory, lifecycle, and inference statistics.
    Stats {
        /// Show statistics for a single model ID.
        #[arg(long)]
        model: Option<String>,
        /// Print the raw statistics response.
        #[arg(long)]
        json: bool,
    },
    /// Check backend availability, boot state, hardware, and disk space.
    Doctor {
        /// Filesystem to check (defaults to the LocalModelHub data filesystem).
        #[arg(long)]
        disk_path: Option<PathBuf>,
    },
    /// Validate and preview a catalog without applying it.
    Preview(PreviewArgs),
    /// Stage a reviewed catalog for the next OS boot; no immediate changes.
    Stage {
        /// Reviewed local JSON catalog.
        #[arg(long)]
        catalog: PathBuf,
    },
    /// Check for and install a newer stable LocalModelHub release.
    Update,
    /// Remove LocalModelHub from the ai account.
    Uninstall {
        /// Also remove LocalModelHub config, history, and cache.
        #[arg(long)]
        purge: bool,
    },
}

#[derive(Args)]
struct ProfileArgs {
    model: String,
    /// Defaults to the model's default profile.
    profile: Option<String>,
}

#[derive(Args)]
#[command(group(ArgGroup::new("source").required(true).args(["url", "catalog"])))]
struct PreviewArgs {
    /// Public HTTPS catalog URL for the backend to preview.
    #[arg(long)]
    url: Option<String>,
    /// Local JSON catalog to preview.
    #[arg(long)]
    catalog: Option<PathBuf>,
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("lmh: {error:#}");
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli) -> Result<()> {
    let command = cli.command.unwrap_or(Command::Tui);
    match command {
        Command::Onboard => {
            let python = installed_python()?;
            return if interactive() {
                tui::onboarding(&python)
            } else {
                python_module(&python, "lmh.onboarding", &[])
            };
        }
        Command::Update => return local_admin(&["update"]),
        Command::Uninstall { purge: false } => return local_admin(&["uninstall"]),
        Command::Uninstall { purge: true } => return local_admin(&["uninstall", "--purge"]),
        Command::HfLogin => return local_admin(&["hf-login"]),
        Command::Tui if !interactive() => {
            bail!("The terminal panel requires a TTY. Use `lmh list` or see `lmh --help`.");
        }
        _ => {}
    }

    let token = match env::var("LMH_API_TOKEN") {
        Ok(token) => Some(token),
        Err(env::VarError::NotPresent) => None,
        Err(env::VarError::NotUnicode(_)) => bail!("LMH_API_TOKEN must be an ASCII bearer token."),
    };
    let client = Client::new(&cli.endpoint, token)?;
    match command {
        Command::Tui => tui::run(client),
        Command::List => {
            print_models(&client.status()?);
            Ok(())
        }
        Command::Start(args) => profile_action(&client, "start", args),
        Command::Stop(args) => profile_action(&client, "stop", args),
        Command::Logs(args) => {
            let profile = profile_id(&client, &args.model, args.profile)?;
            for line in client.logs(&args.model, &profile)? {
                println!("{line}");
            }
            Ok(())
        }
        Command::Jobs => {
            let jobs = client.status()?.jobs;
            if jobs.is_empty() {
                println!("No jobs.");
            }
            for job in jobs {
                println!(
                    "{}  {} {}/{}  {} [{}]{}",
                    job.id,
                    job.action,
                    job.model,
                    job.profile,
                    job.state,
                    job.source.as_deref().unwrap_or("—"),
                    detail(job.message.as_deref())
                );
            }
            Ok(())
        }
        Command::Stats { model, json } => {
            let stats = client.stats(model.as_deref())?;
            if json {
                return print_json(&stats);
            }
            if let Ok(status) = client.status() {
                print_hardware(&status.hardware, "");
                println!();
            }
            print_stats(&stats);
            Ok(())
        }
        Command::Doctor { disk_path } => doctor(&client, disk_path.as_deref()),
        Command::Preview(PreviewArgs { url, catalog }) => {
            let result = match (url, catalog) {
                (Some(url), None) => client.preview(CatalogSource::Url(&url))?,
                (None, Some(path)) => {
                    let catalog = read_catalog(&path)?;
                    client.preview(CatalogSource::Catalog(&catalog))?
                }
                _ => bail!("Provide exactly one of --url or --catalog."),
            };
            print_json(&result)
        }
        Command::Stage { catalog } => {
            let result = client.stage(&read_catalog(&catalog)?)?;
            println!(
                "{}",
                result["message"]
                    .as_str()
                    .unwrap_or("Catalog staged for the next OS boot.")
            );
            Ok(())
        }
        Command::Onboard | Command::HfLogin | Command::Update | Command::Uninstall { .. } => {
            unreachable!("local commands are handled before connecting")
        }
    }
}

fn interactive() -> bool {
    io::stdin().is_terminal() && io::stdout().is_terminal()
}

fn installed_python() -> Result<PathBuf> {
    if let Some(path) = env::var_os("LMH_PYTHON") {
        ensure!(
            !path.is_empty(),
            "LMH_PYTHON is empty; set it to the installed Python interpreter."
        );
        return Ok(PathBuf::from(path));
    }

    let exe = env::current_exe().context("Could not locate the installed lmh binary")?;
    exe.ancestors()
        .skip(1)
        .take(3)
        .map(|dir| dir.join("venv/bin/python"))
        .find(|path| path.is_file())
        .context("Could not locate the release's venv/bin/python; reinstall LocalModelHub or set LMH_PYTHON.")
}

fn local_admin(args: &[&str]) -> Result<()> {
    python_module(&installed_python()?, "lmh.local_admin", args)
}

fn python_module(python: &Path, module: &str, args: &[&str]) -> Result<()> {
    let mut command = Process::new(python);
    command.args(["-I", "-m", module]).args(args);
    // Replace the process so signals and exit status belong to the helper.
    Err(command.exec())
        .with_context(|| format!("Could not launch {module} with {}", python.display()))
}

fn print_models(status: &Status) {
    if status.models.iter().all(|model| model.profiles.is_empty()) {
        println!("No model profiles registered.");
    }
    for model in &status.models {
        for profile in &model.profiles {
            let default = if model.default_profile.as_deref() == Some(profile.id.as_str()) {
                " (default)"
            } else {
                ""
            };
            let port = profile
                .port
                .map_or_else(String::new, |port| format!(" :{port}"));
            let blocked = if profile.controllable == Some(false) {
                " (control blocked)"
            } else {
                ""
            };
            println!(
                "{}/{}{}  {}  [{}] {}{}{}{}",
                model.id,
                profile.id,
                default,
                model.name,
                profile.device,
                profile.state,
                port,
                blocked,
                detail(profile.message.as_deref())
            );
        }
    }
}

fn detail(message: Option<&str>) -> String {
    message
        .filter(|message| !message.is_empty())
        .map_or_else(String::new, |message| format!(" — {message}"))
}

fn profile_id(client: &Client, model: &str, profile: Option<String>) -> Result<String> {
    if let Some(profile) = profile {
        return Ok(profile);
    }
    let model = client
        .status()?
        .models
        .into_iter()
        .find(|entry| entry.id == model)
        .with_context(|| format!("Unknown model {model:?}; see `lmh list`."))?;
    if let Some(profile) = model.default_profile {
        return Ok(profile);
    }
    if let [profile] = model.profiles.as_slice() {
        return Ok(profile.id.clone());
    }
    bail!(
        "Model {:?} has no default profile; specify PROFILE (see `lmh list`).",
        model.id
    )
}

fn profile_action(client: &Client, action: &str, args: ProfileArgs) -> Result<()> {
    let profile = profile_id(client, &args.model, args.profile)?;
    print_json(&client.action(action, &args.model, &profile, None)?)
}

fn print_json(value: &Value) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

fn expand_home(path: &Path) -> PathBuf {
    if let (Ok(rest), Some(home)) = (path.strip_prefix("~"), env::var_os("HOME")) {
        return PathBuf::from(home).join(rest);
    }
    path.to_owned()
}

fn read_catalog(path: &Path) -> Result<Box<RawValue>> {
    let path = expand_home(path);
    let mut bytes = Vec::new();
    // Match the daemon's 1 MiB request limit without reading unbounded input.
    const LIMIT: usize = 1024 * 1024;
    File::open(&path)
        .and_then(|file| file.take((LIMIT + 1) as u64).read_to_end(&mut bytes))
        .with_context(|| format!("Could not read catalog {}", path.display()))?;
    ensure!(bytes.len() <= LIMIT, "Catalog file exceeds 1 MiB.");
    // Preserve the original JSON so the server can reject duplicate object keys.
    let catalog: Box<RawValue> = serde_json::from_slice(&bytes)
        .with_context(|| format!("Invalid JSON catalog {}", path.display()))?;
    ensure!(
        catalog.get().starts_with('{'),
        "Catalog file must contain a JSON object."
    );
    Ok(catalog)
}

fn size(value: Option<u64>) -> String {
    let Some(value) = value else {
        return "unknown".to_owned();
    };
    if value < 1024 {
        return format!("{value} B");
    }
    let units = ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
    let mut scaled = value as f64 / 1024.0;
    for unit in &units[..units.len() - 1] {
        if scaled < 1024.0 {
            return format!("{scaled:.1} {unit}");
        }
        scaled /= 1024.0;
    }
    format!("{scaled:.1} EiB")
}

fn print_hardware(hardware: &Hardware, indent: &str) {
    println!(
        "{indent}CPU: {}",
        hardware.cpu.as_deref().unwrap_or("unknown")
    );
    println!(
        "{indent}RAM: {} available / {} total",
        size(hardware.ram_available_bytes),
        size(hardware.ram_total_bytes)
    );
    if hardware.gpus.is_empty() {
        println!("{indent}GPU: none reported");
    }
    for gpu in &hardware.gpus {
        println!(
            "{indent}GPU {}: {} — {} free / {} total",
            gpu.id,
            gpu.name,
            size(gpu.free_bytes),
            size(gpu.total_bytes)
        );
    }
}

fn print_stats(stats: &Value) {
    println!(
        "Statistics: {}",
        stats["model"].as_str().unwrap_or("all models")
    );
    println!("Host memory usage (latest sample):");
    if let Some(sample) = stats["samples"]
        .as_array()
        .and_then(|samples| samples.last())
    {
        println!(
            "  RAM: {}; VRAM: {}",
            size(sample["ram_used_bytes"].as_u64()),
            size(sample["vram_used_bytes"].as_u64())
        );
    } else {
        println!("  No memory samples reported.");
    }

    println!("\nLifecycle jobs (last 24 hours):");
    let jobs = rows(&stats["jobs_by_model"]);
    if jobs.is_empty() {
        println!("  No finished jobs reported.");
    }
    for job in jobs {
        let fields = [
            ("finished", "finished", Format::Count),
            ("failed", "failed", Format::Count),
            (
                "start_seconds",
                "average successful start/switch",
                Format::Seconds,
            ),
        ];
        let values = metrics(job, &fields).unwrap_or_else(|| "not reported".to_owned());
        println!("  {}: {values}", job["model"].as_str().unwrap_or("—"));
    }

    println!("\nInference counters:");
    let fields = [
        ("requests", "requests", Format::Count),
        ("requests_per_second", "requests/s", Format::Rate),
        ("tokens", "tokens", Format::Count),
        ("prompt_tokens", "prompt tokens", Format::Count),
        ("generation_tokens", "generated tokens", Format::Count),
        (
            "cached_prompt_tokens",
            "cached prompt tokens",
            Format::Count,
        ),
        (
            "prompt_cache_hit_rate",
            "prompt cache hit rate",
            Format::Percent,
        ),
        (
            "expert_cache_hit_rate",
            "expert cache hit rate",
            Format::Percent,
        ),
        (
            "inference_error_rate",
            "inference error rate",
            Format::Percent,
        ),
    ];
    let mut reported = false;
    if let Some(values) = metrics(&stats["metrics"], &fields) {
        println!("  Total: {values}");
        reported = true;
    }
    for profile in rows(&stats["profiles"]) {
        if let Some(values) = metrics(profile, &fields) {
            println!(
                "  {}/{}: {values}",
                profile["model"].as_str().unwrap_or("—"),
                profile["profile"].as_str().unwrap_or("—")
            );
            reported = true;
        }
    }
    if !reported {
        println!("  No inference counters reported.");
    }
    println!("\nLanguage heuristic (approximate English matches, not a quality score):");
    let language = language_stats_lines(stats);
    if language.is_empty() {
        println!("  No generated text scanned.");
    }
    for line in language {
        println!("  {line}");
    }
}

fn language_stats_lines(stats: &Value) -> Vec<String> {
    rows(&stats["language_by_model"])
        .iter()
        .map(|row| {
            let count = |field: &str| row[field].as_u64().unwrap_or(0);
            format!(
                "{}: {} mild profanity · {} strong profanity · {} slurs · {} responses scanned ({} partial)",
                row["model"].as_str().unwrap_or("—"),
                count("mild"),
                count("strong"),
                count("slurs"),
                count("responses"),
                count("truncated"),
            )
        })
        .collect()
}

fn rows(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

enum Format {
    Count,
    Seconds,
    Rate,
    Percent,
}

fn metrics(value: &Value, fields: &[(&str, &str, Format)]) -> Option<String> {
    let parts = fields
        .iter()
        .filter_map(|(key, label, format)| {
            let number = value[key].as_number()?;
            let shown = match format {
                Format::Count => number.to_string(),
                Format::Seconds => format!("{:.2} s", number.as_f64()?),
                Format::Rate => format!("{:.2}", number.as_f64()?),
                Format::Percent => format!("{:.1}%", number.as_f64()? * 100.0),
            };
            Some(format!("{label}: {shown}"))
        })
        .collect::<Vec<_>>();
    (!parts.is_empty()).then(|| parts.join(", "))
}

fn doctor(client: &Client, disk_path: Option<&Path>) -> Result<()> {
    let uid = geteuid();
    let user = User::from_uid(uid)
        .ok()
        .flatten()
        .map_or_else(|| uid.to_string(), |user| user.name);
    let account_note = if user == "ai" {
        "expected"
    } else {
        "LocalModelHub runs as ai"
    };
    println!("Native account: {user} ({account_note})");
    let status = client.status();
    match &status {
        Ok(status) => {
            println!("Backend: reachable at {}", client.endpoint());
            println!(
                "Boot: {}{}",
                status.boot.state,
                detail(status.boot.message.as_deref())
            );
            println!("Device (backend):");
            print_hardware(&status.hardware, "  ");
        }
        Err(error) => {
            println!("Backend: unavailable ({error:#})");
            println!("Device (local, limited fallback):");
            print_hardware(&local_hardware(), "  ");
        }
    }
    let disk_path = disk_path.map_or_else(default_disk_path, expand_home);
    let disk = statvfs(&disk_path);
    match &disk {
        Ok(info) => {
            let bytes =
                |blocks| u64::try_from(u128::from(info.fragment_size()) * u128::from(blocks)).ok();
            println!(
                "Disk ({}): {} free / {} total",
                disk_path.display(),
                size(bytes(info.blocks_available())),
                size(bytes(info.blocks()))
            );
        }
        Err(error) => println!("Disk ({}): unavailable ({error})", disk_path.display()),
    }
    disk.context("Could not check disk space")?;
    ensure!(
        status?.boot.state != "failed",
        "Boot reconciliation failed."
    );
    Ok(())
}

fn default_disk_path() -> PathBuf {
    let home = env::var_os("HOME").map_or_else(|| PathBuf::from("/"), PathBuf::from);
    let data = env::var_os("XDG_DATA_HOME")
        .filter(|path| !path.is_empty())
        .map_or_else(|| home.join(".local/share"), PathBuf::from)
        .join("localmodelhub");
    std::path::absolute(data)
        .ok()
        .and_then(|data| {
            data.ancestors()
                .find(|dir| dir.is_dir())
                .map(Path::to_owned)
        })
        .unwrap_or(home)
}

fn local_hardware() -> Hardware {
    let cpu = fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    let cpu = cpu.lines().find_map(|line| {
        let (key, name) = line.split_once(':')?;
        let name = name.trim();
        (matches!(key.trim(), "model name" | "Hardware" | "Processor") && !name.is_empty())
            .then_some(name)
    });
    let memory = fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let kib = |field| {
        let value = memory.lines().find_map(|line| {
            let (key, value) = line.split_once(':')?;
            (key == field).then_some(value)
        })?;
        let mut words = value.split_whitespace();
        let value = words.next()?.parse::<u64>().ok()?;
        (words.next()? == "kB")
            .then(|| value.checked_mul(1024))
            .flatten()
    };
    Hardware {
        cpu: Some(cpu.unwrap_or(env::consts::ARCH).to_owned()),
        ram_available_bytes: kib("MemAvailable"),
        ram_total_bytes: kib("MemTotal"),
        gpus: Vec::new(),
    }
}
