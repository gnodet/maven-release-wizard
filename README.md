# maven-release-wizard

Interactive TUI wizard for releasing Apache Maven shared components and plugins.

## Features

- **Interactive wizard** — project/version picker, step checkboxes, confirm between steps, retry/skip/quit on failure
- **Non-interactive / CI mode** — `--no-tty --project ... --version ... --all`
- **ATR integration** — starts vote and sends announce email via the [Apache Trusted Releases API](https://releases.apache.org/api/docs)
- **Auto-installs deps** — creates `~/.maven-release-venv` on first run (no manual venv needed)

## Requirements

- Python 3.9+
- Maven on `PATH` (or set `MAVEN_RELEASE_MVN`)
- GPG signing key
- Apache credentials in `~/.m2/settings.xml` (servers `apache.releases.https` and `apache.atr`)
- GitHub token (`GITHUB_TOKEN` env var, or `gh` CLI logged in)

## Quick start

```bash
# Interactive
python3 maven-release.py

# Non-interactive (CI)
python3 maven-release.py --no-tty --project maven-archiver --version 4.0.0-beta-6 --all

# Single step
python3 maven-release.py --no-tty --project maven-archiver --version 4.0.0-beta-6 --step 5-vote
```

## Steps

| Step | Name | Description |
|------|------|-------------|
| `0-prereqs` | Prerequisites | Check GPG, Nexus, gitbox creds, ATR config |
| `1-prepare` | release:prepare | Dry run + real prepare, tag push |
| `2-perform` | release:perform | Checkout, deploy to Nexus + ATR |
| `3-nexus` | Nexus | Close staging repo |
| `4-site` | Site | Build and stage site |
| `5-vote` | Vote | Create release notes Gist + start ATR vote |
| `6-finish` | Finish | Promote Nexus, publish GitHub release, site swap |
| `6-announce` | Announce | Send ANNOUNCE email via ATR API |

## Configuration

### `~/.m2/settings.xml`

```xml
<servers>
  <!-- Nexus deploy -->
  <server>
    <id>apache.releases.https</id>
    <username>YOUR_APACHE_ID</username>
    <password>YOUR_APACHE_PASSWORD</password>
  </server>
  <!-- ATR API PAT -->
  <server>
    <id>apache.atr</id>
    <username>YOUR_APACHE_ID</username>
    <password>YOUR_ATR_PAT</password>
  </server>
</servers>
```

Get your ATR PAT at <https://releases.apache.org/api/docs> (login → Profile → Personal Access Token).

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MAVEN_RELEASE_MVN` | `mvn` (from PATH) | Maven executable |
| `MAVEN_RELEASE_SETTINGS` | `~/.m2/settings.xml` | Maven settings file |
| `MAVEN_RELEASE_REPOS` | `~/src` | Directory where repos are cloned |
| `MAVEN_RELEASE_GH_TOKEN` | `~/.config/gh/hosts.yml` | GitHub token file |
| `GITHUB_TOKEN` | — | GitHub token (takes precedence) |
| `JAVA_HOME` | — | JDK to use |
| `GNUPGHOME` | `~/.gnupg` | GPG home directory |
| `GPG_PASSPHRASE_FILE` | — | Path to file containing GPG passphrase (non-interactive mode) |

### `.asf.yaml` requirements

Your project's `.asf.yaml` must have the full ATR `project:` section.
See [maven-script-interpreter](https://github.com/apache/maven-script-interpreter/blob/master/.asf.yaml) for the reference pattern.

## Adding a new project

Edit the `PROJECTS` dict at the top of `maven-release.py`:

```python
PROJECTS = {
    "maven-my-plugin": {
        "category":              "plugins",   # or "shared"
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven My Plugin",
        "release_drafter_wf_id": 12345678,    # GitHub Actions workflow ID
    },
    ...
}
```

Find the Release Drafter workflow ID:
```bash
gh api /repos/apache/maven-my-plugin/actions/workflows | jq '.workflows[] | select(.name=="Release Drafter") | .id'
```

## License

Apache License 2.0
