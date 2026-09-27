from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import threading
import time
from email.message import EmailMessage

from .config import SmtpConfig
from .imap_client import xoauth2_string

log = logging.getLogger(__name__)


class SmtpError(Exception):
    pass


def default_hostname() -> str:
    name = socket.gethostname()
    return name if name and name.isascii() else "localhost"


class SmtpClient:
    def __init__(self, cfg: SmtpConfig):
        self.cfg = cfg
        self.local_hostname = cfg.local_hostname or default_hostname()
        self._conn: smtplib.SMTP | None = None
        self._used_at = 0.0
        self._lock = threading.Lock()

    def _open(self) -> smtplib.SMTP:
        cfg = self.cfg
        ctx = ssl.create_default_context()
        if cfg.security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, local_hostname=self.local_hostname,
                                                  context=ctx, timeout=cfg.timeout)
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, local_hostname=self.local_hostname, timeout=cfg.timeout)
            smtp.ehlo()
            if cfg.security == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
        try:
            if cfg.auth == "xoauth2":
                smtp.auth("XOAUTH2", lambda challenge=None: xoauth2_string(cfg.username, cfg.secret))
            else:
                smtp.login(cfg.username, cfg.secret)
        except smtplib.SMTPException as e:
            smtp.close()
            raise SmtpError(f"SMTP: не удалось войти как {cfg.username}: {e}") from None
        return smtp

    def check(self) -> None:
        with self._open() as smtp:
            smtp.noop()

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        with self._lock:
            smtp = self._reusable() or self._open()
            try:
                refused = smtp.send_message(msg, from_addr=self.cfg.from_address, to_addrs=recipients)
            except Exception:
                _quit(smtp)
                raise
            if self.cfg.keepalive > 0:
                self._conn, self._used_at = smtp, time.monotonic()
            else:
                _quit(smtp)
        if refused:
            log.warning("SMTP: сервер отклонил адресатов: %s", refused)

    def _reusable(self) -> smtplib.SMTP | None:
        smtp, self._conn = self._conn, None
        if smtp is None:
            return None
        if time.monotonic() - self._used_at <= self.cfg.keepalive:
            try:
                if smtp.noop()[0] == 250:
                    return smtp
            except (smtplib.SMTPException, OSError):
                pass
        smtp.close()
        return None

    def close(self) -> None:
        with self._lock:
            smtp, self._conn = self._conn, None
        if smtp is not None:
            _quit(smtp)


def _quit(smtp: smtplib.SMTP) -> None:
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        smtp.close()
