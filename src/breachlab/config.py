"""Where the token lives and how it is read and written.

The token is a password. It is stored in a file only its owner can read,
written atomically, and never printed.
"""

import os
import re
import stat
import tempfile

DEFAULT_API = "https://breachlab.org"
ENV_TOKEN = "BREACHLAB_TOKEN"
ENV_API = "BREACHLAB_API"

# "blp_" followed by URL-safe base64. The strict shape also guarantees the
# value can never smuggle a newline into the Authorization header.
TOKEN_RE = re.compile(r"^blp_[A-Za-z0-9_-]{8,512}$")


class TokenError(Exception):
    """The stored or supplied token cannot be used."""


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME", "")
    # The XDG spec says a relative path here is invalid and must be ignored.
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "breachlab")


def token_path():
    return os.path.join(config_dir(), "token")


def valid_token(token):
    return isinstance(token, str) and TOKEN_RE.match(token) is not None


def load_token():
    """Return (token, source). Source is "env", "file" or None."""
    env = os.environ.get(ENV_TOKEN)
    if env is not None and env.strip():
        token = env.strip()
        if not valid_token(token):
            raise TokenError(
                "%s does not look like a BreachLab token. Tokens start with blp_." % ENV_TOKEN
            )
        return token, "env"

    path = token_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            token = handle.read(4096).strip()
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenError("Cannot read the token file %s: %s" % (path, _reason(exc)))

    if os.name == "posix" and mode & 0o077:
        # Someone loosened the permissions. Put them back.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    if not token:
        return None, None
    if not valid_token(token):
        raise TokenError(
            'The token file %s is damaged. Run "breachlab login" to replace it.' % path
        )
    return token, "file"


def save_token(token):
    """Write the token atomically with mode 0600 inside a 0700 directory."""
    if not valid_token(token):
        raise TokenError("Refusing to store a value that is not a BreachLab token.")
    directory = config_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)

    # mkstemp creates the file with mode 0600 and O_EXCL, so the secret is
    # never on disk with wider permissions, not even for a moment.
    fd, tmp = tempfile.mkstemp(prefix=".token.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, token_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return token_path()


def delete_token():
    """Remove the token file. Return True when a file was removed."""
    try:
        os.unlink(token_path())
        return True
    except FileNotFoundError:
        return False


def display_path(path):
    home = os.path.expanduser("~")
    if home and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _reason(exc):
    return getattr(exc, "strerror", None) or exc.__class__.__name__
