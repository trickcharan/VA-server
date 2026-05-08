# Virtual Agent — BYOVA Server

Webex Contact Center BYOVA (Bring Your Own Virtual Agent) gRPC server with pluggable STS (Speech-to-Speech) provider support. Currently integrated with Google Gemini Live API.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GOOGLE_API_KEY=your_api_key_here
```

## Run the Server

```bash
source venv/bin/activate
python google_voice_agent.py
```

Server starts on port **8086**.

## Run the Simulator

```bash
source venv/bin/activate
python simulator.py
```

### Simulator Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `localhost` | gRPC server host |
| `--port` | `8086` | gRPC server port |
| `--ssl` | off | Use SSL/TLS |
| `--token` | `""` | Authorization token |
| `--va-id` | `google-live` | Virtual agent ID |

Example connecting to a remote server:

```bash
python simulator.py --host byova-ai-simulator.devus1.ciscoccservice.com --port 443 --ssl --token "your_token"
```

## Project Structure

```
├── google_voice_agent.py          # Server entrypoint
├── simulator.py                   # Test client (mic + speaker)
├── app/
│   ├── adapters/                  # Pluggable STS providers
│   │   ├── base.py                # Abstract STSAdapter interface
│   │   └── google_live/           # Google Gemini Live implementation
│   ├── audio/                     # Shared audio transcoding
│   │   └── transcoder.py
│   ├── config/
│   │   ├── agent_config.py        # Per-agent config loader
│   │   └── agents/                # Agent configurations
│   │       ├── default/           # Fallback config
│   │       └── agent_1/           # Agent-specific config
│   ├── proto/                     # gRPC protobuf definitions
│   ├── server/                    # gRPC server
│   ├── service/                   # Request processing
│   └── utils/                     # Helpers
```
