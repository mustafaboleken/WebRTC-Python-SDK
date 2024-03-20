import asyncio
import json
import logging
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCDataChannel, RTCDataChannelParameters

class WebRTCClient:
    def __init__(self, stream_id, websocket_url, token_id=None):

        peerconnection_config = {
        'iceServers': [
                {
                    'urls': 'stun:stun1.l.google.com:19302'
                }
            ]
        }

        self.stream_id = stream_id
        self.token_id = token_id
        self.websocket_url = websocket_url
        self.pc = RTCPeerConnection()
        self.remote_sdp = None
        self.local_sdp = None
        self.ready = False
        self.peer_connection_config = RTCPeerConnection(configuration=peerconnection_config)
        self.candidate_types = ["udp", "tcp", "relay"]
        self.debug = True
        self.local_stream = None
        self.remote_peer_connection = {}
        self.remote_description_set = {}
        self.receiving_messages = {}
        self.ice_candidate_list = {}
        self.play_stream_id = []
        self.id_mapping = {}  # Assuming this is populated elsewhere
        self.listeners = {"newTrackAvailable": [], "newStreamAvailable": []}
        self.data_channel_enabled = True
        self.degradation_preference = 'balanced'
        self.error_event_listeners = []
        self.callback_error = None
        asyncio.ensure_future(self.connect_to_websocket())

    async def connect_to_websocket(self):
        async with websockets.connect(self.websocket_url) as websocket:
            self.websocket = websocket
            self.ready = True
            print("Connection open!")
            self.start_websocket_main_loop()
            while not self.websocket.open:
                await asyncio.sleep(1)
            print("Websocket connected")

    async def start_websocket_main_loop(self):
       # Main message loop
        while True:
            message = await self.websocket.recv()
            await self.message_received(message)

    async def message_received(self, msg):
        print(f"Message received: {msg}")
        message = json.loads(msg)
        command = message.get("command")

        if command == "start":
            await self.start_message_received()
        elif command == "takeConfiguration":
            sdp = message["sdp"]
            sdp_type = message["type"]
            await self.take_configuration_message_received(sdp, sdp_type)
        elif command == "takeCandidate":
            candidate = message["candidate"]
            sdp_mid = message["id"]
            sdp_mlineindex = message["label"]
            await self.take_candidate_message_received(candidate, sdp_mid, sdp_mlineindex)

    async def take_configuration_message_received(self, sdp, sdp_type):
        self.remote_sdp = RTCSessionDescription(sdp, sdp_type)
        await self.pc.setRemoteDescription(self.remote_sdp)

        if sdp_type == "offer":
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            await self.send_description_message("answer", answer.sdp)

    async def take_candidate_message_received(self, candidate, sdp_mid, sdp_mlineindex):
        ice_candidate = RTCIceCandidate(candidate, sdpMid=sdp_mid, sdpMLineIndex=sdp_mlineindex)
        await self.pc.addIceCandidate(ice_candidate)

    async def start_message_received(self):
        print("Start message received")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await self.send_description_message("offer", offer.sdp)

    async def send_description_message(self, sdp_type, sdp):
        message = {
            "command": "takeConfiguration",
            "streamId": self.stream_id,
            "type": sdp_type,
            "sdp": sdp
        }
        if self.token_id:
            message["token"] = self.token_id
        await self.websocket.send(json.dumps(message))

    async def send_publish_message(self):
        msg = self.create_base_message_object("publish", True)
        await self.send_websocket_message(msg)

    async def send_play_message(self):
        message_obj = {
            "command": "play",
            "token": self.token_id,
            "streamId": self.stream_id,
            "room": "",
            "subscriberCode": "",
            "subscriberId": "",
            "viewerInfo": "",
            "trackList": []
        }
        await self.send_websocket_message(message_obj)

        await self.init_peer_connection(self.stream_id, "play")

    async def send_leave_message(self):
        msg = self.create_base_message_object("leave", False)
        await self.send_websocket_message(msg)

    def create_base_message_object(self, command, set_token):
        message_obj = {
            "command": command,
            "streamId": self.stream_id
        }
        if set_token and self.token_id:
            message_obj["token"] = self.token_id
        return message_obj

    async def send_websocket_message(self, msg):
        if self.websocket and self.websocket.open:
            print(f"Sending Message: {msg}")
            await self.websocket.send(json.dumps(msg))

    async def init_peer_connection(self, stream_id, data_channel_mode):
        if stream_id not in self.remote_peer_connection:
            logging.debug(f"stream id in init peer connection: {stream_id}")
            self.remote_peer_connection[stream_id] = RTCPeerConnection(self.peer_connection_config)
            self.remote_description_set[stream_id] = False
            self.ice_candidate_list[stream_id] = []

            if stream_id not in self.play_stream_id:
                if self.local_stream is not None:
                    for track in self.local_stream.getTracks():
                        rtp_sender = self.remote_peer_connection[stream_id].addTrack(track, self.local_stream)
                        if track.kind == 'video':
                            parameters = rtp_sender.getParameters()
                            parameters.degradationPreference = self.degradation_preference
                            try:
                                await rtp_sender.setParameters(parameters)
                                logging.info(f"Degradation Preference is set to {self.degradation_preference}")
                            except Exception as e:
                                logging.warn(f"Degradation Preference cannot be set to {self.degradation_preference}")

            self.remote_peer_connection[stream_id].on('icecandidate', lambda event: self.ice_candidate_received(event, stream_id))
            self.remote_peer_connection[stream_id].on('track', lambda event: self.on_track(event, stream_id))

            if self.data_channel_enabled:
                if data_channel_mode == "publish":
                    parameters = RTCDataChannelParameters(label=stream_id, ordered=True)
                    data_channel = self.remote_peer_connection[stream_id].createDataChannel(parameters)
                    self.init_data_channel(stream_id, data_channel)
                elif data_channel_mode == "play":
                    self.remote_peer_connection[stream_id].on('datachannel', lambda event: self.init_data_channel(stream_id, event.channel))

        return self.remote_peer_connection[stream_id]

    async def ice_candidate_received(self, candidate, stream_id):
        if candidate:
            protocol_supported = False
            candidate_str = candidate.candidate

            # Check if the candidate string is empty or if the protocol is supported
            if candidate_str == "":
                protocol_supported = True
            elif "protocol" not in dir(candidate):  # Check if the candidate object lacks a 'protocol' attribute
                for element in self.candidate_types:
                    if element in candidate_str.lower():
                        protocol_supported = True
                        break
            else:
                protocol_supported = candidate.protocol.lower() in self.candidate_types

            if protocol_supported:
                js_cmd = {
                    "command": "takeCandidate",
                    "streamId": stream_id,
                    "label": candidate.sdpMLineIndex,
                    "id": candidate.sdpMid,
                    "candidate": candidate_str
                }

                if self.debug:
                    logging.debug(f"sending ice candidate for stream Id {stream_id}")
                    logging.debug(json.dumps(candidate.__dict__))  # Using __dict__ to serialize the candidate object to log

                await self.web_socket_adaptor.send(json.dumps(js_cmd))
            else:
                logging.debug(f"Candidate's protocol (full sdp: {candidate_str}) is not supported. Supported protocols: {', '.join(self.candidate_types)}")
                if candidate_str != "":
                    # Assuming notify_error_event_listeners is a method to notify listeners about errors
                    self.notify_error_event_listeners("protocol_not_supported", f"Support protocols: {', '.join(self.candidate_types)}, candidate: {candidate_str}")
        else:
            logging.debug("No candidate in the iceCandidate event")
        
    async def on_track(self, track, stream_id):
        logging.debug("onTrack for stream")

        # In Python, we don't directly manipulate video sources as in JavaScript.
        # Instead, we might handle the track - e.g., by saving it or preparing it for processing.

        # Example: Notify listeners that a new track is available
        if stream_id in self.id_mapping and track.kind in ['audio', 'video']:
            data_obj = {
                "stream": None,  # aiortc doesn't directly provide stream objects like in web APIs
                "track": track,
                "streamId": stream_id,
                "trackId": self.id_mapping.get(stream_id, {}).get(track.id, None),
            }
            self.notify_event_listeners("newTrackAvailable", data_obj)

            # The line below is for backward compatibility, as per your original JavaScript function
            self.notify_event_listeners("newStreamAvailable", data_obj)
        else:
            logging.debug("Track received without stream ID mapping or unsupported track kind")

    def notify_event_listeners(self, event, data):
        if event in self.listeners:
            for listener in self.listeners[event]:
                listener(data)  # Assuming listeners are callable objects/functions

    def add_event_listener(self, event, listener):
        if event in self.listeners:
            self.listeners[event].append(listener)
        else:
            logging.warning(f"Attempted to add listener to unknown event: {event}")

    def add_error_listener(self, listener):
        """
        Adds a listener to the list of error event listeners.
        """
        self.error_event_listeners.append(listener)

    def set_callback_error(self, callback):
        """
        Sets the callback function for error handling.
        """
        self.callback_error = callback

    def notify_error_event_listeners(self, error, message):
        """
        Notifies all registered error event listeners about an error, and calls the callback error handler if set.
        """
        for listener in self.error_event_listeners:
            listener(error, message)

        if self.callback_error is not None:
            self.callback_error(error, message)

    def sanitize_html(self, data):
        # Implement your HTML sanitization logic here
        return data

    def init_data_channel(self, stream_id, data_channel: RTCDataChannel):
        @data_channel.on('error')
        def on_error(error):
            logging.debug(f"Data Channel Error: {error}")
            obj = {'streamId': stream_id, 'error': error}
            logging.debug(f"channel status: {data_channel.readyState}")
            if data_channel.readyState != "closed":
                self.notify_event_listeners("data_channel_error", obj)

        @data_channel.on('message')
        def on_message(message):
            if isinstance(message, str):  # Handling text message
                message = self.sanitize_html(message)
                self.notify_event_listeners("data_received", {'streamId': stream_id, 'data': message})
            else:  # Handling binary message
                # Simplified binary data handling
                # You'll need to adjust this logic based on how you expect to receive binary data
                self.notify_event_listeners("data_received", {'streamId': stream_id, 'data': message})

        @data_channel.on('open')
        def on_open():
            self.remote_peer_connection[stream_id].data_channel = data_channel
            logging.debug("Data channel is opened")
            self.notify_event_listeners("data_channel_opened", stream_id)

        @data_channel.on('close')
        def on_close():
            logging.debug("Data channel is closed")
            self.notify_event_listeners("data_channel_closed", stream_id)
