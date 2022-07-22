import gevent
import time

from collections import namedtuple
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from disco.gateway.encoding.json import JSONEncoder
from disco.gateway.packets import OPCode
from disco.types.base import cached_property
from disco.util.emitter import Emitter
from disco.util.logging import LoggingClass
from disco.util.websocket import Websocket
from disco.voice.packets import VoiceOPCode
from disco.voice.udp import AudioCodecs, RTPPayloadTypes, UDPVoiceClient


class SpeakingFlags:
    NONE = 0
    VOICE = 1 << 0
    SOUNDSHARE = 1 << 1
    PRIORITY = 1 << 2


class VoiceState:
    DISCONNECTED = 'DISCONNECTED'
    AWAITING_ENDPOINT = 'AWAITING_ENDPOINT'
    AUTHENTICATING = 'AUTHENTICATING'
    CONNECTING = 'CONNECTING'
    CONNECTED = 'CONNECTED'
    VOICE_DISCONNECTED = 'VOICE_DISCONNECTED'
    VOICE_CONNECTING = 'VOICE_CONNECTING'
    VOICE_CONNECTED = 'VOICE_CONNECTED'
    NO_ROUTE = 'NO_ROUTE'
    ICE_CHECKING = 'ICE_CHECKING'
    RECONNECTING = 'RECONNECTING'
    AUTHENTICATED = 'AUTHENTICATED'


VoiceSpeaking = namedtuple('VoiceSpeaking', [
    'client',
    'user_id',
    'speaking',
    'soundshare',
    'priority',
])


class VoiceException(Exception):
    def __init__(self, msg, client):
        self.voice_client = client
        super(VoiceException, self).__init__(msg)


class VoiceClient(LoggingClass):
    VOICE_GATEWAY_VERSION = 4

    SUPPORTED_MODES = {
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    }

    def __init__(self, client, server_id, is_dm=False, encoder=None, max_reconnects=5):
        super(VoiceClient, self).__init__()

        self.client = client
        self.server_id = server_id
        self.channel_id = None
        self.is_dm = is_dm
        self.encoder = encoder or JSONEncoder
        self.max_reconnects = max_reconnects
        self.video_enabled = False
        self.media = None

        # Set the VoiceClient in the state's voice clients
        self.client.state.voice_clients[self.server_id] = self

        # Bind to some WS packets
        self.packets = Emitter()
        self.packets.on(VoiceOPCode.READY, self.on_voice_ready)
        self.packets.on(VoiceOPCode.HEARTBEAT, self.handle_heartbeat)
        self.packets.on(VoiceOPCode.SESSION_DESCRIPTION, self.on_voice_sdp)
        self.packets.on(VoiceOPCode.SPEAKING, self.on_voice_speaking)
        self.packets.on(VoiceOPCode.HEARTBEAT_ACK, self.handle_heartbeat_acknowledge)
        self.packets.on(VoiceOPCode.HELLO, self.on_voice_hello)
        self.packets.on(VoiceOPCode.RESUMED, self.on_voice_resumed)
        self.packets.on(VoiceOPCode.CLIENT_CONNECT, self.on_voice_client_connect)
        self.packets.on(VoiceOPCode.CLIENT_DISCONNECT, self.on_voice_client_disconnect)
        # self.packets.on(VoiceOPCode.CODECS, self.on_voice_codecs)

        # State + state change emitter
        self.state = VoiceState.DISCONNECTED
        self.state_emitter = Emitter()

        # Connection metadata
        self.token = None
        self.endpoint = None
        self.ssrc = None
        self.ip = None
        self.port = None
        self.mode = None
        self.udp = None
        self.audio_codec = None
        self.video_codec = None
        self.transport_id = None

        # Websocket connection
        self.ws = None

        self._session_id = self.client.gw.session_id
        self._reconnects = 0
        self._heartbeat_task = None
        self._heartbeat_acknowledged = True
        self._identified = False

        # Latency
        self._last_heartbeat = 0
        self.latency = -1

        # SSRCs
        self.audio_ssrcs = {}

    def __repr__(self):
        return '<VoiceClient guild_id={}>'.format(self.server_id)

    @cached_property
    def guild(self):
        return self.client.state.guilds.get(self.server_id) if not self.is_dm else None

    @cached_property
    def channel(self):
        return self.client.state.channels.get(self.channel_id)

    @property
    def user_id(self):
        return self.client.state.me.id

    @property
    def ssrc_audio(self):
        return self.ssrc

    @property
    def ssrc_video(self):
        return self.ssrc + 1

    @property
    def ssrc_rtx(self):
        return self.ssrc + 2

    @property
    def ssrc_rtcp(self):
        return self.ssrc + 3

    def set_state(self, state):
        self.log.debug('[{}] state {} -> {}'.format(self, self.state, state))
        prev_state = self.state
        self.state = state
        self.state_emitter.emit(state, prev_state)

    def set_endpoint(self, endpoint):
        endpoint = endpoint.split(':', 1)[0]
        if self.endpoint == endpoint:
            return

        self.log.info('[{}] {} ({})'.format(self, self.state, endpoint))

        self.endpoint = endpoint

        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.close()
            self.ws = None

        self._identified = False

    def set_token(self, token):
        if self.token == token:
            return
        self.token = token
        if not self._identified:
            self.connect_and_run()

    def connect_and_run(self):
        self.ws = Websocket('wss://' + self.endpoint + '/?v={}'.format(self.VOICE_GATEWAY_VERSION))
        self.ws.emitter.on('on_open', self.on_open)
        self.ws.emitter.on('on_error', self.on_error)
        self.ws.emitter.on('on_close', self.on_close)
        self.ws.emitter.on('on_message', self.on_message)
        self.ws.run_forever()

    def heartbeat_task(self, interval):
        while True:
            if not self._heartbeat_acknowledged:
                self.log.warning('[{}] WS Received HEARTBEAT without HEARTBEAT_ACK, reconnecting...'.format(self))
                self._heartbeat_acknowledged = True
                self.ws.close(status=4000)
                self.on_close(0, 'HEARTBEAT failure')
                return
            self._last_heartbeat = time.perf_counter()

            self.send(VoiceOPCode.HEARTBEAT, time.time())
            self._heartbeat_acknowledged = False
            gevent.sleep(interval / 1000)

    def handle_heartbeat(self, _):
        self.send(VoiceOPCode.HEARTBEAT, time.time())

    def handle_heartbeat_acknowledge(self, _):
        self.log.debug('[{}] Received WS HEARTBEAT_ACK'.format(self))
        self._heartbeat_acknowledged = True
        self.latency = float('{:.2f}'.format((time.perf_counter() - self._last_heartbeat) * 1000))

    def set_speaking(self, voice=False, soundshare=False, priority=False, delay=0):
        value = SpeakingFlags.NONE
        if voice:
            value |= SpeakingFlags.VOICE
        if soundshare:
            value |= SpeakingFlags.SOUNDSHARE
        if priority:
            value |= SpeakingFlags.PRIORITY

        self.send(VoiceOPCode.SPEAKING, {
            'speaking': value,
            'delay': delay,
            'ssrc': self.ssrc,
        })

    def set_voice_state(self, channel_id, mute=False, deaf=False, video=False):
        return self.client.gw.send(OPCode.VOICE_STATE_UPDATE, {
            'self_mute': bool(mute),
            'self_deaf': bool(deaf),
            'self_video': bool(video),
            'guild_id': None if self.is_dm else self.server_id,
            'channel_id': channel_id,
        })

    def send(self, op, data):
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.log.debug('[{}] sending OP {} (data = {})'.format(self, op, data))
            self.ws.send(self.encoder.encode({'op': op, 'd': data}), self.encoder.OPCODE)
        else:
            self.log.debug('[{}] dropping because ws is closed OP {} (data = {})'.format(self, op, data))

    def on_voice_client_connect(self, data):
        user_id = int(data['user_id'])

        self.audio_ssrcs[data['audio_ssrc']] = user_id
        # ignore data['voice_ssrc'] for now

    def on_voice_client_disconnect(self, data):
        user_id = int(data['user_id'])

        for ssrc in self.audio_ssrcs.keys():
            if self.audio_ssrcs[ssrc] == user_id:
                del self.audio_ssrcs[ssrc]
                break

    def on_voice_codecs(self, data):
        self.audio_codec = data['audio_codec']
        self.video_codec = data['video_codec']
        if 'media_session_id' in data.keys():
            self.transport_id = data['media_session_id']

        # Set the UDP's RTP Audio Header's Payload Type
        self.udp.set_audio_codec(data['audio_codec'])

    def on_voice_hello(self, packet):
        self.log.info('[{}] Received Voice HELLO payload, starting heartbeater'.format(self))
        self._heartbeat_task = gevent.spawn(self.heartbeat_task, packet['heartbeat_interval'])
        self.set_state(VoiceState.AUTHENTICATED)

    def on_voice_ready(self, data):
        self.log.info('[{}] Received READY payload, RTC connecting'.format(self))
        self.set_state(VoiceState.CONNECTING)
        self.ssrc = data['ssrc']
        self.ip = data['ip']
        self.port = data['port']
        self._identified = True

        for mode in self.SUPPORTED_MODES:
            if mode in data['modes']:
                self.mode = mode
                self.log.debug('[{}] Selected mode {}'.format(self, mode))
                break
        else:
            raise Exception('Failed to find a supported voice mode')

        self.log.debug('[{}] Attempting IP discovery over UDP to {}:{}'.format(self, self.ip, self.port))
        self.udp = UDPVoiceClient(self)
        ip, port = self.udp.connect(self.ip, self.port)

        if not ip:
            self.log.error('Failed to discover bot IP, perhaps a network configuration error is present.')
            self.disconnect()
            return

        codecs = []

        # Sending discord our available codecs and rtp payload type for it
        for idx, codec in enumerate(AudioCodecs):
            codecs.append({
                'name': codec,
                'type': 'audio',
                'priority': (idx + 1) * 1000,
                'payload_type': RTPPayloadTypes.get(codec).value,
            })

        self.log.debug('[{}] IP discovery completed ({}:{}), sending SELECT_PROTOCOL'.format(self, ip, port))
        self.send(VoiceOPCode.SELECT_PROTOCOL, {
            'protocol': 'udp',
            'data': {
                'port': port,
                'address': ip,
                'mode': self.mode,
            },
            'codecs': codecs,
        })
        self.send(VoiceOPCode.CLIENT_CONNECT, {
            'audio_ssrc': self.ssrc,
            'video_ssrc': 0,
            'rtx_ssrc': 0,
        })

    def on_voice_resumed(self, data):
        self.log.info('[{}] WS Resumed'.format(self))
        self.set_state(VoiceState.CONNECTED)

    def on_voice_sdp(self, sdp):
        self.log.info('[{}] Received session description; connected'.format(self))

        self.mode = sdp['mode']
        self.audio_codec = sdp['audio_codec']
        self.video_codec = sdp['video_codec']
        self.transport_id = sdp['media_session_id']

        # Set the UDP's RTP Audio Header's Payload Type
        self.udp.set_audio_codec(sdp['audio_codec'])

        # Create a secret box for encryption/decryption
        self.udp.setup_encryption(bytes(bytearray(sdp['secret_key'])))

        self.set_state(VoiceState.CONNECTED)

    def on_voice_speaking(self, data):
        user_id = int(data['user_id'])

        self.audio_ssrcs[data['ssrc']] = user_id

        # Maybe rename speaking to voice in future
        payload = VoiceSpeaking(
            client=self,
            user_id=user_id,
            speaking=bool(data['speaking'] & SpeakingFlags.VOICE),
            soundshare=bool(data['speaking'] & SpeakingFlags.SOUNDSHARE),
            priority=bool(data['speaking'] & SpeakingFlags.PRIORITY),
        )

        self.client.gw.events.emit('VoiceSpeaking', payload)

    def on_message(self, msg):
        try:
            data = self.encoder.decode(msg)
            self.packets.emit(data['op'], data['d'])
        except Exception:
            self.log.exception('Failed to parse voice gateway message: ')

    def on_error(self, error):
        if isinstance(error, WebSocketTimeoutException):
            return self.log.error('[{}] WS has timed out. An upstream connection issue is likely present.'.format(self))
        if not isinstance(error, WebSocketConnectionClosedException):
            self.log.error('[{}] WS received error: {}'.format(self, error))

    def on_open(self):
        if self._identified:
            self.send(VoiceOPCode.RESUME, {
                'server_id': self.server_id,
                'session_id': self._session_id,
                'token': self.token,
            })
        else:
            self.send(VoiceOPCode.IDENTIFY, {
                'server_id': self.server_id,
                'user_id': self.user_id,
                'session_id': self._session_id,
                'token': self.token,
                'video': self.video_enabled,
            })

    def on_close(self, code=None, reason=None):
        self.log.info('[{}] WS Closed:{}{} ({})'.format(self, ' [{}]'.format(code) if code else '', ' {}'.format(reason) if reason else '', self._reconnects))

        if self._heartbeat_task:
            self.log.info('[{}] WS Closed: killing heartbeater'.format(self))
            self._heartbeat_task.kill()
            self._heartbeat_task = None

        self.ws = None

        # If we killed the connection, don't try resuming
        if self.state == VoiceState.DISCONNECTED:
            return

        self.log.info('[{}] Attempting WS resumption'.format(self))
        self.set_state(VoiceState.RECONNECTING)
        self._reconnects += 1

        if self.max_reconnects and self._reconnects > self.max_reconnects:
            raise VoiceException(
                'Failed to reconnect after {} attempts, giving up'.format(self.max_reconnects), self)

        # Check if code is not None, was not from us
        if code and (4000 < code <= 4016 or code == 1001):
            self._identified = False

            if self.udp and self.udp.connected:
                self.udp.disconnect()

            wait_time = 5
        else:
            wait_time = 1

        self.log.info('[{}] Will attempt {} after {} seconds'.format(self, 'resumption' if self._identified else 'reconnection', wait_time))
        gevent.sleep(wait_time)
        self.connect_and_run()

    def connect(self, channel_id, timeout=10, **kwargs):
        if self.is_dm:
            channel_id = self.server_id

        if not channel_id:
            raise VoiceException('[{}] cannot connect to an empty channel id'.format(self), self)

        if self.channel_id == channel_id:
            if self.state == VoiceState.CONNECTED:
                self.log.debug('[{}] Already connected to {}, returning'.format(self, self.channel))
                return self
        else:
            if self.state == VoiceState.CONNECTED:
                self.log.debug('[{}] Moving to channel {}'.format(self, channel_id))
            else:
                self.log.debug('[{}] Attempting connection to channel id {}'.format(self, channel_id))
                self.set_state(VoiceState.AWAITING_ENDPOINT)

        self.set_voice_state(channel_id, **kwargs)

        if not self.state_emitter.once(VoiceState.CONNECTED, timeout=timeout):
            self.disconnect()
            raise VoiceException('Failed to connect to voice', self)
        else:
            return self

    def disconnect(self):
        if self.state == VoiceState.DISCONNECTED:
            return

        self.set_state(VoiceState.DISCONNECTED)

        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.close()
            self.ws = None

        try:
            self.set_voice_state(None)
        except:
            pass

        if self.udp:
            self.udp.disconnect()

        try:
            self.media.now_playing.source.proc.kill()
        except:
            pass

        del self.client.state.voice_clients[self.server_id]
        return self.client.gw.events.emit('VoiceDisconnect', self)

    def send_frame(self, *args, **kwargs):
        self.udp.send_frame(*args, **kwargs)

    def increment_timestamp(self, *args, **kwargs):
        self.udp.increment_timestamp(*args, **kwargs)
