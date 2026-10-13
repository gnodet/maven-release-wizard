#!/usr/bin/env python3
"""
Generic Maven shared-component / plugin release wizard.

Interactive (default):
    python3 maven-release.py

Non-interactive (CI / scripting):
    python3 maven-release.py --no-tty --project maven-archiver --version 4.0.0-beta-6 --all
    python3 maven-release.py --no-tty --project maven-jar-plugin --version 4.0.0-beta-2 --step 0-prereqs

Steps (run in order):
    0-prereqs    Check prerequisites (GPG, Nexus, gitbox creds, ATR config)
    1-prepare    release:prepare (dry run + real)
    2-perform    release:perform + deploy to Nexus + upload to ATR
    3-nexus      Close the Nexus staging repo
    4-site       Build and deploy the staged site
    5-vote       Generate release notes Gist + start ATR vote
    6-finish     Post-vote: promote Nexus, publish GitHub release, site swap
    6-announce   Send ANNOUNCE email via ATR API

Project registry (add new projects in the PROJECTS dict below).
"""

import sys, os, re, subprocess, json, urllib.request, urllib.parse, argparse, time, shutil
from pathlib import Path

# ── Project registry ────────────────────────────────────────────────────────────
PROJECTS = {
    "maven-archiver": {
        "category":              "shared",
        "groupId":               "org.apache.maven.shared",
        "display_name":          "Apache Maven Archiver",
        "release_drafter_wf_id": 161489861,
    },
    "maven-jar-plugin": {
        "category":              "plugins",
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven JAR Plugin",
        "release_drafter_wf_id": 19931017,
    },
    "maven-filtering": {
        "category":              "shared",
        "groupId":               "org.apache.maven.shared",
        "display_name":          "Apache Maven Filtering",
        "release_drafter_wf_id": 30160804,
    },
    "maven-resources-plugin": {
        "category":              "plugins",
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven Resources Plugin",
        "release_drafter_wf_id": 27273008,
    },
    "maven-clean-plugin": {
        "category":              "plugins",
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven Clean Plugin",
        "release_drafter_wf_id": 137478710,
    },
    "maven-compiler-plugin": {
        "category":              "plugins",
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven Compiler Plugin",
        "release_drafter_wf_id": 19504154,
    },
    "maven-install-plugin": {
        "category":              "plugins",
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven Install Plugin",
        "release_drafter_wf_id": 142127453,
    },
    "maven-deploy-plugin": {
        "category":              "plugins",
        "groupId":               "org.apache.maven.plugins",
        "display_name":          "Apache Maven Deploy Plugin",
        "release_drafter_wf_id": 31852205,
    },
}

# ── Toolchain — all overridable via env vars ────────────────────────────────────
#
# Defaults work on a standard Mac/Linux dev machine with Maven and GPG on PATH.
# Override as needed, e.g.:
#
#   export MAVEN_RELEASE_MVN=/opt/homebrew/bin/mvn
#   export MAVEN_RELEASE_REPOS=~/code          # where repos are cloned (default: ~/src)
#   export MAVEN_RELEASE_GH_TOKEN=~/.config/gh/token
#   export GNUPGHOME=~/.gnupg
#   export JAVA_HOME=/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home

HOME      = os.path.expanduser("~")
MVN       = os.environ.get("MAVEN_RELEASE_MVN",      shutil.which("mvn") or "mvn")
JAVA_HOME = os.environ.get("JAVA_HOME",              "")
GNUPGHOME = os.environ.get("GNUPGHOME",              os.path.join(HOME, ".gnupg"))
SETTINGS  = os.environ.get("MAVEN_RELEASE_SETTINGS", os.path.join(HOME, ".m2", "settings.xml"))
REPOS_DIR = os.environ.get("MAVEN_RELEASE_REPOS",    os.path.join(HOME, "src"))

_gh_token_path = os.environ.get(
    "MAVEN_RELEASE_GH_TOKEN",
    os.path.join(HOME, ".config", "gh", "hosts.yml"),
)
GH_TOKEN_FILE = Path(_gh_token_path)

BASE_ENV = {
    **os.environ,
    "GNUPGHOME": GNUPGHOME,
    "MAVEN_OPTS": "-Xmx2g",
    **({"JAVA_HOME": JAVA_HOME} if JAVA_HOME else {}),
}

# ── Runtime context ─────────────────────────────────────────────────────────────
CTX = {}

def ctx():
    return CTX


# ── TUI helpers (rich + questionary, graceful fallback) ────────────────────────

def _ensure_deps():
    """
    Ensure rich and questionary are importable.

    Strategy (macOS Homebrew Python is PEP 668 — both plain and --user installs are blocked):
      1. If already importable → done.
      2. If running inside a venv we created (~/.maven-release-venv) → pip install normally.
      3. Otherwise: create ~/.maven-release-venv with uv (preferred) or python3 -m venv,
         then re-exec this script inside that venv so all imports work.
    """
    missing = []
    try:
        import rich  # noqa: F401
    except ImportError:
        missing.append("rich")
    try:
        import questionary  # noqa: F401
    except ImportError:
        missing.append("questionary")
    if not missing:
        return

    venv_dir = Path(HOME) / ".maven-release-venv"
    venv_python = venv_dir / "bin" / "python3"

    # If we're already inside our venv (either via sys.prefix or VIRTUAL_ENV env), just install and return
    if sys.prefix == str(venv_dir) or os.environ.get("VIRTUAL_ENV") == str(venv_dir):
        subprocess.check_call(
            [str(venv_python), "-m", "pip", "install", "--quiet"] + missing
        )
        return

    # Create the venv if it doesn't exist yet
    if not venv_python.exists():
        print(f"Creating ~/.maven-release-venv ...")
        uv = shutil.which("uv")
        if uv:
            subprocess.check_call([uv, "venv", str(venv_dir), "--quiet"])
        else:
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])

    # Install missing packages into the venv
    print(f"Installing {', '.join(missing)} into ~/.maven-release-venv ...")
    uv = shutil.which("uv")
    if uv:
        subprocess.check_call(
            [uv, "pip", "install", "--quiet", "--python", str(venv_python)] + missing
        )
    else:
        subprocess.check_call(
            [str(venv_python), "-m", "pip", "install", "--quiet"] + missing
        )

    # Re-launch inside the venv as a subprocess (not execv — execv silently
    # fails when stdin/stdout are weird, e.g. bash process substitution).
    script_path = os.path.abspath(sys.argv[0]) if sys.argv else ""
    if not script_path or not os.path.isfile(script_path):
        import tempfile, urllib.request as _ur
        stable = os.path.join(tempfile.gettempdir(), "maven-release.py")
        url = ("https://raw.githubusercontent.com/gnodet/maven-release-wizard"
               "/main/maven-release.py")
        print("Re-fetching script from GitHub ...")
        _ur.urlretrieve(url, stable)
        script_path = stable
    print("Re-launching inside venv...")
    # Pass VIRTUAL_ENV so the child knows it's already inside our venv
    child_env = {**os.environ, "VIRTUAL_ENV": str(venv_dir),
                 "PATH": str(venv_dir / "bin") + os.pathsep + os.environ.get("PATH", "")}
    r = subprocess.run([str(venv_python), script_path] + sys.argv[1:], env=child_env)
    sys.exit(r.returncode)


NO_TTY = False   # set to True by --no-tty


def console():
    from rich.console import Console
    return Console()


def tui_header():
    from rich.panel import Panel
    from rich.text import Text
    c = console()
    t = Text("\U0001f680 Maven Release Wizard", style="bold cyan")
    c.print(Panel(t, border_style="cyan"))


def tui_step_banner(name, desc):
    from rich import print as rprint
    rprint(f"\n[bold cyan]\u2550\u2550\u2550 Step {name}: {desc} \u2550\u2550\u2550[/bold cyan]")


def tui_ok(msg):
    from rich import print as rprint
    rprint(f"  [green]\u2705[/green] {msg}")


def tui_warn(msg):
    from rich import print as rprint
    rprint(f"  [yellow]\u26a0\ufe0f [/yellow]  {msg}")


def tui_err(msg):
    from rich import print as rprint
    rprint(f"  [red]\u274c[/red] {msg}")


def tui_link(label, url):
    from rich import print as rprint
    rprint(f"  [bold]\u2192[/bold] [blue underline]{url}[/blue underline]  [dim]{label}[/dim]")


def confirm_continue(step_name, elapsed=None):
    """Ask user whether to continue to the next step. In --no-tty mode, always continues."""
    if NO_TTY:
        return True
    import questionary
    from rich import print as rprint
    elapsed_str = f"  [dim]({elapsed:.0f}s)[/dim]" if elapsed else ""
    rprint(f"\n[green]\u2705 Step '{step_name}' complete.[/green]{elapsed_str}")
    return questionary.confirm("Continue to next step?", default=True).ask()


def confirm_action(prompt, default=True):
    """Simple yes/no. In --no-tty mode, returns the default."""
    if NO_TTY:
        return default
    import questionary
    return questionary.confirm(prompt, default=default).ask()


def prompt_input(prompt, default=""):
    """Text input. In --no-tty mode, returns the default."""
    if NO_TTY:
        return default
    import questionary
    return questionary.text(prompt, default=default).ask()


def prompt_select(prompt, choices):
    """Single-select. In --no-tty mode, returns first choice."""
    if NO_TTY:
        return choices[0]
    import questionary
    return questionary.select(prompt, choices=choices).ask()


def on_step_failure(step_name, exc):
    """
    Called when a step raises. In --no-tty: re-raise.
    Interactive: offer retry / skip / quit.
    """
    if NO_TTY:
        raise exc
    import questionary
    from rich import print as rprint
    rprint(f"\n[red bold]\u274c Step '{step_name}' failed:[/red bold] {exc}")
    log_dir = CTX.get("log_dir")
    if log_dir:
        rprint(f"   Logs: [dim]{log_dir}/[/dim]")
    action = questionary.select(
        "What do you want to do?",
        choices=["retry", "skip", "quit"],
    ).ask()
    return action   # caller handles


# ── Apache / GitHub credential helpers ────────────────────────────────────────

def read_apache_id():
    settings = Path(SETTINGS).read_text()
    m = re.search(r"apache\.releases\.https.*?<username>([^<]+)", settings, re.DOTALL)
    return m.group(1).strip() if m else os.environ.get("APACHE_ID", "")


def read_apache_password():
    settings = Path(SETTINGS).read_text()
    m = re.search(r"apache\.releases\.https.*?<password>([^<]+)", settings, re.DOTALL)
    if not m:
        raise SystemExit("\u274c Cannot find apache.releases.https password in settings.xml")
    return m.group(1)


def read_atr_pat():
    settings = Path(SETTINGS).read_text()
    m = re.search(r"apache\.atr.*?<password>([^<]+)", settings, re.DOTALL)
    if not m:
        raise SystemExit(
            "\u274c No ATR PAT found in settings.xml. Add:\n"
            "   <server><id>apache.atr</id><username>YOUR_ASF_ID</username>"
            "<password>YOUR_ATR_PAT</password></server>"
        )
    return m.group(1).strip()


def gh_token():
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and GH_TOKEN_FILE.exists():
        text = GH_TOKEN_FILE.read_text()
        if not text.strip().startswith("hosts:"):
            token = text.strip()
        else:
            m = re.search(r"oauth_token:\s*(\S+)", text)
            token = m.group(1) if m else ""
    if not token:
        raise SystemExit(
            "\u274c No GitHub token. Set GITHUB_TOKEN or MAVEN_RELEASE_GH_TOKEN."
        )
    return token


def gh_api(path, method="GET", data=None):
    url = f"https://api.github.com{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"token {gh_token()}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req) as r:
        return json.load(r) if r.status != 204 else {}


_atr_jwt_cache = {}

def atr_jwt():
    now = time.time()
    if _atr_jwt_cache.get("expires", 0) > now:
        return _atr_jwt_cache["token"]
    apache_id = read_apache_id()
    pat = read_atr_pat()
    req = urllib.request.Request(
        "https://releases.apache.org/api/jwt/create",
        data=json.dumps({"asfuid": apache_id, "pat": pat}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        data = json.load(r)
    token = data.get("token") or data.get("jwt") or data.get("access_token")
    if not token:
        raise SystemExit(f"\u274c ATR JWT exchange failed: {data}")
    _atr_jwt_cache["token"] = token
    _atr_jwt_cache["expires"] = now + 25 * 60
    return token


def atr_api(path, data=None, method=None):
    jwt = atr_jwt()
    if method is None:
        method = "POST" if data is not None else "GET"
    url = f"https://releases.apache.org/api{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt}",
    })
    with urllib.request.urlopen(req) as r:
        return json.load(r) if r.status not in (204, 201) else {}


def nexus_curl(path, method="GET", data=None, pw=None):
    import base64
    pw = pw or read_apache_password()
    apache_id = read_apache_id()
    auth = base64.b64encode(f"{apache_id}:{pw}".encode()).decode()
    url = f"https://repository.apache.org{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req) as r:
        return json.load(r) if r.status != 204 else {}


# ── Shell helper ───────────────────────────────────────────────────────────────

def run(cmd, cwd=None, check=True, log_file=None, env=None):
    c = ctx()
    log_dir = c.get("log_dir", Path("/tmp"))
    log_dir.mkdir(parents=True, exist_ok=True)
    from rich import print as rprint
    rprint(f"[dim]\u25b6 {' '.join(str(x) for x in cmd)}[/dim]")
    if log_file:
        rprint(f"[dim]  \u2192 {log_file}[/dim]")
    result = subprocess.run(
        cmd,
        cwd=cwd or c.get("repo_dir"),
        env=env or c.get("env", BASE_ENV),
        capture_output=(log_file is not None),
        text=True,
    )
    if log_file:
        Path(log_file).write_text(result.stdout + result.stderr)
        if result.returncode != 0:
            print(result.stdout[-3000:])
            print(result.stderr[-1000:])
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(str(x) for x in cmd)}")
    return result


def gh_pom_version(project, branch="HEAD"):
    """Fetch pom.xml from GitHub and return the project version (skips parent block)."""
    repo = f"apache/{project}"
    try:
        data = gh_api(f"/repos/{repo}/contents/pom.xml?ref={branch}")
        import base64
        text = base64.b64decode(data["content"]).decode()
        text_no_parent = re.sub(r"<parent>.*?</parent>", "", text, flags=re.DOTALL)
        m = re.search(r"<version>([^<]+)</version>", text_no_parent)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def find_draft_release(repo, version):
    releases = gh_api(f"/repos/{repo}/releases?per_page=20")
    tag = f"{repo.split('/')[-1]}-{version}"
    for rel in releases:
        if rel.get("draft") and (rel["tag_name"] == tag or rel["name"] == version):
            return rel["id"]
    for rel in releases:
        if rel.get("draft") and version in (rel.get("name") or ""):
            return rel["id"]
    return None


def get_gpg_passphrase():
    if not NO_TTY:
        import questionary
        return questionary.password("GPG key passphrase:").ask()
    p = Path(os.environ.get("GPG_PASSPHRASE_FILE", "/tmp/gpg-pass.txt"))
    if p.exists():
        return p.read_text().strip()
    raise SystemExit(
        "\u274c GPG passphrase not available. Set GPG_PASSPHRASE_FILE env var "
        "pointing to a file containing the passphrase."
    )


# ── Step 0: Prerequisites ──────────────────────────────────────────────────────

def step_prereqs():
    c = ctx()
    tui_step_banner("0", "Prerequisites")
    errors = []

    # 1. Clone or sync repo
    if not c["repo_dir"].exists():
        tui_warn(f"Repo not found locally \u2014 cloning {c['repo']}...")
        Path(REPOS_DIR).mkdir(parents=True, exist_ok=True)
        run(["git", "clone", f"https://github.com/{c['repo']}.git", str(c["repo_dir"])],
            cwd=Path(REPOS_DIR))
    else:
        print("1. Syncing local repo...")
        run(["git", "checkout", c["branch"]])
        run(["git", "pull", "origin", c["branch"]])
    tui_ok("Repo ready")

    # 2. Check .asf.yaml
    print("\n2. Checking .asf.yaml for ATR config...")
    asf_yaml = (c["repo_dir"] / ".asf.yaml").read_text()
    if "atr_sync" not in asf_yaml:
        errors.append(".asf.yaml missing 'atr_sync: true'")
        tui_err(".asf.yaml missing 'atr_sync: true'")
        print(f"""   Add to .asf.yaml:
   project:
     metadata:
       key: {c['project']}
       committee: maven
     policy:
       github_repository_name: {c['project']}
       download_path_suffix: {c['category']}
     features:
       atr_sync: true""")
    else:
        tui_ok("atr_sync found in .asf.yaml")

    # 3. Check settings.xml for apache.atr
    print("\n3. Checking settings.xml for ATR PAT...")
    settings_txt = Path(SETTINGS).read_text()
    apache_id = read_apache_id()
    if "apache.atr" not in settings_txt:
        errors.append("settings.xml missing <server id='apache.atr'>")
        tui_err("settings.xml missing apache.atr server")
        print(f"   Add: <server><id>apache.atr</id>"
              f"<username>{apache_id or 'YOUR_APACHE_ID'}</username>"
              f"<password>YOUR_ATR_PAT</password></server>")
    else:
        tui_ok("apache.atr server configured")

    # 4. GPG key
    print("\n4. Checking GPG signing key...")
    result = subprocess.run(
        ["gpg", "--list-secret-keys", "--keyid-format=long"],
        env={**BASE_ENV, "GNUPGHOME": GNUPGHOME}, capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        tui_ok("GPG signing key present")
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            print(f"   {lines[1]}")
    else:
        errors.append("No GPG secret key found")
        tui_err("No GPG secret key \u2014 import your key first")

    # 5. Nexus clean
    print("\n5. Checking Nexus for stale staging repos...")
    try:
        repos = nexus_curl("/service/local/staging/profile_repositories")
        open_repos = [r for r in repos.get("data", []) if r["type"] == "open"]
        if open_repos:
            tui_warn(f"Found {len(open_repos)} open staging repo(s) \u2014 drop them first:")
            for r in open_repos:
                print(f"   {r['repositoryId']} (updated: {r.get('updatedDate', '')})")
            errors.append(f"{len(open_repos)} stale Nexus staging repo(s)")
        else:
            tui_ok("Nexus is clean")
    except Exception as e:
        tui_warn(f"Could not check Nexus: {e}")

    # 6. Gitbox credential
    print("\n6. Setting up gitbox credential...")
    try:
        apache_pass = read_apache_password()
        encoded = urllib.parse.quote(apache_pass, safe="")
        store = Path(f"{HOME}/.git-credentials")
        lines = store.read_text().splitlines(keepends=True) if store.exists() else []
        lines = [l for l in lines if "gitbox.apache.org" not in l]
        lines.append(f"https://{apache_id}:{encoded}@gitbox.apache.org\n")
        store.write_text("".join(lines))
        store.chmod(0o600)
        run(["git", "config", "--local", "credential.https://gitbox.apache.org.helper",
             f"store --file={store}"])
        r = run(["git", "ls-remote",
                 f"https://gitbox.apache.org/repos/asf/{c['project']}.git", "HEAD"],
                check=False)
        if r.returncode == 0:
            tui_ok("gitbox credential OK")
        else:
            errors.append("gitbox credential verification failed")
            tui_err("gitbox credential verification failed")
    except Exception as e:
        tui_warn(f"Could not set up gitbox credential: {e}")

    if errors:
        tui_err(f"Prerequisites FAILED ({len(errors)} issue(s)):")
        for e in errors:
            print(f"   \u2022 {e}")
        sys.exit(1)
    tui_ok("All prerequisites passed!")


# ── Step 1: release:prepare ────────────────────────────────────────────────────

def step_prepare():
    c = ctx()
    tui_step_banner("1", "release:prepare")

    from rich.table import Table
    from rich.console import Console
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("Project",      c["display_name"])
    t.add_row("Version",      c["version"])
    t.add_row("Next version", c["next_version"])
    t.add_row("Tag",          c["tag"])
    t.add_row("Repo",         str(c["repo_dir"]))
    Console().print(t)

    if not confirm_action("Proceed with release:prepare?"):
        print("Aborted.")
        sys.exit(0)

    print("\n1. Disabling Release Drafter workflow...")
    gh_api(f"/repos/{c['repo']}/actions/workflows/{c['release_drafter_wf_id']}/disable",
           method="PUT")
    tui_ok("Release Drafter disabled")

    print("\n2. Starting GPG agent...")
    subprocess.run(["gpgconf", "--homedir", GNUPGHOME, "--kill", "gpg-agent"],
                   env=c["env"], capture_output=True)
    subprocess.run(["gpg-agent", "--homedir", GNUPGHOME, "--daemon"],
                   env=c["env"], capture_output=True)

    gpg_pass = get_gpg_passphrase()
    common = [f"-Dmaven.repo.local={c['local_repo']}",
              f"-Darguments=-Dmaven.repo.local={c['local_repo']}"
              f" -Dgpg.passphrase={gpg_pass} -Dgpg.pinentryMode=loopback"]

    print("\n3. Dry run...")
    run([MVN, "release:clean", "-B", f"-Dmaven.repo.local={c['local_repo']}"])
    run([MVN, "release:prepare", "-B", "-DdryRun=true",
         f"-DreleaseVersion={c['version']}",
         f"-DdevelopmentVersion={c['next_version']}",
         f"-Dtag={c['tag']}"] + common,
        log_file=c["log_dir"] / "prepare-dryrun.log")

    r = subprocess.run(["diff", "pom.xml", "pom.xml.tag"],
                       capture_output=True, text=True, cwd=c["repo_dir"])
    if r.stdout:
        print("Diff pom.xml vs pom.xml.tag:")
        print(r.stdout)

    if not confirm_action("Dry run looks good \u2014 proceed with real release:prepare?"):
        print("Aborted after dry run.")
        sys.exit(0)

    run([MVN, "release:clean", "-B", f"-Dmaven.repo.local={c['local_repo']}"])

    print("\n4. Real release:prepare...")
    run([MVN, "release:prepare", "-B",
         f"-DreleaseVersion={c['version']}",
         f"-DdevelopmentVersion={c['next_version']}",
         f"-Dtag={c['tag']}"] + common,
        log_file=c["log_dir"] / "prepare.log")

    print("\n5. Verifying...")
    run(["git", "log", "--oneline", f"origin/{c['branch']}", "-5"])
    run(["git", "ls-remote", "origin", f"refs/tags/{c['tag']}"])

    tui_ok("release:prepare complete!")


# ── Step 2: release:perform + ATR upload ──────────────────────────────────────

def step_perform():
    c = ctx()
    tui_step_banner("2", "release:perform + ATR upload")
    gpg_pass = get_gpg_passphrase()
    checkout_dir = c["repo_dir"] / "target" / "checkout"

    print("\n1. Running release:perform (checkout phase)...")
    subprocess.run([
        MVN, "release:perform", "-B",
        f"-Dmaven.repo.local={c['local_repo']}",
        f"-Darguments=-Dmaven.repo.local={c['local_repo']}"
        f" -Dgpg.passphrase={gpg_pass} -Dgpg.pinentryMode=loopback",
    ], cwd=c["repo_dir"], env=c["env"], capture_output=True, text=True)

    if not checkout_dir.exists():
        raise RuntimeError("target/checkout not created \u2014 release:perform failed.")

    print("\n2. Creating ATR release version via API...")
    try:
        atr_api("/release/create", {"project": c["project"], "version": c["version"]})
        tui_ok("ATR release version created")
    except Exception as e:
        if any(x in str(e) for x in ["already exists", "409", "duplicate"]):
            tui_ok("ATR release already exists (continuing)")
        else:
            tui_warn(f"ATR release/create: {e} \u2014 will rely on -Datr.createVersion=true")

    print("\n3. Deploying from target/checkout to Nexus + ATR...")
    run([
        MVN, "deploy", "-B",
        f"-Dmaven.repo.local={c['local_repo']}",
        f"-Dgpg.passphrase={gpg_pass}",
        "-Dgpg.pinentryMode=loopback",
        "-s", SETTINGS,
        "-Papache-release,push-to-atr",
        "-Datr.createVersion=true",
    ], cwd=checkout_dir, log_file=c["log_dir"] / "deploy.log")

    source_zip = checkout_dir / "target" / f"{c['project']}-{c['version']}-source-release.zip"
    if source_zip.exists():
        tui_ok(f"{source_zip.name} ({source_zip.stat().st_size:,} bytes)")
    else:
        tui_warn(f"Source zip not found at: {source_zip}")

    tui_ok("Deploy complete!")
    tui_link("Verify ATR",  f"{c['atr_url']}/compose/{c['project']}/{c['version']}")
    tui_link("Check Nexus", "https://repository.apache.org/#stagingRepositories")


# ── Step 3: Close Nexus staging ───────────────────────────────────────────────

def step_nexus():
    c = ctx()
    tui_step_banner("3", "Close Nexus staging repo")
    pw = read_apache_password()

    repos_data = nexus_curl("/service/local/staging/profile_repositories", pw=pw)
    open_repos = [r for r in repos_data.get("data", []) if r["type"] == "open"]

    if not open_repos:
        tui_warn("No open staging repos \u2014 already closed?")
        return

    print(f"   Found {len(open_repos)} open repo(s):")
    for r in open_repos:
        print(f"      {r['repositoryId']} (updated: {r.get('updatedDate', '')})")

    group_path = c["groupId"].replace(".", "/")
    target_repo = None
    for r in open_repos:
        repo_id = r["repositoryId"]
        try:
            content = nexus_curl(
                f"/service/local/repositories/{repo_id}/content"
                f"/{group_path}/{c['project']}/{c['version']}/",
                pw=pw,
            )
            count = len(content.get("data", []))
            print(f"      {repo_id}: {count} artifacts")
            if count >= 10:
                target_repo = repo_id
        except Exception as e:
            print(f"      {repo_id}: error ({e})")

    stale = [r["repositoryId"] for r in open_repos if r["repositoryId"] != target_repo]
    if stale:
        print(f"\n   Dropping stale repos: {stale}")
        nexus_curl("/service/local/staging/bulk/drop", method="POST", pw=pw,
                   data={"data": {"stagedRepositoryIds": stale, "description": "Dropping incomplete"}})

    if not target_repo:
        raise RuntimeError("Could not identify complete staging repo \u2014 check Nexus manually.")

    if not NO_TTY and len(open_repos) > 1:
        import questionary
        target_repo = questionary.select(
            "Which staging repo to close?",
            choices=[r["repositoryId"] for r in open_repos],
            default=target_repo,
        ).ask()

    print(f"\n   Closing {target_repo}...")
    nexus_curl("/service/local/staging/bulk/close", method="POST", pw=pw,
               data={"data": {
                   "stagedRepositoryIds": [target_repo],
                   "description": f"[VOTE] {c['display_name']} {c['version']}",
               }})

    for _ in range(12):
        time.sleep(10)
        info = nexus_curl(f"/service/local/staging/repository/{target_repo}", pw=pw)
        state, transitioning = info.get("type", "?"), info.get("transitioning", True)
        print(f"   state={state} transitioning={transitioning}")
        if state == "closed" and not transitioning:
            break

    (c["log_dir"] / "nexus-repo-id.txt").write_text(target_repo)
    tui_ok(f"Nexus staging repo closed: {target_repo}")
    tui_link("Nexus repo", f"https://repository.apache.org/content/repositories/{target_repo}/")


# ── Step 4: Site staging ──────────────────────────────────────────────────────

def step_site():
    c = ctx()
    tui_step_banner("4", "Site staging")
    checkout_dir = c["repo_dir"] / "target" / "checkout"
    if not checkout_dir.exists():
        raise RuntimeError("target/checkout not found \u2014 run step 2 first.")

    archives = f"{c['category']}-archives"

    print("\n1. Building and publishing staged site...")
    run([MVN, "site", "site-deploy", "-B"], cwd=checkout_dir,
        log_file=c["log_dir"] / "site.log")

    print("\n2. SVN archive: LATEST \u2192 versioned...")
    svn_base = (f"https://svn.apache.org/repos/infra/websites/production"
                f"/maven/content/{archives}")
    run(["svn", "copy",
         f"{svn_base}/{c['project']}-LATEST",
         f"{svn_base}/{c['project']}-{c['version']}",
         "-m", f"Publishing {c['project']}-{c['version']} staged site"])

    tui_ok("Site staged!")
    tui_link("Staged site",
             f"https://maven.apache.org/{archives}/{c['project']}-{c['version']}/")


# ── Step 5: Vote ──────────────────────────────────────────────────────────────

def step_vote():
    c = ctx()
    tui_step_banner("5", "Vote email + ATR vote start")

    repo_id_file = c["log_dir"] / "nexus-repo-id.txt"
    staging_id = (repo_id_file.read_text().strip() if repo_id_file.exists()
                  else prompt_input("Enter Nexus staging repo ID (e.g. maven-1234):", ""))

    checkout_dir = c["repo_dir"] / "target" / "checkout"
    sha_file = checkout_dir / "target" / f"{c['project']}-{c['version']}-source-release.zip.sha512"
    sha512 = (sha_file.read_text().strip().split()[0] if sha_file.exists()
              else prompt_input("Enter SHA-512 of source-release.zip:", ""))

    print("\n1. Fetching draft release notes...")
    draft_id = c.get("draft_release_id") or find_draft_release(c["repo"], c["version"])
    if not draft_id:
        tui_warn("No draft release found \u2014 using empty release notes")
        body = ""
    else:
        rel = gh_api(f"/repos/{c['repo']}/releases/{draft_id}")
        body = rel.get("body") or ""

    PLACEHOLDER = "<!-- Optional: add a release summary here -->"
    gist_body = body.replace(PLACEHOLDER, "").strip()
    gist_body = f"# {c['display_name']} {c['version']}\n\n{gist_body}\n"

    if not NO_TTY:
        print("\n   Release notes preview (first 20 lines):")
        for line in gist_body.splitlines()[:20]:
            print(f"   {line}")
        if not confirm_action("Use these release notes for the Gist?"):
            gist_body = prompt_input("Paste your release notes (one line):", gist_body)

    print("\n2. Creating public Gist...")
    token = gh_token()
    req = urllib.request.Request(
        "https://api.github.com/gists",
        data=json.dumps({
            "description": f"{c['display_name']} {c['version']} \u2014 Release Vote",
            "public": True,
            "files": {f"{c['project']}-{c['version']}-release-notes.md": {"content": gist_body}},
        }).encode(),
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        gist = json.load(r)
    gist_url = gist["html_url"]
    tui_ok("Gist created")
    tui_link("Release notes", gist_url)

    group_path = c["groupId"].replace(".", "/")
    archives = f"{c['category']}-archives"
    nexus_base = f"https://repository.apache.org/content/repositories/{staging_id}"
    source_url = (f"{nexus_base}/{group_path}/{c['project']}/{c['version']}"
                  f"/{c['project']}-{c['version']}-source-release.zip")
    staged_site = f"https://maven.apache.org/{archives}/{c['project']}-{c['version']}/"
    apache_id = read_apache_id()

    vote_email = f"""To: dev@maven.apache.org
Subject: [VOTE] Release {c['display_name']} version {c['version']}

Hi,

Release notes:
{gist_url}

Staging repo:
{nexus_base}/
{source_url}

Source release checksum(s):
{c['project']}-{c['version']}-source-release.zip sha512: {sha512}

Staging site:
{staged_site}

Guide to testing staged releases:
https://maven.apache.org/guides/development/guide-testing-releases.html

Vote open for at least 72 hours.

[ ] +1
[ ] +0
[ ] -1

-{apache_id}
"""

    vote_file = c["log_dir"] / "vote-email.txt"
    vote_file.write_text(vote_email)

    print("\n3. Starting vote via ATR API...")
    try:
        atr_api("/vote/start", {
            "project": c["project"],
            "version": c["version"],
            "email_to": "dev@maven.apache.org",
            "body": vote_email,
            "automatic_resolve_when_finished": False,
            "automatic_publish_when_resolved": False,
            "notify_when_finished": True,
        })
        tui_ok("Vote started via ATR!")
        tui_link("Monitor vote", f"{c['atr_url']}/release/{c['project']}/{c['version']}")
    except Exception as e:
        tui_warn(f"ATR vote/start failed: {e}")
        tui_link("Start vote manually", f"{c['atr_url']}/compose/{c['project']}/{c['version']}")

    tui_ok(f"Vote email preview saved to {vote_file}")


# ── Step 6: Post-vote finish ──────────────────────────────────────────────────

def step_finish():
    c = ctx()
    tui_step_banner("6", "Post-vote finish")
    archives = f"{c['category']}-archives"
    site_index = "shared/index.md" if c["category"] == "shared" else "plugins/index.md"
    repo_id_file = c["log_dir"] / "nexus-repo-id.txt"
    staging_id = repo_id_file.read_text().strip() if repo_id_file.exists() else "<STAGING_ID>"
    draft_id = find_draft_release(c["repo"], c["version"])

    # 1. Promote Nexus
    if confirm_action(f"Promote Nexus staging repo '{staging_id}'?"):
        try:
            nexus_curl("/service/local/staging/bulk/promote", method="POST",
                       data={"data": {"stagedRepositoryIds": [staging_id],
                                      "description": "Promoting"}})
            tui_ok(f"Nexus repo {staging_id} promoted")
        except Exception as e:
            tui_warn(f"Nexus promote failed: {e}")
            tui_link("Promote manually", "https://repository.apache.org/#stagingRepositories")

    # 2. Publish GitHub Release
    if draft_id and confirm_action("Publish GitHub draft release?"):
        try:
            gh_api(f"/repos/{c['repo']}/releases/{draft_id}", method="PATCH",
                   data={"draft": False, "prerelease": True})
            tui_ok("GitHub release published")
            tui_link("GitHub release", f"https://github.com/{c['repo']}/releases/tag/{c['tag']}")
        except Exception as e:
            tui_warn(f"GitHub release publish failed: {e}")
    elif not draft_id:
        tui_warn("No draft GitHub release found \u2014 publish manually")
        tui_link("GitHub releases", f"https://github.com/{c['repo']}/releases")

    # 3. Re-enable Release Drafter
    if confirm_action("Re-enable Release Drafter workflow?"):
        try:
            gh_api(f"/repos/{c['repo']}/actions/workflows/{c['release_drafter_wf_id']}/enable",
                   method="PUT")
            tui_ok("Release Drafter re-enabled")
        except Exception as e:
            tui_warn(f"Could not re-enable Release Drafter: {e}")

    # 4. SVN site swap
    tui_warn("SVN site swap (LATEST \u2192 live) \u2014 do this manually if ATR doesn't handle it:")
    svn_base = (f"https://svn.apache.org/repos/infra/websites/production"
                f"/maven/content/{archives}")
    print(f"   svn copy {svn_base}/{c['project']}-{c['version']}")
    print(f"            {svn_base}/{c['project']}  -m 'Publish live site'")

    # 5. Maven site index
    tui_warn(f"Update apache/maven-site {site_index}:")
    print(f"   Bump version + date for {c['project']} row")

    # 6. Reporter
    tui_warn("Add release to reporter.apache.org:")
    tui_link("reporter.apache.org", "https://reporter.apache.org/addrelease.html?maven")

    # 7. ANNOUNCE
    if confirm_action("Send [ANNOUNCE] email via ATR API now?", default=False):
        step_announce()
    else:
        tui_warn("Run --step 6-announce when ready to send the ANNOUNCE email.")


def step_announce():
    c = ctx()
    tui_step_banner("6-announce", "Send ANNOUNCE email")
    announce_body = f"""The Apache Maven team is pleased to announce the release of {c['display_name']} version {c['version']}.

Release notes: https://github.com/{c['repo']}/releases/tag/{c['tag']}

Download: https://maven.apache.org/{c['category']}/{c['project']}/download.cgi

Regards,
The Apache Maven Team
"""
    try:
        atr_api("/release/announce", {
            "project": c["project"],
            "version": c["version"],
            "email_to": "announce@apache.org",
            "path_suffix": c["category"],
            "body": announce_body,
        })
        tui_ok("ANNOUNCE sent via ATR!")
    except Exception as e:
        tui_warn(f"ATR release/announce failed: {e}")
        print("   Send ANNOUNCE manually to announce@apache.org")


# ── Steps registry ──────────────────────────────────────────────────────────────

STEPS = {
    "0-prereqs":  (step_prereqs,  "Check prerequisites"),
    "1-prepare":  (step_prepare,  "release:prepare"),
    "2-perform":  (step_perform,  "release:perform + ATR upload"),
    "3-nexus":    (step_nexus,    "Close Nexus staging repo"),
    "4-site":     (step_site,     "Build and stage site"),
    "5-vote":     (step_vote,     "Generate Gist + start ATR vote"),
    "6-finish":   (step_finish,   "Post-vote: promote, publish, announce"),
    "6-announce": (step_announce, "Send ANNOUNCE email via ATR"),
}


# ── Interactive wizard ─────────────────────────────────────────────────────────

def wizard_collect_inputs():
    """Full TUI wizard to collect project, version, and steps to run."""
    import questionary
    from rich.console import Console

    tui_header()

    project = questionary.select(
        "Project:",
        choices=list(PROJECTS.keys()),
    ).ask()
    if not project:
        sys.exit(0)

    meta = PROJECTS[project]
    Console().print(f"  [dim]{meta['display_name']} ({meta['category']})[/dim]")

    # Branch — fetch default + maintenance branches from GitHub
    repo_dir = Path(os.path.join(REPOS_DIR, project))
    Console().print("  [dim]Fetching repo info from GitHub...[/dim]")
    try:
        repo_info = gh_api(f"/repos/apache/{project}")
        default_branch = repo_info.get("default_branch", "master")
        branches_data = gh_api(f"/repos/apache/{project}/branches?per_page=100")
        branch_names = [b["name"] for b in branches_data]
        # Maintenance branches: <anything>-N.x, N.x, N.N.x — not feature/fix/dependabot
        maint_re = re.compile(r'^.+-\d+(\.\d+)*\.x$|^\d+(\.\d+)*\.x$')
        extra = [b for b in branch_names if b != default_branch and maint_re.match(b)]
        branch_choices = [default_branch] + sorted(extra, reverse=True)
    except Exception:
        branch_choices = ["master"]
        default_branch = "master"

    if len(branch_choices) > 1:
        branch = questionary.select(
            "Branch to release from:",
            choices=branch_choices,
        ).ask()
    else:
        branch = questionary.text(
            "Branch to release from:", default=default_branch
        ).ask()
    if not branch:
        sys.exit(0)

    # Infer versions — prefer local pom (fast), fall back to GitHub (not yet cloned)
    pom_version = read_pom_version(repo_dir) or gh_pom_version(project, branch)
    default_release, default_next = infer_versions(pom_version) if pom_version else ("", "")
    if pom_version:
        Console().print(f"  [dim]pom.xml: {pom_version} → release {default_release}, next {default_next}[/dim]")

    version = questionary.text("Release version:", default=default_release).ask()
    if not version:
        sys.exit(0)

    # Re-infer next version from the chosen release version (user may have changed it)
    _, auto_next = infer_versions(version)
    next_version = questionary.text(
        "Next development version:", default=auto_next
    ).ask()
    if not next_version:
        sys.exit(0)

    step_choices = [
        questionary.Choice(f"{k}  \u2014  {desc}", value=k, checked=(k != "6-announce"))
        for k, (_, desc) in STEPS.items()
        if k != "6-announce"
    ]
    selected_steps = questionary.checkbox(
        "Steps to run:",
        choices=step_choices,
    ).ask()
    if selected_steps is None:
        sys.exit(0)

    return project, version, next_version, branch, selected_steps


# ── Main ───────────────────────────────────────────────────────────────────────

def read_pom_version(repo_dir):
    """Read the project <version> from pom.xml in repo_dir (skips <parent> block)."""
    pom = Path(repo_dir) / "pom.xml"
    if not pom.exists():
        return ""
    text = pom.read_text()
    # Strip the <parent>...</parent> block first so we don't pick up the parent version
    text_no_parent = re.sub(r"<parent>.*?</parent>", "", text, flags=re.DOTALL)
    m = re.search(r"<version>([^<]+)</version>", text_no_parent)
    return m.group(1).strip() if m else ""


def infer_versions(pom_version):
    """Given a pom version (with or without -SNAPSHOT), return (release_version, next_dev_version)."""
    v = pom_version.removesuffix("-SNAPSHOT")
    # bump the last numeric segment for the next dev version
    m = re.match(r'^(.*?)(\d+)([^0-9]*)$', v)
    next_v = (f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}-SNAPSHOT"
              if m else f"{v}-SNAPSHOT")
    return v, next_v



def build_context(project, version, next_version, branch="master"):
    meta = PROJECTS[project]
    repo     = f"apache/{project}"
    repo_dir = Path(os.path.join(REPOS_DIR, project))
    tag      = f"{project}-{version}"
    log_dir  = Path(f"/tmp/release-{project}-{version}-logs")
    CTX.update({
        "project":               project,
        "version":               version,
        "next_version":          next_version,
        "tag":                   tag,
        "repo":                  repo,
        "repo_dir":              repo_dir,
        "category":              meta["category"],
        "groupId":               meta["groupId"],
        "display_name":          meta["display_name"],
        "release_drafter_wf_id": meta["release_drafter_wf_id"],
        "branch":                meta.get("default_branch", branch),
        "atr_url":               "https://releases.apache.org",
        "local_repo":            Path(f"{HOME}/.m2/repository-release-{project}-{version}"),
        "log_dir":               log_dir,
        "env":                   BASE_ENV,
        "draft_release_id":      None,
    })
    log_dir.mkdir(parents=True, exist_ok=True)


def run_steps(selected_steps):
    """Run a list of step keys, with inter-step confirmation in interactive mode."""
    for i, key in enumerate(selected_steps):
        fn, desc = STEPS[key]
        t0 = time.time()
        while True:
            try:
                fn()
                elapsed = time.time() - t0
                if i < len(selected_steps) - 1:
                    if not confirm_continue(key, elapsed):
                        print("Stopped.")
                        sys.exit(0)
                break
            except (SystemExit, KeyboardInterrupt):
                raise
            except Exception as exc:
                action = on_step_failure(key, exc)
                if action == "retry":
                    t0 = time.time()
                    continue
                elif action == "skip":
                    tui_warn(f"Skipping step '{key}'")
                    break
                else:
                    sys.exit(1)


def main():
    global NO_TTY

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-tty", action="store_true",
                        help="Non-interactive mode (CI/scripting). Requires --project, "
                             "--version, and --step or --all.")
    parser.add_argument("--project", help="Project name (GitHub repo without 'apache/')")
    parser.add_argument("--version", help="Version to release (e.g. 4.0.0-beta-6)")
    parser.add_argument("--branch", default="master",
                        help="Branch to release from (default: master)")
    parser.add_argument("--next-version", help="Next dev version (auto-inferred if omitted)")
    parser.add_argument("--step", choices=list(STEPS.keys()), help="Run a specific step only")
    parser.add_argument("--all", action="store_true", help="Run all steps in order")
    args = parser.parse_args()

    NO_TTY = args.no_tty or not sys.stdin.isatty()

    if not NO_TTY:
        _ensure_deps()

    if NO_TTY:
        if not args.project or not args.version:
            parser.error("--no-tty requires --project and --version")
        if not args.step and not args.all:
            parser.error("--no-tty requires --step or --all")
        if args.project not in PROJECTS:
            sys.exit(f"\u274c Unknown project '{args.project}'. Known: {', '.join(PROJECTS)}")

        version = args.version
        m = re.match(r'^(.*?)(\d+)([^0-9]*)$', version)
        next_version = (args.next_version or
                        (f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}-SNAPSHOT"
                         if m else f"{version}-SNAPSHOT"))

        build_context(args.project, version, next_version, args.branch)

        if args.step:
            STEPS[args.step][0]()
        else:
            run_steps(list(STEPS.keys()))
    else:
        project, version, next_version, branch, selected_steps = wizard_collect_inputs()

        if project not in PROJECTS:
            sys.exit(f"\u274c Unknown project '{project}'")

        build_context(project, version, next_version, branch)

        c = ctx()
        from rich.table import Table
        from rich.console import Console
        t = Table(title="Release plan", show_header=False, box=None, padding=(0, 2))
        t.add_row("Project",  c["display_name"])
        t.add_row("Branch",   c["branch"])
        t.add_row("Version",  c["version"])
        t.add_row("Next dev", c["next_version"])
        t.add_row("Tag",      c["tag"])
        t.add_row("Repo",     str(c["repo_dir"]))
        t.add_row("Steps",    ", ".join(selected_steps))
        Console().print(t)

        import questionary
        if not questionary.confirm("Start release?", default=True).ask():
            print("Aborted.")
            sys.exit(0)

        run_steps(selected_steps)

        from rich import print as rprint
        rprint("\n[bold green]\U0001f389 All done![/bold green]")


if __name__ == "__main__":
    main()
