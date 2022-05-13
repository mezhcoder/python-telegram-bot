#!/usr/bin/env python
#
# A library that provides a Python interface to the Telegram Bot API
# Copyright (C) 2015-2022
# Leandro Toledo de Souza <devs@python-telegram-bot.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser Public License for more details.
#
# You should have received a copy of the GNU Lesser Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].
# pylint: disable=missing-module-docstring
import asyncio
import logging
from http import HTTPStatus
from ssl import SSLContext
from types import TracebackType
from typing import TYPE_CHECKING, Optional, Type

import tornado.web
from tornado.httpserver import HTTPServer

from telegram import Update
from telegram._utils.json import json_loads
from telegram.ext._extbot import ExtBot

if TYPE_CHECKING:
    from telegram import Bot


class WebhookServer:
    """Thin wrapper around ``tornado.httpserver.HTTPServer``."""

    __slots__ = (
        "_http_server",
        "listen",
        "port",
        "_logger",
        "is_running",
        "_server_lock",
        "_shutdown_lock",
    )

    def __init__(
        self, listen: str, port: int, webhook_app: "WebhookAppClass", ssl_ctx: Optional[SSLContext]
    ):
        self._http_server = HTTPServer(webhook_app, ssl_options=ssl_ctx)
        self.listen = listen
        self.port = port
        self._logger = logging.getLogger(__name__)
        self.is_running = False
        self._server_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()

    async def serve_forever(self, ready: asyncio.Event = None) -> None:
        async with self._server_lock:
            self._http_server.listen(self.port, address=self.listen)

            self.is_running = True
            if ready is not None:
                ready.set()

            self._logger.debug("Webhook Server started.")

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if not self.is_running:
                self._logger.debug("Webhook Server is already shut down. Returning")
                return
            self.is_running = False
            self._http_server.stop()
            await self._http_server.close_all_connections()
            self._logger.debug("Webhook Server stopped")


class WebhookAppClass(tornado.web.Application):
    """Application used in the Webserver"""

    def __init__(self, webhook_path: str, bot: "Bot", update_queue: asyncio.Queue):
        self.shared_objects = {"bot": bot, "update_queue": update_queue}
        handlers = [(rf"{webhook_path}/?", TelegramHandler, self.shared_objects)]  # noqa
        tornado.web.Application.__init__(self, handlers)  # type: ignore

    def log_request(self, handler: tornado.web.RequestHandler) -> None:
        """Overrides the default implementation since we have our own logging setup."""


# pylint: disable=abstract-method
class TelegramHandler(tornado.web.RequestHandler):
    """Handler that processes incoming requests from Telegram"""

    __slots__ = ("bot", "update_queue", "_logger")

    SUPPORTED_METHODS = ("POST",)  # type: ignore[assignment]

    def initialize(self, bot: "Bot", update_queue: asyncio.Queue) -> None:
        """Initialize for each request - that's the interface provided by tornado"""
        # pylint: disable=attribute-defined-outside-init
        self.bot = bot
        self.update_queue = update_queue
        self._logger = logging.getLogger(__name__)

    def set_default_headers(self) -> None:
        """Sets default headers"""
        self.set_header("Content-Type", 'application/json; charset="utf-8"')

    async def post(self) -> None:
        """Handle incoming POST request"""
        self._logger.debug("Webhook triggered")
        self._validate_post()

        json_string = self.request.body.decode()
        data = json_loads(json_string)
        self.set_status(HTTPStatus.OK)
        self._logger.debug("Webhook received data: %s", json_string)

        try:
            update = Update.de_json(data, self.bot)
        except Exception as exc:
            self._logger.critical(
                "Something went wrong processing the data received from Telegram. "
                "Received data was *not* processed!",
                exc_info=exc,
            )

        if update:
            self._logger.debug("Received Update with ID %d on Webhook", update.update_id)

            # handle arbitrary callback data, if necessary
            if isinstance(self.bot, ExtBot):
                self.bot.insert_callback_data(update)

            await self.update_queue.put(update)

    def _validate_post(self) -> None:
        """Only accept requests with content type JSON"""
        ct_header = self.request.headers.get("Content-Type", None)
        if ct_header != "application/json":
            raise tornado.web.HTTPError(HTTPStatus.FORBIDDEN)

    def log_exception(
        self,
        typ: Optional[Type[BaseException]],
        value: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Override the default logging and instead use our custom logging."""
        self._logger.debug(
            "%s - %s",
            self.request.remote_ip,
            "Exception in TelegramHandler",
            exc_info=(typ, value, tb) if typ and value and tb else value,
        )
