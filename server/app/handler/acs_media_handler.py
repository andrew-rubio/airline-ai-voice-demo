"""Handles media streaming to Azure Voice Live API via the VoiceLive SDK."""

import asyncio
import base64
import json
import logging
from typing import Optional

import numpy as np
from azure.identity.aio import ManagedIdentityCredential
from azure.ai.voicelive.aio import connect as vl_connect, AgentSessionConfig
from azure.ai.voicelive.models import (
    AzureStandardVoice,
    FunctionCallOutputItem,
    FunctionTool,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    MessageItem,
    InputTextContentPart,
    LlmInterimResponseConfig,
    InterimResponseTrigger,
)
from websockets.typing import Data

from .ambient_mixer import AmbientMixer

logger = logging.getLogger(__name__)

# Default chunk size in bytes (100ms of audio at 24kHz, 16-bit mono)
DEFAULT_CHUNK_SIZE = 4800  # 24000 samples/sec * 0.1 sec * 2 bytes

AGENT_INSTRUCTIONS = """You are an easyJet customer support agent. Your role is to answer customers' questions and help them complete related tasks.

- First, greet the customer.
- Identify the customer's intent quickly (flight search, baggage, booking changes, refunds, claims, accessibility, easyJet Plus, query on policies).
- If it's a question related to policies, search in the easyJet policies file provided to you to find answers. DO NOT USE YOUR OWN KNOWLEDGE.
- If it's searching for flights, there is flight data available for flights from London to Milan, Barcelona, Marrakech and Amsterdam. Use the provided flight data file only to find flight information and help the customer book the flight. The flight dates in the data (e.g. 22/03, 29/03) refer to the CURRENT year 2026 — always present them as 2026 dates. If there are no flights available, tell the user. If the user selects one of the flights, ask the user to confirm that they are the logged in user 'Andrew Rubio' with email 'andrewrubio@microsoft.com'. If they respond with yes, then present the payment card fields in the request status box and wait for the user to enter the card details and confirm. You should wait for the card details to be submitted and the text message sent to you saying 'The customer has entered their payment details and clicked Confirm and Pay. Please confirm the booking is complete and provide the confirmation details.' Whilst waiting for payment, tell the user that their booking will be confirmed once payment has completed successfully. After this, you can tell the user the booking is confirmed.  Once the flight is booked, confirm the booking details verbally to the customer.
- If it's a request related to delayed baggage, damaged baggage, cancel and request refund, flight compensation claim, or a special assistance request, refer to the sample information in the provided forms file to help you complete the task. First, identify which category the user is asking about and respond with all the booking options at the top of the form data and ask the user to confirm which booking they are referring to. Once they say which booking they are referring to, ask for additional details regarding that task (for example the damage details if they are making a damaged baggage claim). Then after they respond with the additional details, confirm the completion of that task verbally to the customer with the sample confirmation details in the forms file under that specific category/task, and mention that they will receive a confirmation email.
- If it's regarding managing easyJet Plus membership, then refer to the policies unless they are asking about cancellation, in which case ask for their confirmation to cancel and then respond with the sample confirmation data in the forms file.
- If it's a request for adding hold luggage to the booking, let the user know that it will cost £17.50 to add to their booking and that the saved payment method will be used. Do not present the card payment fields on the request status box. If the user confirms to proceed then respond with confirmation that the flight booking has hold luggage included.
- Remember, you are only allowed to use available resources and tools, not your own knowledge.

IMPORTANT - Using the send_status_update tool:
- When you find relevant information for the customer, call send_status_update with the appropriate type and details.
- When the customer confirms an action (booking, claim, etc.), call send_status_update with confirmation details.
- When a task is complete, call send_status_update with completion details.
- Always call send_status_update BEFORE telling the customer verbally. This ensures the visual card appears on their screen while you speak.
- After every send_status_update call, you MUST continue speaking to verbally summarise the update to the customer. Never go silent after a tool call.

Keep responses concise and friendly."""

STATUS_UPDATE_TOOL = FunctionTool(
    name="send_status_update",
    description="Send a visual status update card to the customer's screen. Call this whenever you complete an action like finding flights, confirming a booking, completing a transaction, submitting a baggage report, or any other task completion.",
    parameters={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": [
                    "flight_found",
                    "booking_confirmed",
                    "booking_complete",
                    "baggage_report",
                    "claim_submitted",
                    "refund_requested",
                    "form_completed",
                    "email_sent",
                    "info",
                ],
                "description": "Type of status update",
            },
            "title": {
                "type": "string",
                "description": "Title for the status card",
            },
            "details": {
                "type": "object",
                "description": "Structured details for the card. Include relevant fields for the update type.",
                "properties": {
                    "flight_number": {"type": "string"},
                    "route": {"type": "string"},
                    "departure": {"type": "string"},
                    "price": {"type": "string"},
                    "passenger": {"type": "string"},
                    "confirmation_number": {"type": "string"},
                    "reference_number": {"type": "string"},
                    "booking_reference": {"type": "string"},
                    "status": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
        "required": ["type", "title", "details"],
    },
)


class ACSMediaHandler:
    """Manages audio streaming between client and Azure Voice Live API using the VoiceLive SDK."""

    def __init__(self, config):
        self.endpoint = config["AZURE_VOICE_LIVE_ENDPOINT"]
        self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]
        self.agent_name = config.get("FOUNDRY_AGENT_NAME", "")
        self.project_name = config.get("FOUNDRY_PROJECT_NAME", "")
        self.agent_version = config.get("FOUNDRY_AGENT_VERSION") or None
        self.connection = None
        self.incoming_websocket = None
        self.is_raw_audio = True
        self._active_response = False
        self._response_api_done = False
        self._is_closing = False  # Track if connection is shutting down

        # TTS output buffering for continuous ambient mixing
        self._tts_output_buffer = bytearray()
        self._tts_buffer_lock = asyncio.Lock()
        self._max_buffer_size = 480000  # 10 seconds of audio
        self._buffer_warning_logged = False
        self._tts_playback_started = False
        self._min_buffer_to_start = 9600  # 200ms buffer before starting TTS playback

        # Track pending function outputs awaiting response.create() after RESPONSE_DONE
        self._pending_function_output = False

        # Ambient mixer initialization
        self._ambient_mixer: Optional[AmbientMixer] = None
        ambient_preset = config.get("AMBIENT_PRESET", "none")
        if ambient_preset and ambient_preset != "none":
            try:
                self._ambient_mixer = AmbientMixer(preset=ambient_preset)
            except Exception as e:
                logger.error(f"Failed to initialize AmbientMixer: {e}")

    async def connect(self):
        """Connects to Azure Voice Live API via the VoiceLive SDK in agent mode."""
        # Validate required configuration
        if not self.endpoint:
            logger.error("[VoiceLiveACSHandler] AZURE_VOICE_LIVE_ENDPOINT is not configured")
            return
        if not self.agent_name:
            logger.error("[VoiceLiveACSHandler] FOUNDRY_AGENT_NAME is not configured")
            return
        if not self.project_name:
            logger.error("[VoiceLiveACSHandler] FOUNDRY_PROJECT_NAME is not configured")
            return
        
        logger.info(
            "[VoiceLiveACSHandler] Connecting to Voice Live endpoint=%s agent=%s project=%s version=%s",
            self.endpoint, self.agent_name, self.project_name, self.agent_version or "latest",
        )

        # Create managed identity credential (Entra ID required)
        credential = ManagedIdentityCredential(client_id=self.client_id)

        # Agent mode config — connects Voice Live to the Foundry agent
        agent_config: AgentSessionConfig = {
            "agent_name": self.agent_name,
            "project_name": self.project_name,
            "agent_version": self.agent_version,
        }

        try:
            async with vl_connect(
                endpoint=self.endpoint,
                credential=credential,
                api_version="2026-01-01-preview",
                agent_config=agent_config,
            ) as connection:
                self.connection = connection

                # Configure session (voice/VAD/noise settings are stored with the agent)
                await self._setup_session()
                
                # Check if connection is still valid before continuing
                if self._is_closing or not self.connection:
                    logger.warning("[VoiceLiveACSHandler] Connection closed after session setup")
                    return

                # Send proactive greeting
                await self._send_greeting()

                # Process events from Voice Live
                await self._process_events()
        except ConnectionError as e:
            logger.error("[VoiceLiveACSHandler] Voice Live connection error: %s", e)
            logger.error("[VoiceLiveACSHandler] Please verify: agent exists, credentials are valid, and endpoint is correct")
            self._is_closing = True
        except Exception as e:
            # Check if it's a connection closure (normal) vs actual error
            error_msg = str(e).lower()
            if "closing" in error_msg or "closed" in error_msg or "cannot write" in error_msg:
                logger.debug("[VoiceLiveACSHandler] Connection closed during operation")
            else:
                logger.exception("[VoiceLiveACSHandler] Unexpected error during connection")
        finally:
            self._is_closing = True
            self.connection = None
            await credential.close()

    async def _setup_session(self):
        """Configure the VoiceLive session — agent mode uses agent's stored config for voice/tools/instructions."""
        interim_response_config = LlmInterimResponseConfig(
            triggers=[InterimResponseTrigger.LATENCY],
            latency_threshold_ms=500,
        )

        # In agent mode, instructions, tools, voice, turn_detection, noise reduction,
        # and echo cancellation come from the agent's stored configuration (metadata).
        # Only specify audio format and interim response here.
        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            interim_response=interim_response_config,
        )

        await self.connection.session.update(session=session_config)
        logger.info("[VoiceLiveACSHandler] Session configured with voice, tools, and audio settings")

    async def _send_greeting(self):
        """Send a proactive greeting request to the agent."""
        if self._is_closing or not self.connection:
            logger.debug("[VoiceLiveACSHandler] Skipping greeting - connection closing or not ready")
            return
        
        try:
            await self.connection.conversation.item.create(
                item=MessageItem(
                    role="system",
                    content=[
                        InputTextContentPart(
                            text="Greet the customer warmly as an easyJet customer support assistant."
                        )
                    ],
                )
            )
            await self.connection.response.create()
            logger.info("[VoiceLiveACSHandler] Proactive greeting sent")
        except ConnectionError as e:
            logger.error("[VoiceLiveACSHandler] Voice Live connection error during greeting: %s", e)
            logger.error("[VoiceLiveACSHandler] This usually means the agent configuration is invalid or the connection was rejected")
            self._is_closing = True
        except Exception as e:
            error_msg = str(e).lower()
            if "closing" in error_msg or "closed" in error_msg or "cannot write" in error_msg:
                logger.warning("[VoiceLiveACSHandler] Connection closed while sending greeting - possible configuration issue")
                self._is_closing = True
            else:
                logger.exception("[VoiceLiveACSHandler] Failed to send proactive greeting")

    async def init_incoming_websocket(self, socket, is_raw_audio=True):
        """Sets up incoming ACS WebSocket."""
        self.incoming_websocket = socket
        self.is_raw_audio = is_raw_audio

    async def audio_to_voicelive(self, audio_b64: str):
        """Sends audio data to Voice Live API via the SDK."""
        if self.connection and not self._is_closing:
            try:
                await self.connection.input_audio_buffer.append(audio=audio_b64)
            except Exception as e:
                error_msg = str(e).lower()
                if "closing" in error_msg or "closed" in error_msg or "cannot write" in error_msg:
                    logger.debug("[VoiceLiveACSHandler] Connection closed while sending audio")
                    self._is_closing = True
                else:
                    raise

    async def _process_events(self):
        """Process events from the VoiceLive connection."""
        try:
            async for event in self.connection:
                await self._handle_event(event)
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Event processing error")

    async def _handle_event(self, event):
        """Handle different types of events from VoiceLive."""
        conn = self.connection
        if conn is None:
            return

        if event.type == ServerEventType.SESSION_UPDATED:
            session = event.session
            logger.info("[VoiceLiveACSHandler] Session ready: %s", session.id)

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            transcript = event.get("transcript", "")
            logger.info("User: %s", transcript)
            if transcript.strip():
                await self.send_message(
                    json.dumps({"Kind": "UserTranscription", "Text": transcript})
                )

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            delta = event.get("delta", "")
            if delta:
                await self.send_message(
                    json.dumps({"Kind": "TranscriptionDelta", "Text": delta})
                )

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            transcript = event.get("transcript", "")
            logger.info("AI: %s", transcript)
            await self.send_message(
                json.dumps({"Kind": "TranscriptionDone", "Text": transcript})
            )

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            logger.info("Voice activity detection started")
            await self.stop_audio()
            # Cancel in-progress response (barge-in)
            if self._active_response and not self._response_api_done and not self._is_closing:
                try:
                    await conn.response.cancel()
                    logger.debug("Cancelled in-progress response due to barge-in")
                except Exception as e:
                    error_msg = str(e).lower()
                    if "no active response" in error_msg:
                        logger.debug("Cancel ignored - response already completed")
                    elif "closing" in error_msg or "closed" in error_msg or "cannot write" in error_msg:
                        logger.debug("Cancel ignored - connection closing")
                        self._is_closing = True
                    else:
                        logger.warning("Cancel failed: %s", e)

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("Speech stopped")

        elif event.type == ServerEventType.RESPONSE_CREATED:
            logger.info("Assistant response created")
            self._active_response = True
            self._response_api_done = False

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            audio_bytes = event.delta
            if audio_bytes:
                # Check if ambient mixing is enabled
                if self._ambient_mixer is not None and self._ambient_mixer.is_enabled():
                    async with self._tts_buffer_lock:
                        self._tts_output_buffer.extend(audio_bytes)
                        if len(self._tts_output_buffer) > self._max_buffer_size:
                            if not self._buffer_warning_logged:
                                logger.warning(
                                    "TTS buffer large: %d bytes. Speech may be delayed.",
                                    len(self._tts_output_buffer),
                                )
                                self._buffer_warning_logged = True
                        elif self._buffer_warning_logged and len(self._tts_output_buffer) < self._max_buffer_size // 2:
                            self._buffer_warning_logged = False
                else:
                    # No ambient - send immediately
                    if self.is_raw_audio:
                        await self.send_message(audio_bytes)
                    else:
                        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                        await self.voicelive_to_acs(audio_b64)

        elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
            logger.info("Assistant finished speaking")

        elif event.type == ServerEventType.RESPONSE_DONE:
            logger.info("Response complete")
            self._active_response = False
            self._response_api_done = True

            # If a function call output was submitted during this response,
            # now trigger a new response so the agent can speak the results.
            if self._pending_function_output and not self._is_closing:
                self._pending_function_output = False
                try:
                    await conn.response.create()
                    logger.info("Created follow-up response after function call")
                except Exception as e:
                    error_msg = str(e).lower()
                    if "closing" in error_msg or "closed" in error_msg or "cannot write" in error_msg:
                        logger.debug("Connection closed while creating follow-up response")
                        self._is_closing = True
                    else:
                        logger.exception("Failed to create follow-up response")

        elif event.type == ServerEventType.RESPONSE_TEXT_DONE:
            text = event.get("text", "")
            logger.info("AI text response: %s", text)

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            await self._handle_function_call(event)

        elif event.type == ServerEventType.ERROR:
            msg = event.error.message
            if "no active response" in msg.lower():
                logger.debug("Benign cancellation error: %s", msg)
            else:
                logger.error("VoiceLive error: %s", msg)

        elif event.type == ServerEventType.CONVERSATION_ITEM_CREATED:
            logger.debug("Conversation item created: %s", event.item.id)

        else:
            logger.debug("[VoiceLiveACSHandler] Other event: %s", event.type)

    async def _handle_function_call(self, event):
        """Handle a completed function call from the model."""
        func_name = getattr(event, "name", None)
        call_id = getattr(event, "call_id", None)
        arguments = getattr(event, "arguments", "{}")

        logger.info("[VoiceLiveACSHandler] Function call: %s (call_id=%s)", func_name, call_id)

        if func_name == "send_status_update":
            try:
                args = json.loads(arguments)
                # Forward status update to frontend
                status_msg = {
                    "Kind": "StatusUpdate",
                    "Type": args.get("type", "info"),
                    "Title": args.get("title", ""),
                    "Details": args.get("details", {}),
                }
                await self.send_message(json.dumps(status_msg))
                result = json.dumps({"success": True, "message": "Status update sent to customer screen"})
            except Exception as e:
                logger.error("[VoiceLiveACSHandler] Failed to process status update: %s", e)
                result = json.dumps({"success": False, "error": str(e)})
        else:
            result = json.dumps({"error": f"Unknown function: {func_name}"})

        # Submit function output back to the model.
        # Don't call response.create() here — the current response is still active.
        # RESPONSE_DONE handler will trigger a follow-up response.
        if self._is_closing or not self.connection:
            logger.debug("[VoiceLiveACSHandler] Skipping function output - connection closing")
            return
        
        try:
            await self.connection.conversation.item.create(
                item=FunctionCallOutputItem(call_id=call_id, output=result)
            )
            self._pending_function_output = True
            logger.info("[VoiceLiveACSHandler] Function output submitted, awaiting RESPONSE_DONE")
        except Exception as e:
            error_msg = str(e).lower()
            if "closing" in error_msg or "closed" in error_msg or "cannot write" in error_msg:
                logger.debug("[VoiceLiveACSHandler] Connection closed while submitting function output")
                self._is_closing = True
            else:
                logger.exception("[VoiceLiveACSHandler] Failed to submit function call output")

    async def send_message(self, message: Data):
        """Sends data back to client WebSocket."""
        if self._is_closing:
            return
        
        try:
            await self.incoming_websocket.send(message)
        except Exception as e:
            error_msg = str(e).lower()
            if "closing" in error_msg or "closed" in error_msg:
                logger.debug("[VoiceLiveACSHandler] Client connection closed")
                self._is_closing = True
            else:
                logger.exception("[VoiceLiveACSHandler] Failed to send message")

    async def voicelive_to_acs(self, base64_data):
        """Converts Voice Live audio delta to ACS audio message."""
        try:
            data = {
                "Kind": "AudioData",
                "AudioData": {"Data": base64_data},
                "StopAudio": None,
            }
            await self.send_message(json.dumps(data))
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error in voicelive_to_acs")

    async def stop_audio(self):
        """Sends a StopAudio signal to ACS."""
        stop_audio_data = {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}
        await self.send_message(json.dumps(stop_audio_data))
        
        # Clear TTS buffer when user starts speaking
        if self._ambient_mixer is not None:
            async with self._tts_buffer_lock:
                self._tts_output_buffer.clear()
                self._tts_playback_started = False

    async def _send_continuous_audio(self, chunk_size: int) -> None:
        """
        Send continuous audio (ambient + TTS if available) back to client.
        
        Called for every incoming audio frame, ensuring continuous output.
        Uses buffered TTS with minimum buffer threshold to prevent mid-word cuts.
        
        Args:
            chunk_size: Size of audio chunk to send (matches incoming frame size)
        """
        if self._ambient_mixer is None or not self._ambient_mixer.is_enabled():
            return  # Ambient disabled, skip
            
        try:
            async with self._tts_buffer_lock:
                buffer_len = len(self._tts_output_buffer)
                
                # Always get a consistent ambient chunk first
                ambient_bytes = self._ambient_mixer.get_ambient_only_chunk(chunk_size)
                
                # Determine if we should play TTS
                should_play_tts = False
                if self._tts_playback_started:
                    # Already playing - continue until buffer empty
                    if buffer_len >= chunk_size:
                        should_play_tts = True
                    elif buffer_len > 0:
                        # Partial buffer but still playing - use what we have
                        should_play_tts = True
                    else:
                        # Buffer empty - stop playback mode
                        self._tts_playback_started = False
                else:
                    # Not yet playing - wait for minimum buffer
                    if buffer_len >= self._min_buffer_to_start:
                        self._tts_playback_started = True
                        should_play_tts = True
                
                if should_play_tts and buffer_len >= chunk_size:
                    # Full TTS chunk available - add TTS on top of ambient
                    tts_chunk = bytes(self._tts_output_buffer[:chunk_size])
                    del self._tts_output_buffer[:chunk_size]
                    
                    # Mix: ambient (constant) + TTS
                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    mixed = ambient + tts
                    mixed = np.clip(mixed, -0.95, 0.95)  # Soft limit
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()
                    
                elif should_play_tts and buffer_len > 0:
                    # Partial TTS remaining at end of speech - drain it
                    tts_chunk = bytes(self._tts_output_buffer[:])
                    self._tts_output_buffer.clear()
                    self._tts_playback_started = False
                    
                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    
                    # Only mix TTS for the portion we have
                    tts_samples = len(tts_chunk) // 2
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    ambient[:tts_samples] += tts
                    mixed = np.clip(ambient, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()
                    
                else:
                    # No TTS ready - just send constant ambient
                    output_bytes = ambient_bytes
            
            # Send to client
            if self.is_raw_audio:
                # Web browser - raw bytes
                await self.send_message(output_bytes)
            else:
                # Phone call - JSON wrapped
                output_b64 = base64.b64encode(output_bytes).decode("ascii")
                data = {
                    "Kind": "AudioData",
                    "AudioData": {"Data": output_b64},
                    "StopAudio": None,
                }
                await self.send_message(json.dumps(data))
                
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error in _send_continuous_audio")

    async def acs_to_voicelive(self, stream_data):
        """Processes audio from ACS and forwards to Voice Live if not silent."""
        try:
            data = json.loads(stream_data)
            if data.get("kind") == "AudioData":
                audio_data = data.get("audioData", {})
                incoming_data = audio_data.get("data", "")
                
                # Determine chunk size from incoming audio
                if incoming_data:
                    incoming_bytes = base64.b64decode(incoming_data)
                    chunk_size = len(incoming_bytes)
                else:
                    chunk_size = DEFAULT_CHUNK_SIZE
                
                # Send continuous audio back to caller (ambient + TTS mixed)
                await self._send_continuous_audio(chunk_size)
                
                # Forward non-silent audio to Voice Live (existing logic)
                if not audio_data.get("silent", True):
                    await self.audio_to_voicelive(audio_data.get("data"))
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error processing ACS audio")

    async def web_to_voicelive(self, audio_bytes):
        """Encodes raw audio bytes and sends to Voice Live API."""
        chunk_size = len(audio_bytes)
        
        # Send continuous audio back to browser (ambient + TTS mixed)
        await self._send_continuous_audio(chunk_size)
        
        # Forward to Voice Live
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        await self.audio_to_voicelive(audio_b64)

    async def inject_text_message(self, text: str):
        """Inject a text message into the Voice Live conversation as a user message."""
        if self._is_closing or not self.connection:
            logger.debug("[VoiceLiveACSHandler] Skipping text injection - connection closing")
            return
        try:
            await self.connection.conversation.item.create(
                item=MessageItem(
                    role="user",
                    content=[InputTextContentPart(text=text)],
                )
            )
            await self.connection.response.create()
            logger.info("[VoiceLiveACSHandler] Injected text message: %s", text)
        except Exception as e:
            logger.error("[VoiceLiveACSHandler] Failed to inject text message: %s", e)

    async def stop_audio_output(self):
        """Clean up resources when WebSocket connection closes."""
        self._is_closing = True
        async with self._tts_buffer_lock:
            self._tts_output_buffer.clear()
            self._tts_playback_started = False
        self.connection = None
        logger.info("[VoiceLiveACSHandler] Audio output stopped and resources cleaned up")
