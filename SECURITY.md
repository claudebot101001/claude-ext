# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Please report security issues by emailing [MAINTAINER_EMAIL]. Include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You should receive an acknowledgment within 48 hours. We will work with you to understand and address the issue before any public disclosure.

## Security Considerations

### Permission Mode

claude-ext runs Claude Code with `--permission-mode bypassPermissions` by default. This gives Claude full filesystem and shell access within sessions. This is by design for autonomous agent operation but means:

- Claude can read/write any file accessible to the process user
- Claude can execute arbitrary shell commands
- Access control depends on OS-level permissions (run as a dedicated user with minimal privileges)

### Vault

The vault extension uses Fernet encryption (AES-128-CBC + HMAC) as defense-in-depth. Encryption prevents casual reading of credential files but is **not** the primary security boundary in `bypassPermissions` mode. The real access controls are:

- `_internal_prefixes` — controls what MCP tools can read
- OS file permissions — controls who can run the process

### Sensitive Configuration

`config.yaml` contains secrets (bot tokens, user IDs) and is gitignored. Never commit this file. Use `config.yaml.example` as a template.
