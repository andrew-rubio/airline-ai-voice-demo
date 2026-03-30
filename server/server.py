import asyncio
import logging
import os

from app.handler.acs_event_handler import AcsEventHandler
from app.handler.acs_media_handler import ACSMediaHandler
from dotenv import load_dotenv
from quart import Quart, request, websocket

load_dotenv()

app = Quart(__name__)
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = os.getenv("AZURE_VOICE_LIVE_ENDPOINT")
app.config["ACS_CONNECTION_STRING"] = os.getenv("ACS_CONNECTION_STRING")
app.config["ACS_DEV_TUNNEL"] = os.getenv("ACS_DEV_TUNNEL", "")
app.config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"] = os.getenv(
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", ""
)
app.config["FOUNDRY_PROJECT_ENDPOINT"] = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
app.config["FOUNDRY_AGENT_NAME"] = os.getenv("FOUNDRY_AGENT_NAME", "")
app.config["FOUNDRY_PROJECT_NAME"] = os.getenv("FOUNDRY_PROJECT_NAME", "")
app.config["FOUNDRY_AGENT_VERSION"] = os.getenv("FOUNDRY_AGENT_VERSION", "")

# Ambient Scenes Configuration
# Options: none, office, call_center (or custom presets)
app.config["AMBIENT_PRESET"] = os.getenv("AMBIENT_PRESET", "none")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Log ambient configuration on startup
ambient_preset = app.config["AMBIENT_PRESET"]
if ambient_preset and ambient_preset != "none":
    logger.info(f"Ambient scenes ENABLED: preset='{ambient_preset}'")
else:
    logger.info("Ambient scenes DISABLED (preset=none)")

# Validate critical configuration on startup
required_configs = {
    "AZURE_VOICE_LIVE_ENDPOINT": app.config.get("AZURE_VOICE_LIVE_ENDPOINT"),
    "FOUNDRY_AGENT_NAME": app.config.get("FOUNDRY_AGENT_NAME"),
    "FOUNDRY_PROJECT_NAME": app.config.get("FOUNDRY_PROJECT_NAME"),
}

missing_configs = [key for key, value in required_configs.items() if not value or value.startswith("<")]
if missing_configs:
    logger.error("=" * 80)
    logger.error("CONFIGURATION ERROR: Missing or invalid environment variables")
    logger.error("The following required variables are not set or contain placeholder values:")
    for config in missing_configs:
        logger.error(f"  - {config}")
    logger.error("")
    logger.error("Please ensure .env file is properly configured:")
    logger.error("  1. Run 'python agent/create_agent.py' to set up the Foundry agent")
    logger.error("  2. Verify AZURE_VOICE_LIVE_ENDPOINT is set to your AI Services endpoint")
    logger.error("  3. Check that all required values are populated (no < > placeholders)")
    logger.error("=" * 80)
else:
    agent_version = app.config.get("FOUNDRY_AGENT_VERSION")
    if not agent_version or agent_version.startswith("<"):
        logger.warning("⚠️  FOUNDRY_AGENT_VERSION is not set - will use latest version")
        logger.warning("⚠️  Run 'python agent/create_agent.py' to create/update the agent")
    else:
        logger.info(f"✓ Foundry Agent configured: {app.config['FOUNDRY_AGENT_NAME']} v{agent_version}")

acs_handler = AcsEventHandler(app.config)


@app.route("/acs/incomingcall", methods=["POST"])
async def incoming_call_handler():
    """Handles initial incoming call event from EventGrid."""
    events = await request.get_json()
    host_url = request.host_url.replace("http://", "https://", 1).rstrip("/")
    return await acs_handler.process_incoming_call(events, host_url, app.config)


@app.route("/acs/callbacks/<context_id>", methods=["POST"])
async def acs_event_callbacks(context_id):
    """Handles ACS event callbacks for call connection and streaming events."""
    raw_events = await request.get_json()
    return await acs_handler.process_callback_events(context_id, raw_events, app.config)


@app.websocket("/acs/ws")
async def acs_ws():
    """WebSocket endpoint for ACS to send audio to Voice Live."""
    logger = logging.getLogger("acs_ws")
    logger.info("Incoming ACS WebSocket connection")
    handler = ACSMediaHandler(app.config)
    await handler.init_incoming_websocket(websocket, is_raw_audio=False)
    asyncio.create_task(handler.connect())
    try:
        while True:
            msg = await websocket.receive()
            await handler.acs_to_voicelive(msg)
    except asyncio.CancelledError:
        logger.info("ACS WebSocket cancelled")
    except Exception:
        logger.exception("ACS WebSocket connection closed")
    finally:
        await handler.stop_audio_output()


@app.websocket("/web/ws")
async def web_ws():
    """WebSocket endpoint for web clients to send audio to Voice Live."""
    logger = logging.getLogger("web_ws")
    logger.info("Incoming Web WebSocket connection")
    handler = ACSMediaHandler(app.config)
    await handler.init_incoming_websocket(websocket, is_raw_audio=True)
    asyncio.create_task(handler.connect())
    try:
        while True:
            msg = await websocket.receive()
            if isinstance(msg, str):
                # Text message from frontend (e.g. payment confirmation)
                import json as _json
                try:
                    data = _json.loads(msg)
                    if data.get("Kind") == "TextMessage":
                        await handler.inject_text_message(data.get("Text", ""))
                except Exception:
                    logger.warning("Unrecognised text message from web client")
            else:
                await handler.web_to_voicelive(msg)
    except asyncio.CancelledError:
        logger.info("Web WebSocket cancelled")
    except Exception:
        logger.exception("Web WebSocket connection closed")
    finally:
        await handler.stop_audio_output()


@app.route("/")
async def index():
    """Serves the static index page."""
    return await app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
