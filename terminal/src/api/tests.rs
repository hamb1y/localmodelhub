use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread::{self, JoinHandle};

use serde_json::json;

use super::*;

struct Request {
    line: String,
    headers: String,
    body: String,
}

fn fake_backend(status: &str, headers: &str, body: &str) -> (String, JoinHandle<Request>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Length: {}\r\nContent-Type: application/json\r\nConnection: close\r\n{headers}\r\n{body}",
        body.len()
    );
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut reader = BufReader::new(&mut stream);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        let mut headers = String::new();
        let mut length = 0;
        loop {
            let mut header = String::new();
            assert_ne!(reader.read_line(&mut header).unwrap(), 0);
            if header == "\r\n" {
                break;
            }
            let lowercase = header.to_ascii_lowercase();
            if let Some(value) = lowercase.strip_prefix("content-length:") {
                length = value.trim().parse().unwrap();
            }
            headers.push_str(&header);
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        stream.write_all(response.as_bytes()).unwrap();
        Request {
            line,
            headers,
            body: String::from_utf8(body).unwrap(),
        }
    });
    (endpoint, worker)
}

#[test]
fn client_is_send_sync_and_clone() {
    fn assert_requirements<T: Send + Sync + Clone>() {}
    assert_requirements::<Client>();
}

#[test]
fn invalid_endpoint_does_not_expose_embedded_credentials() {
    for endpoint in [
        "http://user:secret@localhost:5189",
        "http://@localhost:5189",
        "http://localhost:5189?token=secret",
        "http://localhost:5189#secret",
        "http://localhost:99999",
        "http://localhost:5189/a b",
        "http:///localhost:5189",
        "file:///tmp/fake",
    ] {
        let error = Client::new(endpoint, None).err().unwrap().to_string();
        assert!(error.starts_with("Endpoint must be"));
        assert!(!error.contains("secret"));
    }
}

#[test]
fn bearer_token_requires_tls_or_loopback() {
    assert!(Client::new("http://192.0.2.1", Some("token".into())).is_err());
    for endpoint in ["https://example.com", "http://127.0.0.2", "http://[::1]"] {
        assert!(Client::new(endpoint, Some("token".into())).is_ok());
    }
    assert!(Client::new("http://localhost", Some("token\r\nbad".into())).is_err());
}

#[test]
fn status_uses_bearer_auth_and_preserves_unknown_fields() {
    let body = json!({
        "models": [{
            "id": "chat", "name": "Chat", "default_profile": "cpu",
            "profiles": [{"id": "cpu", "device": "cpu", "state": "ready", "port": 8080}]
        }],
        "jobs": [],
        "hardware": {"cpu": "Sample CPU", "ram_total_bytes": 1024, "ram_available_bytes": 0, "gpus": []},
        "boot": {"state": "complete", "message": null},
        "catalog_staged": false,
    });
    let (endpoint, worker) = fake_backend("200 OK", "", &body.to_string());
    let status = Client::new(&format!("{endpoint}/prefix/"), Some("sample-token".into()))
        .unwrap()
        .status()
        .unwrap();
    let request = worker.join().unwrap();
    assert_eq!(request.line, "GET /prefix/api/status HTTP/1.1\r\n");
    assert!(request
        .headers
        .contains("authorization: Bearer sample-token\r\n"));
    assert_eq!(status.models[0].profiles[0].port, Some(8080));
    assert_eq!(status.models[0].profiles[0].controllable, None);
    assert_eq!(status.hardware.ram_available_bytes, Some(0));
    assert_eq!(status.boot.message, None);
}

#[test]
fn logs_encodes_each_query_parameter() {
    let (endpoint, worker) = fake_backend("200 OK", "", r#"{"lines":["one","two"]}"#);
    let lines = Client::new(&endpoint, None)
        .unwrap()
        .logs("a&b", "a+b ?")
        .unwrap();
    let request = worker.join().unwrap();
    assert_eq!(
        request.line,
        "GET /api/logs?model=a%26b&profile=a%2Bb+%3F HTTP/1.1\r\n"
    );
    assert_eq!(lines, ["one", "two"]);
}

#[test]
fn start_omits_switch_source() {
    let (endpoint, worker) = fake_backend("202 Accepted", "", r#"{"state":"queued"}"#);
    let action = Client::new(&endpoint, None)
        .unwrap()
        .action("start", "target", "cpu", None)
        .unwrap();
    let request = worker.join().unwrap();
    assert_eq!(request.line, "POST /api/actions HTTP/1.1\r\n");
    assert_eq!(
        serde_json::from_str::<Value>(&request.body).unwrap(),
        json!({"action": "start", "model": "target", "profile": "cpu"})
    );
    assert_eq!(action["state"], "queued");
}

#[test]
fn switch_serializes_both_explicit_source_fields() {
    let (endpoint, worker) = fake_backend("202 Accepted", "", "{}");
    Client::new(&endpoint, None)
        .unwrap()
        .action("switch", "target", "gpu", Some(("source", "gpu")))
        .unwrap();
    let request = worker.join().unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&request.body).unwrap(),
        json!({
            "action": "switch",
            "model": "target",
            "profile": "gpu",
            "from_model": "source",
            "from_profile": "gpu",
        })
    );
}

#[test]
fn client_reports_backend_conflict_message() {
    let (endpoint, worker) = fake_backend(
        "409 Conflict",
        "",
        r#"{"error":{"message":"Insufficient RAM"}}"#,
    );
    let error = Client::new(&endpoint, None)
        .unwrap()
        .action("start", "target", "cpu", None)
        .unwrap_err();
    worker.join().unwrap();
    assert_eq!(error.to_string(), "HTTP 409: Insufficient RAM");
}

#[test]
fn client_does_not_follow_redirects() {
    let (endpoint, worker) = fake_backend("302 Found", "Location: /redirect-target\r\n", "{}");
    let error = Client::new(&endpoint, Some("sample-token".into()))
        .unwrap()
        .stats()
        .unwrap_err();
    worker.join().unwrap();
    assert_eq!(error.to_string(), "HTTP 302: Found");
}

#[test]
fn preview_sends_catalog_url_to_backend_as_data() {
    let (endpoint, worker) = fake_backend("200 OK", "", "{}");
    Client::new(&endpoint, None)
        .unwrap()
        .preview(CatalogSource::Url("https://example.com/catalog.json"))
        .unwrap();
    let request = worker.join().unwrap();
    assert_eq!(request.line, "POST /api/catalog/preview HTTP/1.1\r\n");
    assert_eq!(
        request.body,
        r#"{"url":"https://example.com/catalog.json"}"#
    );
}

#[test]
fn staging_does_not_normalize_duplicate_catalog_keys() {
    let (endpoint, worker) =
        fake_backend("400 Bad Request", "", r#"{"error":"invalid JSON body"}"#);
    let duplicate_catalog = RawValue::from_string(r#"{"models":{},"models":{}}"#.into()).unwrap();
    let error = Client::new(&endpoint, None)
        .unwrap()
        .stage(&duplicate_catalog)
        .unwrap_err();
    let request = worker.join().unwrap();
    assert_eq!(request.line, "POST /api/catalog/stage HTTP/1.1\r\n");
    assert_eq!(request.body, r#"{"catalog":{"models":{},"models":{}}}"#);
    assert_eq!(error.to_string(), "HTTP 400: invalid JSON body");
}
