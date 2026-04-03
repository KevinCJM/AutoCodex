from __future__ import annotations

import logging
import sys

import lark_oapi as lark

from .backends import BackendManager
from .config import MissingConfigurationError, Settings, load_settings_with_setup, run_interactive_setup
from .delivery import DeliveryService
from .service import BridgeService
from .store import BridgeStore


def _build_logger(settings: Settings) -> logging.Logger:
    logger = logging.getLogger("feishu_codex_bridge")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(settings.log_dir / "bridge.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def _build_feishu_client(settings: Settings) -> lark.Client:
    return (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .log_level(lark.LogLevel.INFO)
        .build()
    )


def main() -> None:
    try:
        settings = load_settings_with_setup()
    except MissingConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
    logger = _build_logger(settings)
    store = BridgeStore(settings.db_path)
    store.mark_running_jobs_failed_on_startup()
    feishu_client = _build_feishu_client(settings)
    delivery = DeliveryService(settings=settings, logger=logger, client=feishu_client)
    backends = BackendManager(settings=settings, logger=logger)
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=backends,
        delivery=delivery,
        logger=logger,
    )

    event_handler = (
        lark.EventDispatcherHandler.builder(
            settings.encrypt_key,
            settings.verification_token,
            lark.LogLevel.INFO,
        )
        .register_p2_im_message_receive_v1(service.handle_event)
        .build()
    )
    ws_client = lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        log_level=lark.LogLevel.INFO,
        event_handler=event_handler,
    )

    logger.info("starting Feishu Codex bridge")
    try:
        ws_client.start()
    finally:
        service.shutdown()


def init_main() -> None:
    config_path = run_interactive_setup()
    print(f"配置完成: {config_path}")
