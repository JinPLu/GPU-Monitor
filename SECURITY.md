# Security

GPU Broker is intentionally a local pilot:

- The service binds to loopback by default and has no login layer.
- The actor header is an audit label, not authentication.
- SSH input is parsed and the collector uses fixed read-only probes; it must not receive arbitrary commands or private keys.
- Leases coordinate ownership but never authorize or control a remote workload.

Do not expose the service outside loopback or place credentials, private keys, host inventories with secrets, or production telemetry in issues or pull requests. Non-loopback deployment, authentication, remote lifecycle control, and automatic allocation require a separate security review.

For a suspected vulnerability, contact the repository maintainers privately before opening a public issue. Include a minimal reproduction, affected version/commit, and impact; do not include secrets.
