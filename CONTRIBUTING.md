# Contributing to Orcaster

Thank you for your interest in contributing to Orcaster! We welcome contributions from the community.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [How to Contribute](#how-to-contribute)
- [Development Workflow](#development-workflow)
- [Coding Standards](#coding-standards)
- [Commit Guidelines](#commit-guidelines)
- [Pull Request Process](#pull-request-process)
- [How Accepted Pull Requests Are Merged](#how-accepted-pull-requests-are-merged)
- [Contributor License Agreement](#contributor-license-agreement)

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment. We expect all contributors to:

- Be respectful and constructive
- Welcome newcomers and help them get started
- Focus on what is best for the community
- Show empathy towards other community members

## Getting Started

### Toolchain

| Component | Directory | Requirement |
|---|---|---|
| Server | `server/` | Python 3.13+ and [uv](https://docs.astral.sh/uv/); Podman (or `TEST_DATABASE_URL` pointing at a PostgreSQL 16+ database) for the integration tests |
| Worker | `client/` | Python 3.13+ and uv; Podman 4.4+ in rootless mode to build and run the worker container |
| CLI | `orcaster/` | Go 1.26.3 or later (the version declared in `orcaster/go.mod`) |
| Local database | | Podman (the `make db-up` target runs PostgreSQL in a container) |

GNU Make drives the common tasks from the repository root.

### Setup

1. **Fork the repository** on GitHub.
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/your-username/orcaster.git
   cd orcaster
   ```
3. **Add upstream remote**:
   ```bash
   git remote add upstream https://github.com/AInvirion/orcaster.git
   ```
4. **Install dependencies** (the `dev` extra provides pytest and ruff):
   ```bash
   (cd server && uv sync --extra dev)
   (cd client && uv sync --extra dev)
   (cd orcaster && go mod download)
   ```
5. **Create a branch** for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## How to Contribute

### Reporting Bugs

Before creating a bug report:
- Check the [existing issues](https://github.com/AInvirion/orcaster/issues) to avoid duplicates
- Collect information about the bug (version, OS, error messages, etc.)

When filing a bug report, include:
- Clear title and description
- Steps to reproduce
- Expected vs actual behavior
- Screenshots (if applicable)
- Environment details

### Suggesting Enhancements

Enhancement suggestions are welcome! Please:
- Check existing issues first
- Provide a clear use case
- Explain why this enhancement would be useful
- Consider backward compatibility

### Code Contributions

We welcome code contributions for:
- Bug fixes
- New features
- Performance improvements
- Documentation improvements
- Test coverage

## Development Workflow

1. **Sync with upstream**:
   ```bash
   git fetch upstream
   git checkout main
   git merge upstream/main
   ```

2. **Create a feature branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes** following our coding standards

4. **Write tests** for new functionality

5. **Run tests** to ensure nothing breaks:
   ```bash
   # Server and worker (run in server/ and in client/)
   uv run python -m pytest

   # CLI (run in orcaster/)
   go test ./...
   ```
   The same from the repository root: `make test server`, `make test client`, `make test orcaster`.

   The server tests start a throw-away PostgreSQL 17 container with Podman (or Docker) when `TEST_DATABASE_URL` is not set. Without either, the database tests are skipped, so a run that reports skips has not tested the database layer.

6. **Run linter**:
   ```bash
   # Server and worker (run in server/ and in client/)
   uv run ruff check .
   uv run ruff format --check .

   # CLI (run in orcaster/); gofmt -l must print nothing
   gofmt -l .
   go vet ./...
   ```
   The same from the repository root: `make lint server`, `make lint client`, `make lint orcaster`. `make fmt <component>` applies the formatter.

7. **Commit your changes** following commit guidelines

8. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

9. **Open a Pull Request** on GitHub

## Coding Standards

### General Guidelines

- Write clear, readable, and maintainable code
- Follow existing code style and patterns
- Add comments for complex logic
- Keep functions small and focused
- Avoid code duplication

### Language-Specific Standards

- **Python** (`server/`, `client/`): code must pass `ruff check` and `ruff format --check` with the configuration in each component's `pyproject.toml`. Continuous integration runs both.
- **Go** (`orcaster/`): code must be formatted with `gofmt` and pass `go vet ./...`. `golangci-lint` runs as part of `make lint orcaster` when it is installed.

### Testing

- Write unit tests for new functionality
- Maintain or improve test coverage
- Ensure all tests pass before submitting PR
- Include integration tests where appropriate

## Commit Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation changes
- **style**: Code style changes (formatting, missing semicolons, etc.)
- **refactor**: Code refactoring
- **perf**: Performance improvements
- **test**: Adding or updating tests
- **chore**: Maintenance tasks

### Examples

```
feat(auth): add OAuth2 authentication

Implement OAuth2 authentication flow using the authorization code grant.
Includes token refresh and revocation.

Closes #<issue-number>
```

```
fix(api): handle null response in user endpoint

Add null check to prevent crash when API returns empty response.

Fixes #<issue-number>
```

## Pull Request Process

1. **Update documentation** if needed
2. **Add tests** for new functionality
3. **Ensure CI passes** (all tests and linting)
4. **Update CHANGELOG.md** with your changes
5. **Request review** from maintainers
6. **Address feedback** promptly and professionally
7. **Squash commits** if requested (we may do this when merging)

### PR Checklist

- [ ] Code follows project style guidelines
- [ ] Tests added/updated and passing
- [ ] Documentation updated
- [ ] Commits follow commit guidelines
- [ ] No breaking changes (or documented if necessary)
- [ ] CHANGELOG.md updated

## How Accepted Pull Requests Are Merged

Orcaster is developed in this repository. Once a pull request is approved and CI passes, a maintainer merges it on GitHub, usually as a squash merge. The change ships with the next release, and the contribution is credited in `CHANGELOG.md`.

## Contributor License Agreement

Every contributor signs the [Contributor License Agreement](CLA.md) (CLA) before their first pull request can be accepted. The CLA is based on the Apache Software Foundation Individual CLA; AInvirion LLC is the recipient.

When you open your first pull request, the CLA bot comments on it. To sign, reply to the pull request with this exact statement:

> I have read the Contributor License Agreement and I hereby accept the terms.

Your GitHub login is then recorded in `.clabot`, and the CLA covers all your later contributions. If you contribute on behalf of an employer, check section 4 of the CLA first. Questions about the CLA go to legal@ainvirion.com.

## Questions?

If you have questions about contributing, please:
- Check existing documentation
- Search closed issues
- Open an issue with your question
- Contact us at contact@ainvirion.com

---

Thank you for contributing to Orcaster!
