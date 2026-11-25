# Security Policy

## Supported Versions

Security fixes are released only for the latest released version. The server, the worker and the CLI share one release number; run the same release of all three.

| Version | Supported |
| ------- | --------- |
| Latest release | Yes |
| Any earlier release | No |

## Reporting a Vulnerability

We take the security of our software seriously. If you discover a security vulnerability, please follow these steps:

### 1. Do Not Disclose Publicly

Please **do not** create a public GitHub issue for security vulnerabilities. This could put all users at risk.

### 2. Report Privately

Send your vulnerability report to **security@ainvirion.com** with the following information:

- Description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Suggested fix (if any)
- Your contact information

### 3. Response Timeline

- **Initial Response**: Within 48 hours of your report
- **Status Update**: Within 7 days with either a fix timeline or request for more information
- **Resolution**: We aim to patch critical vulnerabilities within 30 days

### 4. Disclosure Policy

- We will work with you to understand and resolve the issue
- Once fixed, we will coordinate disclosure timing with you
- You will be credited in our security advisory (unless you prefer to remain anonymous)

## Security Best Practices

When using this project, please follow these security best practices:

1. **Keep Dependencies Updated**: Regularly update dependencies using `uv lock --upgrade` for Python, `go get -u` for Go, or similar
2. **Use Environment Variables**: Never commit secrets, API keys, or credentials to the repository
3. **Review Code**: Review any code changes before deployment
4. **Enable Security Features**: Use security headers, HTTPS, and other built-in security features

## Known Security Considerations

This section states the trust model and the known limits of the current release. Read it before exposing a deployment to untrusted users or running untrusted task code.

### Trust model

- **Workspace members are trusted almost fully.** A member of a workspace can create tasks that run on that workspace's workers, so a member can run code on those workers. Grant membership only to people you would let run code on the worker hosts.
- **A worker is trusted by its workspace.** A worker receives the tasks of its workspace, their environment and their secrets, and reports their results. Enroll only workers on hosts you control.
- **Plugins, where supported, are trusted code.** A plugin registered through the plugin registry runs inside the server or the worker process, with that process's privileges, and sees what that process sees. Install only plugins you trust.

### Task isolation on the worker

- **A task and the worker that runs it share one uid inside the worker container.** Strict isolation limits what a task receives, but task code can read whatever that uid can read inside the container, including the worker's own files and its credential file `state.json` (the worker identifier and API key). The container boundary, not the uid, separates task code from the host. Any `podman exec` into the worker container, by an operator or by fleet tooling, runs with the full container environment, and a task running at that moment can read it; pass only the variables the command needs (`env -i ...`).
- **Without strict isolation, a task inherits the worker environment.** Workers start with strict isolation: the setting defaults to `true` in code, and the worker installer and `client/.env.example` set `ORCH_ENFORCE_ISOLATION=true` explicitly. A worker that sets it to `false` runs in the legacy mode and logs a warning at start; re-running the installer on an existing host keeps an `ORCH_ENFORCE_ISOLATION` line already in its env file; an existing env file without the line gets an explicit `false`, so its behaviour does not change; a value set in `setup.env` takes precedence. In the legacy mode, when a task declares no task environment, the worker passes its own environment to the task process, including any credentials the worker holds. Keep `ORCH_ENFORCE_ISOLATION=true` on every worker that runs code you do not fully trust, and recreate the worker with `make up client` after changing it.
- **The egress allowlist is signed; egress isolation is still not a full boundary.** The per-task egress allowlist is declared by the task author. The worker signs it with HMAC-SHA256 under a per-worker key (a Podman secret that task code cannot read except through a concurrent `podman exec`, see above), and the egress proxy ignores the per-task entries of a list without a valid signature, so a task cannot add a host to its own allowlist. A task can still delete or corrupt its own list, which only removes its own egress, and can replay a signed list of an earlier task on the same worker: the signature covers the list of hosts, not the task, so a replayed list gives the current task the hosts that the earlier task declared, even if the current task did not declare them. A proxy started without the key accepts an unsigned list and logs a warning; `make up client` creates the key. Because of the shared uid above, a task can also read the worker credential in `state.json` and use it against the server.

### Server deployment

- **The application does not rate-limit requests in general.** The only built-in limit covers the anonymous passkey routes (login and invitation registration, begin and complete): a per-client and a per-network token bucket held in the memory of each server process (`AUTH_BEGIN_RATE_PER_MINUTE`, `AUTH_BEGIN_RATE_BURST`, `AUTH_BEGIN_GROUP_RATE_PER_MINUTE` and `AUTH_BEGIN_GROUP_BURST` in `server/orcaster_server/config.py`). With several server processes the effective limit is multiplied, and a restart resets it. Every other route, including worker registration and the token API, is not limited. Put a rate limit in front of the application, in the reverse proxy or the load balancer.
- **Request bodies are bounded in two places.** The application enforces a body-size limit per route (`server/orcaster_server/body_limit.py`), and the reverse-proxy configuration generated by `setup-scripts/debian13/06-caddy.sh` sets `request_body max_size` per route. Both reject a body over the limit with HTTP 413: a declared `Content-Length` over the limit is rejected before the body is read, and a streamed body as soon as the bytes read pass the limit. The limits are settings with documented defaults (`MAX_REQUEST_BODY_BYTES`, `MAX_OUTPUT_BODY_BYTES` and the artifact upload limits in `server/orcaster_server/config.py`). If you use another reverse proxy, configure an equivalent limit there. Routes that take a request body read and parse it, within the limit, before they check credentials; this includes the worker-authenticated routes (poll, task completion, output and artifact upload).
- **Development defaults are not production defaults.** `server/orcaster_server/config.py` defaults to `BIND_HOST=0.0.0.0`, `WEBAUTHN_ORIGIN=http://localhost:8000` and `SESSION_COOKIE_SECURE=false`. In production, bind to the loopback interface behind the reverse proxy, set `WEBAUTHN_ORIGIN` to the public HTTPS origin, and set `SESSION_COOKIE_SECURE=true`.

## Security Updates

Security updates are released as new versions and announced via:

- GitHub Security Advisories
- Release notes

## Contact

For security-related questions or concerns:

- **Email**: security@ainvirion.com

---

Thank you for helping keep AInvirion LLC and our users safe!
