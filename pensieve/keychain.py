"""Tokens in the macOS Keychain, with the `security` command line: a token stays out of settings files and logs.

Each token is a generic password with the service SERVICE and an account name per connection (for example "slack")."""
import subprocess

SERVICE = "Pensieve"


def get(account: str) -> str:
    """The token of `account`, or "" when the Keychain has none (or this is not a Mac)."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def put(account: str, token: str) -> None:
    """Save the token of `account`, in place of the one before."""
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", token],
                       capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError(f"the Keychain refused the token: {(r.stderr or r.stdout).strip()[:200]}")


def delete(account: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                   capture_output=True, text=True, timeout=10)
