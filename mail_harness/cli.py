from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from pathlib import Path

from .agent import AgentContext, load_agent
from .config import Config, ConfigError, load_config
from .imap_client import ImapClient
from .models import Reply
from .parsing import parse_message
from .runner import Harness
from .smtp_client import SmtpClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mail_harness", description="Харнесс для почтовых агентов (IMAP + SMTP)")
    parser.add_argument("-c", "--config", default="config.toml", help="путь к конфигу (по умолчанию config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="запустить агента")
    run.add_argument("--agent", help="module:Object, перекрывает [agent].class")
    run.add_argument("--once", action="store_true", help="один проход и выход")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                      help="не отправлять ответы и не менять ящик")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="боевой режим")

    sub.add_parser("check", help="проверить подключение к IMAP и SMTP")
    sub.add_parser("folders", help="показать папки ящика")

    srv = sub.add_parser("serve", help="бэклог в браузере + обработка почты в фоне")
    srv.add_argument("--agent", help="module:Object, перекрывает [agent].class")
    srv.add_argument("--port", type=int, default=8766)
    srv.add_argument("--no-mail", action="store_true", help="только интерфейс бэклога, без почты")
    srv_mode = srv.add_mutually_exclusive_group()
    srv_mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    srv_mode.add_argument("--live", dest="dry_run", action="store_false", help="боевой режим")

    svc = sub.add_parser("service", help="автозапуск serve в фоне (macOS)")
    svc.add_argument("action", choices=("install", "uninstall", "status"))
    svc.add_argument("--port", type=int, default=8766)
    svc.add_argument("--live", action="store_true", help="боевой режим")

    try_ = sub.add_parser("try", help="прогнать агента на .eml файле без сети")
    try_.add_argument("eml", type=Path)
    try_.add_argument("--agent", help="module:Object, перекрывает [agent].class")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.command == "service":
        return _service(args)
    for env_file in dict.fromkeys((Path.cwd() / ".env", Path(args.config).resolve().parent / ".env")):
        load_dotenv(env_file)
    try:
        cfg = load_config(args.config, require_secrets=args.command not in ("try", "serve"))
    except ConfigError as e:
        print(f"Ошибка конфига: {e}", file=sys.stderr)
        return 2

    if args.command == "check":
        return _check(cfg)
    if args.command == "folders":
        return _folders(cfg)
    if args.command == "try":
        return _try(cfg, args.eml, args.agent)
    if args.command == "serve":
        return _serve(cfg, args)
    return _run(cfg, args)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    logging.getLogger("mail_harness").debug("загружен %s", path)


def _make_agent(cfg: Config, override: str | None):
    spec = override or cfg.agent.spec
    if not spec:
        raise SystemExit("Не задан агент: укажи [agent].class в конфиге или --agent module:Object")
    return load_agent(spec, cfg.agent.options if not override else None)


def _run(cfg: Config, args: argparse.Namespace) -> int:
    if args.dry_run is not None:
        cfg = dataclasses.replace(cfg, harness=dataclasses.replace(cfg.harness, dry_run=args.dry_run))
    harness = Harness(cfg, _make_agent(cfg, args.agent))
    if args.once:
        harness.start()
        try:
            n = harness.poll_once()
            logging.getLogger("mail_harness").info("обработано писем: %d", n)
        finally:
            harness.shutdown()
    else:
        harness.run_forever()
    return 0


def _serve(cfg: Config, args: argparse.Namespace) -> int:
    from .web import serve

    if args.dry_run is not None:
        cfg = dataclasses.replace(cfg, harness=dataclasses.replace(cfg.harness, dry_run=args.dry_run))
    harness = None
    mail_error = None
    if args.no_mail:
        mail_error = "запущено с --no-mail"
    elif not cfg.imap.secret:
        mail_error = "не задан пароль почты (MAIL_PASSWORD в .env)"
    else:
        try:
            harness = Harness(cfg, _make_agent(cfg, args.agent))
        except Exception as e:
            mail_error = f"{type(e).__name__}: {e}"
            logging.getLogger("mail_harness").exception("почта не запущена")
    if mail_error:
        logging.getLogger("mail_harness").warning("почта выключена: %s", mail_error)
    serve(cfg, harness, port=args.port, mail_error=mail_error)
    return 0


def _service(args: argparse.Namespace) -> int:
    from . import service

    if args.action == "install":
        config = Path(args.config)
        if not config.is_file():
            print(f"Нет конфига: {config}", file=sys.stderr)
            return 2
        service.install(config, args.port, args.live)
    elif args.action == "uninstall":
        service.uninstall()
    else:
        service.status()
    return 0


def _check(cfg: Config) -> int:
    ok = True
    imap = ImapClient(cfg.imap)
    try:
        imap.connect()
        imap.select(cfg.imap.mailbox)
        print(f"IMAP ✓  {cfg.imap.host}: папка {cfg.imap.mailbox!r}, последний UID {imap.max_uid()}")
        caps = imap.capabilities
        print(f"        MOVE: {'да' if 'MOVE' in caps else 'нет'}, UIDPLUS: {'да' if 'UIDPLUS' in caps else 'нет'}")
    except Exception as e:
        ok = False
        print(f"IMAP ✗  {e}")
    finally:
        imap.close()
    try:
        SmtpClient(cfg.smtp).check()
        print(f"SMTP ✓  {cfg.smtp.host}: вход как {cfg.smtp.username}, отправитель {cfg.smtp.from_address}")
    except Exception as e:
        ok = False
        print(f"SMTP ✗  {e}")
    print(f"Режим: {'DRY RUN (ничего не отправляется)' if cfg.harness.dry_run else 'БОЕВОЙ'}")
    print(f"Мои адреса: {', '.join(sorted(cfg.safety.my_addresses))}")
    return 0 if ok else 1


def _folders(cfg: Config) -> int:
    imap = ImapClient(cfg.imap)
    try:
        imap.connect()
        for name in imap.list_folders():
            print(name)
    finally:
        imap.close()
    return 0


def _try(cfg: Config, eml: Path, override: str | None) -> int:
    agent = _make_agent(cfg, override)
    ctx = AgentContext(
        data_dir=cfg.harness.data_dir,
        dry_run=True,
        my_addresses=cfg.safety.my_addresses,
        log=logging.getLogger("mail_harness.agent"),
    )
    if hasattr(agent, "setup"):
        agent.setup(ctx)
    email = parse_message(eml.read_bytes(), uid=0)
    print(f"От:       {email.sender}")
    print(f"Кому:     {', '.join(map(str, email.to))}")
    print(f"Тема:     {email.subject}")
    print(f"Вложения: {', '.join(f'{a.filename} ({a.size} Б)' for a in email.attachments) or '-'}")
    print("\nДействия агента:")
    for action in list(agent.handle(email, ctx) or []):
        if isinstance(action, Reply):
            print(f"  Reply(reply_all={action.reply_all}):")
            for line in action.body.splitlines():
                print(f"    | {line}")
        else:
            text = repr(action)
            print(f"  {text if len(text) < 300 else text[:300] + '…'}")
    return 0
