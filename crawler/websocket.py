import faust
import json
import os
import websockets

from mode import Service
from logging import LogRecord
from websockets.server import WebSocketServerProtocol

from app import (
    logger,
    APPNAME,
    BROKER,
    MAX_MESSAGE_SIZE,
    STORE,
    WebExtStart,
    webext_start_topic,
    webext_javascript_topic,
    webext_javascript_cookie_topic,
    webext_navigation_topic,
    webext_http_request_topic,
    webext_http_response_topic,
    webext_http_response_content_topic,
    webext_http_redirect_topic,
    WebExtJavascript,
    WebExtJavascriptCookie,
    WebExtNavigation,
    WebExtHttpRequest,
    WebExtHttpResponse,
    WebExtHttpResponseContent,
    WebExtHttpRedirect,
)

WS_PORT = int(os.environ.get('WS_PORT', 7799))

instrument_type_map = {
    'javascript': {
        'topic': webext_javascript_topic,
        'record': WebExtJavascript,
    },
    'javascript_cookies': {
        'topic': webext_javascript_cookie_topic,
        'record': WebExtJavascriptCookie,
    },
    'http_requests': {
        'topic': webext_http_request_topic,
        'record': WebExtHttpRequest,
    },
    'http_responses': {
        'topic': webext_http_response_topic,
        'record': WebExtHttpResponse,
    },
    'http_redirects': {
        'topic': webext_http_redirect_topic,
        'record': WebExtHttpRedirect,
    },
    'navigations': {
        'topic': webext_navigation_topic,
        'record': WebExtNavigation,
    }
}


class WSApp(faust.App):

    def on_init(self):
        self.websockets = Websockets(self, port=WS_PORT)

    async def on_start(self):
        await self.add_runtime_dependency(self.websockets)


class Websockets(Service):
    # This was cribbed from StackOverflow, it's not clear
    # how much of this I'm using.

    def __init__(self, app, bind: str = '0.0.0.0', port: int = 7799, **kwargs):
        self.app = app
        self.bind = bind
        self.port = port
        super().__init__(**kwargs)

    async def on_message(self, ws, message) -> None:
        # print(message)

        # Parse JSON message
        try:
            parsed = json.loads(message)
        except ValueError as e:
            logger.exception(e)
            return

        # Check it's from the WebExtension
        try:
            _component = parsed.pop('_component')
            message_components = _component.split('::')
            assert message_components[0] == 'WebExtension'
        except (KeyError, IndexError, AssertionError) as e:
            logger.error('Message from webextension does not have valid _component attribute.')
            logger.exception(e)
            return

        # Handle different types of message from WebExtension
        # These correspond with entries in Extension/firefox/logging.js
        if message_components[1] == 'Log':
            record = LogRecord(
                name='WebExtension',
                level=parsed['level'],
                msg=parsed['msg'],
                pathname='',
                lineno=1,
                args=None,
                exc_info=None,
            )
            logger.handle(record)
        elif message_components[1] == 'Start':
            await webext_start_topic.send(value=WebExtStart(**parsed))
        elif message_components[1] == 'Data':
            if len(message_components) != 3:
                logger.error('_component data does not contain instrument_type')
                return
            instrument_type = message_components[2]
            if instrument_type not in instrument_type_map.keys():
                logger.error(f'instrument type {instrument_type} is unknown')
                return
            topic = instrument_type_map[instrument_type]['topic']
            record = instrument_type_map[instrument_type]['record']
            await topic.send(value=record(**parsed))
        elif message_components[1] == 'Content':
            await webext_http_response_content_topic.send(value=WebExtHttpResponseContent(**parsed))
        else:
            logger.error('Invalid message component', message)

    async def on_messages(self, ws: WebSocketServerProtocol, path: str) -> None:
        async for message in ws:
            await self.on_message(ws, message)

    async def on_close(self, ws) -> None:
        # called when websocket socket is closed.
        logger.warn('Websocket closing')

    @Service.task
    async def _background_server(self):
        await websockets.serve(self.on_messages, self.bind, self.port, max_size=MAX_MESSAGE_SIZE)


app_settings = dict(
    broker=BROKER,
    producer_max_request_size=MAX_MESSAGE_SIZE,
    consumer_max_fetch_size=MAX_MESSAGE_SIZE,
    store=STORE,
    process_guarantee="exactly_once",
)
app = WSApp(APPNAME, **app_settings)
