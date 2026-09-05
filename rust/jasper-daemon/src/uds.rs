// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The local control socket both audio daemons answer on: bind, an accept loop
//! that wakes on readiness but still checks shutdown, one bounded command read
//! per connection, and the response write. The command vocabulary and the state
//! it renders belong to the daemon — this owns only the transport.

use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use crate::DaemonHooks;

/// Maximum `poll(2)` wait for the accept loop. Socket readiness wakes it
/// immediately; the timeout only bounds how long shutdown waits. Do not
/// replace this with a blind sleep: that added ~500 ms to every short-lived
/// connection in production.
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// What one command exchange may cost. Both are the hosting daemon's policy:
/// the cap is sized by that daemon's LONGEST command, the timeout by how long
/// its single server thread may sit on one client.
#[derive(Clone, Copy)]
pub struct CommandLimits {
    pub max_command_bytes: usize,
    pub read_timeout: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommandReadError {
    TooLong,
    DeadlineExceeded,
    InvalidUtf8,
}

impl CommandReadError {
    fn response_json(self, max_command_bytes: usize) -> String {
        match self {
            Self::TooLong => format!(
                r#"{{"error":"command too long","code":"command_too_long","max_bytes":{max_command_bytes}}}"#
            ),
            Self::DeadlineExceeded => r#"{"error":"command read deadline exceeded","code":"command_read_deadline_exceeded"}"#.to_string(),
            Self::InvalidUtf8 => {
                r#"{"error":"command must be UTF-8","code":"command_not_utf8"}"#.to_string()
            }
        }
    }
}

pub struct UdsCommandServer {
    socket_path: PathBuf,
    listener: UnixListener,
    hooks: DaemonHooks,
    limits: CommandLimits,
}

impl UdsCommandServer {
    /// Bind the socket, replacing any leftover from a crashed instance. The
    /// caller owns the parent directory (systemd's `RuntimeDirectory=` supplies
    /// it in production) and adds its own error context.
    pub fn bind(
        socket_path: PathBuf,
        hooks: DaemonHooks,
        limits: CommandLimits,
    ) -> io::Result<Self> {
        // bind() would return EADDRINUSE against a stale socket file.
        let _ = std::fs::remove_file(&socket_path);
        let listener = UnixListener::bind(&socket_path)?;
        listener.set_nonblocking(true)?;
        (hooks.info)(&format!(
            "event={}.state_server.listening socket={}",
            hooks.event_prefix,
            socket_path.display()
        ));
        Ok(Self {
            socket_path,
            listener,
            hooks,
            limits,
        })
    }

    /// Answer commands until `shutdown` is set, then unlink the socket.
    /// Intended to be run on a dedicated thread.
    pub fn serve(&self, shutdown: &AtomicBool, dispatch: impl Fn(&str) -> String) {
        let prefix = self.hooks.event_prefix;
        while !shutdown.load(Ordering::Relaxed) {
            match wait_for_listener(&self.listener, ACCEPT_POLL_INTERVAL) {
                Ok(false) => continue,
                Ok(true) => self.drain_ready_connections(shutdown, &dispatch),
                Err(error) => (self.hooks.warn)(&format!(
                    "event={prefix}.state_server.poll_failed detail={error}"
                )),
            }
        }

        let _ = std::fs::remove_file(&self.socket_path);
        (self.hooks.info)(&format!("event={prefix}.state_server.stopped"));
    }

    fn drain_ready_connections(&self, shutdown: &AtomicBool, dispatch: &impl Fn(&str) -> String) {
        let prefix = self.hooks.event_prefix;
        // Re-check shutdown between clients. A continuously replenished local
        // accept queue must not trap the audio daemon here until systemd's
        // SIGKILL deadline; one in-flight client remains bounded by the read
        // deadline.
        while !shutdown.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if let Err(error) = self.handle_connection(stream, dispatch) {
                        (self.hooks.warn)(&format!(
                            "event={prefix}.state_server.handle_failed detail={error}"
                        ));
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) => {
                    (self.hooks.warn)(&format!(
                        "event={prefix}.state_server.accept_failed detail={error}"
                    ));
                    break;
                }
            }
        }
    }

    /// Read one command, hand it to `dispatch`, write the reply and a newline.
    /// A client that hangs up mid-exchange is ordinary local IPC churn, not a
    /// daemon fault, so it returns `Ok`.
    pub fn handle_connection(
        &self,
        stream: UnixStream,
        dispatch: impl Fn(&str) -> String,
    ) -> io::Result<()> {
        self.handle_connection_with_timeout(stream, self.limits.read_timeout, dispatch)
    }

    fn handle_connection_with_timeout(
        &self,
        mut stream: UnixStream,
        timeout: Duration,
        dispatch: impl Fn(&str) -> String,
    ) -> io::Result<()> {
        let mut response =
            match read_bounded_command(&mut stream, timeout, self.limits.max_command_bytes) {
                Ok(Ok(command)) => dispatch(command.trim()),
                Ok(Err(error)) => error.response_json(self.limits.max_command_bytes),
                Err(error) if is_client_disconnect(&error) => return Ok(()),
                Err(error) => return Err(error),
            };
        response.push('\n');
        match stream.write_all(response.as_bytes()) {
            Err(error) if is_client_disconnect(&error) => Ok(()),
            other => other,
        }
    }
}

/// `Ok(true)` once the listener has a connection waiting, `Ok(false)` on
/// timeout. Retries `EINTR` rather than reporting it as a poll failure.
fn wait_for_listener(listener: &UnixListener, timeout: Duration) -> io::Result<bool> {
    let mut descriptor = libc::pollfd {
        fd: listener.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let timeout_ms = i32::try_from(timeout.as_millis()).unwrap_or(i32::MAX);
    loop {
        // SAFETY: `descriptor` points to one initialized pollfd for the duration
        // of the syscall; poll neither retains nor aliases the pointer.
        let ready = unsafe { libc::poll(&mut descriptor, 1, timeout_ms) };
        if ready >= 0 {
            return Ok(ready > 0);
        }
        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return Err(error);
    }
}

/// Read one newline-delimited command under both a byte cap and a total
/// monotonic deadline. Re-applying the *remaining* timeout before each read is
/// load-bearing: a client that trickles one byte per socket timeout cannot keep
/// the single state-server thread occupied indefinitely.
fn read_bounded_command(
    stream: &mut UnixStream,
    timeout: Duration,
    max_command_bytes: usize,
) -> io::Result<Result<String, CommandReadError>> {
    let started = Instant::now();
    let Some(deadline) = started.checked_add(timeout) else {
        return Ok(Err(CommandReadError::DeadlineExceeded));
    };
    let mut bytes = Vec::with_capacity(64);
    let mut chunk = [0u8; 64];

    loop {
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Ok(Err(CommandReadError::DeadlineExceeded));
        };
        if remaining.is_zero() {
            return Ok(Err(CommandReadError::DeadlineExceeded));
        }
        stream.set_read_timeout(Some(remaining))?;

        match stream.read(&mut chunk) {
            Ok(0) => {
                if Instant::now() >= deadline {
                    return Ok(Err(CommandReadError::DeadlineExceeded));
                }
                break;
            }
            Ok(read) => {
                if Instant::now() >= deadline {
                    return Ok(Err(CommandReadError::DeadlineExceeded));
                }
                let newline = chunk[..read].iter().position(|byte| *byte == b'\n');
                let command_bytes = &chunk[..newline.unwrap_or(read)];
                if bytes.len().saturating_add(command_bytes.len()) > max_command_bytes {
                    return Ok(Err(CommandReadError::TooLong));
                }
                bytes.extend_from_slice(command_bytes);
                if newline.is_some() {
                    break;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                return Ok(Err(CommandReadError::DeadlineExceeded));
            }
            Err(error) => return Err(error),
        }
    }

    match String::from_utf8(bytes) {
        Ok(command) => Ok(Ok(command)),
        Err(_) => Ok(Err(CommandReadError::InvalidUtf8)),
    }
}

fn is_client_disconnect(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::BrokenPipe
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::NotConnected
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIMITS: CommandLimits = CommandLimits {
        max_command_bytes: 256,
        read_timeout: Duration::from_secs(2),
    };

    const HOOKS: DaemonHooks = DaemonHooks {
        event_prefix: "test",
        writer_stack_bytes: 0,
        info: |_| {},
        warn: |_| {},
        error: |_| {},
    };

    fn echo(command: &str) -> String {
        format!(r#"{{"received":{}}}"#, crate::json::json_string(command))
    }

    fn test_socket_path(kind: &str) -> PathBuf {
        static NEXT_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        // Keep the AF_UNIX path short on macOS (sun_path is only 104 bytes).
        std::env::temp_dir().join(format!("jts-uds-{kind}-{}-{id}.sock", std::process::id()))
    }

    fn test_server(kind: &str) -> UdsCommandServer {
        let path = test_socket_path(kind);
        let server = UdsCommandServer::bind(path.clone(), HOOKS, LIMITS).expect("bind test server");
        // The socket-backed tests drive `handle_connection` over a socketpair,
        // so unlink the listener path immediately: a test panic then cannot
        // leave filesystem residue. The bound listener stays valid until drop.
        std::fs::remove_file(path).expect("unlink test socket");
        server
    }

    fn exchange(server: &UdsCommandServer, command: &[u8]) -> String {
        use std::net::Shutdown;

        let (mut client, server_stream) = UnixStream::pair().expect("socket pair");
        client.write_all(command).expect("write command");
        client
            .shutdown(Shutdown::Write)
            .expect("finish command write");
        server
            .handle_connection(server_stream, echo)
            .expect("handle command");

        let mut response = String::new();
        client.read_to_string(&mut response).expect("read response");
        response
    }

    #[test]
    fn listener_poll_wakes_on_connection_before_shutdown_timeout() {
        let path = test_socket_path("poll");
        let listener = UnixListener::bind(&path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let client_path = path.clone();
        let connector = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            UnixStream::connect(client_path).unwrap()
        });
        let started = Instant::now();
        assert!(wait_for_listener(&listener, Duration::from_millis(500)).unwrap());
        assert!(
            started.elapsed() < Duration::from_millis(300),
            "socket readiness must wake poll rather than pay the 500 ms timeout",
        );
        let _stream = connector.join().unwrap();
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn listener_backlog_drain_honors_shutdown_before_next_client() {
        let path = test_socket_path("drain");
        let server = UdsCommandServer::bind(path.clone(), HOOKS, LIMITS).expect("bind");
        let _pending_client = UnixStream::connect(&path).unwrap();
        let shutdown = AtomicBool::new(true);

        server.drain_ready_connections(&shutdown, &echo);

        // The pending connection remains untouched: shutdown won over draining
        // the ready backlog, so the outer serve loop can exit promptly.
        assert!(server.listener.accept().is_ok());
        let _ = std::fs::remove_file(path);
    }

    /// The cap is exact, every rejection is a bounded structured reply rather
    /// than an echo of the oversized input, and a client that vanishes before
    /// sending is not a daemon fault.
    #[test]
    #[cfg_attr(
        target_os = "macos",
        ignore = "pre-existing AF_UNIX EINVAL on macOS (abandoned-client SO_RCVTIMEO); CI (Linux) is authoritative"
    )]
    fn command_cap_is_exact_and_errors_stay_bounded() {
        let server = test_server("cap");

        let mut at_cap = vec![b'X'; LIMITS.max_command_bytes];
        at_cap.push(b'\n');
        let accepted: serde_json::Value = serde_json::from_str(exchange(&server, &at_cap).trim())
            .expect("at-cap command must reach dispatch");
        assert_eq!(
            accepted["received"].as_str().map(str::len),
            Some(LIMITS.max_command_bytes)
        );

        let mut over_cap = vec![b'X'; LIMITS.max_command_bytes + 1];
        over_cap.push(b'\n');
        let rejected_response = exchange(&server, &over_cap);
        let rejected: serde_json::Value = serde_json::from_str(rejected_response.trim())
            .expect("oversized command must return JSON");
        assert_eq!(rejected["code"].as_str(), Some("command_too_long"));
        assert_eq!(
            rejected["max_bytes"].as_u64(),
            Some(LIMITS.max_command_bytes as u64)
        );
        assert!(
            rejected_response.len() < 128,
            "oversized input must not be echoed into an oversized response"
        );

        let invalid_utf8: serde_json::Value =
            serde_json::from_str(exchange(&server, &[0xff, b'\n']).trim())
                .expect("invalid UTF-8 must return JSON");
        assert_eq!(invalid_utf8["code"].as_str(), Some("command_not_utf8"));

        // A client that disappears before sending a command is normal local
        // IPC churn, not a daemon fault that should emit handle_failed spam.
        let (abandoned, server_stream) = UnixStream::pair().expect("socket pair");
        drop(abandoned);
        server
            .handle_connection(server_stream, echo)
            .expect("abandoned client is handled quietly");
    }

    #[test]
    fn total_deadline_rejects_a_slow_trickle() {
        let server = test_server("deadline");
        let (mut client, server_stream) = UnixStream::pair().expect("socket pair");

        let writer = std::thread::spawn(move || {
            client
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound test response read");
            for byte in b"STATUS\n" {
                if client.write_all(&[*byte]).is_err() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            let mut response = String::new();
            client
                .read_to_string(&mut response)
                .expect("read deadline response");
            response
        });

        let started = Instant::now();
        server
            .handle_connection_with_timeout(server_stream, Duration::from_millis(50), echo)
            .expect("slow client receives a structured error");
        let elapsed = started.elapsed();
        let response = writer.join().expect("slow writer thread");
        let error: serde_json::Value =
            serde_json::from_str(response.trim()).expect("deadline response must be JSON");

        assert_eq!(
            error["code"].as_str(),
            Some("command_read_deadline_exceeded")
        );
        assert!(
            elapsed < Duration::from_millis(500),
            "total deadline must not reset for each trickled byte: {elapsed:?}"
        );
    }
}
