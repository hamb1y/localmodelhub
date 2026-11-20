use std::net::IpAddr;
use std::time::Duration;

use anyhow::{bail, ensure, Context, Result};
use reqwest::blocking::{Client as HttpClient, RequestBuilder};
use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, AUTHORIZATION};
use reqwest::{redirect, retry, Url};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use serde_json::{Map, Value};

pub const DEFAULT_ENDPOINT: &str = "http://localhost:5189";

#[derive(Clone)]
pub struct Client {
    http: HttpClient,
    base: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Status {
    pub models: Vec<Model>,
    pub jobs: Vec<Job>,
    pub hardware: Hardware,
    pub boot: Boot,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Model {
    pub id: String,
    pub name: String,
    pub profiles: Vec<Profile>,
    pub default_profile: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Profile {
    pub id: String,
    pub device: String,
    pub state: String,
    pub port: Option<u16>,
    pub controllable: Option<bool>,
    pub message: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Job {
    pub id: String,
    pub action: String,
    pub model: String,
    pub profile: String,
    pub state: String,
    pub message: Option<String>,
    pub source: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Hardware {
    pub cpu: Option<String>,
    pub ram_available_bytes: Option<u64>,
    pub ram_total_bytes: Option<u64>,
    pub gpus: Vec<Gpu>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Gpu {
    pub id: String,
    pub name: String,
    pub total_bytes: Option<u64>,
    pub free_bytes: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Boot {
    pub state: String,
    pub message: Option<String>,
}

#[derive(Deserialize)]
struct LogResponse {
    lines: Vec<String>,
}

#[derive(Serialize)]
struct Action<'a> {
    action: &'a str,
    model: &'a str,
    profile: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    from_model: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    from_profile: Option<&'a str>,
}

#[derive(Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CatalogSource<'a> {
    Catalog(&'a RawValue),
    Url(&'a str),
}

impl Client {
    pub fn new(base: &str, token: Option<String>) -> Result<Self> {
        let url = parse_endpoint(base)?;
        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
        if let Some(token) = token.filter(|token| !token.is_empty()) {
            let host = url.host_str().unwrap_or_default();
            let loopback = host == "localhost"
                || host
                    .trim_matches(['[', ']'])
                    .parse::<IpAddr>()
                    .is_ok_and(|address| address.is_loopback());
            ensure!(
                url.scheme() == "https" || loopback,
                "LMH_API_TOKEN requires HTTPS or a loopback endpoint."
            );
            ensure!(
                token.bytes().all(|byte| byte.is_ascii_graphic()),
                "LMH_API_TOKEN must be an ASCII bearer token without whitespace."
            );
            let mut value = HeaderValue::from_str(&format!("Bearer {token}"))
                .context("LMH_API_TOKEN cannot be used as an HTTP header.")?;
            value.set_sensitive(true);
            headers.insert(AUTHORIZATION, value);
        }
        let http = HttpClient::builder()
            .default_headers(headers)
            .no_proxy()
            .redirect(redirect::Policy::none())
            .retry(retry::never())
            .timeout(Duration::from_secs(8))
            .build()
            .context("Could not initialize the backend HTTP client")?;
        Ok(Self {
            http,
            base: url.as_str().trim_end_matches('/').to_owned(),
        })
    }

    pub fn endpoint(&self) -> &str {
        &self.base
    }

    pub fn status(&self) -> Result<Status> {
        self.send(self.http.get(self.url("/api/status")))
    }

    pub fn stats(&self) -> Result<Value> {
        self.send_object(self.http.get(self.url("/api/stats")))
    }

    pub fn logs(&self, model: &str, profile: &str) -> Result<Vec<String>> {
        let request = self
            .http
            .get(self.url("/api/logs"))
            .query(&[("model", model), ("profile", profile)]);
        let response: LogResponse = self.send(request)?;
        Ok(response.lines)
    }

    pub fn action(
        &self,
        action: &str,
        model: &str,
        profile: &str,
        source: Option<(&str, &str)>,
    ) -> Result<Value> {
        let (from_model, from_profile) = match source {
            Some((model, profile)) => (Some(model), Some(profile)),
            None => (None, None),
        };
        let request = self.http.post(self.url("/api/actions")).json(&Action {
            action,
            model,
            profile,
            from_model,
            from_profile,
        });
        self.send_object(request)
    }

    pub fn preview(&self, source: CatalogSource<'_>) -> Result<Value> {
        self.send_object(
            self.http
                .post(self.url("/api/catalog/preview"))
                .json(&source),
        )
    }

    pub fn stage(&self, catalog: &RawValue) -> Result<Value> {
        let request = self
            .http
            .post(self.url("/api/catalog/stage"))
            .json(&CatalogSource::Catalog(catalog));
        self.send_object(request)
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base)
    }

    fn send_object(&self, request: RequestBuilder) -> Result<Value> {
        self.send::<Map<String, Value>>(request).map(Value::Object)
    }

    fn send<T: DeserializeOwned>(&self, request: RequestBuilder) -> Result<T> {
        let response = request
            .send()
            .with_context(|| format!("Cannot reach {}", self.base))?;
        let status = response.status();
        if !status.is_success() {
            let body = response.json::<Value>().ok();
            let detail = body
                .as_ref()
                .and_then(error_detail)
                .unwrap_or_else(|| status.canonical_reason().unwrap_or("Backend error"));
            bail!("HTTP {}: {detail}", status.as_u16());
        }
        response
            .json()
            .context("Backend returned invalid JSON or an unexpected response")
    }
}

fn parse_endpoint(base: &str) -> Result<Url> {
    const INVALID: &str = "Endpoint must be an HTTP(S) URL without credentials, query or fragment.";
    let (_, rest) = base.split_once("://").context(INVALID)?;
    let authority = rest.split('/').next().unwrap_or_default();
    ensure!(
        !authority.is_empty()
            && !authority.contains('@')
            && !base.contains('\\')
            && !base.chars().any(|c| c.is_whitespace() || c.is_control()),
        INVALID
    );
    // Do not include the raw input in diagnostics: it might contain credentials.
    let url = Url::parse(base).map_err(|_| anyhow::anyhow!(INVALID))?;
    ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.has_host()
            && url.query().is_none()
            && url.fragment().is_none(),
        INVALID
    );
    Ok(url)
}

fn error_detail(value: &Value) -> Option<&str> {
    ["message", "error", "detail"].into_iter().find_map(|key| {
        let detail = value.get(key)?;
        detail
            .as_str()
            .or_else(|| detail.get("message").and_then(Value::as_str))
            .or_else(|| detail.get("detail").and_then(Value::as_str))
            .filter(|detail| !detail.is_empty())
    })
}

#[cfg(test)]
mod tests;
