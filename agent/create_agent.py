"""Create agent in existing Foundry project with File Search and status update tools. (WorkIQ Mail disabled)"""

import json
import os
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    FileSearchTool,
    FunctionTool as ProjectFunctionTool,
    # MCPTool,  # Disabled - WorkIQ not working
    PromptAgentDefinition,
)
from azure.identity import DefaultAzureCredential

# Load .env file if present
env_file = Path(__file__).parent.parent / "server" / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value

# Agent configuration
AGENT_NAME = "easyjet-customer-support-agent"
AGENT_MODEL = "gpt-4.1"
AGENT_INSTRUCTIONS = """You are an easyJet customer support agent. Your role is to answer customers' questions and help them complete related tasks.

- First, greet the customer.
- Identify the customer's intent quickly (flight search, baggage, booking changes, refunds, claims, accessibility, easyJet Plus, query on policies).
- If it's a question related to policies, search in the easyJet policies file provided to you to find answers. DO NOT USE YOUR OWN KNOWLEDGE.
- If it's searching for flights, there is flight data available for flights from London to Milan, Barcelona, Marrakech and Amsterdam. Use the provided flight data file only to find flight information and help the customer book the flight. The flight dates in the data (e.g. 22/03, 29/03) refer to the CURRENT year 2026 — always present them as 2026 dates. If there are no flights available, tell the user. If the user selects one of the flights, ask the user to confirm that they are the logged in user 'Andrew Rubio' with email 'andrewrubio@microsoft.com'. If they respond with yes, then present the payment card fields in the request status box and wait for the user to enter the card details and confirm. You should wait for the card details to be submitted and the text message sent to you saying 'The customer has entered their payment details and clicked Confirm and Pay. Please confirm the booking is complete and provide the confirmation details.' Whilst waiting for payment, tell the user that their booking will be confirmed once payment has completed successfully. After this, you can tell the user the booking is confirmed.  Once the flight is booked, confirm the booking details verbally to the customer.
- If it's a request related to delayed baggage, damaged baggage, cancel and request refund, flight compensation claim, or a special assistance request, refer to the sample information in the provided forms file to help you complete the task. First, identify which category the user is asking about and respond with all the booking options at the top of the form data and ask the user to confirm which booking they are referring to. Once they say which booking they are referring to, ask for additional details regarding that task (for example the damage details if they are making a damaged baggage claim). Then after they respond with the additional details, confirm the completion of that task verbally to the customer with the sample confirmation details in the forms file under that specific category/task, and mention that they will receive a confirmation email.
- If it's regarding managing easyJet Plus membership, then refer to the policies unless they are asking about cancellation, in which case ask for their confirmation to cancel and then respond with the sample confirmation data in the forms file.
- If it's a request for adding hold luggage to the booking, let the user know that it will cost £17.50 to add to their booking and that the saved payment method will be used, and ask the user to confirm if they wish to proceed. Do not present the card payment fields on the request status box when doing this! If the user confirms to proceed then respond with confirmation that the flight booking has hold luggage included.
- Remember, you are only allowed to use available resources and tools, not your own knowledge.

IMPORTANT - Using the send_status_update tool:
- When you find relevant information for the customer, call send_status_update with the appropriate type and details.
- When the customer confirms an action (booking, claim, etc.), call send_status_update with confirmation details.
- When a task is complete, call send_status_update with completion details.
- Always call send_status_update BEFORE telling the customer verbally. This ensures the visual card appears on their screen while you speak.
- After every send_status_update call, you MUST continue speaking to verbally summarise the update to the customer. Never go silent after a tool call.

Keep responses concise and friendly."""

# Function tool definitions (flat format for azure.ai.projects SDK)
STATUS_UPDATE_TOOL = ProjectFunctionTool(
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

# Data files to upload for file search
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_FILES = [
    DATA_DIR / "easyJetPolicies.pdf",
    DATA_DIR / "flightsData.md",
    DATA_DIR / "forms.md",
]

# WorkIQ Mail MCP configuration (DISABLED - WorkIQ not working)
# WORKIQ_MAIL_SERVER_URL = os.getenv(
#     "WORKIQ_MAIL_SERVER_URL",
#     "https://agent365.svc.cloud.microsoft/agents/servers/mcp_MailTools",
# )
# WORKIQ_MAIL_CONNECTION_ID = os.getenv(
#     "WORKIQ_MAIL_CONNECTION_ID",
#     "WorkIQMail",
# )

# Voice Live configuration stored as metadata
VOICE_LIVE_CONFIG = {
    "session": {
        "voice": {
            "name": "en-GB-Ollie:DragonHDLatestNeural",
            "type": "azure-standard",
            "temperature": 0.8,
        },
        "input_audio_transcription": {"model": "azure-speech"},
        "turn_detection": {
            "type": "azure_semantic_vad",
            "end_of_utterance_detection": {
                "model": "semantic_detection_v1_multilingual",
            },
        },
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
    }
}


def chunk_config(config_json: str, limit: int = 512) -> dict:
    """Split config into chunked metadata entries (512-char metadata limit)."""
    metadata = {"microsoft.voice-live.configuration": config_json[:limit]}
    remaining = config_json[limit:]
    chunk_num = 1
    while remaining:
        metadata[f"microsoft.voice-live.configuration.{chunk_num}"] = remaining[:limit]
        remaining = remaining[limit:]
        chunk_num += 1
    return metadata


def create_file_search_tool(openai_client) -> FileSearchTool:
    """Upload data files to a vector store and return a FileSearchTool."""
    vector_store = openai_client.vector_stores.create(name="EasyJetKnowledgeBase")
    print(f"  Vector store created (id: {vector_store.id})")

    for file_path in DATA_FILES:
        if not file_path.exists():
            print(f"  WARNING: Data file not found: {file_path}")
            continue
        with open(file_path, "rb") as f:
            uploaded = openai_client.vector_stores.files.upload_and_poll(
                vector_store_id=vector_store.id, file=f
            )
        print(f"  Uploaded {file_path.name} (id: {uploaded.id})")

    return FileSearchTool(vector_store_ids=[vector_store.id])


# DISABLED - WorkIQ not working
# def create_mcp_mail_tool() -> MCPTool:
#     """Create WorkIQ Mail MCP tool for sending emails."""
#     return MCPTool(
#         server_label="workiq-mail",
#         server_url=WORKIQ_MAIL_SERVER_URL,
#         require_approval="never",
#         project_connection_id=WORKIQ_MAIL_CONNECTION_ID,
#     )


def main():
    """Create agent in the Foundry project with File Search and status update tools. (WorkIQ Mail disabled)"""
    project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT")

    if not project_endpoint:
        print("ERROR: FOUNDRY_PROJECT_ENDPOINT environment variable not set", file=sys.stderr)
        sys.exit(1)

    credential = DefaultAzureCredential()

    try:
        print(f"Connecting to Foundry project: {project_endpoint}")

        project_client = AIProjectClient(
            endpoint=project_endpoint,
            credential=credential,
        )

        openai_client = project_client.get_openai_client()

        # Delete existing agent with same name if present
        try:
            existing = project_client.agents.list()
            for a in existing:
                if getattr(a, "name", None) == AGENT_NAME:
                    print(f"  Deleting existing agent: {a.id}")
                    project_client.agents.delete(a.id)
        except Exception:
            pass

        # Tool 1: File Search with uploaded data files
        print("Setting up File Search tool...")
        file_search_tool = create_file_search_tool(openai_client)

        # Tool 2: WorkIQ Mail MCP (DISABLED - WorkIQ not working)
        # print("Setting up WorkIQ Mail MCP tool...")
        # mcp_mail_tool = create_mcp_mail_tool()

        # Tool 2: Status update function tool (handled by Voice Live handler)
        print("Setting up status update function tool...")

        # Build Voice Live metadata
        voice_live_metadata = chunk_config(json.dumps(VOICE_LIVE_CONFIG))

        # Build agent definition (PromptAgentDefinition includes kind='prompt')
        definition = PromptAgentDefinition(
            model=AGENT_MODEL,
            instructions=AGENT_INSTRUCTIONS,
            tools=[file_search_tool, STATUS_UPDATE_TOOL],  # mcp_mail_tool disabled
        )

        # Create agent (visible in portal)
        print(f"Creating agent: {AGENT_NAME}")
        agent = project_client.agents.create_version(
            agent_name=AGENT_NAME,
            definition=definition,
            metadata=voice_live_metadata,
        )
        print(f"[OK] Agent created: {agent.name} (version: {agent.version})")

        # Extract project name from endpoint
        project_name = project_endpoint.rstrip("/").split("/")[-1]

        # Read existing .env and update/add values (avoid duplicates)
        env_vars = {}
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_vars[k] = v

        env_vars["FOUNDRY_AGENT_NAME"] = AGENT_NAME
        env_vars["FOUNDRY_AGENT_VERSION"] = str(agent.version)
        env_vars["FOUNDRY_PROJECT_NAME"] = project_name

        with open(env_file, "w") as f:
            for k, v in env_vars.items():
                f.write(f"{k}={v}\n")

        print(f"\n[OK] Agent config saved to .env file")
        print(f"  FOUNDRY_AGENT_NAME={AGENT_NAME}")
        print(f"  FOUNDRY_AGENT_VERSION={agent.version}")
        print(f"  FOUNDRY_PROJECT_NAME={project_name}")
        print(f"\nTools configured:")
        print(f"  1. File Search (easyJetPolicies.pdf, flightsData.md, forms.md)")
        # print(f"  2. WorkIQ Mail MCP (email sending via {WORKIQ_MAIL_CONNECTION_ID})")
        print(f"  2. send_status_update (live frontend status cards)")
        print(f"\nNext step: Deploy the container app with 'azd deploy app'")

        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
