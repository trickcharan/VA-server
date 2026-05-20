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

Server starts on port **8086** (gRPC) and **8080** (Admin UI).

### Admin UI

Open [http://localhost:8080/admin/](http://localhost:8080/admin/) in your browser.

**Default credentials (auto-seeded on first run):**

| Role | Username | Password |
|------|----------|----------|
| Super Admin | `admin` | `admin123` |
| Acme Travel Org Admin | `acme_admin` | `acme123` |
| Rameswaram Cafe Org Admin | `rameswaram_admin` | `rameswaram123` |

## Run with Docker Compose

```bash
# Set your Google API key
export GOOGLE_API_KEY=your_api_key_here

# Start app + PostgreSQL
docker-compose up --build

# Access:
#   gRPC server: localhost:8086
#   Admin UI:    http://localhost:8080/admin/
```

To reset the database:
```bash
docker-compose down -v   # removes the postgres volume
docker-compose up --build
```

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
├── docker-compose.yml             # App + PostgreSQL containers
├── app/
│   ├── adapters/                  # Pluggable STS providers
│   │   ├── base.py                # Abstract STSAdapter interface
│   │   └── google_live/           # Google Gemini Live implementation
│   ├── admin/                     # FastAPI Admin UI
│   │   ├── app.py                 # Routes (login, CRUD, auth)
│   │   └── templates/             # Jinja2 + TailwindCSS templates
│   ├── audio/                     # Shared audio transcoding
│   │   └── transcoder.py
│   ├── config/
│   │   ├── agent_config.py        # Per-agent config loader (DB → file fallback)
│   │   └── agents/                # File-based agent configs (local dev fallback)
│   ├── db/                        # Database layer
│   │   ├── database.py            # Engine, session factory
│   │   ├── models.py              # Org, User, Agent models
│   │   ├── auth.py                # Password hashing, session cookies
│   │   └── seed.py                # Initial data seeding
│   ├── proto/                     # gRPC protobuf definitions
│   ├── server/                    # gRPC server + Admin UI startup
│   ├── service/                   # Request processing
│   └── utils/                     # Helpers
```
