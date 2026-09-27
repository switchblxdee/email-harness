from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SECURITY_MODES = ("ssl", "starttls", "none")
AUTH_MODES = ("password", "xoauth2")
START_FROM = ("now", "all")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    secret: str = field(repr=False)
    port: int = 993
    security: str = "ssl"
    auth: str = "password"
    mailbox: str = "INBOX"
    sent_folder: str | None = None
    timeout: float = 60.0


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    username: str
    secret: str = field(repr=False)
    from_address: str = ""
    from_name: str = ""
    port: int = 587
    security: str = "starttls"
    auth: str = "password"
    timeout: float = 60.0
    local_hostname: str = ""
    keepalive: float = 120.0


@dataclass(frozen=True)
class SafetyConfig:
    my_addresses: frozenset[str]
    allowed_domains: tuple[str, ...] = ()
    blocked_senders: frozenset[str] = frozenset()
    skip_automated: bool = True
    one_reply_per_thread: bool = True
    max_replies_per_sender_per_hour: int = 3
    max_replies_per_hour: int = 30


@dataclass(frozen=True)
class HarnessConfig:
    data_dir: Path
    state_db: Path
    dry_run: bool = True
    poll_interval: float = 60.0
    start_from: str = "now"
    max_message_bytes: int = 25 * 1024 * 1024
    max_attempts: int = 3


@dataclass(frozen=True)
class AgentConfig:
    spec: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


DEFAULT_NOTIFY_TEMPLATE = (
    "Здравствуйте!\n\n"
    "{owner} взял Вашу задачу «{title}» в работу.\n\n"
    "Сообщение создано автоматически."
)
CLEANUP_MODES = ("archive", "delete", "off")


@dataclass(frozen=True)
class BacklogConfig:
    owner: str = ""
    author_name: str = ""
    notify_in_progress: bool = True
    notify_delay: float = 10.0
    notify_template: str = DEFAULT_NOTIFY_TEMPLATE
    cleanup_done: str = "archive"
    link_replies: bool = True


@dataclass(frozen=True)
class ChatConfig:
    spec: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    confirm_changes: bool = True


@dataclass(frozen=True)
class Config:
    imap: ImapConfig
    smtp: SmtpConfig
    safety: SafetyConfig
    harness: HarnessConfig
    agent: AgentConfig
    backlog: BacklogConfig = field(default_factory=BacklogConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)


def load_config(path: str | Path, require_secrets: bool = True) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"конфиг не найден: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"ошибка в {path}: {e}") from None
    return parse_config(data, base_dir=path.resolve().parent, require_secrets=require_secrets)


def parse_config(data: dict[str, Any], base_dir: Path, require_secrets: bool = True) -> Config:
    imap_d = _section(data, "imap", required=True)
    smtp_d = _section(data, "smtp", required=True)
    safety_d = _section(data, "safety")
    harness_d = _section(data, "harness")
    agent_d = _section(data, "agent")
    backlog_d = _section(data, "backlog")
    chat_d = _section(data, "chat")

    imap = ImapConfig(
        host=_req(imap_d, "imap", "host"),
        username=_req(imap_d, "imap", "username"),
        secret=_secret(imap_d, "imap", require_secrets),
        port=int(imap_d.get("port", 993)),
        security=_choice(imap_d, "imap", "security", SECURITY_MODES, "ssl"),
        auth=_choice(imap_d, "imap", "auth", AUTH_MODES, "password"),
        mailbox=imap_d.get("mailbox", "INBOX"),
        sent_folder=imap_d.get("sent_folder") or None,
        timeout=float(imap_d.get("timeout", 60)),
    )

    smtp_user = smtp_d.get("username", imap.username)
    has_own_secret = any(k in smtp_d for k in ("password", "password_env", "token_env"))
    smtp = SmtpConfig(
        host=_req(smtp_d, "smtp", "host"),
        username=smtp_user,
        secret=_secret(smtp_d, "smtp", require_secrets) if has_own_secret else imap.secret,
        from_address=(smtp_d.get("from_address") or smtp_user).lower(),
        from_name=smtp_d.get("from_name", ""),
        port=int(smtp_d.get("port", 587)),
        security=_choice(smtp_d, "smtp", "security", SECURITY_MODES, "starttls"),
        auth=_choice(smtp_d, "smtp", "auth", AUTH_MODES, imap.auth),
        timeout=float(smtp_d.get("timeout", 60)),
        local_hostname=str(smtp_d.get("local_hostname", "")),
        keepalive=float(smtp_d.get("keepalive", 120)),
    )
    if "@" not in smtp.from_address:
        raise ConfigError("smtp.from_address должен быть e-mail адресом")

    me = {a.lower() for a in safety_d.get("my_addresses", [])}
    me.add(smtp.from_address)
    if "@" in imap.username:
        me.add(imap.username.lower())

    safety = SafetyConfig(
        my_addresses=frozenset(me),
        allowed_domains=tuple(d.lower().lstrip("@") for d in safety_d.get("allowed_domains", [])),
        blocked_senders=frozenset(s.lower() for s in safety_d.get("blocked_senders", [])),
        skip_automated=bool(safety_d.get("skip_automated", True)),
        one_reply_per_thread=bool(safety_d.get("one_reply_per_thread", True)),
        max_replies_per_sender_per_hour=int(safety_d.get("max_replies_per_sender_per_hour", 3)),
        max_replies_per_hour=int(safety_d.get("max_replies_per_hour", 30)),
    )

    harness = HarnessConfig(
        data_dir=_path(base_dir, harness_d.get("data_dir", "data")),
        state_db=_path(base_dir, harness_d.get("state_db", "state.sqlite3")),
        dry_run=bool(harness_d.get("dry_run", True)),
        poll_interval=float(harness_d.get("poll_interval", 60)),
        start_from=_choice(harness_d, "harness", "start_from", START_FROM, "now"),
        max_message_bytes=int(float(harness_d.get("max_message_mb", 25)) * 1024 * 1024),
        max_attempts=int(harness_d.get("max_attempts", 3)),
    )

    agent = AgentConfig(spec=agent_d.get("class"), options=dict(agent_d.get("options", {})))
    backlog = BacklogConfig(
        owner=str(backlog_d.get("owner", "")),
        author_name=str(backlog_d.get("author_name", "")),
        notify_in_progress=bool(backlog_d.get("notify_in_progress", True)),
        notify_delay=float(backlog_d.get("notify_delay", 10)),
        notify_template=str(backlog_d.get("notify_template", DEFAULT_NOTIFY_TEMPLATE)),
        cleanup_done=_choice(backlog_d, "backlog", "cleanup_done", CLEANUP_MODES, "archive"),
        link_replies=bool(backlog_d.get("link_replies", True)),
    )
    chat = ChatConfig(spec=chat_d.get("class"), options=dict(chat_d.get("options", {})),
                      confirm_changes=bool(chat_d.get("confirm_changes", True)))
    return Config(imap=imap, smtp=smtp, safety=safety, harness=harness, agent=agent, backlog=backlog, chat=chat)


def _section(data: dict[str, Any], name: str, required: bool = False) -> dict[str, Any]:
    if name not in data:
        if required:
            raise ConfigError(f"в конфиге нет секции [{name}]")
        return {}
    return data[name]


def _req(d: dict[str, Any], section: str, key: str) -> str:
    value = d.get(key)
    if not value:
        raise ConfigError(f"не задан {section}.{key}")
    return str(value)


def _choice(d: dict[str, Any], section: str, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = str(d.get(key, default)).lower()
    if value not in allowed:
        raise ConfigError(f"{section}.{key} = {value!r}, допустимо: {', '.join(allowed)}")
    return value


def _secret(d: dict[str, Any], section: str, required: bool = True) -> str:
    for key in ("password_env", "token_env"):
        if key in d:
            var = d[key]
            value = os.environ.get(var)
            if not value and not required:
                return ""
            if not value:
                raise ConfigError(f"переменная окружения {var} ({section}.{key}) не задана")
            return value
    if "password" in d:
        return str(d["password"])
    if not required:
        return ""
    raise ConfigError(f"не задан {section}.password_env (или {section}.token_env для xoauth2)")


def _path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p
