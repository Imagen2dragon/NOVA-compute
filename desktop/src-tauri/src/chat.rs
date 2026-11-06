use futures_util::StreamExt;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::sync::watch;

use crate::storage::log_diagnostic;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StreamTokenPayload {
    pub conversation_id: String,
    pub message_id: String,
    pub token: String,
    pub done: bool,
    pub status: String, // "streaming", "complete", "cancelled", "error"
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequestMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OpenAiChatRequest {
    pub model: String,
    pub messages: Vec<ChatRequestMessage>,
    pub stream: bool,
    pub max_tokens: u32,
    pub temperature: f32,
}

#[derive(Debug, Deserialize)]
struct Delta {
    content: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Choice {
    delta: Option<Delta>,
    #[allow(dead_code)]
    finish_reason: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Chunk {
    choices: Option<Vec<Choice>>,
}

pub trait ChatEventSink: Send + Sync {
    fn emit_stream_token(&self, payload: StreamTokenPayload);
}

impl ChatEventSink for AppHandle {
    fn emit_stream_token(&self, payload: StreamTokenPayload) {
        let _ = self.emit("nova://stream-token", payload);
    }
}

struct ActiveGeneration {
    id: u64,
    cancel_tx: watch::Sender<bool>,
    completion_rx: watch::Receiver<bool>,
}

struct GenerationState {
    active: Option<ActiveGeneration>,
    is_unloading: bool,
}

/// RAII Drop Guard ensuring that active generation state is unconditionally released,
/// even in the event of task abortion, panic unwind, early return, or network error.
pub struct GenerationGuard {
    state: Arc<Mutex<GenerationState>>,
    gen_id: u64,
    completion_tx: Option<watch::Sender<bool>>,
}

impl Drop for GenerationGuard {
    fn drop(&mut self) {
        {
            let mut guard = self.state.lock().unwrap();
            if let Some(ref active) = guard.active {
                if active.id == self.gen_id {
                    guard.active = None;
                }
            }
        }
        if let Some(tx) = self.completion_tx.take() {
            let _ = tx.send(true);
        }
    }
}

impl std::fmt::Debug for GenerationGuard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GenerationGuard")
            .field("gen_id", &self.gen_id)
            .finish()
    }
}

pub struct ChatService {
    state: Arc<Mutex<GenerationState>>,
    next_id: AtomicU64,
}

impl ChatService {
    pub fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(GenerationState {
                active: None,
                is_unloading: false,
            })),
            next_id: AtomicU64::new(1),
        }
    }

    pub fn is_generating(&self) -> bool {
        self.state.lock().unwrap().active.is_some()
    }

    pub fn is_unloading(&self) -> bool {
        self.state.lock().unwrap().is_unloading
    }

    /// Atomically claims a generation slot under the synchronized boundary.
    /// Rejects the request if unloading is in progress or if another generation is active.
    /// Returns an RAII GenerationGuard that automatically releases the slot on drop.
    pub fn claim_generation_slot(
        &self,
    ) -> Result<(GenerationGuard, watch::Receiver<bool>), String> {
        let mut guard = self.state.lock().unwrap();
        if guard.is_unloading {
            return Err("Cannot send message: model is currently unloading.".to_string());
        }
        if guard.active.is_some() {
            return Err("Another generation is already in progress.".to_string());
        }
        let gen_id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let (completion_tx, completion_rx) = watch::channel(false);

        guard.active = Some(ActiveGeneration {
            id: gen_id,
            cancel_tx,
            completion_rx,
        });

        let gen_guard = GenerationGuard {
            state: self.state.clone(),
            gen_id,
            completion_tx: Some(completion_tx),
        };

        Ok((gen_guard, cancel_rx))
    }

    /// Requests cancellation of the active generation task.
    /// Does NOT clear the active-generation state early!
    pub fn stop_generation(&self) {
        let guard = self.state.lock().unwrap();
        if let Some(ref active) = guard.active {
            log_diagnostic(&format!(
                "Triggering per-request cancellation on generation {}",
                active.id
            ));
            let _ = active.cancel_tx.send(true);
        }
    }

    /// Begins the unloading transition atomically:
    /// Sets is_unloading to true, cancels any active generation, and returns its completion receiver.
    pub fn begin_unload(&self) -> Option<watch::Receiver<bool>> {
        let mut guard = self.state.lock().unwrap();
        guard.is_unloading = true;
        if let Some(ref active) = guard.active {
            log_diagnostic(&format!("begin_unload: canceling generation {}", active.id));
            let _ = active.cancel_tx.send(true);
            Some(active.completion_rx.clone())
        } else {
            None
        }
    }

    /// Ends the unloading transition.
    pub fn end_unload(&self) {
        let mut guard = self.state.lock().unwrap();
        guard.is_unloading = false;
    }

    /// Awaits actual generation-task completion and socket release.
    /// Unload signals cancellation and waits on the per-generation completion channel without holding the lock.
    pub async fn await_generation_complete(&self) {
        let waiter = self.begin_unload();
        if let Some(mut rx) = waiter {
            if !*rx.borrow() {
                log_diagnostic("Awaiting active generation task termination...");
                let _ = tokio::time::timeout(Duration::from_secs(5), rx.changed()).await;
                log_diagnostic("Active generation task terminated and confirmed complete.");
            }
        }
    }

    pub async fn stream_completion<S: ChatEventSink>(
        &self,
        sink: &S,
        port: u16,
        api_key: &str,
        conversation_id: String,
        assistant_message_id: String,
        messages: Vec<ChatRequestMessage>,
    ) -> Result<String, String> {
        let (_guard, cancel_rx) = self.claim_generation_slot()?;

        // Run stream loop. When this future completes or is aborted/panics, _guard automatically cleans up.
        Self::stream_loop(
            sink,
            port,
            api_key,
            conversation_id,
            assistant_message_id,
            messages,
            cancel_rx,
        )
        .await
    }

    async fn stream_loop<S: ChatEventSink>(
        sink: &S,
        port: u16,
        api_key: &str,
        conversation_id: String,
        assistant_message_id: String,
        messages: Vec<ChatRequestMessage>,
        mut cancel_rx: watch::Receiver<bool>,
    ) -> Result<String, String> {
        let client = Client::builder()
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|e| format!("Failed to create HTTP client: {}", e))?;

        let url = format!("http://127.0.0.1:{}/v1/chat/completions", port);
        let request_body = OpenAiChatRequest {
            model: "qwen3-30b".to_string(),
            messages,
            stream: true,
            max_tokens: 512,
            temperature: 0.7,
        };

        // If cancellation was already requested before sending request
        if *cancel_rx.borrow() {
            log_diagnostic("Generation cancelled before HTTP request was initiated.");
            sink.emit_stream_token(StreamTokenPayload {
                conversation_id,
                message_id: assistant_message_id,
                token: String::new(),
                done: true,
                status: "cancelled".to_string(),
                error: None,
            });
            return Err("Generation cancelled by user".to_string());
        }

        // Send request with cancellation select
        let response = tokio::select! {
            biased;
            res = cancel_rx.changed() => {
                if res.is_ok() && *cancel_rx.borrow() {
                    log_diagnostic("Generation cancelled before HTTP response headers received.");
                    sink.emit_stream_token(StreamTokenPayload {
                        conversation_id,
                        message_id: assistant_message_id,
                        token: String::new(),
                        done: true,
                        status: "cancelled".to_string(),
                        error: None,
                    });
                    return Err("Generation cancelled by user".to_string());
                }
                return Err("Cancellation channel disconnected".to_string());
            }
            res = client
                .post(&url)
                .header("Authorization", format!("Bearer {}", api_key))
                .header("Content-Type", "application/json")
                .json(&request_body)
                .send() => {
                    match res {
                        Ok(resp) => {
                            if !resp.status().is_success() {
                                let status = resp.status();
                                let err_text = resp.text().await.unwrap_or_default();
                                let msg = format!("Inference server returned status {}: {}", status, err_text);
                                sink.emit_stream_token(StreamTokenPayload {
                                    conversation_id,
                                    message_id: assistant_message_id,
                                    token: String::new(),
                                    done: true,
                                    status: "error".to_string(),
                                    error: Some(msg.clone()),
                                });
                                return Err(msg);
                            }
                            resp
                        }
                        Err(e) => {
                            let msg = format!("Failed to connect to inference server: {}", e);
                            sink.emit_stream_token(StreamTokenPayload {
                                conversation_id,
                                message_id: assistant_message_id,
                                token: String::new(),
                                done: true,
                                status: "error".to_string(),
                                error: Some(msg.clone()),
                            });
                            return Err(msg);
                        }
                    }
                }
        };

        let mut stream = response.bytes_stream();
        let mut raw_buffer: Vec<u8> = Vec::new();
        let mut accumulated_full_text = String::new();

        loop {
            // Check cancellation flag between chunk reads
            if *cancel_rx.borrow() {
                log_diagnostic("Streaming interrupted by user cancellation between chunk reads.");
                sink.emit_stream_token(StreamTokenPayload {
                    conversation_id,
                    message_id: assistant_message_id,
                    token: String::new(),
                    done: true,
                    status: "cancelled".to_string(),
                    error: None,
                });
                return Err("Generation cancelled by user".to_string());
            }

            tokio::select! {
                biased;
                res = cancel_rx.changed() => {
                    if res.is_ok() && *cancel_rx.borrow() {
                        log_diagnostic("Streaming interrupted by user cancellation. Aborting HTTP stream immediately.");
                        sink.emit_stream_token(StreamTokenPayload {
                            conversation_id,
                            message_id: assistant_message_id,
                            token: String::new(),
                            done: true,
                            status: "cancelled".to_string(),
                            error: None,
                        });
                        return Err("Generation cancelled by user".to_string());
                    }
                }
                chunk_opt = stream.next() => {
                    match chunk_opt {
                        Some(Ok(bytes)) => {
                            raw_buffer.extend_from_slice(&bytes);

                            // Process complete lines delimited by \n
                            while let Some(pos) = raw_buffer.iter().position(|&b| b == b'\n') {
                                let line_bytes = &raw_buffer[..pos];
                                let line_str = String::from_utf8_lossy(line_bytes).trim().to_string();
                                raw_buffer.drain(..=pos);

                                if line_str.is_empty() {
                                    continue;
                                }

                                if let Some(data) = line_str.strip_prefix("data: ") {
                                    let data = data.trim();
                                    if data == "[DONE]" {
                                        sink.emit_stream_token(StreamTokenPayload {
                                            conversation_id: conversation_id.clone(),
                                            message_id: assistant_message_id.clone(),
                                            token: String::new(),
                                            done: true,
                                            status: "complete".to_string(),
                                            error: None,
                                        });
                                        return Ok(accumulated_full_text);
                                    }

                                    match serde_json::from_str::<Chunk>(data) {
                                        Ok(chunk) => {
                                            if let Some(choices) = chunk.choices {
                                                for choice in choices {
                                                    if let Some(delta) = choice.delta {
                                                        if let Some(token) = delta.content {
                                                            if !token.is_empty() {
                                                                accumulated_full_text.push_str(&token);
                                                                sink.emit_stream_token(StreamTokenPayload {
                                                                    conversation_id: conversation_id.clone(),
                                                                    message_id: assistant_message_id.clone(),
                                                                    token,
                                                                    done: false,
                                                                    status: "streaming".to_string(),
                                                                    error: None,
                                                                });
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        Err(e) => {
                                            let err_msg = format!("Malformed JSON event in SSE: {}", e);
                                            log_diagnostic(&err_msg);
                                            sink.emit_stream_token(StreamTokenPayload {
                                                conversation_id: conversation_id.clone(),
                                                message_id: assistant_message_id.clone(),
                                                token: String::new(),
                                                done: true,
                                                status: "error".to_string(),
                                                error: Some(err_msg.clone()),
                                            });
                                            return Err(err_msg);
                                        }
                                    }
                                }
                            }
                        }
                        Some(Err(e)) => {
                            let err_msg = format!("Error reading stream chunk: {}", e);
                            sink.emit_stream_token(StreamTokenPayload {
                                conversation_id,
                                message_id: assistant_message_id,
                                token: String::new(),
                                done: true,
                                status: "error".to_string(),
                                error: Some(err_msg.clone()),
                            });
                            return Err(err_msg);
                        }
                        None => {
                            let err_msg = "Stream closed unexpectedly before [DONE] was received.".to_string();
                            sink.emit_stream_token(StreamTokenPayload {
                                conversation_id,
                                message_id: assistant_message_id,
                                token: String::new(),
                                done: true,
                                status: "error".to_string(),
                                error: Some(err_msg.clone()),
                            });
                            return Err(err_msg);
                        }
                    }
                }
            }
        }
    }
}
